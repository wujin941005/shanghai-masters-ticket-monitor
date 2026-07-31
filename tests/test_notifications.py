import unittest
from dataclasses import dataclass
from unittest.mock import patch

from notifications import (
    PUBLIC_CHANNELS,
    BarkNotifier,
    NotificationDispatcher,
    NotificationEvent,
    ServerChanNotifier,
    WxPusherNotifier,
    build_dispatcher,
)


class FakeResponse:
    def __init__(self, payload, *, ok=True, status_code=200):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self._payload


class PublicDispatcherTests(unittest.TestCase):
    def test_public_distribution_supports_three_personal_channels(self):
        dispatcher = build_dispatcher(
            {
                "SERVERCHAN_SENDKEY": "SCT_key",
                "WXPUSHER_APP_TOKEN": "AT_token",
                "WXPUSHER_UID": "UID_personal",
            },
            bark_key="bark-key",
            channels=None,
            icon_url="https://example/icon.png",
        )

        self.assertEqual(PUBLIC_CHANNELS, ("bark", "serverchan", "wxpusher"))
        self.assertEqual(
            dispatcher.supported_channels,
            ("bark", "serverchan", "wxpusher"),
        )
        self.assertEqual(
            [notifier.channel for notifier in dispatcher.notifiers],
            ["bark", "serverchan", "wxpusher"],
        )
        self.assertIsInstance(dispatcher.notifiers[0], BarkNotifier)
        self.assertIsInstance(dispatcher.notifiers[1], ServerChanNotifier)
        self.assertIsInstance(dispatcher.notifiers[2], WxPusherNotifier)

    def test_requested_channel_selection_and_aliases(self):
        dispatcher = build_dispatcher(
            {
                "SERVERCHAN_SENDKEY": "SCT_key",
                "WXPUSHER_APP_TOKEN": "AT_token",
                "WXPUSHER_UID": "UID_personal",
            },
            bark_key="bark-key",
            channels="server酱,wxpush",
            icon_url="https://example/icon.png",
        )

        self.assertEqual(
            [notifier.channel for notifier in dispatcher.notifiers],
            ["serverchan", "wxpusher"],
        )

    def test_public_wxpusher_rejects_multiple_uids(self):
        with self.assertRaisesRegex(ValueError, "仅支持一个"):
            build_dispatcher(
                {
                    "WXPUSHER_APP_TOKEN": "AT_token",
                    "WXPUSHER_UID": "UID_one,UID_two",
                },
                bark_key=None,
                channels="wxpusher",
                icon_url="https://example/icon.png",
            )

    def test_unknown_channel_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "未知值"):
            build_dispatcher(
                {},
                bark_key=None,
                channels="sms",
                icon_url="https://example/icon.png",
            )

    def test_one_notifier_failure_does_not_block_the_next(self):
        @dataclass
        class StubNotifier:
            channel: str
            name: str
            result: bool | None

            def send(self, event):
                if self.result is None:
                    raise RuntimeError("boom")
                return self.result

        dispatcher = NotificationDispatcher(
            (
                StubNotifier("failed", "失败渠道", None),
                StubNotifier("working", "正常渠道", True),
            )
        )

        results = dispatcher.send(NotificationEvent("title", "body"))

        self.assertEqual(results, {"failed": False, "working": True})


class BarkSendTests(unittest.TestCase):
    def test_urgent_event_uses_time_sensitive_call_and_purchase_url(self):
        response = FakeResponse({"code": 200})
        event = NotificationEvent(
            "回流票来了",
            "半决赛 A+ 可购",
            url="https://ztmen.jussyun.com/content/example?showId=example",
            urgent=True,
        )

        with patch("notifications.requests.post", return_value=response) as mocked:
            sent = BarkNotifier("key", "https://example/icon.png").send(event)

        self.assertTrue(sent)
        self.assertEqual(mocked.call_args.args[0], "https://api.day.app/key")
        payload = mocked.call_args.kwargs["json"]
        self.assertEqual(payload["title"], event.title)
        self.assertEqual(payload["body"], event.body)
        self.assertEqual(payload["level"], "timeSensitive")
        self.assertEqual(payload["call"], "1")
        self.assertEqual(payload["url"], event.url)

    def test_api_error_is_reported(self):
        response = FakeResponse({"code": 400}, ok=True)

        with patch("notifications.requests.post", return_value=response):
            sent = BarkNotifier("key", "https://example/icon.png").send(
                NotificationEvent("title", "body")
            )

        self.assertFalse(sent)


class ServerChanSendTests(unittest.TestCase):
    def test_posts_markdown_with_purchase_url(self):
        response = FakeResponse({"code": 0})
        event = NotificationEvent(
            "回流票来了",
            "半决赛 A+ 可购",
            url="https://tickets.example/show",
        )

        with patch("notifications.requests.post", return_value=response) as mocked:
            sent = ServerChanNotifier("SCT_key").send(event)

        self.assertTrue(sent)
        self.assertEqual(
            mocked.call_args.args[0],
            "https://sctapi.ftqq.com/SCT_key.send",
        )
        self.assertEqual(mocked.call_args.kwargs["data"]["title"], event.title)
        self.assertIn(event.url, mocked.call_args.kwargs["data"]["desp"])

    def test_title_is_limited_to_official_32_character_limit(self):
        response = FakeResponse({"code": 0})
        event = NotificationEvent("很长的标题" * 10, "正文")

        with patch("notifications.requests.post", return_value=response) as mocked:
            sent = ServerChanNotifier("SCT_key").send(event)

        self.assertTrue(sent)
        self.assertEqual(len(mocked.call_args.kwargs["data"]["title"]), 32)


class WxPusherSendTests(unittest.TestCase):
    def test_posts_single_uid_markdown_and_purchase_url(self):
        response = FakeResponse(
            {
                "code": 1000,
                "success": True,
                "data": [{"uid": "UID_personal", "code": 1000}],
            }
        )
        event = NotificationEvent(
            "回流票来了",
            "半决赛 A+ 可购",
            url="https://tickets.example/show",
        )

        with patch("notifications.requests.post", return_value=response) as mocked:
            sent = WxPusherNotifier("AT_token", "UID_personal").send(event)

        self.assertTrue(sent)
        payload = mocked.call_args.kwargs["json"]
        self.assertEqual(payload["appToken"], "AT_token")
        self.assertEqual(payload["uids"], ["UID_personal"])
        self.assertEqual(payload["contentType"], 3)
        self.assertEqual(payload["url"], event.url)
        self.assertEqual(
            payload["summary"],
            "半决赛 A+ 可购",
        )
        self.assertIn(
            f"[👉 立即打开官方购票入口]({event.url})",
            payload["content"],
        )

    def test_preview_contains_body_without_newlines_and_respects_limit(self):
        response = FakeResponse(
            {
                "code": 1000,
                "success": True,
                "data": [{"uid": "UID_personal", "code": 1000}],
            }
        )
        event = NotificationEvent(
            "🎾 指定票档回流！",
            "🟢 半决赛 · A+ ¥1,580 · 当前最多可选 2 张\n"
            "🟢 决赛 · B ¥1,280 · 当前最多可选 4 张",
        )

        with patch(
            "notifications.requests.post",
            return_value=response,
        ) as mocked:
            sent = WxPusherNotifier("AT_token", "UID_personal").send(event)

        self.assertTrue(sent)
        summary = mocked.call_args.kwargs["json"]["summary"]
        self.assertIn("半决赛", summary)
        self.assertIn("A+", summary)
        self.assertNotIn("\n", summary)
        self.assertLessEqual(len(summary), 100)

    def test_recipient_failure_is_reported(self):
        response = FakeResponse(
            {
                "code": 1000,
                "success": True,
                "data": [{"uid": "UID_personal", "code": 1001}],
            }
        )

        with patch("notifications.requests.post", return_value=response):
            sent = WxPusherNotifier("AT_token", "UID_personal").send(
                NotificationEvent("title", "body")
            )

        self.assertFalse(sent)


if __name__ == "__main__":
    unittest.main()
