"""Offline regression tests. No live network requests or Discord messages."""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from tracker import (BASE, KST, Episode, Series, SiteBlocked, ToptoonClient,
                     TrackerError, load_config, load_state, make_payload,
                     normalize_slug, parse_series, process_series, release_value,
                     save_state, send_discord, webhook_url)


def row(chapter="2", episode_id="e2", slug="example", release="26.09.07", classes=""):
    return (f'<a class="episode-items {classes}" data-comic-id="{slug}" '
            f'data-episode-id="{episode_id}"><p class="ep_title">제{chapter}화</p>'
            f'<p class="ep_date"><span class="date">{release}</span></p></a>')


def page(rows):
    return ('<meta property="og:title" content="[탑툰] Example">'
            '<meta property="og:image" content="https://smurfs.toptoon.com/cover.jpg">' + rows)


NOW = datetime(2026, 9, 8, 12, tzinfo=KST)
EP2 = Episode(Decimal("2"), "e2", BASE + "/comic/ep_view/example/e2", "2026-09-07", "date")
EP3 = Episode(Decimal("3"), "e3", BASE + "/comic/ep_view/example/e3", "2026-09-08", "date")
SERIES = Series("example", "Example", "https://smurfs.toptoon.com/cover.jpg", (EP2, EP3))


class ParserTests(unittest.TestCase):
    def test_extracts_requested_series_and_sorts_actual_chapter_numbers(self):
        html = page(row("10", "non-numeric-id") + row("2") + row("999", slug="recommendation"))
        series = parse_series(html, "example", NOW)
        self.assertEqual(series.title, "Example")
        self.assertEqual([ep.number for ep in series.episodes], [Decimal(2), Decimal(10)])
        self.assertTrue(series.episodes[-1].url.endswith("/example/non-numeric-id"))
        self.assertEqual(series.episodes[-1].release, "2026-09-07")
        self.assertEqual(series.episodes[-1].precision, "date")

    def test_future_disabled_and_special_chapters_are_excluded(self):
        html = page(row("2") + row("3", "e3", release="26.09.09")
                    + row("4", "e4", classes="disabled") + row("프롤로그", "prologue"))
        self.assertEqual(len(parse_series(html, "example", NOW).episodes), 1)

    def test_decimal_chapters(self):
        series = parse_series(page(row("2.5")), "example", NOW)
        self.assertEqual(series.episodes[0].number, Decimal("2.5"))

    def test_missing_or_gated_page_fails_closed(self):
        with self.assertRaises(TrackerError):
            parse_series("<html>Login required</html>", "example", NOW)

    def test_missing_or_invalid_date_fails_closed(self):
        # Updated to replace the entire ep_date class instead of just the inner date class
        for html in [page(row(release="unknown")), page(row().replace('class="ep_date"', 'class="changed"'))]:
            with self.subTest(html=html), self.assertRaises(TrackerError):
                parse_series(html, "example", NOW)

    def test_ambiguous_numbering_fails_closed(self):
        with self.assertRaises(TrackerError):
            parse_series(page(row("2", "a") + row("2", "b")), "example", NOW)

    def test_release_precision(self):
        self.assertEqual(release_value("26.09.07"), ("2026-09-07", "date"))
        self.assertEqual(release_value("2026-09-07 22:00:00"), ("2026-09-07T22:00:00+09:00", "timestamp"))
        with self.assertRaises(ValueError):
            release_value("26.02.30")

    def test_payload_has_requested_fields_and_honest_timestamp(self):
        payload = make_payload(SERIES, EP3)
        embed = payload["embeds"][0]
        self.assertEqual(embed["url"], EP3.url)
        self.assertEqual(embed["thumbnail"]["url"], SERIES.thumbnail)
        self.assertIn("exact time not published", embed["fields"][1]["value"])
        self.assertIn("Detection time", embed["footer"]["text"])
        self.assertEqual(payload["allowed_mentions"], {"parse": []})


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.state = {"version": 1, "series": {"example": {"last_chapter": "1"}}}

    @patch("tracker.send_discord")
    def test_first_run_baselines_without_alerts(self, send):
        state = {"version": 1, "series": {}}
        process_series(SERIES, state, self.path, "unused")
        send.assert_not_called()
        self.assertEqual(load_state(self.path)["series"]["example"]["last_chapter"], "3")

    @patch("tracker.time.sleep")
    @patch("tracker.send_discord")
    def test_all_new_chapters_delivered_in_order_and_not_repeated(self, send, sleep):
        process_series(SERIES, self.state, self.path, "unused")
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].args[1]["embeds"][0]["url"], EP2.url)
        state = load_state(self.path)
        process_series(SERIES, state, self.path, "unused")
        self.assertEqual(send.call_count, 2)

    @patch("tracker.time.sleep")
    @patch("tracker.send_discord", side_effect=[None, TrackerError("delivery failed")])
    def test_partial_failure_saves_only_acknowledged_chapters(self, send, sleep):
        with self.assertRaises(TrackerError):
            process_series(SERIES, self.state, self.path, "unused")
        self.assertEqual(load_state(self.path)["series"]["example"]["last_chapter"], "2")

    @patch("tracker.send_discord", side_effect=TrackerError("failure"))
    def test_failed_webhook_never_advances_state(self, send):
        save_state(self.path, self.state)
        with self.assertRaises(TrackerError):
            process_series(SERIES, self.state, self.path, "unused")
        self.assertEqual(load_state(self.path)["series"]["example"]["last_chapter"], "1")

    @patch("tracker.send_discord")
    def test_dry_run_does_not_write_or_notify(self, send):
        process_series(SERIES, self.state, self.path, None, True)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.state["series"]["example"]["last_chapter"], "1")
        send.assert_not_called()

    def test_regression_never_lowers_state(self):
        self.state["series"]["example"]["last_chapter"] = "100"
        with self.assertRaises(TrackerError):
            process_series(SERIES, self.state, self.path, "unused")
        self.assertEqual(self.state["series"]["example"]["last_chapter"], "100")

    def test_corrupt_state_is_not_silently_reset(self):
        for text in ['{broken', '{"version":1,"series":{"example":{"last_chapter":"NaN"}}}',
                     '{"version":1,"series":{"example":{"last_chapter":true}}}']:
            self.path.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises((ValueError, TrackerError)):
                load_state(self.path)

    def test_config_validation_and_normalization(self):
        self.assertEqual(normalize_slug(BASE + "/comic/ep_list/example?tracking=1"), "example")
        for value in ["https://other.com/comic/ep_list/example", "../../x", "https://toptoon.com/", 123]:
            with self.subTest(value=value), self.assertRaises(TrackerError):
                normalize_slug(value)
        self.path.write_text(json.dumps(["example", BASE + "/comic/ep_list/example"]), encoding="utf-8")
        with self.assertRaises(TrackerError):
            load_config(self.path)


class HttpTests(unittest.TestCase):
    @patch("tracker.time.sleep")
    @patch("tracker.requests.post")
    def test_discord_retries_explicit_rate_limit(self, post, sleep):
        post.side_effect = [Mock(status_code=429, json=lambda: {"retry_after": 0.1}), Mock(status_code=200)]
        send_discord("unused", {"content": "test"})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["params"], {"wait": "true"})

    @patch("tracker.requests.post", side_effect=requests.Timeout("sensitive webhook address"))
    def test_ambiguous_discord_failure_is_not_immediately_retried_or_logged(self, post):
        with self.assertRaises(TrackerError) as error:
            send_discord("unused", {})
        self.assertNotIn("sensitive", str(error.exception))
        self.assertEqual(post.call_count, 1)

    @patch("tracker.requests.post", return_value=Mock(status_code=500))
    def test_discord_server_failure_raises(self, post):
        with self.assertRaises(TrackerError):
            send_discord("unused", {})

    def test_site_blocks_end_requests_without_retry(self):
        for status in [401, 403, 429]:
            client = ToptoonClient(interval=0)
            self.addCleanup(client.close)
            with patch.object(client.session, "get", return_value=Mock(status_code=status)) as get:
                with self.assertRaises(SiteBlocked):
                    client.get(BASE)
                self.assertEqual(get.call_count, 1)

    @patch("tracker.time.sleep")
    def test_site_get_retries_server_failure(self, sleep):
        client = ToptoonClient(interval=0)
        self.addCleanup(client.close)
        with patch.object(client.session, "get", side_effect=[Mock(status_code=503), Mock(status_code=200, text="ok")]):
            self.assertEqual(client.get(BASE), "ok")

    @patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": ""})
    def test_missing_webhook_fails_before_polling(self):
        with self.assertRaises(TrackerError):
            webhook_url()


if __name__ == "__main__":
    unittest.main()
