"""Regression tests for storage.upsert_account — guards against:

- audit #6: upsert clobbering populated fields with null re-fetches
- audit #7: empty user_id corrupting the table
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from threads_tracker.parser import UserProfile
from threads_tracker.storage import TrackerStore


def test_partial_refetch_does_not_clobber_good_data() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "t.db")
        store = TrackerStore(db)

        full = UserProfile(
            user_id="111",
            username="x",
            full_name="Eva Chien",
            biography="bio line",
            follower_count=12345,
            following_count=678,
            is_verified=False,
            profile_pic_url="https://cdn/full.jpg",
        )
        store.upsert_account(full, fetched_at=1000)

        # A later partial fetch where nothing came through except the user_id.
        partial = UserProfile(user_id="111", username="x")
        store.upsert_account(partial, fetched_at=2000)

        row = store.get_account("x")
        assert row["full_name"] == "Eva Chien", row
        assert row["biography"] == "bio line", row
        assert row["follower_count"] == 12345, row
        assert row["following_count"] == 678, row
        assert row["profile_pic_url"] == "https://cdn/full.jpg", row
        assert row["last_fetched_at"] == 2000, row  # but last-seen IS updated
        store.close()


def test_empty_user_id_is_rejected() -> None:
    """Accounts with empty user_id can't be uniquely identified and would
    block a future real upsert via the UNIQUE(username) constraint. Skip
    them entirely."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "t.db")
        store = TrackerStore(db)

        ghost = UserProfile(user_id="", username="x", full_name="Eva")
        store.upsert_account(ghost)
        assert store.get_account("x") is None, "empty user_id should not be persisted"

        # A real fetch later succeeds and doesn't conflict.
        real = UserProfile(user_id="222", username="x", full_name="Eva Chien", follower_count=100)
        store.upsert_account(real)
        row = store.get_account("x")
        assert row is not None
        assert row["user_id"] == "222"
        assert row["full_name"] == "Eva Chien"
        store.close()


def test_follower_snapshot_persists_across_fetches() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "t.db")
        store = TrackerStore(db)
        store.upsert_account(UserProfile(user_id="1", username="x", follower_count=100), fetched_at=1000)
        store.upsert_account(UserProfile(user_id="1", username="x", follower_count=110), fetched_at=2000)
        store.upsert_account(UserProfile(user_id="1", username="x", follower_count=120), fetched_at=3000)
        history = store.follower_history("1")
        assert [h["follower_count"] for h in history] == [100, 110, 120]
        store.close()


if __name__ == "__main__":
    test_partial_refetch_does_not_clobber_good_data()
    test_empty_user_id_is_rejected()
    test_follower_snapshot_persists_across_fetches()
    print("OK: all storage tests passed")
