"""End-to-end test: feed a synthetic HTML fixture through parse → store → analyze."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_parser import _build_html
from threads_tracker.analyzer import analyze_account
from threads_tracker.parser import parse_profile_html
from threads_tracker.storage import TrackerStore


def test_full_pipeline() -> None:
    html = _build_html("evachien.chien", "1234567890")
    profile, posts = parse_profile_html(html, "evachien.chien")
    assert profile and len(posts) == 2

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        store = TrackerStore(db_path)
        store.upsert_account(profile)
        counts = store.upsert_posts(posts)
        assert counts["new"] == 2
        assert counts["updated"] == 0

        # Re-insert: should update, not insert.
        counts2 = store.upsert_posts(posts)
        assert counts2["new"] == 0
        assert counts2["updated"] == 2
        assert counts2["snapshots"] == 2

        # Stored posts come back via the store API.
        stored = store.list_posts("evachien.chien", limit=10)
        assert len(stored) == 2
        # And image_urls roundtrip through JSON.
        with_image = next(p for p in stored if p["image_urls"])
        assert with_image["image_urls"][0].endswith(".jpg")

        acc = store.get_account("evachien.chien")
        assert acc and acc["follower_count"] == 12345

        report = analyze_account(acc, stored)
        assert report["post_count"] == 2
        assert report["engagement"]["likes"]["total"] == 42 + 17
        # Hashtags from "#日常".
        assert any(tag == "日常" for tag, _ in report["top_hashtags"])
        store.close()


if __name__ == "__main__":
    test_full_pipeline()
    print("OK: end-to-end pipeline works (parse → store → analyze)")
