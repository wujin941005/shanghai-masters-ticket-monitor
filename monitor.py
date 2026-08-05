#!/usr/bin/env python3
"""2026 上海劳力士大师赛回流票监控。"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import re
import signal
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TypeVar
from urllib.parse import quote, urlencode

from curl_cffi import CurlOpt
from curl_cffi import requests as cffi_requests

from notifications import (
    NotificationDispatcher,
    NotificationEvent,
    build_dispatcher,
)


EVENT_NAME = "2026 上海劳力士大师赛"
EVENT_DATE = "10月5日–18日 · 旗忠网球中心"
ERROR_ALERT_THRESHOLD = 5
STATE_VERSION = 1
DEFAULT_NOTIFICATION_MAX_RETRIES = 5
BUYABLE_COUNT_NOTE = (
    "“当前最多可选”由售票平台实时返回，受余票、单笔限购和配票规则共同影响，"
    "不等于剩余总票数。"
)
ICON_URL = (
    "https://en.rolexshanghaimasters.com/-/media/sites/tournaments/"
    "shanghai/logos/rsm_logo_2025.png"
)


@dataclass(frozen=True)
class RuntimeOptions:
    """Operational policy supplied by the public or a separate local launcher."""

    session_refresh_interval: int = 300
    request_gap_ms: int = 250
    jitter: float = 2.0
    availability_confirmations: int = 1
    notification_cooldown: int = 300
    notification_max_retries: int = DEFAULT_NOTIFICATION_MAX_RETRIES
    rate_limit_backoff: int = 60
    notify_initial: bool = False
    inventory_mode: str = "auto"
    state_path: Path = Path(__file__).with_name("monitor-state.json")
    state_enabled: bool = True
    log_path: str = "monitor.log"
    max_monitored_sessions: int | None = 20
    allow_unfiltered_monitoring: bool = False

STATUS_TEXT = {
    "ON_SALE": "🟢 有票",
    "ONSALE": "🟢 有票",
    "LACK_OF_TICKET": "🔴 售罄",
    "SOLD_OUT": "🔴 售罄",
    "NOT_YET_ON_SALE": "⚪ 未开售",
    "PENDING_SALE": "⚪ 未开售",
    "SALE_END": "⚫ 已结束",
    "OFF_SALE": "⚫ 已下架",
}
AVAILABLE_STATUSES = {"ON_SALE", "ONSALE"}
SOLD_OUT_STATUSES = {"LACK_OF_TICKET", "SOLD_OUT"}


@dataclass(frozen=True)
class EventTarget:
    channel: str
    channel_label: str
    base_url: str
    show_id: str
    show_label: str
    buy_url: str
    buy_hint: str
    date_hint: str = EVENT_DATE


@dataclass(frozen=True)
class InventoryItem:
    target: EventTarget
    session_id: str
    session_name: str
    status: str

    @property
    def key(self) -> str:
        return f"{self.target.channel}:{self.target.show_id}:{self.session_id}"


@dataclass(frozen=True)
class PriceLevelItem:
    session: InventoryItem
    seat_plan_id: str
    seat_plan_name: str
    original_price: float | None
    can_buy_count: int
    sale_started: bool | None = None
    is_stop_sale: bool | None = None
    channel_hide_flag: bool | None = None

    @property
    def target(self) -> EventTarget:
        return self.session.target

    @property
    def key(self) -> str:
        return f"{self.session.key}:{self.seat_plan_id}"

    @property
    def available(self) -> bool:
        """平台当前可选数大于 0，且没有明确标记为未开售、停售或隐藏。"""
        return (
            self.can_buy_count > 0
            and self.sale_started is not False
            and self.is_stop_sale is not True
            and self.channel_hide_flag is not True
        )


@dataclass
class TargetResult:
    target: EventTarget
    items: list[InventoryItem]
    error: str | None = None
    rate_limited: bool = False
    retry_after_seconds: float | None = None


@dataclass
class PriceLevelResult:
    session: InventoryItem
    items: list[PriceLevelItem]
    error: str | None = None
    rate_limited: bool = False
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class NotificationRetry:
    signature: str
    attempts: int


@dataclass
class MonitorState:
    """可跨进程重启延续的稳定库存、通知与轻量健康状态。"""

    session_statuses: dict[str, str]
    baselined_session_targets: set[str]
    price_level_availability: dict[str, bool]
    price_level_streaks: dict[str, int]
    baselined_price_sessions: set[str]
    last_notified_at: dict[str, float]
    notification_retries: dict[str, NotificationRetry]
    active_rate_limit_channels: set[str] = field(default_factory=set)
    poll_count: int = 0
    alert_count: int = 0
    last_success_at: str | None = None
    last_error: str | None = None

    @classmethod
    def empty(cls) -> "MonitorState":
        return cls({}, set(), {}, {}, set(), {}, {})


MonitorItemT = TypeVar("MonitorItemT", InventoryItem, PriceLevelItem)


JUSS_BASE = "https://ztmen.jussyun.com"
PXQ_BASE = "https://m.piaoxingqiu.com"
PXQ_ALIPAY_APP_ID = "2021004123672725"
PXQ_ALIPAY_PATH = "/pages/show-detail/show-detail"
JUSS_CHANNEL_LABEL = "久事体育 / 莓塔甄选"
JUSS_BUY_HINT = "点击打开久事体育对应活动页；莓塔甄选的大师赛入口也跳转久事"
# 久事体育支付宝小程序 appId 与首页路径，来自官方分享口令解析。
JUSS_ALIPAY_APP_ID = "2021003127624300"
JUSS_ALIPAY_PATH = "/pages/Home/Home"
JUSS_ALIPAY_BUY_HINT = "点击打开久事体育支付宝小程序，进入后选择大师赛购票入口；莓塔甄选大师赛入口同款"


def build_juss_web_url(show_id: str) -> str:
    """久事当前没有下发可复用深链，使用可访问的精确官方活动页。"""
    return f"{JUSS_BASE}/content/{show_id}?{urlencode({'showId': show_id})}"


def build_juss_alipay_url(_show_id: str = "") -> str:
    """久事体育支付宝小程序首页入口，用户在小程序内自行进入购票页。"""
    scheme = (
        f"alipays://platformapi/startapp?appId={JUSS_ALIPAY_APP_ID}"
        f"&page={JUSS_ALIPAY_PATH}"
    )
    return f"https://ds.alipay.com/?{urlencode({'scheme': scheme})}"


def build_piaoxingqiu_alipay_url(show_id: str) -> str:
    """使用票星球官方 terminal/jump 接口返回格式构造支付宝跳转页。"""
    scheme = (
        f"alipays://platformapi/startapp?appId={PXQ_ALIPAY_APP_ID}"
        f"&page={PXQ_ALIPAY_PATH}&query=showId={quote(show_id, safe='')}"
    )
    return f"https://ds.alipay.com/?{urlencode({'scheme': scheme})}"


def build_piaoxingqiu_app_url(show_id: str) -> str:
    """票星球官方 terminal/jump 接口返回的原生 App Scheme 格式。"""
    return f"piaoxingqiu://piaoxingqiu.com/show_detail?{urlencode({'showId': show_id})}"


def build_piaoxingqiu_web_url(show_id: str) -> str:
    return f"{PXQ_BASE}/content/{show_id}?{urlencode({'showId': show_id})}"


def build_targets(buy_url_mode: str = "alipay") -> tuple[EventTarget, ...]:
    """创建可实际访问的官方活动入口；默认优先票星球支付宝小程序。"""
    mode = buy_url_mode.strip().lower()
    if mode not in {"alipay", "app", "web"}:
        raise ValueError("BARK_BUY_URL_MODE 仅支持 alipay、app 或 web")

    pxq_hint = {
        "alipay": "点击通过票星球官方链接打开支付宝小程序",
        "app": "点击打开票星球 App 对应活动",
        "web": "点击打开票星球对应活动页",
    }[mode]

    # 活动 ID 于 2026-07-30 从两端公开 show_list/search 接口核实。
    target_specs = (
        (
            "juss",
            JUSS_CHANNEL_LABEL,
            JUSS_BASE,
            "6a5ee5f00a20c700012c2ebd",
            "中央场馆（看台）",
            EVENT_DATE,
        ),
        (
            "juss",
            JUSS_CHANNEL_LABEL,
            JUSS_BASE,
            "6a5edab37623c600014ccd45",
            "中央场馆贵宾座席",
            EVENT_DATE,
        ),
        (
            "juss",
            JUSS_CHANNEL_LABEL,
            JUSS_BASE,
            "6a5ede547623c600014ebe7c",
            "2号馆",
            EVENT_DATE,
        ),
        (
            "juss",
            JUSS_CHANNEL_LABEL,
            JUSS_BASE,
            "6a5edd677623c600014e3d34",
            "资格赛",
            EVENT_DATE,
        ),
        (
            "juss",
            JUSS_CHANNEL_LABEL,
            JUSS_BASE,
            "6a5edaef0a20c70001267361",
            "资格赛（乐享座席）",
            EVENT_DATE,
        ),
        (
            "piaoxingqiu",
            "票星球",
            PXQ_BASE,
            "6a672fe6a8ae9000013e03ab",
            "中央场馆（看台及套票）",
            EVENT_DATE,
        ),
        (
            "piaoxingqiu",
            "票星球",
            PXQ_BASE,
            "6a686291213f8600014a1672",
            "2号馆",
            EVENT_DATE,
        ),
        (
            "piaoxingqiu",
            "票星球",
            PXQ_BASE,
            "6a6862868ddcf20001b5093f",
            "资格赛",
            EVENT_DATE,
        ),
        (
            "piaoxingqiu",
            "票星球",
            PXQ_BASE,
            "6a6862c49d56230001976265",
            "大师赛嘉年华 Fan Week",
            "10月1日–4日 · 旗忠网球中心",
        ),
    )
    targets: list[EventTarget] = []
    for channel, channel_label, base_url, show_id, show_label, date_hint in target_specs:
        if channel == "juss":
            if mode == "alipay":
                buy_url = build_juss_alipay_url(show_id)
                buy_hint = JUSS_ALIPAY_BUY_HINT
            else:
                buy_url = build_juss_web_url(show_id)
                buy_hint = JUSS_BUY_HINT
        else:
            buy_url = {
                "alipay": build_piaoxingqiu_alipay_url,
                "app": build_piaoxingqiu_app_url,
                "web": build_piaoxingqiu_web_url,
            }[mode](show_id)
            buy_hint = pxq_hint
        targets.append(
            EventTarget(
                channel,
                channel_label,
                base_url,
                show_id,
                show_label,
                buy_url,
                buy_hint,
                date_hint,
            )
        )
    return tuple(targets)


TARGETS = build_targets()

JUSS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
    ),
    "terminal-src": "WEB",
    "src": "WEB",
    "x-requested-with": "XMLHttpRequest",
    "Referer": f"{JUSS_BASE}/",
}

PXQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
        "Mobile/15E148 Safari/604.1"
    ),
    "terminal-src": "WEB",
    "src": "WEB",
    "ver": "4.1.2-20240305183007",
    "Referer": f"{PXQ_BASE}/",
}

log = logging.getLogger("shanghai-masters")
STOP_REQUESTED = threading.Event()


def request_stop(_signum: int, _frame: Any) -> None:
    """信号处理器只设置标记，避免在 curl-cffi 回调中抛 KeyboardInterrupt。"""
    STOP_REQUESTED.set()


def setup_logging(log_file: str | None = None) -> None:
    """同时输出到终端与轮转日志。"""
    log.handlers.clear()
    log.setLevel(logging.INFO)
    terminal = logging.StreamHandler()
    terminal.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(terminal)

    if log_file:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        log.addHandler(file_handler)


def load_env() -> None:
    """读取项目目录中的 .env，不覆盖调用者显式设置的环境变量。"""
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _typed_dict(
    value: Any,
    *,
    key_type: type,
    value_type: type | tuple[type, ...],
    field_name: str,
) -> dict[Any, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} 不是对象")
    if not all(isinstance(key, key_type) and isinstance(item, value_type) for key, item in value.items()):
        raise ValueError(f"{field_name} 包含错误类型")
    return dict(value)


def monitor_state_to_dict(state: MonitorState) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "session_statuses": state.session_statuses,
        "baselined_session_targets": sorted(state.baselined_session_targets),
        "price_level_availability": state.price_level_availability,
        "price_level_streaks": state.price_level_streaks,
        "baselined_price_sessions": sorted(state.baselined_price_sessions),
        "last_notified_at": state.last_notified_at,
        "notification_retries": {
            key: {"signature": retry.signature, "attempts": retry.attempts}
            for key, retry in state.notification_retries.items()
        },
        "health": {
            "active_rate_limit_channels": sorted(state.active_rate_limit_channels),
            "poll_count": state.poll_count,
            "alert_count": state.alert_count,
            "last_success_at": state.last_success_at,
            "last_error": state.last_error,
        },
    }


def monitor_state_from_dict(payload: Any) -> MonitorState:
    """严格解析状态文件；任何形状异常都整份降级，避免半份状态制造误报。"""
    if not isinstance(payload, dict):
        raise ValueError("状态根节点不是对象")
    if payload.get("version") != STATE_VERSION:
        raise ValueError(f"不支持的状态版本: {payload.get('version')!r}")

    baselined_targets = payload.get("baselined_session_targets")
    if not isinstance(baselined_targets, list) or not all(
        isinstance(key, str) for key in baselined_targets
    ):
        raise ValueError("baselined_session_targets 不是字符串数组")
    baselined_prices = payload.get("baselined_price_sessions")
    if not isinstance(baselined_prices, list) or not all(
        isinstance(key, str) for key in baselined_prices
    ):
        raise ValueError("baselined_price_sessions 不是字符串数组")

    raw_retries = payload.get("notification_retries")
    if not isinstance(raw_retries, dict):
        raise ValueError("notification_retries 不是对象")
    retries: dict[str, NotificationRetry] = {}
    for key, value in raw_retries.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, dict)
            or not isinstance(value.get("signature"), str)
            or not isinstance(value.get("attempts"), int)
            or isinstance(value.get("attempts"), bool)
            or value["attempts"] < 1
        ):
            raise ValueError("notification_retries 包含错误记录")
        retries[key] = NotificationRetry(value["signature"], value["attempts"])

    health = payload.get("health")
    if not isinstance(health, dict):
        raise ValueError("health 不是对象")
    poll_count = health.get("poll_count")
    alert_count = health.get("alert_count")
    if (
        not isinstance(poll_count, int)
        or isinstance(poll_count, bool)
        or poll_count < 0
        or not isinstance(alert_count, int)
        or isinstance(alert_count, bool)
        or alert_count < 0
    ):
        raise ValueError("health 计数无效")
    last_success_at = health.get("last_success_at")
    last_error = health.get("last_error")
    if last_success_at is not None and not isinstance(last_success_at, str):
        raise ValueError("last_success_at 类型无效")
    if last_error is not None and not isinstance(last_error, str):
        raise ValueError("last_error 类型无效")
    active_rate_limit_channels = health.get("active_rate_limit_channels")
    if active_rate_limit_channels is None:
        # Upgrade state files written before persistent operator-alert state.
        # A successful first round clears this inferred incident immediately.
        active_rate_limit_channels = (
            [
                channel.strip()
                for channel in last_error.removeprefix("接口限流:").split("、")
                if channel.strip()
            ]
            if isinstance(last_error, str) and last_error.startswith("接口限流:")
            else []
        )
    if not isinstance(active_rate_limit_channels, list) or not all(
        isinstance(channel, str) for channel in active_rate_limit_channels
    ):
        raise ValueError("active_rate_limit_channels 不是字符串数组")

    session_statuses = _typed_dict(
        payload.get("session_statuses"),
        key_type=str,
        value_type=str,
        field_name="session_statuses",
    )
    price_level_availability = _typed_dict(
        payload.get("price_level_availability"),
        key_type=str,
        value_type=bool,
        field_name="price_level_availability",
    )
    price_level_streaks = _typed_dict(
        payload.get("price_level_streaks"),
        key_type=str,
        value_type=int,
        field_name="price_level_streaks",
    )
    if any(isinstance(value, bool) for value in price_level_streaks.values()):
        raise ValueError("price_level_streaks 包含布尔值")
    last_notified_at = _typed_dict(
        payload.get("last_notified_at"),
        key_type=str,
        value_type=(int, float),
        field_name="last_notified_at",
    )

    return MonitorState(
        session_statuses=session_statuses,
        baselined_session_targets=set(baselined_targets),
        price_level_availability=price_level_availability,
        price_level_streaks=price_level_streaks,
        baselined_price_sessions=set(baselined_prices),
        last_notified_at={key: float(value) for key, value in last_notified_at.items()},
        notification_retries=retries,
        active_rate_limit_channels=set(active_rate_limit_channels),
        poll_count=poll_count,
        alert_count=alert_count,
        last_success_at=last_success_at,
        last_error=last_error,
    )


def load_monitor_state(path: Path) -> tuple[MonitorState, bool]:
    """读取有效状态；缺失或损坏时返回空状态并要求首轮静默重建。"""
    if not path.exists():
        return MonitorState.empty(), False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return monitor_state_from_dict(payload), True
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        log.warning("⚠️ 状态文件不可用，将静默重建基线: %s", type(exc).__name__)
        return MonitorState.empty(), False


def save_monitor_state(path: Path, state: MonitorState) -> None:
    """先完整写入同目录临时文件，再原子替换正式状态。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                monitor_state_to_dict(state),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class TicketApiHttpError(RuntimeError):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class RateLimitError(TicketApiHttpError):
    def __init__(
        self,
        *,
        status_code: int = 429,
        retry_after_seconds: float | None = None,
        source: str = "HTTP",
    ):
        super().__init__(status_code)
        self.retry_after_seconds = retry_after_seconds
        self.source = source
        retry_text = (
            f"，Retry-After={retry_after_seconds:g}s"
            if retry_after_seconds is not None
            else ""
        )
        self.args = (f"{source} 限流（status={status_code}{retry_text}）",)


RATE_LIMIT_HINTS = (
    "rate limit",
    "too many requests",
    "访问频繁",
    "请求频繁",
    "操作频繁",
    "限流",
    "人数过多",
)
# 久事常见 403，票星球实测会用非标准 469 表示风控拦截。
RATE_LIMIT_HTTP_STATUSES = {403, 429, 469}


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def payload_is_rate_limited(payload: dict[str, Any]) -> bool:
    raw_status = payload.get("statusCode")
    try:
        if int(raw_status) == 429:
            return True
    except (TypeError, ValueError):
        pass
    message = " ".join(
        str(payload.get(field) or "")
        for field in ("comments", "message", "subComments")
    ).lower()
    return any(hint in message for hint in RATE_LIMIT_HINTS)


class HttpClient:
    """Public direct-only HTTP client with a Chrome TLS fingerprint."""

    def __init__(self):
        self.session = self._new_session()

    @staticmethod
    def _new_session() -> cffi_requests.Session:
        # The public client is deliberately forced to a direct connection.
        return cffi_requests.Session(
            impersonate="chrome",
            trust_env=False,
            curl_options={CurlOpt.PROXY: ""},
        )

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str],
        timeout: int = 12,
    ) -> dict[str, Any]:
        return self._get_json_once(
            self.session,
            url,
            headers=headers,
            params=params,
            timeout=timeout,
        )

    @staticmethod
    def _get_json_once(
        session: Any,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        response = session.get(url, headers=headers, params=params, timeout=timeout)
        status_code = int(response.status_code)
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        # 两个平台都可能用非标准状态码表达风控，而不是标准的 429。
        # 两者都必须进入同渠道短路和退避，避免把临时风控打成持续封禁。
        if status_code in RATE_LIMIT_HTTP_STATUSES:
            raise RateLimitError(
                status_code=status_code,
                retry_after_seconds=retry_after,
            )
        if status_code >= 400:
            raise TicketApiHttpError(status_code)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("API 响应不是 JSON 对象")
        if payload_is_rate_limited(payload):
            raw_status = payload.get("statusCode")
            try:
                api_status = int(raw_status)
            except (TypeError, ValueError):
                api_status = 429
            raise RateLimitError(
                status_code=api_status,
                retry_after_seconds=retry_after,
                source="API",
            )
        return payload

    def reset(self) -> None:
        previous_session = self.session
        self.session = self._new_session()
        try:
            previous_session.close()
        except Exception:
            # Reset is used while recovering from a failed request. Failure to
            # clean up the old handle must not block the replacement session.
            log.debug("旧 HTTP 会话关闭失败", exc_info=True)


def build_http_client(env: Mapping[str, str]) -> tuple[HttpClient, str]:
    """Build the direct-only client shipped in the public distribution."""
    del env
    return HttpClient(), "直连"


def headers_for(target: EventTarget) -> dict[str, str]:
    return JUSS_HEADERS if target.channel == "juss" else PXQ_HEADERS


def parse_sessions(target: EventTarget, payload: dict[str, Any]) -> list[InventoryItem]:
    """把 v5 sessions 响应转换为稳定的库存条目。"""
    if payload.get("statusCode") != 200:
        message = payload.get("comments") or payload.get("message") or "未知响应"
        raise ValueError(f"API status={payload.get('statusCode')}: {message}")
    sessions = payload.get("data")
    if not isinstance(sessions, list):
        raise ValueError("API data 不是场次数组")
    if not sessions:
        raise ValueError("API 返回空场次数组，保留上一份有效状态")

    items: list[InventoryItem] = []
    for index, session in enumerate(sessions):
        if not isinstance(session, dict):
            continue
        session_id = str(
            session.get("bizShowSessionId") or session.get("showSessionId") or index
        )
        session_name = str(session.get("sessionName") or f"场次 {index + 1}")
        status = str(session.get("sessionStatus") or "UNKNOWN")
        if status == "UNKNOWN" and session.get("hasSessionSoldOut") is True:
            status = "LACK_OF_TICKET"
        items.append(InventoryItem(target, session_id, session_name, status))
    return items


def parse_price_levels(
    session: InventoryItem,
    payload: dict[str, Any],
) -> list[PriceLevelItem]:
    """解析公开 v5 seat_plans 响应中的票档名称、价格和动态可购状态。"""
    if payload.get("statusCode") != 200:
        message = payload.get("comments") or payload.get("message") or "未知响应"
        raise ValueError(f"API status={payload.get('statusCode')}: {message}")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("seatPlans"), list):
        raise ValueError("API data.seatPlans 不是票档数组")
    if not data["seatPlans"]:
        raise ValueError("API 返回空票档数组，保留上一份有效状态")

    items: list[PriceLevelItem] = []
    for index, seat_plan in enumerate(data["seatPlans"]):
        if not isinstance(seat_plan, dict):
            continue
        seat_plan_id = str(seat_plan.get("seatPlanId") or index)
        seat_plan_name = str(seat_plan.get("seatPlanName") or f"票档 {index + 1}")
        raw_price = seat_plan.get("originalPrice")
        try:
            original_price = float(raw_price) if raw_price is not None else None
        except (TypeError, ValueError):
            original_price = None
        try:
            can_buy_count = int(seat_plan.get("canBuyCount") or 0)
        except (TypeError, ValueError):
            can_buy_count = 0
        items.append(
            PriceLevelItem(
                session=session,
                seat_plan_id=seat_plan_id,
                seat_plan_name=seat_plan_name,
                original_price=original_price,
                can_buy_count=can_buy_count,
                sale_started=seat_plan.get("saleStarted"),
                is_stop_sale=seat_plan.get("isStopSale"),
                channel_hide_flag=seat_plan.get("channelHideFlag"),
            )
        )
    return items


def check_target(client: HttpClient, target: EventTarget) -> TargetResult:
    url = (
        f"{target.base_url}/cyy_gatewayapi/show/pub/v5/show/"
        f"{target.show_id}/sessions"
    )
    params = {
        "src": "WEB",
        "terminalSrc": "WEB",
        "source": "FROM_QUICK_ORDER",
        "isQueryShowBasicInfo": "true",
    }
    if target.channel == "piaoxingqiu":
        params["ver"] = PXQ_HEADERS["ver"]
    try:
        payload = client.get_json(url, headers=headers_for(target), params=params)
        return TargetResult(target, parse_sessions(target, payload))
    except RateLimitError as exc:
        return TargetResult(
            target,
            [],
            str(exc),
            rate_limited=True,
            retry_after_seconds=exc.retry_after_seconds,
        )
    except Exception as exc:
        return TargetResult(target, [], str(exc))


def check_all(
    client: HttpClient,
    targets: Iterable[EventTarget],
    *,
    request_gap_seconds: float = 0,
) -> list[TargetResult]:
    results: list[TargetResult] = []
    rate_limited_channels: set[str] = set()
    for index, target in enumerate(targets):
        if STOP_REQUESTED.is_set():
            break
        if target.channel in rate_limited_channels:
            continue
        if index and request_gap_seconds > 0:
            if STOP_REQUESTED.wait(request_gap_seconds):
                break
        result = check_target(client, target)
        results.append(result)
        if result.rate_limited:
            rate_limited_channels.add(target.channel)
    return results


def check_price_level(client: HttpClient, session: InventoryItem) -> PriceLevelResult:
    target = session.target
    url = (
        f"{target.base_url}/cyy_gatewayapi/show/pub/v5/show/{target.show_id}/"
        f"session/{session.session_id}/seat_plans"
    )
    params = {
        "src": "WEB",
        "terminalSrc": "WEB",
        "source": "FROM_QUICK_ORDER",
        "isQueryShowBasicInfo": "true",
    }
    if target.channel == "piaoxingqiu":
        params["ver"] = PXQ_HEADERS["ver"]
    try:
        payload = client.get_json(url, headers=headers_for(target), params=params)
        return PriceLevelResult(session, parse_price_levels(session, payload))
    except RateLimitError as exc:
        return PriceLevelResult(
            session,
            [],
            str(exc),
            rate_limited=True,
            retry_after_seconds=exc.retry_after_seconds,
        )
    except Exception as exc:
        return PriceLevelResult(session, [], str(exc))


def check_all_price_levels(
    client: HttpClient,
    sessions: Iterable[InventoryItem],
    *,
    request_gap_seconds: float = 0,
    blocked_channels: Iterable[str] = (),
    result_callback: Callable[[PriceLevelResult], None] | None = None,
) -> list[PriceLevelResult]:
    results: list[PriceLevelResult] = []
    rate_limited_channels = set(blocked_channels)
    request_count = 0
    for session in sessions:
        if STOP_REQUESTED.is_set():
            break
        if session.target.channel in rate_limited_channels:
            continue
        if request_count and request_gap_seconds > 0:
            if STOP_REQUESTED.wait(request_gap_seconds):
                break
        result = check_price_level(client, session)
        request_count += 1
        results.append(result)
        if result_callback is not None:
            result_callback(result)
        if result.rate_limited:
            rate_limited_channels.add(session.target.channel)
    return results


def update_session_cache(
    cache: dict[str, TargetResult],
    results: Iterable[TargetResult],
) -> None:
    """只用成功响应更新场次目录，临时失败时保留上一份可用目录。"""
    for result in results:
        if result.error is None:
            cache[result.target.show_id] = result


def cached_target_results(
    cache: dict[str, TargetResult],
    targets: Iterable[EventTarget],
) -> list[TargetResult]:
    return [cache[target.show_id] for target in targets if target.show_id in cache]


def calculate_sleep_seconds(
    interval: float,
    elapsed: float,
    jitter_offset: float = 0,
) -> float:
    """按每轮开始时间计算固定周期等待，而不是请求结束后再完整等待。"""
    return max(0.0, interval - elapsed + jitter_offset)


def session_catalog_refresh_due(
    *,
    price_mode: bool,
    has_cached_results: bool,
    last_refresh_at: float | None,
    now: float,
    refresh_interval: float,
) -> bool:
    return (
        not price_mode
        or not has_cached_results
        or last_refresh_at is None
        or now - last_refresh_at >= refresh_interval
    )


def calculate_rate_limit_backoff(
    current_interval: float,
    minimum_backoff: float,
    retry_after_seconds: float | None,
    *,
    maximum_backoff: float = 1800,
) -> float:
    """限流至少指数退避一次，并优先遵守平台 Retry-After。"""
    return min(
        maximum_backoff,
        max(
            current_interval * 2,
            minimum_backoff,
            retry_after_seconds or 0,
        ),
    )


def parse_terms(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip().lower() for part in value.split(",") if part.strip())


def filter_items(items: Iterable[InventoryItem], match: str | None) -> list[InventoryItem]:
    terms = parse_terms(match)
    if not terms:
        return list(items)
    return [
        item
        for item in items
        if any(
            term in f"{item.target.show_label} {item.session_name}".lower()
            for term in terms
        )
    ]


def filter_price_levels(
    items: Iterable[PriceLevelItem],
    grades: str | None,
) -> list[PriceLevelItem]:
    """按完整票档名筛选；例如 S,A+,A,B 不会误匹配“底线S双人票”。"""
    requested = set(parse_terms(grades))
    if not requested:
        return list(items)
    return [item for item in items if item.seat_plan_name.strip().lower() in requested]


def filter_price_level_results(
    results: Iterable[PriceLevelResult],
    grades: str | None,
) -> list[PriceLevelResult]:
    return [
        PriceLevelResult(
            result.session,
            filter_price_levels(result.items, grades),
            result.error,
            result.rate_limited,
            result.retry_after_seconds,
        )
        for result in results
    ]


def select_targets(
    channels: str | None,
    targets: tuple[EventTarget, ...] = TARGETS,
) -> tuple[EventTarget, ...]:
    requested = set(parse_terms(channels))
    if not requested:
        return targets
    if "meta" in requested or "莓塔" in requested:
        requested.add("juss")
    if "票星球" in requested:
        requested.add("piaoxingqiu")
    if "久事" in requested:
        requested.add("juss")
    selected = tuple(target for target in targets if target.channel in requested)
    if not selected:
        raise ValueError("--channels 仅支持 juss、meta、piaoxingqiu")
    return selected


def find_newly_available(
    previous: dict[str, str],
    current: Iterable[InventoryItem],
    *,
    initial_round: bool,
    notify_initial: bool,
) -> list[InventoryItem]:
    """只报告从非有票变为有票的场次；首轮默认仅建立基线。"""
    if initial_round and not notify_initial:
        return []
    return [
        item
        for item in current
        if item.status in AVAILABLE_STATUSES
        and previous.get(item.key) not in AVAILABLE_STATUSES
    ]


def find_newly_available_price_levels(
    previous: dict[str, bool],
    current: Iterable[PriceLevelItem],
    *,
    initial_round: bool,
    notify_initial: bool,
) -> list[PriceLevelItem]:
    """只报告从不可购变为可购的票档；首轮默认仅建立基线。"""
    if initial_round and not notify_initial:
        return []
    return [
        item
        for item in current
        if item.available and previous.get(item.key) is not True
    ]


def update_price_level_state(
    previous: dict[str, bool],
    results: Iterable[PriceLevelResult],
) -> None:
    """更新成功查询的票档状态；单场查询失败时保留上一轮状态。"""
    for result in results:
        if result.error is not None:
            continue
        prefix = f"{result.session.key}:"
        current_keys = {item.key for item in result.items}
        for key in list(previous):
            if key.startswith(prefix) and key not in current_keys:
                previous[key] = False
        for item in result.items:
            previous[item.key] = item.available


def advance_availability_streak(
    current: int,
    available: bool,
    confirmations: int,
) -> int:
    """用正/负连续次数记录可购/不可购观测，并限制在确认阈值内。"""
    if available:
        return min(confirmations, (current if current > 0 else 0) + 1)
    return max(-confirmations, (current if current < 0 else 0) - 1)


def update_price_level_streaks(
    streaks: dict[str, int],
    results: Iterable[PriceLevelResult],
    *,
    confirmations: int,
    baselined_sessions: set[str],
) -> None:
    """只用成功查询推进票档连续观测；新场次首轮直接建立稳定基线。"""
    for result in results:
        if result.error is not None:
            continue
        prefix = f"{result.session.key}:"
        initial_session_round = result.session.key not in baselined_sessions
        current_keys = {item.key for item in result.items}
        for key in list(streaks):
            if key.startswith(prefix) and key not in current_keys:
                streaks[key] = advance_availability_streak(
                    streaks[key], False, confirmations
                )
        for item in result.items:
            if initial_session_round:
                streaks[item.key] = confirmations if item.available else -confirmations
            else:
                streaks[item.key] = advance_availability_streak(
                    streaks.get(item.key, 0), item.available, confirmations
                )


def confirmed_available_price_levels(
    items: Iterable[PriceLevelItem],
    streaks: dict[str, int],
    *,
    confirmations: int,
) -> list[PriceLevelItem]:
    """返回已经连续达到阈值的可购票档。"""
    return [
        item
        for item in items
        if item.available and streaks.get(item.key, 0) >= confirmations
    ]


def update_confirmed_price_level_state(
    previous: dict[str, bool],
    results: Iterable[PriceLevelResult],
    streaks: dict[str, int],
    *,
    confirmations: int,
    preserve_available_keys: Iterable[str] = (),
) -> None:
    """只吸收已确认状态；推送待重试的可购票档可显式保留旧状态。"""
    preserved = set(preserve_available_keys)
    for result in results:
        if result.error is not None:
            continue
        prefix = f"{result.session.key}:"
        current_keys = {item.key for item in result.items}
        for key in list(previous):
            if (
                key.startswith(prefix)
                and key not in current_keys
                and streaks.get(key, 0) <= -confirmations
            ):
                previous[key] = False
        for item in result.items:
            streak = streaks.get(item.key, 0)
            if streak >= confirmations and item.key not in preserved:
                previous[item.key] = True
            elif streak <= -confirmations:
                previous[item.key] = False


def notification_signature(item: MonitorItemT) -> str:
    """内容变化会形成新事件并重新获得完整推送重试预算。"""
    if isinstance(item, PriceLevelItem):
        return "|".join(
            (
                "price",
                item.key,
                compact_session_name(item.session.session_name),
                item.seat_plan_name,
                format(item.original_price, ".2f")
                if item.original_price is not None
                else "",
            )
        )
    return "|".join(
        ("session", item.key, compact_session_name(item.session_name), item.status)
    )


def record_notification_outcome(
    items: Iterable[MonitorItemT],
    delivered_keys: Iterable[str],
    retries: dict[str, NotificationRetry],
    *,
    max_attempts: int,
) -> tuple[set[str], set[str]]:
    """记录本轮结果，返回仍待重试和已耗尽预算的条目 key。"""
    delivered = set(delivered_keys)
    pending: set[str] = set()
    exhausted: set[str] = set()
    for item in items:
        if item.key in delivered:
            retries.pop(item.key, None)
            continue
        signature = notification_signature(item)
        existing = retries.get(item.key)
        attempts = (
            existing.attempts + 1
            if existing is not None and existing.signature == signature
            else 1
        )
        if attempts >= max_attempts:
            exhausted.add(item.key)
            retries.pop(item.key, None)
        else:
            retries[item.key] = NotificationRetry(signature, attempts)
            pending.add(item.key)
    return pending, exhausted


def clear_resolved_notification_retries(
    retries: dict[str, NotificationRetry],
    session_statuses: dict[str, str],
    price_level_availability: dict[str, bool],
    *,
    preserve_keys: Iterable[str] = (),
) -> None:
    """售罄后清掉旧事件预算，让下一次真实回流重新获得 5 次机会。"""
    preserved = set(preserve_keys)
    for key in list(retries):
        if key in preserved:
            continue
        if key in price_level_availability:
            if price_level_availability[key] is False:
                retries.pop(key, None)
        elif key in session_statuses and session_statuses[key] not in AVAILABLE_STATUSES:
            retries.pop(key, None)


def filter_notification_cooldown(
    items: Iterable[MonitorItemT],
    last_notified_at: dict[str, float],
    *,
    now: float,
    cooldown: float,
) -> list[MonitorItemT]:
    """同一场次/票档在冷却期内即使状态抖动，也不重复发送。"""
    if cooldown <= 0:
        return list(items)
    return [
        item
        for item in items
        if now - last_notified_at.get(item.key, float("-inf")) >= cooldown
    ]


def mark_notified(
    items: Iterable[MonitorItemT],
    last_notified_at: dict[str, float],
    *,
    now: float,
) -> None:
    for item in items:
        last_notified_at[item.key] = now


def compact_session_name(name: str) -> str:
    for marker in ("-Center Court", "–Center Court"):
        if marker in name:
            return name.split(marker, 1)[0].strip()
    return name.strip()


def session_comparison_key(session: InventoryItem) -> str:
    """Build a conservative cross-channel identity for the same playing session."""
    compact = re.sub(r"\s+", "", compact_session_name(session.session_name)).casefold()
    date_match = re.search(r"\d{1,2}月\d{1,2}日", compact)
    if date_match is None:
        return f"{session.target.show_label.casefold()}|{compact}"

    show_label = session.target.show_label.casefold()
    venue = next(
        (
            value
            for marker, value in (
                ("中央", "center"),
                ("2号", "court-2"),
                ("资格赛", "qualifying"),
                ("嘉年华", "fan-week"),
            )
            if marker.casefold() in show_label
        ),
        show_label,
    )
    stage = next(
        (
            marker
            for marker in (
                "四分之一决赛",
                "半决赛",
                "资格赛",
                "决赛",
                "第三轮",
                "第二轮",
                "第一轮",
            )
            if marker in compact
        ),
        "",
    )
    daypart = next(
        (marker for marker in ("日场", "夜场", "上午", "下午", "晚场") if marker in compact),
        "",
    )
    return "|".join((venue, date_match.group(0), stage, daypart))


def format_price(price: float | None) -> str:
    if price is None:
        return "价格未知"
    numeric_price = float(price)
    if numeric_price.is_integer():
        return f"¥{numeric_price:,.0f}"
    return f"¥{numeric_price:,.2f}".rstrip("0").rstrip(".")


def build_other_channel_context(
    trigger_items: Iterable[PriceLevelItem],
    price_results: Iterable[PriceLevelResult],
    catalog_items: Iterable[InventoryItem],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Describe other channels that currently sell the same playing session."""
    triggers = list(trigger_items)
    results = list(price_results)
    catalogs = list(catalog_items)
    source_pairs = {
        (item.target.channel, session_comparison_key(item.session))
        for item in triggers
    }
    newly_available_pairs = set(source_pairs)

    labels: dict[str, str] = {}
    observed_pairs: set[tuple[str, str]] = set()
    available_levels: dict[tuple[str, str], list[PriceLevelItem]] = defaultdict(list)
    for result in results:
        channel = result.session.target.channel
        labels[channel] = result.session.target.channel_label
        pair = (channel, session_comparison_key(result.session))
        if result.error is not None:
            continue
        observed_pairs.add(pair)
        available_levels[pair].extend(item for item in result.items if item.available)

    catalog_available_pairs: set[tuple[str, str]] = set()
    for item in catalogs:
        labels[item.target.channel] = item.target.channel_label
        if item.status in AVAILABLE_STATUSES:
            catalog_available_pairs.add(
                (item.target.channel, session_comparison_key(item))
            )

    contexts: dict[tuple[str, str], tuple[str, ...]] = {}
    for source_channel, session_key in source_pairs:
        messages: list[str] = []
        other_channels = sorted(
            channel for channel in labels if channel != source_channel
        )
        for other_channel in other_channels:
            pair = (other_channel, session_key)
            label = labels[other_channel]
            if pair in observed_pairs:
                levels = available_levels.get(pair, [])
                if not levels:
                    continue
                grade_labels = list(
                    dict.fromkeys(
                        f"{item.seat_plan_name} {format_price(item.original_price)}"
                        for item in levels
                    )
                )
                grade_summary = "、".join(grade_labels[:4])
                if len(grade_labels) > 4:
                    grade_summary += f"等 {len(grade_labels)} 个票档"
                status_text = (
                    "本轮也检测到回流"
                    if pair in newly_available_pairs
                    else "当前已有票"
                )
                messages.append(f"{label}同场次{status_text}：{grade_summary}")
            elif pair in catalog_available_pairs:
                messages.append(
                    f"{label}同场次最近一次目录状态为有票（具体票档未确认）"
                )
        if messages:
            contexts[(source_channel, session_key)] = tuple(messages)
    return contexts


def select_other_channel_context_sessions(
    trigger_items: Iterable[PriceLevelItem],
    price_results: Iterable[PriceLevelResult],
    catalog_items: Iterable[InventoryItem],
) -> list[InventoryItem]:
    """Select missing same-session channels for a one-off notification lookup."""
    source_pairs = {
        (item.target.channel, session_comparison_key(item.session))
        for item in trigger_items
    }
    observed_pairs = {
        (result.session.target.channel, session_comparison_key(result.session))
        for result in price_results
    }
    selected: list[InventoryItem] = []
    selected_keys: set[str] = set()
    for candidate in catalog_items:
        if candidate.status not in AVAILABLE_STATUSES:
            continue
        candidate_pair = (
            candidate.target.channel,
            session_comparison_key(candidate),
        )
        if candidate_pair in observed_pairs or candidate.key in selected_keys:
            continue
        if not any(
            source_channel != candidate.target.channel
            and source_session_key == candidate_pair[1]
            for source_channel, source_session_key in source_pairs
        ):
            continue
        selected.append(candidate)
        selected_keys.add(candidate.key)
    return selected


def display_results(
    results: Iterable[TargetResult],
    count: int,
    interval: int,
    *,
    match: str | None = None,
    verbose: bool = False,
) -> list[InventoryItem]:
    all_items: list[InventoryItem] = []
    summaries: list[str] = []
    for result in results:
        label = f"{result.target.channel_label}/{result.target.show_label}"
        if result.error:
            summaries.append(f"❓ {label}")
            log.warning("请求失败 %s: %s", label, result.error)
            continue
        items = filter_items(result.items, match)
        all_items.extend(items)
        available = sum(item.status in AVAILABLE_STATUSES for item in items)
        sold_out = sum(item.status in SOLD_OUT_STATUSES for item in items)
        other = len(items) - available - sold_out
        summaries.append(f"{label} 🟢{available}/🔴{sold_out}/⚪{other}")
        if verbose:
            for item in items:
                log.info(
                    "    %s | %s | %s",
                    STATUS_TEXT.get(item.status, f"❓ {item.status}"),
                    result.target.show_label,
                    item.session_name,
                )

    suffix = "仅检查一次" if interval <= 0 else f"下次 {interval}s"
    log.info("#%d  %s  [%s]", count, " | ".join(summaries), suffix)
    return all_items


def display_price_level_results(
    results: Iterable[PriceLevelResult],
    *,
    verbose: bool = False,
) -> list[PriceLevelItem]:
    all_items: list[PriceLevelItem] = []
    failed = 0
    for result in results:
        if result.error:
            failed += 1
            log.warning(
                "票档请求失败 %s/%s: %s",
                result.session.target.channel_label,
                compact_session_name(result.session.session_name),
                result.error,
            )
            continue
        all_items.extend(result.items)
        if verbose:
            for item in result.items:
                log.info(
                    "    %s | %s | %s | %s | %s",
                    "🟢 可购" if item.available else "🔴 暂无",
                    item.target.show_label,
                    compact_session_name(item.session.session_name),
                    item.seat_plan_name,
                    format_price(item.original_price),
                )
    available = sum(item.available for item in all_items)
    log.info(
        "    票档库存 🟢%d/🔴%d%s",
        available,
        len(all_items) - available,
        f"/❓{failed}" if failed else "",
    )
    return all_items


def format_session_rows(
    results: Iterable[TargetResult],
    *,
    match: str | None = None,
) -> list[str]:
    """生成当前公开场次清单，供 --list-sessions 展示。"""
    rows = ["channel\tshow\tstatus\tsession_id\tsession_name"]
    for result in results:
        target = result.target
        if result.error:
            rows.append(
                f"{target.channel}\t{target.show_label}\tERROR\t-\t请求失败"
            )
            continue
        for item in filter_items(result.items, match):
            rows.append(
                "\t".join(
                    (
                        target.channel,
                        target.show_label,
                        STATUS_TEXT.get(item.status, item.status),
                        item.session_id,
                        item.session_name,
                    )
                )
            )
    return rows


def format_price_level_rows(results: Iterable[PriceLevelResult]) -> list[str]:
    """生成票档级实时库存清单，供 --list-price-levels 展示。"""
    rows = [
        "channel\tshow\tsession\tstatus\tseat_plan_id\tseat_plan_name\tprice\tcan_buy_count"
    ]
    for result in results:
        session = result.session
        if result.error:
            rows.append(
                "\t".join(
                    (
                        session.target.channel,
                        session.target.show_label,
                        compact_session_name(session.session_name),
                        "ERROR",
                        "-",
                        "请求失败",
                        "-",
                        "-",
                    )
                )
            )
            continue
        for item in result.items:
            rows.append(
                "\t".join(
                    (
                        item.target.channel,
                        item.target.show_label,
                        compact_session_name(item.session.session_name),
                        "🟢 可购" if item.available else "🔴 暂无",
                        item.seat_plan_id,
                        item.seat_plan_name,
                        format_price(item.original_price),
                        str(item.can_buy_count),
                    )
                )
            )
    return rows


def notify_available(
    dispatcher: NotificationDispatcher,
    items: Iterable[InventoryItem],
) -> set[str]:
    grouped: dict[tuple[str, str], list[InventoryItem]] = defaultdict(list)
    for item in items:
        grouped[(item.target.channel, item.target.buy_url)].append(item)

    delivered_keys: set[str] = set()
    for channel_items in grouped.values():
        target = channel_items[0].target
        lines = [
            f"🟢 {item.target.show_label} · {compact_session_name(item.session_name)}"
            for item in channel_items[:8]
        ]
        if len(channel_items) > 8:
            lines.append(f"…另有 {len(channel_items) - 8} 个场次")
        lines.extend(("", target.date_hint, f"👉 {target.buy_hint}"))
        results = dispatcher.send(
            NotificationEvent(
                title=f"🎾 {target.channel_label} 回流票来了！",
                body="\n".join(lines),
                url=target.buy_url,
                urgent=True,
                preview="；".join(
                    f"{compact_session_name(item.session_name)}有票"
                    for item in channel_items[:3]
                ),
            )
        )
        if any(results.values()):
            delivered_keys.update(item.key for item in channel_items)
    return delivered_keys


def notify_price_levels_available(
    dispatcher: NotificationDispatcher,
    items: Iterable[PriceLevelItem],
    *,
    other_channel_context: Mapping[tuple[str, str], tuple[str, ...]] | None = None,
) -> set[str]:
    grouped: dict[tuple[str, str], list[PriceLevelItem]] = defaultdict(list)
    for item in items:
        grouped[(item.target.channel, item.target.buy_url)].append(item)

    delivered_keys: set[str] = set()
    for channel_items in grouped.values():
        target = channel_items[0].target
        lines = [
            (
                f"🟢 {item.target.show_label} · "
                f"{compact_session_name(item.session.session_name)} · "
                f"{item.seat_plan_name} {format_price(item.original_price)} · "
                f"当前最多可选 {item.can_buy_count} 张"
            )
            for item in channel_items[:8]
        ]
        if len(channel_items) > 8:
            lines.append(f"…另有 {len(channel_items) - 8} 个可购票档")
        context_lines: list[str] = []
        for item in channel_items:
            key = (item.target.channel, session_comparison_key(item.session))
            for message in (other_channel_context or {}).get(key, ()):
                session_message = (
                    f"{compact_session_name(item.session.session_name)} · {message}"
                )
                if session_message not in context_lines:
                    context_lines.append(session_message)
        if context_lines:
            lines.extend(("", *(f"📌 其他入口：{line}" for line in context_lines)))
        lines.extend(
            (
                "",
                BUYABLE_COUNT_NOTE,
                f"👉 {target.buy_hint}",
            )
        )
        results = dispatcher.send(
            NotificationEvent(
                title=f"🎾 {target.channel_label} 指定票档回流！",
                body="\n".join(lines),
                url=target.buy_url,
                urgent=True,
                preview="；".join(
                    (
                        f"{compact_session_name(item.session.session_name)} · "
                        f"{item.seat_plan_name} {format_price(item.original_price)} · "
                        f"最多可选 {item.can_buy_count} 张"
                    )
                    for item in channel_items[:2]
                ),
            )
        )
        if any(results.values()):
            delivered_keys.update(item.key for item in channel_items)
    return delivered_keys


class StreamingPriceResultProcessor:
    """Process and notify each completed price query before the batch ends."""

    def __init__(
        self,
        *,
        client: HttpClient,
        dispatcher: NotificationDispatcher,
        state: MonitorState,
        runtime: RuntimeOptions,
        catalog_items: Iterable[InventoryItem],
        previous_price_levels: dict[str, bool],
        price_level_streaks: dict[str, int],
        baselined_price_sessions: set[str],
        last_notified_at: dict[str, float],
        notification_retries: dict[str, NotificationRetry],
        round_rate_limits: list[TargetResult | PriceLevelResult],
        round_started: float,
        request_gap_seconds: float,
        grades: str | None,
        dry_run: bool,
    ) -> None:
        self.client = client
        self.dispatcher = dispatcher
        self.state = state
        self.runtime = runtime
        self.catalog_items = list(catalog_items)
        self.previous_price_levels = previous_price_levels
        self.price_level_streaks = price_level_streaks
        self.baselined_price_sessions = baselined_price_sessions
        self.last_notified_at = last_notified_at
        self.notification_retries = notification_retries
        self.round_rate_limits = round_rate_limits
        self.round_started = round_started
        self.request_gap_seconds = request_gap_seconds
        self.grades = grades
        self.dry_run = dry_run
        self.observed_results: list[PriceLevelResult] = []
        self.processed_session_keys: set[str] = set()
        self.pending_notification_keys: set[str] = set()
        self.pending_confirmation = 0

    def handle(self, raw_result: PriceLevelResult) -> None:
        """Run on the monitor thread as soon as one worker result completes."""
        result = filter_price_level_results([raw_result], self.grades)[0]
        session_key = result.session.key
        if session_key in self.processed_session_keys:
            return
        self.processed_session_keys.add(session_key)
        self.observed_results.append(result)
        if result.rate_limited:
            self.round_rate_limits.append(result)

        update_price_level_streaks(
            self.price_level_streaks,
            [result],
            confirmations=self.runtime.availability_confirmations,
            baselined_sessions=self.baselined_price_sessions,
        )
        if result.error is not None:
            return

        initial_session_round = session_key not in self.baselined_price_sessions
        confirmed_items = confirmed_available_price_levels(
            result.items,
            self.price_level_streaks,
            confirmations=self.runtime.availability_confirmations,
        )
        newly_available = find_newly_available_price_levels(
            self.previous_price_levels,
            confirmed_items,
            initial_round=initial_session_round,
            notify_initial=self.runtime.notify_initial,
        )
        if not initial_session_round:
            self.pending_confirmation += sum(
                item.available
                and self.previous_price_levels.get(item.key) is not True
                and 0
                < self.price_level_streaks.get(item.key, 0)
                < self.runtime.availability_confirmations
                for item in result.items
            )

        result_received_at = time.monotonic()
        now = time.time()
        eligible = filter_notification_cooldown(
            newly_available,
            self.last_notified_at,
            now=now,
            cooldown=self.runtime.notification_cooldown,
        )
        suppressed = len(newly_available) - len(eligible)
        if suppressed:
            log.info("🔕 %d 个票档仍在通知冷却期，已抑制重复提醒", suppressed)

        result_pending_keys: set[str] = set()
        if eligible:
            result_pending_keys = self._notify(
                eligible,
                now=now,
                result_received_at=result_received_at,
            )
        update_confirmed_price_level_state(
            self.previous_price_levels,
            [result],
            self.price_level_streaks,
            confirmations=self.runtime.availability_confirmations,
            preserve_available_keys=result_pending_keys,
        )
        self.baselined_price_sessions.add(session_key)

    def _notify(
        self,
        eligible: list[PriceLevelItem],
        *,
        now: float,
        result_received_at: float,
    ) -> set[str]:
        log.info("🎉 连续确认 %d 个可购票档", len(eligible))
        for item in eligible:
            log.info(
                "   ↳ %s · %s · %s · %s · 当前最多可选 %d 张",
                item.target.channel_label,
                compact_session_name(item.session.session_name),
                item.seat_plan_name,
                format_price(item.original_price),
                item.can_buy_count,
            )

        delivered_keys: set[str] = set()
        if self.dry_run:
            log.info(
                "🧪 静默演练：本轮原本会发送 %d 个票档的合并通知",
                len(eligible),
            )
            delivered_keys = {item.key for item in eligible}
        elif self.dispatcher.configured:
            context_price_results = list(self.observed_results)
            context_sessions = select_other_channel_context_sessions(
                eligible,
                self.observed_results,
                self.catalog_items,
            )
            if context_sessions:
                log.info(
                    "🔗 为回流通知补查 %d 个同场次其他平台入口",
                    len(context_sessions),
                )
                blocked_channels = {
                    (
                        result.target.channel
                        if isinstance(result, TargetResult)
                        else result.session.target.channel
                    )
                    for result in self.round_rate_limits
                }
                extra_context_results = filter_price_level_results(
                    check_all_price_levels(
                        self.client,
                        context_sessions,
                        request_gap_seconds=self.request_gap_seconds,
                        blocked_channels=blocked_channels,
                    ),
                    self.grades,
                )
                self.round_rate_limits.extend(
                    result
                    for result in extra_context_results
                    if result.rate_limited
                )
                context_price_results.extend(extra_context_results)
            other_channel_context = build_other_channel_context(
                eligible,
                context_price_results,
                self.catalog_items,
            )
            delivered_keys = notify_price_levels_available(
                self.dispatcher,
                eligible,
                other_channel_context=other_channel_context,
            )
        else:
            log.warning("未配置通知渠道，未发送推送")

        delivered_items = [
            item for item in eligible if item.key in delivered_keys
        ]
        if delivered_items:
            mark_notified(delivered_items, self.last_notified_at, now=now)
            self.state.alert_count += len(delivered_items)
            log.info(
                "⚡ 结果到达后 %.2fs 完成推送（本轮开始后 %.2fs）",
                time.monotonic() - result_received_at,
                time.monotonic() - self.round_started,
            )

        pending_keys, exhausted_keys = record_notification_outcome(
            eligible,
            delivered_keys,
            self.notification_retries,
            max_attempts=self.runtime.notification_max_retries,
        )
        self.pending_notification_keys.update(pending_keys)
        if pending_keys:
            highest_attempt = max(
                self.notification_retries[key].attempts for key in pending_keys
            )
            log.warning(
                "⚠️ %d 个票档推送全部失败，保留回流事件下轮重试（%d/%d）",
                len(pending_keys),
                highest_attempt,
                self.runtime.notification_max_retries,
            )
        if exhausted_keys:
            log.warning(
                "🛑 %d 个票档推送已连续失败 %d 次；停止本次事件重试，"
                "待其售罄后再次回流会重新提醒",
                len(exhausted_keys),
                self.runtime.notification_max_retries,
            )
        return pending_keys


def notify_error(dispatcher: NotificationDispatcher, error_count: int) -> None:
    dispatcher.send(
        NotificationEvent(
            title="⚠️ 上海大师赛监控异常",
            body=(
                f"全部票务接口已连续 {error_count} 轮失败\n"
                "请检查网络或平台状态"
            ),
            urgent=False,
            audience="operator",
        )
    )


def notify_rate_limit(
    dispatcher: NotificationDispatcher,
    channels: Iterable[str],
    backoff_seconds: float,
) -> bool:
    results = dispatcher.send(
        NotificationEvent(
            title="⏳ 上海大师赛接口触发限流",
            body=(
                f"受影响渠道：{'、'.join(channels)}\n"
                f"监控已自动退避约 {backoff_seconds:g} 秒，期间不会继续高频请求"
            ),
            urgent=False,
            audience="operator",
        )
    )
    return any(results.values())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="🎾 2026 上海劳力士大师赛回流票监控")
    parser.add_argument(
        "--bark",
        "-b",
        default=os.environ.get("BARK_KEY"),
        help="Bark key 或完整 URL（也可设 BARK_KEY）",
    )
    parser.add_argument(
        "--notify-channels",
        default=os.environ.get("NOTIFY_CHANNELS"),
        help="通知渠道：bark,serverchan,wxpusher（默认使用全部已配置渠道）",
    )
    parser.add_argument(
        "--interval",
        "-i",
        type=int,
        default=int(os.environ.get("MONITOR_INTERVAL", "15")),
        help="票档/库存刷新周期秒数（默认 15，最低 5）",
    )
    parser.add_argument(
        "--channels",
        default=os.environ.get("CHANNELS"),
        help="渠道：juss,meta,piaoxingqiu（meta 与 juss 共用久事库存）",
    )
    parser.add_argument(
        "--match",
        default=os.environ.get("MATCH"),
        help="只保留名称含指定词的场次，多个词用逗号分隔",
    )
    parser.add_argument(
        "--grades",
        default=os.environ.get("GRADES"),
        help="只监控完整名称匹配的票档，如 S,A+,A,B（默认全部票档）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="静默演练：真实查询和判断，但绝不调用任何通知接口",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="输出每个场次的状态")
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="查询并列出当前全部场次、状态和 session ID 后退出",
    )
    parser.add_argument(
        "--list-price-levels",
        action="store_true",
        help="查询并列出所选场次的票档、价格和实时可购状态后退出",
    )
    parser.add_argument(
        "--list-notifiers",
        action="store_true",
        help="列出支持及已配置的通知渠道后退出",
    )
    parser.add_argument(
        "--test-notification",
        action="store_true",
        help="向已配置的通知渠道发送一条测试消息后退出",
    )
    parser.add_argument("--once", action="store_true", help="检查一次后退出")
    return parser


def session_scope_is_allowed(
    sessions: Iterable[InventoryItem],
    limit: int | None,
) -> bool:
    if limit is None:
        return True
    count = len(sessions) if isinstance(sessions, list) else sum(1 for _ in sessions)
    if count <= limit:
        return True
    log.error(
        "个人版单次最多监控 %d 个场次入口，当前匹配 %d 个；请缩小 --match 范围",
        limit,
        count,
    )
    return False


def main(
    argv: list[str] | None = None,
    *,
    runtime: RuntimeOptions | None = None,
    dispatcher_builder: Any = build_dispatcher,
    client_builder: Any = build_http_client,
    target_checker: Any = check_all,
    price_checker: Any = check_all_price_levels,
    item_scope_filter: Any = None,
    item_scope_label: str = "监控范围",
) -> int:
    load_env()
    runtime = runtime or RuntimeOptions()
    args = build_parser().parse_args(argv)
    setup_logging(
        None
        if args.once
        or args.list_sessions
        or args.list_price_levels
        or args.list_notifiers
        or args.test_notification
        else runtime.log_path
    )

    if args.interval < 5:
        log.error("--interval 最低为 5 秒，避免对公开票务接口造成过高压力")
        return 2
    if runtime.session_refresh_interval < 5:
        log.error("场次目录刷新周期最低为 5 秒")
        return 2
    if runtime.rate_limit_backoff < 5:
        log.error("限流退避最低为 5 秒")
        return 2
    if runtime.availability_confirmations < 1:
        log.error("票档确认轮数最低为 1")
        return 2
    if runtime.notification_max_retries < 1:
        log.error("通知失败重试次数最低为 1")
        return 2
    if (
        runtime.request_gap_ms < 0
        or runtime.jitter < 0
        or runtime.notification_cooldown < 0
    ):
        log.error("请求间隔、随机抖动和通知冷却时间不能为负数")
        return 2

    if runtime.inventory_mode not in {"auto", "session", "price"}:
        log.error("库存粒度必须是 auto、session 或 price")
        return 2

    request_gap_seconds = runtime.request_gap_ms / 1000
    STOP_REQUESTED.clear()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)

    try:
        dispatcher = dispatcher_builder(
            os.environ,
            bark_key=args.bark,
            channels=args.notify_channels,
            icon_url=ICON_URL,
        )
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    if args.list_notifiers:
        configured = {notifier.channel for notifier in dispatcher.notifiers}
        for channel in dispatcher.supported_channels:
            print(f"{channel:<10} {'configured' if channel in configured else 'not configured'}")
        return 0

    try:
        target_catalog = build_targets(
            os.environ.get("BUY_URL_MODE")
            or os.environ.get("BARK_BUY_URL_MODE", "alipay")
        )
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    if args.test_notification:
        if args.dry_run:
            log.error("--dry-run 不会调用通知接口，不能与 --test-notification 同时使用")
            return 2
        if not dispatcher.configured:
            log.error("没有已配置的通知渠道，请先填写 .env")
            return 2
        try:
            test_catalog = select_targets(args.channels, target_catalog)
        except ValueError as exc:
            log.error("%s", exc)
            return 2
        test_targets: list[EventTarget] = []
        seen_channels: set[str] = set()
        for target in test_catalog:
            if target.channel not in seen_channels:
                test_targets.append(target)
                seen_channels.add(target.channel)
        sent_results: list[dict[str, bool]] = []
        for target in test_targets:
            sent_results.append(
                dispatcher.send(
                    NotificationEvent(
                        title=f"🎾 {target.channel_label}入口测试",
                        body=(
                            f"点击本通知应打开“{target.show_label}”的官方购票入口。"
                            "正式监控只会在发现回流票或连续异常时推送。"
                        ),
                        url=target.buy_url,
                    )
                )
            )
        return (
            0
            if sent_results
            and all(result and all(result.values()) for result in sent_results)
            else 1
        )

    try:
        targets = select_targets(args.channels, target_catalog)
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    if (
        not runtime.allow_unfiltered_monitoring
        and not args.match
        and not args.list_sessions
    ):
        log.error("个人版需要使用 --match 选择要监控的场次；可先用 --list-sessions 查看")
        return 2

    try:
        client, network_label = client_builder(os.environ)
    except ValueError as exc:
        log.error("网络配置无效: %s", exc)
        return 2

    if args.list_sessions or args.list_price_levels:
        log.info("正在查询 %d 个票务活动的公开场次…", len(targets))
        results = target_checker(
            client,
            targets,
            request_gap_seconds=request_gap_seconds,
        )
        if STOP_REQUESTED.is_set():
            log.info("👋 已停止")
            return 0
        if args.list_sessions:
            for row in format_session_rows(results, match=args.match):
                print(row)
            return 1 if any(result.rate_limited for result in results) or all(
                result.error is not None for result in results
            ) else 0

        sessions = [
            item
            for result in results
            if result.error is None
            for item in filter_items(result.items, args.match)
        ]
        if not session_scope_is_allowed(sessions, runtime.max_monitored_sessions):
            return 2
        log.info("正在查询 %d 个所选场次的公开票档…", len(sessions))
        price_results = filter_price_level_results(
            price_checker(
                client,
                sessions,
                request_gap_seconds=request_gap_seconds,
            ),
            args.grades,
        )
        if STOP_REQUESTED.is_set():
            log.info("👋 已停止")
            return 0
        for row in format_price_level_rows(price_results):
            print(row)
        all_sessions_failed = all(result.error is not None for result in results)
        all_prices_failed = bool(price_results) and all(
            result.error is not None for result in price_results
        )
        any_rate_limited = any(result.rate_limited for result in results) or any(
            result.rate_limited for result in price_results
        )
        return 1 if all_sessions_failed or all_prices_failed or any_rate_limited else 0

    price_mode = (
        runtime.inventory_mode == "price"
        or item_scope_filter is not None
        or (runtime.inventory_mode == "auto" and bool(args.match or args.grades))
    )
    state_enabled = runtime.state_enabled and not args.dry_run
    state_path = runtime.state_path
    state, state_restored = (
        load_monitor_state(state_path)
        if state_enabled
        else (MonitorState.empty(), False)
    )

    log.info(
        (
            "🎾 启动 %s | 库存 %ds | 场次目录 %ds | 请求间隔 %dms | "
            "抖动 ±%.1fs | 票档确认 %d 轮 | 通知冷却 %ds | 推送重试 %d 次 | "
            "限流退避 ≥%ds | 状态 %s | "
            "通知 %s | 网络 %s | "
            "活动 %d 个 | %s%s%s%s"
        ),
        EVENT_NAME,
        args.interval,
        runtime.session_refresh_interval,
        runtime.request_gap_ms,
        runtime.jitter,
        runtime.availability_confirmations,
        runtime.notification_cooldown,
        runtime.notification_max_retries,
        runtime.rate_limit_backoff,
        (
            f"已恢复（累计 {state.poll_count} 轮）"
            if state_restored
            else ("本轮静默建基线" if state_enabled else "仅内存")
        ),
        "静默演练" if args.dry_run else dispatcher.summary,
        network_label,
        len(targets),
        "票档级" if price_mode else "场次级",
        f" | 筛选 {args.match}" if args.match else "",
        f" | 票档 {args.grades}" if args.grades else "",
        f" | {item_scope_label}" if item_scope_filter is not None else "",
    )

    previous = state.session_statuses
    baselined_session_targets = state.baselined_session_targets
    previous_price_levels = state.price_level_availability
    price_level_streaks = state.price_level_streaks
    baselined_price_sessions = state.baselined_price_sessions
    last_notified_at = state.last_notified_at
    notification_retries = state.notification_retries
    count = 0
    consecutive_errors = 0
    error_alerted = False
    rate_limit_alerted = bool(state.active_rate_limit_channels)
    current_interval = args.interval
    session_cache: dict[str, TargetResult] = {}
    last_session_refresh_at: float | None = None
    catalog_warning: str | None = None

    while True:
        round_started = time.monotonic()
        round_wall_time = time.time()
        try:
            count += 1
            state.poll_count += 1
            round_rate_limits: list[TargetResult | PriceLevelResult] = []
            refresh_sessions = session_catalog_refresh_due(
                price_mode=price_mode,
                has_cached_results=bool(session_cache),
                last_refresh_at=last_session_refresh_at,
                now=round_started,
                refresh_interval=runtime.session_refresh_interval,
            )
            fresh_results: list[TargetResult] = []
            if refresh_sessions:
                fresh_results = target_checker(
                    client,
                    targets,
                    request_gap_seconds=request_gap_seconds,
                )
                if STOP_REQUESTED.is_set():
                    log.info("👋 已停止")
                    return 0
                round_rate_limits.extend(
                    result for result in fresh_results if result.rate_limited
                )
                failed_targets = sum(
                    result.error is not None for result in fresh_results
                )
                catalog_warning = (
                    f"场次目录部分失败: {failed_targets}/{len(fresh_results)}"
                    if failed_targets
                    else None
                )
                if price_mode:
                    update_session_cache(session_cache, fresh_results)
                    results = cached_target_results(session_cache, targets)
                    if results:
                        # A partial first refresh is still useful. Retry failed
                        # catalogs on the configured catalog interval instead
                        # of hammering them on every inventory cycle.
                        last_session_refresh_at = round_started
                    if failed_targets and results:
                        log.warning(
                            "⚠️ 场次目录刷新 %d/%d 个失败，继续使用成功缓存",
                            failed_targets,
                            len(fresh_results),
                        )
                else:
                    results = fresh_results
            else:
                results = cached_target_results(session_cache, targets)

            catalog_items = display_results(
                results,
                count,
                0 if args.once else current_interval,
                match=args.match,
                verbose=args.verbose,
            )
            current_items = catalog_items
            if item_scope_filter is not None:
                catalog_item_count = len(current_items)
                current_items = item_scope_filter(
                    current_items,
                    previous,
                    price_level_availability=previous_price_levels,
                    active_notification_keys=notification_retries,
                )
                if refresh_sessions:
                    log.info(
                        "🎯 %s %d/%d 个场次入口"
                        "（含刚恢复票档探测与待重试通知）",
                        item_scope_label,
                        len(current_items),
                        catalog_item_count,
                    )
            if not session_scope_is_allowed(
                current_items,
                runtime.max_monitored_sessions,
            ):
                return 2
            session_catalog_unavailable = not results
            if price_mode and results and not current_items:
                if item_scope_filter is not None:
                    log.info("ℹ️ 当前%s没有需要高频查询的场次", item_scope_label)
                else:
                    log.warning("⚠️ 当前场次筛选没有匹配结果，请检查 MATCH")

            price_results: list[PriceLevelResult] = []
            all_prices_failed = False
            pending_session_notification_keys: set[str] = set()
            pending_price_notification_keys: set[str] = set()
            if price_mode and current_items:
                # 场次目录接口已限流的渠道，本轮不能再拿缓存场次继续请求票档接口。
                # 否则虽然目录层停止了，同渠道仍会在票档层继续施压。
                blocked_price_channels = {
                    result.target.channel
                    for result in round_rate_limits
                    if isinstance(result, TargetResult)
                }
                stream_processor = StreamingPriceResultProcessor(
                    client=client,
                    dispatcher=dispatcher,
                    state=state,
                    runtime=runtime,
                    catalog_items=catalog_items,
                    previous_price_levels=previous_price_levels,
                    price_level_streaks=price_level_streaks,
                    baselined_price_sessions=baselined_price_sessions,
                    last_notified_at=last_notified_at,
                    notification_retries=notification_retries,
                    round_rate_limits=round_rate_limits,
                    round_started=round_started,
                    request_gap_seconds=request_gap_seconds,
                    grades=args.grades,
                    dry_run=args.dry_run,
                )
                raw_price_results = price_checker(
                    client,
                    current_items,
                    request_gap_seconds=request_gap_seconds,
                    blocked_channels=blocked_price_channels,
                    result_callback=stream_processor.handle,
                )
                # Custom checkers may return results without supporting the
                # callback contract. Process any such result before state save.
                for raw_result in raw_price_results:
                    if (
                        raw_result.session.key
                        not in stream_processor.processed_session_keys
                    ):
                        stream_processor.handle(raw_result)
                price_results = filter_price_level_results(
                    raw_price_results,
                    args.grades,
                )
                if STOP_REQUESTED.is_set():
                    log.info("👋 已停止")
                    return 0
                display_price_level_results(
                    price_results,
                    verbose=args.verbose,
                )
                pending_price_notification_keys = (
                    stream_processor.pending_notification_keys
                )
                if stream_processor.pending_confirmation:
                    log.info(
                        "🔎 检测到 %d 个候选可购票档，等待连续 %d 轮确认",
                        stream_processor.pending_confirmation,
                        runtime.availability_confirmations,
                    )
                all_prices_failed = bool(price_results) and all(
                    result.error is not None for result in price_results
                )
            elif not price_mode:
                items_by_target: dict[str, list[InventoryItem]] = defaultdict(list)
                for item in current_items:
                    items_by_target[item.target.show_id].append(item)
                newly: list[InventoryItem] = []
                for show_id, target_items in items_by_target.items():
                    newly.extend(
                        find_newly_available(
                            previous,
                            target_items,
                            initial_round=show_id not in baselined_session_targets,
                            notify_initial=runtime.notify_initial,
                        )
                    )
                eligible_sessions = filter_notification_cooldown(
                    newly,
                    last_notified_at,
                    now=round_wall_time,
                    cooldown=runtime.notification_cooldown,
                )
                suppressed = len(newly) - len(eligible_sessions)
                if suppressed:
                    log.info("🔕 %d 个场次仍在通知冷却期，已抑制重复提醒", suppressed)
                if eligible_sessions:
                    log.info("🎉 新出现 %d 个有票场次", len(eligible_sessions))
                    delivered_keys: set[str] = set()
                    if args.dry_run:
                        log.info(
                            "🧪 静默演练：本轮原本会发送 %d 个场次的合并通知",
                            len(eligible_sessions),
                        )
                        delivered_keys = {item.key for item in eligible_sessions}
                    elif dispatcher.configured:
                        delivered_keys = notify_available(dispatcher, eligible_sessions)
                    else:
                        log.warning("未配置通知渠道，未发送推送")
                    delivered_items = [
                        item for item in eligible_sessions if item.key in delivered_keys
                    ]
                    if delivered_items:
                        mark_notified(
                            delivered_items,
                            last_notified_at,
                            now=round_wall_time,
                        )
                        state.alert_count += len(delivered_items)
                    (
                        pending_session_notification_keys,
                        exhausted_session_keys,
                    ) = record_notification_outcome(
                        eligible_sessions,
                        delivered_keys,
                        notification_retries,
                        max_attempts=runtime.notification_max_retries,
                    )
                    if pending_session_notification_keys:
                        highest_attempt = max(
                            notification_retries[key].attempts
                            for key in pending_session_notification_keys
                        )
                        log.warning(
                            "⚠️ %d 个场次推送全部失败，保留回流事件下轮重试（%d/%d）",
                            len(pending_session_notification_keys),
                            highest_attempt,
                            runtime.notification_max_retries,
                        )
                    if exhausted_session_keys:
                        log.warning(
                            "🛑 %d 个场次推送已连续失败 %d 次；停止本次事件重试，"
                            "待其售罄后再次回流会重新提醒",
                            len(exhausted_session_keys),
                            runtime.notification_max_retries,
                        )

            successful_price_session_keys = {
                result.session.key
                for result in price_results
                if result.error is None
            }
            for item in current_items:
                if item.key not in pending_session_notification_keys:
                    if (
                        item_scope_filter is not None
                        and price_mode
                        and previous.get(item.key) in SOLD_OUT_STATUSES
                        and item.status in AVAILABLE_STATUSES
                        and item.key not in successful_price_session_keys
                    ):
                        continue
                    previous[item.key] = item.status
            baselined_session_targets.update(
                result.target.show_id for result in results if result.error is None
            )

            clear_resolved_notification_retries(
                notification_retries,
                previous,
                previous_price_levels,
                preserve_keys=(
                    pending_session_notification_keys
                    | pending_price_notification_keys
                ),
            )

            round_failed = (
                session_catalog_unavailable or all_prices_failed
                if price_mode
                else bool(fresh_results)
                and all(result.error is not None for result in fresh_results)
            )
            rate_limited = bool(round_rate_limits)
            round_failed = round_failed or rate_limited
            if rate_limited:
                consecutive_errors += 1
                retry_after = max(
                    (
                        result.retry_after_seconds or 0
                        for result in round_rate_limits
                    ),
                    default=0,
                ) or None
                current_interval = math.ceil(
                    calculate_rate_limit_backoff(
                        current_interval,
                        runtime.rate_limit_backoff,
                        retry_after,
                    )
                )
                affected_channels = sorted(
                    {
                        (
                            result.target.channel_label
                            if isinstance(result, TargetResult)
                            else result.session.target.channel_label
                        )
                        for result in round_rate_limits
                    }
                )
                log.warning(
                    "⏳ 检测到接口限流：%s；退避到 %ds%s",
                    "、".join(affected_channels),
                    current_interval,
                    f"（平台要求至少 {retry_after:g}s）" if retry_after else "",
                )
                state.last_error = f"接口限流: {'、'.join(affected_channels)}"
                if not rate_limit_alerted:
                    if args.dry_run:
                        log.info("🧪 静默演练：本轮原本会发送一次限流运维告警")
                    elif dispatcher.configured:
                        notify_rate_limit(
                            dispatcher,
                            affected_channels,
                            current_interval,
                        )
                    rate_limit_alerted = True
                state.active_rate_limit_channels.update(affected_channels)
            elif round_failed:
                consecutive_errors += 1
                current_interval = min(max(current_interval, 1) * 2, 300)
                state.last_error = "本轮所选库存接口全部失败"
                if consecutive_errors == 1:
                    log.warning("⚠️ 全部接口失败，重建会话并退避到 %ds", current_interval)
                    client.reset()
                if (
                    consecutive_errors >= ERROR_ALERT_THRESHOLD
                    and not error_alerted
                    and dispatcher.configured
                    and not args.dry_run
                ):
                    notify_error(dispatcher, consecutive_errors)
                    error_alerted = True
            else:
                if consecutive_errors:
                    log.info("✅ 接口恢复，间隔回到 %ds", args.interval)
                if rate_limit_alerted:
                    log.info("✅ 本轮未再触发接口限流，已退出限流退避")
                consecutive_errors = 0
                error_alerted = False
                rate_limit_alerted = False
                state.active_rate_limit_channels.clear()
                current_interval = args.interval
                state.last_success_at = datetime.now(timezone.utc).isoformat()
                state.last_error = catalog_warning

            if state_enabled:
                try:
                    save_monitor_state(state_path, state)
                except OSError as exc:
                    log.warning("⚠️ 状态文件保存失败: %s", type(exc).__name__)

            if args.once:
                return 1 if round_failed else 0
            elapsed = time.monotonic() - round_started
            jitter_offset = random.uniform(-runtime.jitter, runtime.jitter)
            if STOP_REQUESTED.wait(
                calculate_sleep_seconds(
                    current_interval,
                    elapsed,
                    jitter_offset,
                )
            ):
                log.info("👋 已停止")
                return 0
        except KeyboardInterrupt:
            log.info("👋 已停止")
            return 0
        except Exception as exc:
            log.warning("⚠️ 未处理异常: %s，%ds 后重试", exc, current_interval)
            state.last_error = f"未处理异常: {type(exc).__name__}"
            if state_enabled:
                try:
                    save_monitor_state(state_path, state)
                except OSError as state_exc:
                    log.warning("⚠️ 状态文件保存失败: %s", type(state_exc).__name__)
            if args.once:
                return 1
            elapsed = time.monotonic() - round_started
            if STOP_REQUESTED.wait(
                calculate_sleep_seconds(current_interval, elapsed)
            ):
                log.info("👋 已停止")
                return 0


if __name__ == "__main__":
    sys.exit(main())
