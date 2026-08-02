"""Personal notification adapters shipped with the public monitor."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import quote

import requests

log = logging.getLogger("shanghai-masters")
PUBLIC_CHANNELS = ("bark", "serverchan", "wxpusher")
PUBLIC_CHANNEL_ALIASES = {
    "server酱": "serverchan",
    "server_chan": "serverchan",
    "wxpush": "wxpusher",
    "微信推送": "wxpusher",
}
SERVERCHAN_ENDPOINT = "https://sctapi.ftqq.com"
WXPUSHER_ENDPOINT = "https://wxpusher.zjiecode.com/api/send/message"


@dataclass(frozen=True)
class NotificationEvent:
    title: str
    body: str
    url: str | None = None
    urgent: bool = False
    audience: str = "subscriber"
    preview: str | None = None


class Notifier(Protocol):
    name: str
    channel: str

    def send(self, event: NotificationEvent) -> bool: ...


def wxpusher_markdown_content(event: NotificationEvent) -> str:
    content = (
        f"## {event.title}\n\n"
        f"{event.body.replace(chr(10), chr(10) + chr(10))}"
    )
    if event.url:
        content += f"\n\n[👉 立即打开官方购票入口]({event.url})"
    return content


def wxpusher_summary(event: NotificationEvent, *, limit: int = 100) -> str:
    """Build the plain-text preview shown by WxPusher system notifications."""
    body_preview = "；".join(
        line.strip()
        for line in event.body.splitlines()
        if line.strip()
    )
    # WxPusher documents that some clients may show only about 20 characters.
    # Its client already adds a generic subscription title, so put actionable
    # ticket details first instead of repeating our event title.
    preview = (event.preview or body_preview or event.title).strip()
    return " ".join(preview.split())[:limit]


def _response_json(response: requests.Response) -> dict[str, Any] | None:
    try:
        value = response.json()
    except (ValueError, requests.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _finish_response(
    channel_name: str,
    response: requests.Response,
    *,
    code_fields: tuple[str, ...],
) -> bool:
    if not response.ok:
        log.warning("⚠️ %s 推送异常: HTTP %s", channel_name, response.status_code)
        return False
    payload = _response_json(response)
    if payload is None:
        log.warning("⚠️ %s 推送返回了无法解析的响应", channel_name)
        return False
    response_code_found = False
    for field in code_fields:
        if field not in payload:
            continue
        response_code_found = True
        if payload[field] not in (0, 200, "0", "200"):
            log.warning("⚠️ %s 推送返回失败码: %s", channel_name, payload[field])
            return False
    if not response_code_found:
        log.warning("⚠️ %s 推送响应缺少状态码", channel_name)
        return False
    log.info("✅ %s 已推送", channel_name)
    return True


@dataclass(frozen=True)
class BarkNotifier:
    key: str
    icon_url: str
    group: str = "上海大师赛票务"
    timeout: float = 10.0

    name = "Bark"
    channel = "bark"

    def send(self, event: NotificationEvent) -> bool:
        endpoint = (
            self.key.rstrip("/")
            if self.key.startswith("http")
            else f"https://api.day.app/{self.key}"
        )
        payload = {
            "title": event.title,
            "body": event.body,
            "group": self.group,
            "level": "timeSensitive" if event.urgent else "active",
            "icon": self.icon_url,
        }
        if event.url:
            payload["url"] = event.url
        if event.urgent:
            payload["call"] = "1"
        response = requests.post(
            endpoint,
            json=payload,
            timeout=self.timeout,
        )
        return _finish_response(self.name, response, code_fields=("code",))


@dataclass(frozen=True)
class ServerChanNotifier:
    send_key: str
    timeout: float = 10.0

    name = "Server酱"
    channel = "serverchan"

    def send(self, event: NotificationEvent) -> bool:
        endpoint = (
            self.send_key
            if self.send_key.startswith("http")
            else f"{SERVERCHAN_ENDPOINT}/{quote(self.send_key, safe='')}.send"
        )
        description = event.body.replace("\n", "\n\n")
        if event.url:
            description += f"\n\n[立即查看购票入口]({event.url})"
        response = requests.post(
            endpoint,
            data={"title": event.title[:32], "desp": description},
            timeout=self.timeout,
        )
        return _finish_response(self.name, response, code_fields=("code",))


@dataclass(frozen=True)
class WxPusherNotifier:
    app_token: str
    uid: str
    timeout: float = 10.0

    name = "WxPusher"
    channel = "wxpusher"

    def send(self, event: NotificationEvent) -> bool:
        payload: dict[str, Any] = {
            "appToken": self.app_token,
            "content": wxpusher_markdown_content(event),
            "summary": wxpusher_summary(event),
            "contentType": 3,
            "uids": [self.uid],
            "verifyPayType": 0,
        }
        if event.url:
            payload["url"] = event.url
        response = requests.post(
            WXPUSHER_ENDPOINT,
            json=payload,
            timeout=self.timeout,
        )
        if not response.ok:
            log.warning("⚠️ %s 推送异常: HTTP %s", self.name, response.status_code)
            return False
        result = _response_json(response)
        if (
            result is None
            or result.get("code") != 1000
            or result.get("success") is False
        ):
            code = result.get("code") if result is not None else None
            log.warning("⚠️ %s 推送返回失败码: %s", self.name, code)
            return False
        records = result.get("data")
        if isinstance(records, list) and any(
            isinstance(record, dict) and record.get("code") != 1000
            for record in records
        ):
            log.warning("⚠️ %s 未能为当前 UID 创建发送任务", self.name)
            return False
        log.info("✅ %s 已推送", self.name)
        return True


@dataclass(frozen=True)
class NotificationDispatcher:
    notifiers: tuple[Notifier, ...]
    supported_channels: tuple[str, ...] = PUBLIC_CHANNELS

    @property
    def configured(self) -> bool:
        return bool(self.notifiers)

    @property
    def summary(self) -> str:
        return "/".join(notifier.name for notifier in self.notifiers) or "未配置"

    def send(self, event: NotificationEvent) -> dict[str, bool]:
        if not self.notifiers:
            return {}

        def send_one(notifier: Notifier) -> tuple[bool, float]:
            started = time.monotonic()
            try:
                return notifier.send(event), time.monotonic() - started
            except Exception as exc:
                # requests exceptions may contain secret URLs, so log only the type.
                log.warning("⚠️ %s 推送失败: %s", notifier.name, type(exc).__name__)
                return False, time.monotonic() - started

        started = time.monotonic()
        completed: dict[str, tuple[bool, float]] = {}
        with ThreadPoolExecutor(
            max_workers=len(self.notifiers),
            thread_name_prefix="masters-notify",
        ) as executor:
            futures = {
                executor.submit(send_one, notifier): notifier
                for notifier in self.notifiers
            }
            for future in as_completed(futures):
                notifier = futures[future]
                completed[notifier.channel] = future.result()

        if len(self.notifiers) > 1:
            timings = "、".join(
                f"{notifier.name} {completed[notifier.channel][1]:.2f}s"
                for notifier in self.notifiers
            )
            log.info(
                "⚡ 通知通道并发完成 %.2fs（%s）",
                time.monotonic() - started,
                timings,
            )
        return {
            notifier.channel: completed[notifier.channel][0]
            for notifier in self.notifiers
        }


def parse_notification_channels(
    value: str | None,
    *,
    supported_channels: tuple[str, ...] = PUBLIC_CHANNELS,
    aliases: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    if not value:
        return ()
    normalized_aliases = aliases or {}
    channels: list[str] = []
    for raw in value.split(","):
        raw_channel = raw.strip().lower()
        channel = normalized_aliases.get(raw_channel, raw_channel)
        if channel and channel not in channels:
            channels.append(channel)
    unknown = sorted(set(channels) - set(supported_channels))
    if unknown:
        raise ValueError(
            f"通知渠道仅支持 {','.join(supported_channels)}；未知值: "
            + ", ".join(unknown)
        )
    return tuple(channels)


def parse_personal_wxpusher_uid(value: str | None) -> str | None:
    if not value:
        return None
    uid = value.strip()
    if any(separator in uid for separator in (",", "，", ";", "；", " ")):
        raise ValueError("公开个人版 WxPusher 仅支持一个 WXPUSHER_UID")
    if not uid.startswith("UID_"):
        raise ValueError("WXPUSHER_UID 应以 UID_ 开头")
    return uid


def build_dispatcher(
    env: Mapping[str, str],
    *,
    bark_key: str | None,
    channels: str | None,
    icon_url: str,
) -> NotificationDispatcher:
    channels = channels if channels is not None else env.get("NOTIFY_CHANNELS")
    serverchan_key = (
        env.get("SERVERCHAN_SENDKEY")
        or env.get("SERVERCHAN_SEND_KEY")
        or env.get("SERVERCHAN_KEY")
    )
    wxpusher_token = env.get("WXPUSHER_APP_TOKEN")
    wxpusher_uid = parse_personal_wxpusher_uid(env.get("WXPUSHER_UID"))
    candidates: dict[str, Notifier | None] = {
        "bark": BarkNotifier(bark_key, icon_url) if bark_key else None,
        "serverchan": ServerChanNotifier(serverchan_key) if serverchan_key else None,
        "wxpusher": (
            WxPusherNotifier(wxpusher_token, wxpusher_uid)
            if wxpusher_token and wxpusher_uid
            else None
        ),
    }
    requested = set(
        parse_notification_channels(
            channels,
            supported_channels=PUBLIC_CHANNELS,
            aliases=PUBLIC_CHANNEL_ALIASES,
        )
    )
    if requested:
        missing = [channel for channel in requested if candidates[channel] is None]
        if missing:
            log.warning("⚠️ 已选择但未配置的通知渠道: %s", ", ".join(sorted(missing)))
    selected = tuple(
        notifier
        for channel in PUBLIC_CHANNELS
        if not requested or channel in requested
        if (notifier := candidates[channel]) is not None
    )
    return NotificationDispatcher(selected, PUBLIC_CHANNELS)
