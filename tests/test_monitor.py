import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from monitor import (
    AVAILABLE_STATUSES,
    HttpClient,
    InventoryItem,
    JUSS_ALIPAY_APP_ID,
    JUSS_ALIPAY_PATH,
    MonitorState,
    PXQ_ALIPAY_APP_ID,
    PXQ_ALIPAY_PATH,
    PriceLevelItem,
    PriceLevelResult,
    RateLimitError,
    RuntimeOptions,
    STOP_REQUESTED,
    StreamingPriceResultProcessor,
    TARGETS,
    TargetResult,
    build_http_client,
    build_juss_alipay_url,
    build_other_channel_context,
    build_juss_web_url,
    build_parser,
    build_piaoxingqiu_alipay_url,
    build_piaoxingqiu_app_url,
    build_targets,
    cached_target_results,
    calculate_rate_limit_backoff,
    calculate_sleep_seconds,
    check_all_price_levels,
    confirmed_available_price_levels,
    clear_resolved_notification_retries,
    filter_items,
    filter_notification_cooldown,
    filter_price_levels,
    find_newly_available,
    find_newly_available_price_levels,
    format_price_level_rows,
    format_session_rows,
    mark_notified,
    load_monitor_state,
    monitor_state_from_dict,
    monitor_state_to_dict,
    notify_price_levels_available,
    parse_price_levels,
    parse_retry_after,
    parse_sessions,
    record_notification_outcome,
    request_stop,
    select_targets,
    save_monitor_state,
    select_other_channel_context_sessions,
    session_comparison_key,
    session_catalog_refresh_due,
    session_scope_is_allowed,
    update_session_cache,
    update_confirmed_price_level_state,
    update_price_level_streaks,
    update_price_level_state,
)


class HttpClientTests(unittest.TestCase):
    def test_public_session_is_direct_and_ignores_environment(self):
        session = HttpClient._new_session()

        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies, {})

    def test_public_builder_uses_direct_client(self):
        client, summary = build_http_client({})

        self.assertIsInstance(client, HttpClient)
        self.assertEqual(type(client), HttpClient)
        self.assertEqual(summary, "直连")

    def test_reset_closes_replaced_session(self):
        class Session:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        previous_session = Session()
        replacement_session = Session()
        client = HttpClient.__new__(HttpClient)
        client.session = previous_session

        with patch.object(
            HttpClient,
            "_new_session",
            return_value=replacement_session,
        ):
            client.reset()

        self.assertTrue(previous_session.closed)
        self.assertIs(client.session, replacement_session)

    def test_http_429_exposes_retry_after(self):
        class Response:
            status_code = 429
            headers = {"Retry-After": "45"}

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        client = HttpClient()
        client.session = Session()

        with self.assertRaises(RateLimitError) as context:
            client.get_json("https://example.test", headers={}, params={})

        self.assertEqual(context.exception.retry_after_seconds, 45)

    def test_http_403_is_treated_as_platform_rate_limit(self):
        class Response:
            status_code = 403
            headers = {}

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        client = HttpClient()
        client.session = Session()

        with self.assertRaises(RateLimitError) as context:
            client.get_json("https://example.test", headers={}, params={})

        self.assertEqual(context.exception.status_code, 403)
        self.assertIsNone(context.exception.retry_after_seconds)

    def test_http_469_is_treated_as_piaoxingqiu_rate_limit(self):
        class Response:
            status_code = 469
            headers = {}

            def json(self):
                return {"message": "risk control"}

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        client = HttpClient()
        client.session = Session()

        with self.assertRaises(RateLimitError) as context:
            client.get_json("https://example.test", headers={}, params={})

        self.assertEqual(context.exception.status_code, 469)
        self.assertIsNone(context.exception.retry_after_seconds)

    def test_success_http_with_rate_limit_payload_is_detected(self):
        class Response:
            status_code = 200
            headers = {}

            def json(self):
                return {"statusCode": 429, "comments": "请求频繁，请稍后再试"}

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        client = HttpClient()
        client.session = Session()

        with self.assertRaises(RateLimitError):
            client.get_json("https://example.test", headers={}, params={})

    def test_retry_after_seconds_parser(self):
        self.assertEqual(parse_retry_after("120"), 120)
        self.assertIsNone(parse_retry_after("not-a-date"))


class RefreshSchedulingTests(unittest.TestCase):
    def test_fixed_cycle_subtracts_request_time(self):
        self.assertEqual(calculate_sleep_seconds(30, 6), 24)

    def test_jitter_and_slow_round_never_produce_negative_sleep(self):
        self.assertEqual(calculate_sleep_seconds(30, 5, 2), 27)
        self.assertEqual(calculate_sleep_seconds(30, 35, -2), 0)

    def test_partial_catalog_cache_waits_for_catalog_refresh_interval(self):
        self.assertFalse(
            session_catalog_refresh_due(
                price_mode=True,
                has_cached_results=True,
                last_refresh_at=100,
                now=105,
                refresh_interval=300,
            )
        )
        self.assertTrue(
            session_catalog_refresh_due(
                price_mode=True,
                has_cached_results=True,
                last_refresh_at=100,
                now=400,
                refresh_interval=300,
            )
        )

    def test_failed_catalog_refresh_preserves_last_successful_cache(self):
        session = InventoryItem(TARGETS[0], "s1", "半决赛", "ON_SALE")
        successful = TargetResult(TARGETS[0], [session])
        failed = TargetResult(TARGETS[0], [], error="timeout")
        cache = {}

        update_session_cache(cache, [successful])
        update_session_cache(cache, [failed])

        self.assertEqual(cached_target_results(cache, [TARGETS[0]]), [successful])

    def test_stop_signal_sets_cooperative_shutdown_flag(self):
        STOP_REQUESTED.clear()
        try:
            request_stop(2, None)
            self.assertTrue(STOP_REQUESTED.is_set())
        finally:
            STOP_REQUESTED.clear()

    def test_rate_limit_backoff_honors_minimum_and_retry_after(self):
        self.assertEqual(calculate_rate_limit_backoff(15, 60, None), 60)
        self.assertEqual(calculate_rate_limit_backoff(60, 60, 180), 180)
        self.assertEqual(calculate_rate_limit_backoff(1000, 60, 3600), 1800)

    def test_rate_limit_stops_remaining_requests_for_same_channel(self):
        sessions = [
            InventoryItem(TARGETS[0], "j1", "久事1", "ON_SALE"),
            InventoryItem(TARGETS[0], "j2", "久事2", "ON_SALE"),
            InventoryItem(TARGETS[5], "p1", "票星球1", "ON_SALE"),
        ]
        results_to_return = [
            PriceLevelResult(sessions[0], [], "HTTP 限流", True, 60),
            PriceLevelResult(sessions[2], []),
        ]

        with patch("monitor.check_price_level", side_effect=results_to_return) as mocked:
            results = check_all_price_levels(object(), sessions)

        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(
            [result.session.target.channel for result in results],
            ["juss", "piaoxingqiu"],
        )

    def test_catalog_rate_limit_blocks_same_channel_price_requests(self):
        client = object()
        sessions = [
            InventoryItem(TARGETS[0], "j1", "久事1", "ON_SALE"),
            InventoryItem(TARGETS[5], "p1", "票星球1", "ON_SALE"),
        ]

        with patch(
            "monitor.check_price_level",
            return_value=PriceLevelResult(sessions[1], []),
        ) as mocked:
            results = check_all_price_levels(
                client,
                sessions,
                blocked_channels={"juss"},
            )

        mocked.assert_called_once_with(client, sessions[1])
        self.assertEqual(
            [result.session.target.channel for result in results],
            ["piaoxingqiu"],
        )

    def test_price_result_callback_runs_before_next_sequential_request(self):
        sessions = [
            InventoryItem(TARGETS[0], "j1", "久事1", "ON_SALE"),
            InventoryItem(TARGETS[0], "j2", "久事2", "ON_SALE"),
        ]
        timeline = []

        def check(_client, session):
            timeline.append(f"query:{session.session_id}")
            return PriceLevelResult(session, [])

        with patch("monitor.check_price_level", side_effect=check):
            check_all_price_levels(
                object(),
                sessions,
                result_callback=lambda result: timeline.append(
                    f"callback:{result.session.session_id}"
                ),
            )

        self.assertEqual(
            timeline,
            ["query:j1", "callback:j1", "query:j2", "callback:j2"],
        )


class MonitorStateTests(unittest.TestCase):
    def setUp(self):
        self.session = InventoryItem(TARGETS[0], "s1", "半决赛", "ON_SALE")
        self.level = PriceLevelItem(
            self.session, "plan-b", "B", 960.0, 4, sale_started=True
        )

    def test_state_round_trip_is_atomic_and_preserves_restart_edge(self):
        state = MonitorState.empty()
        state.session_statuses[self.session.key] = "LACK_OF_TICKET"
        state.baselined_session_targets.add(self.session.target.show_id)
        state.price_level_availability[self.level.key] = False
        state.price_level_streaks[self.level.key] = -1
        state.baselined_price_sessions.add(self.session.key)
        state.last_notified_at[self.level.key] = 123.5
        state.active_rate_limit_channels.add("久事体育 / 莓塔甄选")
        state.poll_count = 8
        state.alert_count = 2

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor-state.json"
            save_monitor_state(path, state)
            loaded, restored = load_monitor_state(path)

            self.assertTrue(restored)
            self.assertEqual(loaded.poll_count, 8)
            self.assertEqual(loaded.alert_count, 2)
            self.assertEqual(loaded.last_notified_at[self.level.key], 123.5)
            self.assertEqual(
                loaded.active_rate_limit_channels,
                {"久事体育 / 莓塔甄选"},
            )
            self.assertFalse(loaded.price_level_availability[self.level.key])
            self.assertFalse(list(path.parent.glob(".*.tmp")))

            newly = find_newly_available_price_levels(
                loaded.price_level_availability,
                [self.level],
                initial_round=self.session.key
                not in loaded.baselined_price_sessions,
                notify_initial=False,
            )
            self.assertEqual(newly, [self.level])

    def test_legacy_rate_limit_health_suppresses_duplicate_after_upgrade(self):
        state = MonitorState.empty()
        state.last_error = "接口限流: 久事体育 / 莓塔甄选"
        payload = monitor_state_to_dict(state)
        payload["health"].pop("active_rate_limit_channels")

        loaded = monitor_state_from_dict(payload)

        self.assertEqual(
            loaded.active_rate_limit_channels,
            {"久事体育 / 莓塔甄选"},
        )

    def test_corrupt_state_falls_back_to_silent_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor-state.json"
            path.write_text("{broken", encoding="utf-8")

            loaded, restored = load_monitor_state(path)

        self.assertFalse(restored)
        self.assertEqual(loaded.session_statuses, {})
        self.assertEqual(
            find_newly_available_price_levels(
                loaded.price_level_availability,
                [self.level],
                initial_round=True,
                notify_initial=False,
            ),
            [],
        )

    def test_notification_failure_retries_five_times_then_absorbs_event(self):
        retries = {}
        for expected_attempt in range(1, 5):
            pending, exhausted = record_notification_outcome(
                [self.level], [], retries, max_attempts=5
            )
            self.assertEqual(pending, {self.level.key})
            self.assertEqual(exhausted, set())
            self.assertEqual(retries[self.level.key].attempts, expected_attempt)

        pending, exhausted = record_notification_outcome(
            [self.level], [], retries, max_attempts=5
        )
        self.assertEqual(pending, set())
        self.assertEqual(exhausted, {self.level.key})
        self.assertNotIn(self.level.key, retries)

    def test_volatile_buyable_count_does_not_reset_retry_budget(self):
        retries = {}
        record_notification_outcome([self.level], [], retries, max_attempts=5)
        changed = PriceLevelItem(
            self.session, "plan-b", "B", 960.0, 2, sale_started=True
        )

        pending, exhausted = record_notification_outcome(
            [changed], [], retries, max_attempts=5
        )

        self.assertEqual(pending, {changed.key})
        self.assertEqual(exhausted, set())
        self.assertEqual(retries[changed.key].attempts, 2)

    def test_material_event_change_resets_retry_budget(self):
        retries = {}
        record_notification_outcome([self.level], [], retries, max_attempts=5)
        changed = PriceLevelItem(
            self.session, "plan-b", "B 调整票", 980.0, 2, sale_started=True
        )

        pending, exhausted = record_notification_outcome(
            [changed], [], retries, max_attempts=5
        )

        self.assertEqual(pending, {changed.key})
        self.assertEqual(exhausted, set())
        self.assertEqual(retries[changed.key].attempts, 1)

    def test_successful_delivery_clears_pending_retry(self):
        retries = {}
        record_notification_outcome([self.level], [], retries, max_attempts=5)

        pending, exhausted = record_notification_outcome(
            [self.level], [self.level.key], retries, max_attempts=5
        )

        self.assertEqual((pending, exhausted), (set(), set()))
        self.assertEqual(retries, {})

    def test_sold_out_state_clears_old_retry_budget(self):
        retries = {}
        record_notification_outcome([self.level], [], retries, max_attempts=5)

        clear_resolved_notification_retries(
            retries,
            {},
            {self.level.key: False},
        )

        self.assertEqual(retries, {})

    def test_pending_available_event_is_not_mistaken_for_resolved_state(self):
        retries = {}
        record_notification_outcome([self.level], [], retries, max_attempts=5)

        clear_resolved_notification_retries(
            retries,
            {},
            {self.level.key: False},
            preserve_keys={self.level.key},
        )

        self.assertEqual(retries[self.level.key].attempts, 1)


class NotificationGuardTests(unittest.TestCase):
    def setUp(self):
        session = InventoryItem(TARGETS[0], "s1", "半决赛", "ON_SALE")
        self.level = PriceLevelItem(session, "p1", "B", 960, 4)

    def test_same_grade_is_suppressed_during_cooldown(self):
        notified = {}
        mark_notified([self.level], notified, now=100)

        self.assertEqual(
            filter_notification_cooldown(
                [self.level], notified, now=399, cooldown=300
            ),
            [],
        )
        self.assertEqual(
            filter_notification_cooldown(
                [self.level], notified, now=400, cooldown=300
            ),
            [self.level],
        )

    def test_zero_cooldown_disables_suppression(self):
        self.assertEqual(
            filter_notification_cooldown(
                [self.level], {self.level.key: 100}, now=101, cooldown=0
            ),
            [self.level],
        )

    def test_dry_run_flag_is_available(self):
        args = build_parser().parse_args(["--dry-run"])
        self.assertTrue(args.dry_run)

    def test_default_is_immediate_single_round_confirmation(self):
        self.assertEqual(RuntimeOptions().availability_confirmations, 1)

    def test_public_cli_exposes_only_personal_controls(self):
        options = set(build_parser()._option_string_actions)
        self.assertEqual(
            options,
            {
                "-h",
                "--help",
                "--bark",
                "-b",
                "--notify-channels",
                "--interval",
                "-i",
                "--channels",
                "--match",
                "--grades",
                "--dry-run",
                "--verbose",
                "-v",
                "--list-sessions",
                "--list-price-levels",
                "--list-notifiers",
                "--test-notification",
                "--once",
            },
        )

    def test_price_notification_includes_api_buyable_count(self):
        class Dispatcher:
            def __init__(self):
                self.events = []

            def send(self, event):
                self.events.append(event)
                return {"test": True}

        dispatcher = Dispatcher()
        self.assertTrue(notify_price_levels_available(dispatcher, [self.level]))
        self.assertIn("当前最多可选 4 张", dispatcher.events[0].body)
        self.assertIn("受余票、单笔限购和配票规则共同影响", dispatcher.events[0].body)
        self.assertIn("不等于剩余总票数", dispatcher.events[0].body)
        self.assertEqual(
            dispatcher.events[0].preview,
            "半决赛 · B ¥960 · 最多可选 4 张",
        )

    def test_stream_processor_notifies_and_commits_before_batch_end(self):
        timeline = []

        class Dispatcher:
            configured = True

            def send(self, event):
                timeline.append(event)
                return {"test": True}

        state = MonitorState.empty()
        previous = {self.level.key: False}
        streaks = {self.level.key: -1}
        baselined = {self.level.session.key}
        processor = StreamingPriceResultProcessor(
            client=object(),
            dispatcher=Dispatcher(),
            state=state,
            runtime=RuntimeOptions(notification_cooldown=0),
            catalog_items=[self.level.session],
            previous_price_levels=previous,
            price_level_streaks=streaks,
            baselined_price_sessions=baselined,
            last_notified_at={},
            notification_retries={},
            round_rate_limits=[],
            round_started=0,
            request_gap_seconds=0,
            grades=None,
            dry_run=False,
        )

        processor.handle(PriceLevelResult(self.level.session, [self.level]))

        self.assertEqual(len(timeline), 1)
        self.assertTrue(previous[self.level.key])
        self.assertEqual(state.alert_count, 1)
        self.assertEqual(processor.pending_notification_keys, set())

    def test_notification_acknowledges_only_successful_ticket_platform_group(self):
        other_session = InventoryItem(TARGETS[5], "s2", "决赛", "ON_SALE")
        other_level = PriceLevelItem(other_session, "p2", "A", 1280, 2)

        class Dispatcher:
            def __init__(self):
                self.call_count = 0

            def send(self, _event):
                self.call_count += 1
                return {"test": self.call_count == 1}

        delivered = notify_price_levels_available(
            Dispatcher(), [self.level, other_level]
        )

        self.assertEqual(delivered, {self.level.key})

    def test_piaoxingqiu_products_get_separate_click_targets(self):
        first_session = InventoryItem(TARGETS[5], "s1", "半决赛", "ON_SALE")
        second_session = InventoryItem(TARGETS[6], "s2", "决赛", "ON_SALE")
        levels = [
            PriceLevelItem(first_session, "p1", "A+", 1680, 2),
            PriceLevelItem(second_session, "p2", "A", 1280, 2),
        ]

        class Dispatcher:
            def __init__(self):
                self.events = []

            def send(self, event):
                self.events.append(event)
                return {"test": True}

        dispatcher = Dispatcher()
        delivered = notify_price_levels_available(dispatcher, levels)

        self.assertEqual(delivered, {level.key for level in levels})
        self.assertEqual(len(dispatcher.events), 2)
        self.assertEqual(
            {event.url for event in dispatcher.events},
            {TARGETS[5].buy_url, TARGETS[6].buy_url},
        )

    def test_notification_mentions_existing_stock_on_other_channel(self):
        juss_session = InventoryItem(
            TARGETS[0], "juss-night", "中央馆10月10日周六夜场", "ON_SALE"
        )
        pxq_session = InventoryItem(
            TARGETS[5], "pxq-night", "中央馆10月10日周六夜场", "ON_SALE"
        )
        juss_restock = PriceLevelItem(juss_session, "juss-b", "B", 280, 2)
        pxq_existing = PriceLevelItem(pxq_session, "pxq-a", "A", 340, 4)

        context = build_other_channel_context(
            [juss_restock],
            [
                PriceLevelResult(juss_session, [juss_restock]),
                PriceLevelResult(pxq_session, [pxq_existing]),
            ],
            [juss_session, pxq_session],
        )

        class Dispatcher:
            def __init__(self):
                self.events = []

            def send(self, event):
                self.events.append(event)
                return {"test": True}

        dispatcher = Dispatcher()
        notify_price_levels_available(
            dispatcher,
            [juss_restock],
            other_channel_context=context,
        )

        self.assertIn(
            "📌 其他入口：中央馆10月10日周六夜场 · "
            "票星球同场次当前已有票：A ¥340",
            dispatcher.events[0].body,
        )

    def test_cross_channel_identity_does_not_mix_day_and_night_sessions(self):
        day = InventoryItem(
            TARGETS[0], "day", "中央馆10月10日周六日场", "ON_SALE"
        )
        night = InventoryItem(
            TARGETS[5], "night", "中央馆10月10日周六夜场", "ON_SALE"
        )

        self.assertNotEqual(
            session_comparison_key(day),
            session_comparison_key(night),
        )

    def test_missing_other_channel_is_selected_for_one_off_price_lookup(self):
        juss_session = InventoryItem(
            TARGETS[0], "juss-night", "中央馆10月10日周六夜场", "ON_SALE"
        )
        pxq_session = InventoryItem(
            TARGETS[5], "pxq-night", "中央馆10月10日周六夜场", "ON_SALE"
        )
        wrong_daypart = InventoryItem(
            TARGETS[5], "pxq-day", "中央馆10月10日周六日场", "ON_SALE"
        )
        restock = PriceLevelItem(juss_session, "juss-b", "B", 280, 2)

        selected = select_other_channel_context_sessions(
            [restock],
            [PriceLevelResult(juss_session, [restock])],
            [juss_session, pxq_session, wrong_daypart],
        )

        self.assertEqual(selected, [pxq_session])


class ParseSessionsTests(unittest.TestCase):
    def test_parses_public_v5_session_response(self):
        payload = {
            "statusCode": 200,
            "data": [
                {
                    "bizShowSessionId": "session-on",
                    "sessionName": "10月17日半决赛",
                    "sessionStatus": "ON_SALE",
                    "hasSessionSoldOut": False,
                },
                {
                    "bizShowSessionId": "session-off",
                    "sessionName": "10月18日决赛",
                    "sessionStatus": "LACK_OF_TICKET",
                    "hasSessionSoldOut": True,
                },
            ],
        }

        items = parse_sessions(TARGETS[0], payload)

        self.assertEqual([item.session_id for item in items], ["session-on", "session-off"])
        self.assertEqual([item.status for item in items], ["ON_SALE", "LACK_OF_TICKET"])

    def test_rejects_non_success_response(self):
        with self.assertRaisesRegex(ValueError, "API status=403"):
            parse_sessions(TARGETS[0], {"statusCode": 403, "comments": "forbidden"})

    def test_rejects_suspicious_empty_session_response(self):
        with self.assertRaisesRegex(ValueError, "空场次"):
            parse_sessions(TARGETS[0], {"statusCode": 200, "data": []})

    def test_uses_sold_out_flag_when_status_is_missing(self):
        payload = {
            "statusCode": 200,
            "data": [
                {
                    "bizShowSessionId": "session-off",
                    "sessionName": "决赛",
                    "hasSessionSoldOut": True,
                }
            ],
        }

        [item] = parse_sessions(TARGETS[0], payload)

        self.assertEqual(item.status, "LACK_OF_TICKET")


class ParsePriceLevelsTests(unittest.TestCase):
    def setUp(self):
        self.session = InventoryItem(
            TARGETS[0], "semi", "10月17日半决赛", "LACK_OF_TICKET"
        )

    def test_parses_grade_price_and_live_availability(self):
        payload = {
            "statusCode": 200,
            "data": {
                "seatPlans": [
                    {
                        "seatPlanId": "plan-s",
                        "seatPlanName": "S",
                        "originalPrice": 1920,
                        "canBuyCount": 0,
                    },
                    {
                        "seatPlanId": "plan-b",
                        "seatPlanName": "B",
                        "originalPrice": 960,
                        "canBuyCount": 4,
                        "saleStarted": True,
                        "isStopSale": False,
                        "channelHideFlag": False,
                    },
                ]
            },
        }

        items = parse_price_levels(self.session, payload)

        self.assertEqual([item.seat_plan_name for item in items], ["S", "B"])
        self.assertEqual([item.original_price for item in items], [1920.0, 960.0])
        self.assertEqual([item.available for item in items], [False, True])

    def test_explicit_stop_sale_overrides_positive_count(self):
        payload = {
            "statusCode": 200,
            "data": {
                "seatPlans": [
                    {
                        "seatPlanId": "plan-a",
                        "seatPlanName": "A",
                        "canBuyCount": 4,
                        "isStopSale": True,
                    }
                ]
            },
        }

        [item] = parse_price_levels(self.session, payload)

        self.assertFalse(item.available)

    def test_rejects_missing_seat_plan_array(self):
        with self.assertRaisesRegex(ValueError, "data.seatPlans"):
            parse_price_levels(self.session, {"statusCode": 200, "data": {}})

    def test_rejects_suspicious_empty_seat_plan_response(self):
        with self.assertRaisesRegex(ValueError, "空票档"):
            parse_price_levels(
                self.session,
                {"statusCode": 200, "data": {"seatPlans": []}},
            )


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.target = TARGETS[0]
        self.available = InventoryItem(self.target, "s1", "半决赛", "ON_SALE")
        self.sold_out = InventoryItem(self.target, "s1", "半决赛", "LACK_OF_TICKET")

    def test_first_round_builds_baseline_without_alert(self):
        newly = find_newly_available(
            {}, [self.available], initial_round=True, notify_initial=False
        )
        self.assertEqual(newly, [])

    def test_initial_alert_can_be_requested(self):
        newly = find_newly_available(
            {}, [self.available], initial_round=True, notify_initial=True
        )
        self.assertEqual(newly, [self.available])

    def test_sold_out_to_available_transition_alerts(self):
        previous = {self.sold_out.key: self.sold_out.status}
        newly = find_newly_available(
            previous, [self.available], initial_round=False, notify_initial=False
        )
        self.assertEqual(newly, [self.available])

    def test_unchanged_available_does_not_alert(self):
        previous = {self.available.key: self.available.status}
        newly = find_newly_available(
            previous, [self.available], initial_round=False, notify_initial=False
        )
        self.assertEqual(newly, [])


class PriceLevelTransitionTests(unittest.TestCase):
    def setUp(self):
        self.session = InventoryItem(TARGETS[0], "s1", "半决赛", "ON_SALE")
        self.available = PriceLevelItem(
            self.session, "plan-b", "B", 960.0, 4, sale_started=True
        )
        self.sold_out = PriceLevelItem(
            self.session, "plan-b", "B", 960.0, 0, sale_started=True
        )

    def test_first_round_builds_price_baseline_without_alert(self):
        newly = find_newly_available_price_levels(
            {}, [self.available], initial_round=True, notify_initial=False
        )
        self.assertEqual(newly, [])

    def test_sold_out_grade_to_available_alerts(self):
        previous = {self.sold_out.key: False}
        newly = find_newly_available_price_levels(
            previous, [self.available], initial_round=False, notify_initial=False
        )
        self.assertEqual(newly, [self.available])

    def test_failed_query_preserves_previous_state(self):
        previous = {self.available.key: True}
        update_price_level_state(
            previous,
            [PriceLevelResult(self.session, [], error="timeout")],
        )
        self.assertEqual(previous, {self.available.key: True})

    def test_missing_grade_is_marked_unavailable_after_successful_query(self):
        previous = {self.available.key: True}
        update_price_level_state(previous, [PriceLevelResult(self.session, [])])
        self.assertEqual(previous, {self.available.key: False})

    def test_single_available_observation_waits_for_confirmation(self):
        previous = {self.sold_out.key: False}
        streaks = {self.sold_out.key: -2}
        result = PriceLevelResult(self.session, [self.available])

        update_price_level_streaks(
            streaks,
            [result],
            confirmations=2,
            baselined_sessions={self.session.key},
        )
        confirmed = confirmed_available_price_levels(
            result.items, streaks, confirmations=2
        )
        update_confirmed_price_level_state(
            previous, [result], streaks, confirmations=2
        )

        self.assertEqual(confirmed, [])
        self.assertEqual(previous, {self.sold_out.key: False})

    def test_second_available_observation_confirms_transition(self):
        previous = {self.sold_out.key: False}
        streaks = {self.sold_out.key: 1}
        result = PriceLevelResult(self.session, [self.available])

        update_price_level_streaks(
            streaks,
            [result],
            confirmations=2,
            baselined_sessions={self.session.key},
        )
        confirmed = confirmed_available_price_levels(
            result.items, streaks, confirmations=2
        )

        self.assertEqual(confirmed, [self.available])
        self.assertEqual(
            find_newly_available_price_levels(
                previous,
                confirmed,
                initial_round=False,
                notify_initial=False,
            ),
            [self.available],
        )

    def test_single_unavailable_observation_does_not_reset_stable_state(self):
        previous = {self.available.key: True}
        streaks = {self.available.key: 2}
        result = PriceLevelResult(self.session, [self.sold_out])

        update_price_level_streaks(
            streaks,
            [result],
            confirmations=2,
            baselined_sessions={self.session.key},
        )
        update_confirmed_price_level_state(
            previous, [result], streaks, confirmations=2
        )

        self.assertEqual(previous, {self.available.key: True})

    def test_failed_notification_preserves_unavailable_state_for_retry(self):
        previous = {self.available.key: False}
        streaks = {self.available.key: 1}
        result = PriceLevelResult(self.session, [self.available])

        update_confirmed_price_level_state(
            previous,
            [result],
            streaks,
            confirmations=1,
            preserve_available_keys={self.available.key},
        )

        self.assertEqual(previous, {self.available.key: False})

    def test_initial_available_ticket_is_silently_baselined(self):
        previous = {}
        streaks = {}
        result = PriceLevelResult(self.session, [self.available])

        update_price_level_streaks(
            streaks,
            [result],
            confirmations=2,
            baselined_sessions=set(),
        )
        confirmed = confirmed_available_price_levels(
            result.items, streaks, confirmations=2
        )
        newly = find_newly_available_price_levels(
            previous,
            confirmed,
            initial_round=True,
            notify_initial=False,
        )
        update_confirmed_price_level_state(
            previous, [result], streaks, confirmations=2
        )

        self.assertEqual(newly, [])
        self.assertEqual(previous, {self.available.key: True})


class SelectionTests(unittest.TestCase):
    def test_default_links_never_generate_unapproved_plain_wechat_schemes(self):
        self.assertTrue(
            all(not target.buy_url.startswith("weixin://") for target in TARGETS)
        )

    def test_juss_targets_default_to_alipay_mini_program(self):
        targets = build_targets()[:5]

        for target in targets:
            outer_query = parse_qs(urlsplit(target.buy_url).query)
            scheme = outer_query["scheme"][0]
            scheme_query = parse_qs(urlsplit(scheme).query)
            self.assertEqual(urlsplit(target.buy_url).netloc, "ds.alipay.com")
            self.assertEqual(urlsplit(scheme).scheme, "alipays")
            self.assertEqual(scheme_query["appId"], [JUSS_ALIPAY_APP_ID])
            self.assertEqual(scheme_query["page"], [JUSS_ALIPAY_PATH])

    def test_juss_web_mode_keeps_precise_official_event_pages(self):
        targets = build_targets("web")[:5]

        for target in targets:
            self.assertEqual(target.buy_url, build_juss_web_url(target.show_id))
            self.assertIn(f"/content/{target.show_id}", target.buy_url)

    def test_piaoxingqiu_alipay_links_point_each_target_to_its_show(self):
        targets = build_targets("alipay")[5:]

        for target in targets:
            outer_query = parse_qs(urlsplit(target.buy_url).query)
            scheme = outer_query["scheme"][0]
            scheme_query = parse_qs(urlsplit(scheme).query)
            self.assertEqual(urlsplit(target.buy_url).netloc, "ds.alipay.com")
            self.assertEqual(urlsplit(scheme).scheme, "alipays")
            self.assertEqual(scheme_query["appId"], [PXQ_ALIPAY_APP_ID])
            self.assertEqual(scheme_query["page"], [PXQ_ALIPAY_PATH])
            self.assertEqual(
                parse_qs(scheme_query["query"][0]),
                {"showId": [target.show_id]},
            )

    def test_piaoxingqiu_alipay_url_matches_official_encoding(self):
        show_id = "6a672fe6a8ae9000013e03ab"

        self.assertEqual(
            build_piaoxingqiu_alipay_url(show_id),
            "https://ds.alipay.com/?scheme="
            "alipays%3A%2F%2Fplatformapi%2Fstartapp%3F"
            "appId%3D2021004123672725%26"
            "page%3D%2Fpages%2Fshow-detail%2Fshow-detail%26"
            f"query%3DshowId%3D{show_id}",
        )

    def test_piaoxingqiu_app_mode_uses_official_native_scheme(self):
        target = build_targets("app")[5]

        self.assertEqual(target.buy_url, build_piaoxingqiu_app_url(target.show_id))
        self.assertEqual(urlsplit(target.buy_url).scheme, "piaoxingqiu")

    def test_web_mode_keeps_precise_piaoxingqiu_fallback(self):
        target = build_targets("web")[5]

        self.assertEqual(
            target.buy_url,
            f"https://m.piaoxingqiu.com/content/{target.show_id}?showId={target.show_id}",
        )

    def test_removed_wechat_mode_is_rejected_instead_of_guessing_a_link(self):
        with self.assertRaisesRegex(ValueError, "alipay、app 或 web"):
            build_targets("wechat")

    def test_personal_scope_accepts_twenty_sessions_and_rejects_more(self):
        sessions = [
            InventoryItem(TARGETS[0], f"s{index}", f"场次 {index}", "ON_SALE")
            for index in range(21)
        ]

        self.assertTrue(session_scope_is_allowed(sessions[:20], 20))
        self.assertFalse(session_scope_is_allowed(sessions, 20))
        self.assertTrue(session_scope_is_allowed(sessions, None))

    def test_meta_alias_selects_juss_inventory(self):
        selected = select_targets("meta")
        self.assertTrue(selected)
        self.assertEqual({target.channel for target in selected}, {"juss"})

    def test_chinese_meta_alias_selects_juss_inventory(self):
        selected = select_targets("莓塔")
        self.assertTrue(selected)
        self.assertEqual({target.channel for target in selected}, {"juss"})

    def test_meta_and_juss_do_not_duplicate_targets(self):
        selected = select_targets("juss,meta")
        self.assertEqual(len(selected), 5)
        self.assertEqual(len({target.show_id for target in selected}), 5)

    def test_piaoxingqiu_labels_do_not_claim_meta_inventory(self):
        targets = select_targets("piaoxingqiu")
        self.assertTrue(targets)
        self.assertTrue(all(target.channel_label == "票星球" for target in targets))
        self.assertTrue(all("莓塔" not in target.buy_hint for target in targets))

    def test_match_filters_by_session_or_show_name(self):
        items = [
            InventoryItem(TARGETS[0], "s1", "10月17日半决赛", "ON_SALE"),
            InventoryItem(TARGETS[0], "s2", "10月18日决赛", "ON_SALE"),
        ]
        filtered = filter_items(items, "半决赛")
        self.assertEqual([item.session_id for item in filtered], ["s1"])

    def test_target_ids_are_unique(self):
        self.assertEqual(len({target.show_id for target in TARGETS}), len(TARGETS))
        self.assertTrue(AVAILABLE_STATUSES)
        self.assertEqual(TARGETS[-1].date_hint, "10月1日–4日 · 旗忠网球中心")

    def test_session_rows_include_status_id_and_name(self):
        item = InventoryItem(TARGETS[0], "session-1", "10月17日半决赛", "ON_SALE")

        rows = format_session_rows([TargetResult(TARGETS[0], [item])])

        self.assertEqual(rows[0], "channel\tshow\tstatus\tsession_id\tsession_name")
        self.assertIn("session-1", rows[1])
        self.assertIn("10月17日半决赛", rows[1])
        self.assertIn("有票", rows[1])

    def test_grade_filter_uses_exact_names(self):
        session = InventoryItem(TARGETS[0], "s1", "半决赛", "ON_SALE")
        levels = [
            PriceLevelItem(session, "p1", "S", 1920, 4),
            PriceLevelItem(session, "p2", "底线S基础票", 2200, 4),
            PriceLevelItem(session, "p3", "A+", 1680, 0),
        ]

        filtered = filter_price_levels(levels, "S,A+")

        self.assertEqual([item.seat_plan_name for item in filtered], ["S", "A+"])

    def test_price_level_rows_include_grade_price_and_count(self):
        session = InventoryItem(TARGETS[0], "s1", "半决赛", "ON_SALE")
        level = PriceLevelItem(session, "p1", "B", 960, 4)

        rows = format_price_level_rows([PriceLevelResult(session, [level])])

        self.assertIn("seat_plan_name", rows[0])
        self.assertIn("B", rows[1])
        self.assertIn("¥960", rows[1])
        self.assertTrue(rows[1].endswith("\t4"))


if __name__ == "__main__":
    unittest.main()
