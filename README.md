# 🎾 2026 上海劳力士大师赛回流票监控

面向个人自用的上海大师赛回流票提醒。程序监控 **久事体育 / 莓塔甄选**与**票星球**的公开库存；某个票档从「暂无」变为「可购」时，可通过 Bark、Server酱或 WxPusher 推送场次、S/A+/A/B 等票档、票面价、平台当前最多可选张数和购票入口。

赛事地点为上海旗忠网球中心：Fan Week 为 2026 年 10 月 1–4 日，资格赛及正赛为 10 月 5–18 日。

## 监控范围

- 久事体育与票星球按各自库存独立查询。
- 莓塔甄选的大师赛购票入口跳转久事售票页，因此与久事共用库存，不会重复请求或重复推送。
- 场次目录名称由售票平台读取，但公开版的持续监控范围固定为进程启动时的
  `MATCH`；不会根据其他场次的售罄状态自动扩大。修改 `MATCH` 后需要重启进程。
- 公开个人版必须使用 `MATCH` 选择场次，单次最多监控 20 个跨平台场次入口。默认的半决赛、决赛筛选约为 14 个入口。
- `--list-sessions` 只执行一次目录查询，方便选择 `MATCH`，不会进入全场持续监控。

先查看平台当前场次，再选择自己要买的日期或轮次：

```bash
# 查看全部场次，仅用于个人选择监控目标
uv run monitor.py --list-sessions --match ''

# 只查看半决赛和决赛
uv run monitor.py --list-sessions --match '10月17日,10月18日'

# 只查看久事 / 莓塔共用库存
uv run monitor.py --list-sessions --channels juss --match ''
```

## 通知里有什么

票档级查询会读取平台公开返回的票档名称、票面价和动态可选张数。首轮只建立当前库存基线；之后仅在某个票档从不可购变为可购时推送，不会因为库存状态没变而反复提醒。

通知点击入口只使用已核验的平台链接：久事 / 莓塔默认通过 `ds.alipay.com` 打开**久事体育支付宝小程序**首页（用户在小程序内进入购票入口）；票星球默认通过 `ds.alipay.com` 打开对应活动的支付宝小程序。设置 `BUY_URL_MODE=web` 时，久事 / 莓塔改回对应的久事官方活动页、票星球改回活动网页；`BUY_URL_MODE=app` 时票星球使用官方原生 App Scheme。旧配置名 `BARK_BUY_URL_MODE` 仍兼容。

Bark 点击系统通知会直接尝试打开购票入口。WxPusher 的 `url` 字段作为原文
链接发送，同时在 Markdown 正文末尾显示“立即打开官方购票入口”；即使某个
客户端点击系统通知时先进入消息详情，也可以从正文继续一键打开。

项目不再自行拼接微信明文 URL Scheme。微信要求小程序所有者先在后台声明允许明文 Scheme；久事和票星球没有为这些路由开放该能力，自行拼出的链接会显示“无法访问”。

`canBuyCount` 只能解释为“平台当前最多可选几张”。它同时受到实时余票、单笔限购和配票规则影响，**不等于全场剩余总票数**。

当前公开响应不提供可靠的看台分区、排号或座位号，因此通知可以告诉你 S、A+、A、B 等票档，但不能在下单前给出具体座位。

```bash
# 查看半决赛、决赛当前票档、票价和可购状态
uv run monitor.py --list-price-levels --match '10月17日,10月18日'

# 只监控指定完整票档名
uv run monitor.py --match '10月17日,10月18日' --grades 'S,A+,A,B'
```

## 刷新与可靠性

- 库存默认每 15 秒刷新，可通过 `MONITOR_INTERVAL` 或 `--interval` 调整，最低 5 秒。
- 场次目录每 300 秒重新发现一次；相邻公开请求固定留出间隔，并带随机抖动。
- 同一票档默认有 300 秒通知冷却，避免库存信号抖动造成重复提醒。
- 所有已选通知渠道均失败时保留回流事件，最多重试 5 次；任一渠道成功送达后才确认事件。
- 显式识别 HTTP 429、久事 HTTP 403 风控和业务层“访问频繁”，遵守 `Retry-After`，最低退避 60 秒。
- 状态固定保存在被 Git 忽略的 `monitor-state.json`，重启后继续沿用库存边沿和通知冷却。
- `--dry-run` 使用纯内存状态，真实查询和判断，但绝不发送通知。

## 安装

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)：

```bash
cd shanghai-masters-ticket-monitor
uv sync
cp .env.example .env
```

### 通知渠道怎么选

- **iPhone / iPad 首选 Bark**：配置最简单，系统通知显示直接，点击通知可立即尝试打开对应购票入口。
- **Android / HarmonyOS / 电脑首选 WxPusher**：适合跨平台接收，创建个人应用后填写自己的 AppToken 和一个 UID。
- **Server酱作为备用渠道**：适合已经在使用 Server酱的用户；免费额度较少，不建议作为可能连续出现多条回流提醒时的唯一渠道。

选择后编辑 `.env`。推荐只配置一种主渠道，需要冗余时再同时配置多种。下面默认以 iPhone / iPad 的 Bark 为例，Android 或电脑用户把 `NOTIFY_CHANNELS` 改为 `wxpusher`，并填写 WxPusher 两项配置：

```env
# iPhone / iPad 推荐 bark；Android / HarmonyOS / 电脑推荐 wxpusher。
NOTIFY_CHANNELS=bark

# Bark：iPhone / iPad 首选，Mac 也可使用。
BARK_KEY=https://api.day.app/your_key_here

# Android / HarmonyOS / 电脑用户改用以下配置：
# NOTIFY_CHANNELS=wxpusher
# WXPUSHER_APP_TOKEN=AT_xxxxxxxxx
# WXPUSHER_UID=UID_xxxxxxxxx

# 可选备用：在 Server酱后台复制个人 SendKey。
# SERVERCHAN_SENDKEY=SCTxxxxxxxx

MONITOR_INTERVAL=15
MATCH=10月17日,10月18日
# BUY_URL_MODE=app  # 可选：票星球原生 App；默认 alipay
# BUY_URL_MODE=web  # 可选：久事/莓塔回网页，票星球用活动网页
# GRADES=S,A+,A,B
```

至少配置一个通知渠道即可：

| 渠道 | 适合设备 | 个人配置 |
| --- | --- | --- |
| Bark（iOS 首选） | iPhone、iPad，Mac 也可使用 | 安装 Bark 后复制设备 Key；使用官方 POST JSON，点击链接支持 URL Scheme 和 Universal Link |
| WxPusher（Android / 电脑首选） | Android、HarmonyOS、Windows、macOS、Linux，iOS 也可使用 | 创建标准应用，获取 AppToken，并填写自己的一个 UID；正文和原文链接均带购票入口 |
| Server酱（备用） | 微信、Android 客户端及其后台可选通道 | 微信扫码登录后复制 SendKey；标题按官方上限截为 32 字，免费用户目前每天最多 5 条 |

如果 WxPusher 的折叠通知只显示标题，请在手机系统的 WxPusher 通知设置中开启内容预览、锁屏显示和横幅/悬浮通知；完整票档信息和购票链接仍可在消息详情中查看。

`NOTIFY_CHANNELS` 可写一个或多个渠道，例如只使用 Server酱：

```env
NOTIFY_CHANNELS=serverchan
SERVERCHAN_SENDKEY=SCTxxxxxxxx
```

公开版的 WxPusher 是个人模式，只接受一个 `WXPUSHER_UID`，不支持批量 UID、Topic、订单回调或用户数据库。先在 WxPusher 创建标准应用并关注该应用，再从 WxPusher 获取自己的 UID。需要多人运营或付费订单管理时，请自行设计权限、订阅期限与隐私保护，不要把个人配置直接公开。

所有 Key、AppToken 和 UID 只保存在被 Git 忽略的 `.env` 中，不要提交、截图公开或跨项目共享。

## 使用

```bash
# 静默演练
uv run monitor.py --dry-run

# 检查一次
uv run monitor.py --once

# 持续监控
uv run monitor.py

# 临时改为 10 秒刷新
uv run monitor.py --interval 10

# 只监控票星球
uv run monitor.py --channels piaoxingqiu

# 查看三种通知渠道是否已配置，不显示密钥
uv run monitor.py --list-notifiers

# 向所有已选通知渠道分别发送久事、票星球入口测试消息，不查询库存接口
uv run monitor.py --test-notification

# 临时只测试 Server酱
uv run monitor.py --notify-channels serverchan --test-notification
```

公开个人版只内置 Bark、Server酱、单 UID WxPusher 和直连网络访问，不包含群机器人、代理、多实例状态、批量 UID、Topic 或订单运营配置。

## 公开版结构与边界

公开发布包刻意保持为一个可直接审计的个人工具：

| 文件 | 职责 |
| --- | --- |
| `monitor.py` | 场次发现、票档解析、状态跃迁、限流退避和命令行入口 |
| `notifications.py` | Bark、Server酱和单 UID WxPusher 个人通知 |
| `tests/` | 库存逻辑、通知格式以及公开发布边界的自动测试 |

公开版不读取隐藏目录或外部私有模块，不包含代理池、订单数据库、批量用户、群机器人、动态售罄范围或自动下单能力。`.env`、运行日志、状态文件、SQLite 文件及 `.private/` 都由 `.gitignore` 排除；发布前的边界测试也会检查这些规则以及示例配置中的密钥泄漏。

## 后台运行

```bash
cd shanghai-masters-ticket-monitor
tmux new-session -d -s masters-ticket "uv run monitor.py"

tail -f monitor.log
tmux attach -t masters-ticket
tmux kill-session -t masters-ticket
```

## 测试

```bash
uv run python -m unittest discover -s tests -v
```

测试覆盖公开响应解析、库存状态跃迁、首轮静默、跨重启连续性、损坏状态降级、推送失败重试、分平台送达确认、个人监控范围限制、票档过滤、Bark / Server酱 / WxPusher 消息格式和限流退避。

## 非官方声明与使用边界

本项目由个人独立开发，与 Rolex、上海劳力士大师赛、久事体育、莓塔甄选、票星球及其关联方不存在隶属、合作、授权、赞助或背书关系。相关名称、商标和活动标识归各自权利人所有，仅用于说明兼容的平台和赛事。

售票页面、公开接口和字段可能随时调整、限流或停止提供，项目不保证持续可用。使用者应遵守适用法律、售票平台协议和购票规则，合理设置刷新频率，不得利用本项目绕过访问控制、实施自动下单或干扰平台正常服务。

## 信息来源

- [上海劳力士大师赛官方票务页](https://en.rolexshanghaimasters.com/en/tickets/tickets)
- [微信官方 URL Scheme 规范与明文 Scheme 开通条件](https://developers.weixin.qq.com/miniprogram/dev/framework/open-ability/url-scheme.html)
- [Bark 官方 POST JSON、`url` 与通知参数](https://github.com/Finb/Bark/blob/master/docs/tutorial.md)
- [Server酱 Turbo 发送 API、标题限制与个人额度](https://sct.ftqq.com/sendkey)
- [WxPusher 标准推送与 UID 文档](https://wxpusher.zjiecode.com/docs/#/?id=send-msg)
- [2026 开票公告（上观新闻转载）](https://sports.sina.cn/2026-07-28/detail-inikizzz2453581.d.html?vt=4)
- [莓塔甄选公开介绍与官方合作售票案例](https://cn.chinadaily.com.cn/a/202607/10/WS6a508bf7a310d709c2fbcd04.html)

本项目只读取公开票务状态，不登录账号、不锁座、不自动下单。

## License

本项目采用 [Shanghai Masters Ticket Monitor Non-Commercial License 1.0](LICENSE)：源码公开，可用于个人、教育、研究等非商业用途，也可在保留版权与许可声明的前提下修改和分发。

未经版权所有者另行书面授权，**禁止任何直接或间接商业使用**，包括出售软件或访问权、收费部署或托管、收费监控/通知/代抢服务、订阅或会员服务、商业转售，以及集成到商业产品或服务中。

需要商业授权请联系版权所有者。由于限制商业用途，本协议属于源码可用（source-available）协议，并非 OSI 定义的开源许可证。
