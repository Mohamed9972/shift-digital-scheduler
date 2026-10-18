#!/usr/bin/env python3
"""Offline tests for the SHIFT DIGITAL Instagram scheduler.

Run: python -m pytest publisher/test_publisher.py -v
  or: python publisher/test_publisher.py  (unittest fallback)

No network calls. All Graph interactions are mocked.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make publisher/ importable
PUB = Path(__file__).resolve().parent
sys.path.insert(0, str(PUB))

import graph as g
import publish_next as pn
import import_calendar as ic


class TestTZConversion(unittest.TestCase):
    """Africa/Tunis = UTC+01:00 fixed, no DST (verified IANA tzdb 2026d)."""

    def test_fixed_offset(self):
        utc = dt.datetime(2026, 10, 19, 18, 0, tzinfo=dt.timezone.utc)
        tunis = pn.to_tunis(utc)
        self.assertEqual(tunis.hour, 19)
        self.assertEqual(tunis.day, 19)

    def test_summer_no_dst(self):
        """July: still +1h (no DST since 2008)."""
        utc = dt.datetime(2026, 7, 15, 12, 0, tzinfo=dt.timezone.utc)
        tunis = pn.to_tunis(utc)
        self.assertEqual(tunis.hour, 13)

    def test_slot_conversion(self):
        """19:00 Tunis = 18:00 UTC; 12:30 Tunis = 11:30 UTC."""
        self.assertEqual(pn._parse_utc("18:00").hour, 18)
        # import_calendar uses fixed -1h
        self.assertEqual(ic.hhmm_to_utc("19:00", "2026-10-19"), "18:00")
        self.assertEqual(ic.hhmm_to_utc("12:30", "2026-10-20"), "11:30")


class TestDueSelection(unittest.TestCase):
    def setUp(self):
        self.runbook = {
            "period_start": "2026-10-19",
            "items": [
                {"slug": "a", "date": "2026-10-19", "time_utc": "18:00", "kind": "REELS"},
                {"slug": "b", "date": "2026-10-19", "time_utc": "11:30", "kind": "IMAGE"},
                {"slug": "c", "date": "2026-10-20", "time_utc": "18:00", "kind": "CAROUSEL"},
            ],
        }
        self.state = {"published": {}}

    def test_nothing_due_before_slot(self):
        ts = dt.datetime(2026, 10, 19, 11, 0, tzinfo=dt.timezone.utc)  # 12:00 Tunis
        due, missed = pn.due_items(self.runbook, self.state, ts=ts)
        self.assertEqual(len(due), 0)

    def test_due_after_slot(self):
        ts = dt.datetime(2026, 10, 19, 18, 5, tzinfo=dt.timezone.utc)  # 19:05 Tunis
        due, missed = pn.due_items(self.runbook, self.state, ts=ts)
        self.assertEqual(len(due), 2)  # both a (18:00) and b (11:30) are past slot
        slugs = [d[0]["slug"] for d in due]
        self.assertIn("a", slugs)

    def test_multiple_due_picks_earliest(self):
        ts = dt.datetime(2026, 10, 19, 18, 30, tzinfo=dt.timezone.utc)
        due, missed = pn.due_items(self.runbook, self.state, ts=ts)
        self.assertEqual(len(due), 2)  # both a and b due
        self.assertEqual(due[0][0]["slug"], "b")  # earliest first (11:30 < 18:00)

    def test_missed_day_flagged(self):
        ts = dt.datetime(2026, 10, 20, 10, 0, tzinfo=dt.timezone.utc)  # Oct 20 11:00 Tunis
        due, missed = pn.due_items(self.runbook, self.state, ts=ts)
        self.assertIn("a", missed)
        self.assertIn("b", missed)
        self.assertNotIn("c", missed)  # c is Oct 20, still today (slot not reached)

    def test_already_published_skipped(self):
        self.state["published"]["a"] = {"published_at_utc": "2026-10-19T18:05:00Z"}
        ts = dt.datetime(2026, 10, 19, 18, 30, tzinfo=dt.timezone.utc)
        due, missed = pn.due_items(self.runbook, self.state, ts=ts)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0][0]["slug"], "b")


class TestRedaction(unittest.TestCase):
    def test_token_stripped(self):
        g.configure("SECRET_TOKEN_12345", "123")
        out = g.redact("error at https://x.com/?access_token=SECRET_TOKEN_12345&y=1")
        self.assertNotIn("SECRET_TOKEN_12345", out)
        self.assertIn("[REDACTED]", out)

    def test_query_param_stripped(self):
        g.configure("TOKEN", "123")
        out = g.redact("url?access_token=abc&foo=bar")
        self.assertNotIn("abc", out)


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)

    def _make_jpeg(self, name="test.jpg", size=1000):
        p = self.repo / name
        p.write_bytes(b"\xff\xd8\xff" + b"\x00" * (size - 3))
        return p

    def _make_mp4(self, name="test.mp4", size=5000):
        p = self.repo / name
        p.write_bytes(b"\x00" * 4 + b"ftyp" + b"\x00" * (size - 8))
        return p

    def test_valid_image(self):
        self._make_jpeg()
        item = {"kind": "IMAGE", "assets": {"image": "test.jpg"},
                "caption": "Hello #test", "hashtags": ["#test"]}
        errs, warns, cap = pn.validate_item(item, self.repo)
        self.assertEqual(errs, [])

    def test_image_too_large(self):
        self._make_jpeg(size=9 * 1024 * 1024)
        item = {"kind": "IMAGE", "assets": {"image": "test.jpg"},
                "caption": "Hello", "hashtags": []}
        errs, _, _ = pn.validate_item(item, self.repo)
        self.assertTrue(any("8MB" in e for e in errs))

    def test_image_not_jpeg(self):
        p = self.repo / "fake.jpg"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        item = {"kind": "IMAGE", "assets": {"image": "fake.jpg"},
                "caption": "Hello", "hashtags": []}
        errs, _, _ = pn.validate_item(item, self.repo)
        self.assertTrue(any("not a JPEG" in e for e in errs))

    def test_valid_video(self):
        self._make_mp4()
        item = {"kind": "REELS",
                "assets": {"video": "test.mp4", "cover": "test.jpg"},
                "caption": "Hello", "hashtags": []}
        self._make_jpeg()
        errs, _, _ = pn.validate_item(item, self.repo)
        self.assertEqual(errs, [])

    def test_caption_too_long(self):
        self._make_jpeg()
        item = {"kind": "IMAGE", "assets": {"image": "test.jpg"},
                "caption": "x" * 2201, "hashtags": []}
        errs, _, _ = pn.validate_item(item, self.repo)
        self.assertTrue(any("2200" in e for e in errs))

    def test_too_many_hashtags(self):
        self._make_jpeg()
        tags = " ".join(f"#tag{i}" for i in range(31))
        item = {"kind": "IMAGE", "assets": {"image": "test.jpg"},
                "caption": tags, "hashtags": []}
        errs, _, _ = pn.validate_item(item, self.repo)
        self.assertTrue(any("30" in e for e in errs))

    def test_missing_asset(self):
        item = {"kind": "IMAGE", "assets": {"image": "missing.jpg"},
                "caption": "Hello", "hashtags": []}
        errs, _, _ = pn.validate_item(item, self.repo)
        self.assertTrue(any("not committed" in e for e in errs))

    def test_carousel_too_many_children(self):
        imgs = [f"img{i}.jpg" for i in range(11)]
        for i in range(11):
            self._make_jpeg(f"img{i}.jpg")
        item = {"kind": "CAROUSEL", "assets": {"images": imgs},
                "caption": "Hello", "hashtags": []}
        errs, _, _ = pn.validate_item(item, self.repo)
        self.assertTrue(any("10" in e for e in errs))


class TestRunbookConsistency(unittest.TestCase):
    """Runbook must match the calendar exactly."""

    def test_all_30_items(self):
        rb = json.loads((PUB / "runbook.json").read_text(encoding="utf-8"))
        self.assertEqual(len(rb["items"]), 30)

    def test_kinds(self):
        rb = json.loads((PUB / "runbook.json").read_text(encoding="utf-8"))
        kinds = [it["kind"] for it in rb["items"]]
        self.assertEqual(kinds.count("REELS"), 12)
        self.assertEqual(kinds.count("CAROUSEL"), 8)
        self.assertEqual(kinds.count("IMAGE"), 10)

    def test_utc_times(self):
        rb = json.loads((PUB / "runbook.json").read_text(encoding="utf-8"))
        for it in rb["items"]:
            h, m = map(int, it["time_tunis"].split(":"))
            uh, um = map(int, it["time_utc"].split(":"))
            self.assertEqual((h - 1) % 24, uh,
                             f"D{it['day']:02d} {it['slug']}: "
                             f"{it['time_tunis']} -> {it['time_utc']}")

    def test_captions_have_hashtags(self):
        rb = json.loads((PUB / "runbook.json").read_text(encoding="utf-8"))
        for it in rb["items"]:
            if it["hashtags"]:
                for tag in it["hashtags"]:
                    self.assertIn(tag, it["caption"],
                                  f"D{it['day']:02d} missing hashtag {tag}")

    def test_period(self):
        rb = json.loads((PUB / "runbook.json").read_text(encoding="utf-8"))
        self.assertEqual(rb["period_start"], "2026-10-19")
        self.assertEqual(rb["period_end"], "2026-11-17")


class TestDryRun(unittest.TestCase):
    """Dry-run must perform ZERO Graph writes."""

    def test_dry_run_no_writes(self):
        rb = json.loads((PUB / "runbook.json").read_text(encoding="utf-8"))
        state = {"published": {}}
        # Pick a reel item
        item = next(it for it in rb["items"] if it["kind"] == "REELS")
        g = MagicMock()
        g._session = {"ig_user_id": "123"}
        # Should not raise, should return planned dict
        result = pn.publish_reel(item, g, Path("/fake"), dry=True)
        self.assertTrue(result.get("planned"))
        g.post.assert_not_called()
        g.upload_video_bytes.assert_not_called()

    def test_dry_run_image(self):
        rb = json.loads((PUB / "runbook.json").read_text(encoding="utf-8"))
        item = next(it for it in rb["items"] if it["kind"] == "IMAGE")
        g = MagicMock()
        result = pn.publish_image(item, g, Path("/fake"), dry=True)
        self.assertTrue(result.get("planned"))
        g.post.assert_not_called()

    def test_dry_run_carousel(self):
        rb = json.loads((PUB / "runbook.json").read_text(encoding="utf-8"))
        item = next(it for it in rb["items"] if it["kind"] == "CAROUSEL")
        # carousel images are dir paths in runbook; dry-run just logs
        g = MagicMock()
        result = pn.publish_carousel(item, g, Path("/fake"), dry=True)
        self.assertTrue(result.get("planned"))
        g.post.assert_not_called()


class TestStateLedger(unittest.TestCase):
    def test_idempotent_record(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "published.json"
            state = {"published": {}}
            state["published"]["test-slug"] = {
                "media_id": "123", "permalink": "https://instagram.com/p/ABC",
                "published_at_utc": "2026-10-19T18:05:00Z"}
            state_path.write_text(json.dumps(state), encoding="utf-8")
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("test-slug", loaded["published"])
            self.assertEqual(loaded["published"]["test-slug"]["media_id"], "123")


class TestGraphClient(unittest.TestCase):
    def test_configure(self):
        g.configure("MY_TOKEN", "IG123", "v25.0")
        self.assertEqual(g._session["token"], "MY_TOKEN")
        self.assertEqual(g._session["ig_user_id"], "IG123")

    def test_redact_in_graph_error(self):
        g.configure("SECRET", "123")
        # Simulate an error message containing the token
        msg = "HTTP 400 on media: access_token=SECRET&error=bad"
        redacted = g.redact(msg)
        self.assertNotIn("SECRET", redacted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
