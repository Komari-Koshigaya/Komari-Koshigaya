from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import push_bbc_learning_english as bbc  # noqa: E402

FIXTURE_6MIN = ROOT / "tests" / "fixtures" / "bbc_6min.rss"
FIXTURE_OTHER = ROOT / "tests" / "fixtures" / "bbc_other.rss"
NOW = datetime(2026, 11, 25, 0, 5, tzinfo=timezone.utc)  # 08:05 Beijing


def load_episodes():
    _, six = bbc.parse_rss(
        FIXTURE_6MIN.read_bytes(), bbc.FeedSpec("6min", "6 Minute English", ())
    )
    _, other = bbc.parse_rss(
        FIXTURE_OTHER.read_bytes(), bbc.FeedSpec("tews", "The English We Speak", ())
    )
    return six + other


class ParseTests(unittest.TestCase):
    def test_parse_six_minute_english(self) -> None:
        series, episodes = bbc.parse_rss(
            FIXTURE_6MIN.read_bytes(), bbc.FeedSpec("6min", "fallback", ())
        )
        self.assertEqual(series, "6 Minute English")
        self.assertEqual(len(episodes), 2)
        first = episodes[0]
        self.assertEqual(first.title, "Why do we dream?")
        self.assertEqual(first.feed_id, "6min")
        self.assertEqual(first.duration_seconds, 372)
        self.assertEqual(
            first.published_at, datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(first.audio_url, "https://example.com/6min-261120.mp3")
        self.assertIn("Neil", first.summary)
        self.assertNotIn("<b>", first.summary)

    def test_parse_missing_enclosure(self) -> None:
        _, episodes = bbc.parse_rss(
            FIXTURE_OTHER.read_bytes(), bbc.FeedSpec("tews", "fallback", ())
        )
        self.assertEqual(len(episodes), 2)
        self.assertIsNone(episodes[1].audio_url)
        self.assertTrue(episodes[1].link.startswith("https://www.bbc.co.uk/"))

    def test_malformed_xml_raises(self) -> None:
        with self.assertRaises(ET.ParseError):
            bbc.parse_rss(b"<rss><channel>", bbc.FeedSpec("x", "x", ()))

    def test_strip_html(self) -> None:
        self.assertEqual(
            bbc.strip_html("<p>Hello&nbsp; <b>world</b></p>"),
            "Hello world",
        )

    def test_parse_duration(self) -> None:
        self.assertEqual(bbc.parse_duration("06:12"), 372)
        self.assertEqual(bbc.parse_duration("372"), 372)
        self.assertEqual(bbc.parse_duration("1:02:03"), 3723)
        self.assertIsNone(bbc.parse_duration("nope"))
        self.assertIsNone(bbc.parse_duration(""))

    def test_format_duration(self) -> None:
        self.assertEqual(bbc.format_duration(372), "6:12")
        self.assertEqual(bbc.format_duration(3723), "1:02:03")
        self.assertEqual(bbc.format_duration(None), "")

    def test_parse_pubdate_naive_is_utc(self) -> None:
        parsed = bbc.parse_pubdate("Fri, 20 Nov 2026 10:00:00")
        self.assertEqual(parsed, datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc))


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.episodes = load_episodes()
        self.state = bbc.empty_state()

    def test_morning_prefers_six_minute_english(self) -> None:
        selections = bbc.select_episodes(self.episodes, self.state, "morning", NOW)
        self.assertEqual(len(selections), 1)
        self.assertEqual(selections[0].kind, "new")
        self.assertEqual(selections[0].episode.series, "6 Minute English")
        self.assertEqual(selections[0].episode.title, "Why do we dream?")

    def test_evening_prefers_english_we_speak(self) -> None:
        selections = bbc.select_episodes(self.episodes, self.state, "evening", NOW)
        self.assertEqual(len(selections), 1)
        self.assertEqual(selections[0].episode.series, "The English We Speak")

    def test_max_items(self) -> None:
        selections = bbc.select_episodes(
            self.episodes, self.state, "morning", NOW, max_items=2
        )
        self.assertEqual(len(selections), 2)
        self.assertTrue(all(item.kind == "new" for item in selections))

    def test_review_picks_least_recently_sent(self) -> None:
        sent = {}
        for episode in self.episodes:
            sent[episode.guid] = "2026-11-20T08:00:00+08:00"
        sent["urn:bbc:podcast:6min:20261113"] = "2026-11-01T08:00:00+08:00"
        self.state["sent"] = sent
        selections = bbc.select_episodes(self.episodes, self.state, "morning", NOW)
        self.assertEqual(len(selections), 1)
        self.assertEqual(selections[0].kind, "review")
        self.assertEqual(selections[0].episode.title, "Are you a good listener?")

    def test_future_episode_is_skipped(self) -> None:
        future = bbc.Episode(
            feed_id="6min",
            series="6 Minute English",
            title="From the future",
            link="https://example.com/future",
            guid="future-guid",
            published_at=NOW + timedelta(days=30),
            duration_seconds=360,
            audio_url=None,
            summary="",
        )
        selections = bbc.select_episodes(
            [future] + self.episodes, self.state, "morning", NOW
        )
        self.assertEqual(selections[0].episode.title, "Why do we dream?")

    def test_no_candidates(self) -> None:
        self.assertEqual(bbc.select_episodes([], self.state, "morning", NOW), [])

    def test_dedupe_keeps_richer_copy(self) -> None:
        episodes = load_episodes()
        duplicate = bbc.Episode(
            feed_id="tews",
            series="Duplicate",
            title=episodes[0].title,
            link=episodes[0].link,
            guid=episodes[0].guid,
            published_at=episodes[0].published_at,
            duration_seconds=episodes[0].duration_seconds,
            audio_url=episodes[0].audio_url,
            summary="a much longer summary " * 10,
        )
        result = bbc.dedupe_episodes(episodes + [duplicate])
        self.assertEqual(len(result), 4)
        kept = next(item for item in result if item.guid == episodes[0].guid)
        self.assertEqual(kept.summary, "a much longer summary " * 10)


class StateTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = bbc.empty_state()
            state["sent"]["guid"] = "2026-11-25T08:05:00+08:00"
            state["last_push"]["2026-11-25-morning"] = "2026-11-25T08:05:00+08:00"
            bbc.save_state(path, state)
            loaded = bbc.load_state(path)
            self.assertEqual(loaded["sent"], state["sent"])
            self.assertEqual(loaded["last_push"], state["last_push"])

    def test_unreadable_state_is_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(bbc.load_state(path), bbc.empty_state())

    def test_prune_removes_old_entries(self) -> None:
        state = bbc.empty_state()
        state["sent"]["old"] = "2026-01-01T00:00:00+08:00"
        state["sent"]["new"] = "2026-11-20T00:00:00+08:00"
        state["last_push"]["2026-01-01-morning"] = "2026-01-01T00:00:00+08:00"
        state["last_push"]["2026-11-25-morning"] = "2026-11-25T00:00:00+08:00"
        bbc.prune_state(state, NOW)
        self.assertEqual(set(state["sent"]), {"new"})
        self.assertEqual(set(state["last_push"]), {"2026-11-25-morning"})


class ServerChanTests(unittest.TestCase):
    def test_turbo_endpoint(self) -> None:
        self.assertEqual(
            bbc.resolve_endpoint("SCT123abc"),
            "https://sctapi.ftqq.com/SCT123abc.send",
        )

    def test_serverchan3_endpoint(self) -> None:
        self.assertEqual(
            bbc.resolve_endpoint("sctp123tSECRET"),
            "https://123.push.ft07.com/send/sctp123tSECRET.send",
        )

    def test_empty_key_fails(self) -> None:
        with self.assertRaises(ValueError):
            bbc.resolve_endpoint("  ")

    def test_invalid_serverchan3_key_fails(self) -> None:
        with self.assertRaises(ValueError):
            bbc.resolve_endpoint("sctp-not-a-valid-key")

    def test_mask_hides_key(self) -> None:
        masked = bbc.mask_endpoint("https://123.push.ft07.com/send/sctp123tSECRET.send")
        self.assertEqual(masked, "https://123.push.ft07.com/.../***.send")
        self.assertNotIn("SECRET", masked)

    def test_successful_push(self) -> None:
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'{"code": 0, "message": ""}'
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with mock.patch.object(bbc.urllib.request, "urlopen", return_value=response):
            result = bbc.send_serverchan(
                "https://sctapi.ftqq.com/SCT123.send", "title", "body"
            )
        self.assertEqual(result["code"], 0)

    def test_api_error_raises(self) -> None:
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'{"code": 40001, "message": "bad key"}'
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with mock.patch.object(bbc.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(RuntimeError):
                bbc.send_serverchan(
                    "https://sctapi.ftqq.com/SCT123.send", "title", "body"
                )

    def test_http_4xx_does_not_leak_key(self) -> None:
        error = urllib.error.HTTPError(
            "https://sctapi.ftqq.com/SCT123.send", 401, "Unauthorized", {}, None
        )
        with mock.patch.object(bbc.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as ctx:
                bbc.send_serverchan(
                    "https://sctapi.ftqq.com/SCT123.send", "title", "body"
                )
        self.assertNotIn("SCT123", str(ctx.exception))


class MessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.episodes = load_episodes()

    def test_new_message(self) -> None:
        selection = bbc.Selection(self.episodes[0], "new")
        title, body = bbc.build_message([selection], "morning", NOW)
        self.assertLessEqual(len(title), 32)
        self.assertIn("BBC英语学习", title)
        self.assertIn("早间", title)
        self.assertIn("新节目", title)
        self.assertIn("## 🎧 6 Minute English", body)
        self.assertIn("Why do we dream?", body)
        self.assertIn("[▶ 收听音频]", body)
        self.assertIn("[📄 节目页面]", body)
        self.assertIn("💡", body)
        self.assertNotIn("♻️", body)

    def test_review_message(self) -> None:
        selection = bbc.Selection(self.episodes[1], "review")
        _, body = bbc.build_message([selection], "evening", NOW)
        self.assertIn("♻️", body)
        self.assertIn("晚间", bbc.build_message([selection], "evening", NOW)[0])


class MainTests(unittest.TestCase):
    def test_dry_run_uses_fixtures_and_masks_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            output = io.StringIO()
            with mock.patch.dict(
                os.environ, {"SERVERCHAN_SENDKEY": "sctp123tSECRETKEY"}
            ):
                with contextlib.redirect_stdout(output):
                    code = bbc.main([
                        "--slot", "morning",
                        "--state", str(state_path),
                        "--feed-file", f"6min={FIXTURE_6MIN}",
                        "--feed-file", f"tews={FIXTURE_OTHER}",
                        "--dry-run",
                        "--now", "2026-11-25T08:05:00+08:00",
                    ])
            text = output.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("Why do we dream?", text)
            self.assertIn("123.push.ft07.com", text)
            self.assertNotIn("SECRETKEY", text)
            self.assertFalse(state_path.exists())

    def test_duplicate_slot_skips_before_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            bbc.save_state(
                state_path,
                {
                    "version": 1,
                    "sent": {},
                    "last_push": {
                        "2026-11-25-morning": "2026-11-25T08:05:00+08:00"
                    },
                },
            )
            with mock.patch.dict(
                os.environ, {"SERVERCHAN_SENDKEY": "SCTfake"}
            ), mock.patch.object(bbc, "send_serverchan") as sender:
                code = bbc.main([
                    "--slot", "morning",
                    "--state", str(state_path),
                    "--feed-file", f"6min={FIXTURE_6MIN}",
                    "--feed-file", f"tews={FIXTURE_OTHER}",
                    "--now", "2026-11-25T08:05:00+08:00",
                ])
            self.assertEqual(code, 0)
            sender.assert_not_called()

    def test_missing_key_fails_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"SERVERCHAN_SENDKEY": ""}):
                code = bbc.main([
                    "--slot", "morning",
                    "--state", str(Path(tmp) / "state.json"),
                    "--feed-file", f"6min={FIXTURE_6MIN}",
                    "--feed-file", f"tews={FIXTURE_OTHER}",
                    "--now", "2026-11-25T08:05:00+08:00",
                ])
            self.assertEqual(code, 1)

    def test_check_feeds(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = bbc.main([
                "--check-feeds",
                "--state", "unused.json",
                "--feed-file", f"6min={FIXTURE_6MIN}",
                "--feed-file", f"tews={FIXTURE_OTHER}",
                "--now", "2026-11-25T08:05:00+08:00",
            ])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["total_episodes"], 4)
        self.assertEqual(len(report["feeds"]), 2)

    def test_missing_feed_file_is_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = bbc.main([
                    "--dry-run",
                    "--slot", "morning",
                    "--state", str(Path(tmp) / "state.json"),
                    "--feed-file", f"6min={FIXTURE_6MIN}",
                    "--feed-file", f"missing={Path(tmp) / 'nope.rss'}",
                    "--now", "2026-11-25T08:05:00+08:00",
                ])
            self.assertEqual(code, 0)
            self.assertIn("6 Minute English", output.getvalue())

    def test_all_feeds_missing_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                code = bbc.main([
                    "--dry-run",
                    "--state", str(Path(tmp) / "state.json"),
                    "--feed-file", f"missing={Path(tmp) / 'nope.rss'}",
                    "--now", "2026-11-25T08:05:00+08:00",
                ])
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
