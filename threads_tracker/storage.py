"""SQLite store for accounts + posts + per-fetch snapshots.

The schema keeps two tables:
  - posts: latest known version of each post (one row per pk).
  - post_snapshots: one row per (pk, fetched_at) so we can chart how
    like_count / reply_count drift over time and detect edits to text.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from .parser import ThreadPost, UserProfile

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    user_id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    full_name TEXT,
    biography TEXT,
    follower_count INTEGER,
    following_count INTEGER,
    is_verified INTEGER,
    profile_pic_url TEXT,
    last_fetched_at INTEGER
);

CREATE TABLE IF NOT EXISTS posts (
    pk TEXT PRIMARY KEY,
    code TEXT,
    user_id TEXT,
    username TEXT,
    text TEXT,
    taken_at INTEGER,
    like_count INTEGER,
    reply_count INTEGER,
    repost_count INTEGER,
    quote_count INTEGER,
    image_urls TEXT,
    video_urls TEXT,
    link_previews TEXT,
    is_repost INTEGER,
    first_seen_at INTEGER,
    last_seen_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id, taken_at DESC);

CREATE TABLE IF NOT EXISTS post_snapshots (
    pk TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    text TEXT,
    like_count INTEGER,
    reply_count INTEGER,
    repost_count INTEGER,
    quote_count INTEGER,
    PRIMARY KEY (pk, fetched_at)
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    user_id TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    follower_count INTEGER,
    following_count INTEGER,
    PRIMARY KEY (user_id, fetched_at)
);
"""


class TrackerStore:
    def __init__(self, path: str | Path = "data/threads.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- accounts ----------------------------------------------------------
    def upsert_account(self, profile: UserProfile, fetched_at: int | None = None) -> None:
        fetched_at = fetched_at or int(time.time())
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO accounts(user_id, username, full_name, biography,
                    follower_count, following_count, is_verified, profile_pic_url,
                    last_fetched_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    full_name=excluded.full_name,
                    biography=excluded.biography,
                    follower_count=COALESCE(excluded.follower_count, accounts.follower_count),
                    following_count=COALESCE(excluded.following_count, accounts.following_count),
                    is_verified=excluded.is_verified,
                    profile_pic_url=excluded.profile_pic_url,
                    last_fetched_at=excluded.last_fetched_at
                """,
                (
                    profile.user_id,
                    profile.username,
                    profile.full_name,
                    profile.biography,
                    profile.follower_count,
                    profile.following_count,
                    int(bool(profile.is_verified)) if profile.is_verified is not None else None,
                    profile.profile_pic_url,
                    fetched_at,
                ),
            )
            if profile.follower_count is not None or profile.following_count is not None:
                conn.execute(
                    """INSERT OR IGNORE INTO account_snapshots(user_id, fetched_at,
                       follower_count, following_count) VALUES(?,?,?,?)""",
                    (profile.user_id, fetched_at, profile.follower_count, profile.following_count),
                )

    # --- posts -------------------------------------------------------------
    def upsert_posts(self, posts: Iterable[ThreadPost], fetched_at: int | None = None) -> dict[str, int]:
        """Insert/update posts. Returns counts: {new, updated, snapshots}."""
        fetched_at = fetched_at or int(time.time())
        new_count = updated_count = snap_count = 0
        with self.tx() as conn:
            for p in posts:
                existing = conn.execute(
                    "SELECT pk, text, like_count, reply_count, repost_count, quote_count FROM posts WHERE pk=?",
                    (p.pk,),
                ).fetchone()
                params = (
                    p.pk,
                    p.code,
                    p.user_id,
                    p.username,
                    p.text,
                    p.taken_at,
                    p.like_count,
                    p.reply_count,
                    p.repost_count,
                    p.quote_count,
                    json.dumps(p.image_urls, ensure_ascii=False),
                    json.dumps(p.video_urls, ensure_ascii=False),
                    json.dumps(p.link_previews, ensure_ascii=False),
                    int(bool(p.is_repost)),
                    fetched_at,
                    fetched_at,
                )
                if existing is None:
                    conn.execute(
                        """INSERT INTO posts(pk, code, user_id, username, text, taken_at,
                           like_count, reply_count, repost_count, quote_count,
                           image_urls, video_urls, link_previews, is_repost,
                           first_seen_at, last_seen_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        params,
                    )
                    new_count += 1
                else:
                    conn.execute(
                        """UPDATE posts SET code=?, user_id=?, username=?, text=?,
                           taken_at=?, like_count=?, reply_count=?, repost_count=?,
                           quote_count=?, image_urls=?, video_urls=?, link_previews=?,
                           is_repost=?, last_seen_at=? WHERE pk=?""",
                        (
                            p.code,
                            p.user_id,
                            p.username,
                            p.text,
                            p.taken_at,
                            p.like_count,
                            p.reply_count,
                            p.repost_count,
                            p.quote_count,
                            json.dumps(p.image_urls, ensure_ascii=False),
                            json.dumps(p.video_urls, ensure_ascii=False),
                            json.dumps(p.link_previews, ensure_ascii=False),
                            int(bool(p.is_repost)),
                            fetched_at,
                            p.pk,
                        ),
                    )
                    updated_count += 1
                conn.execute(
                    """INSERT OR REPLACE INTO post_snapshots(pk, fetched_at, text,
                       like_count, reply_count, repost_count, quote_count)
                       VALUES(?,?,?,?,?,?,?)""",
                    (p.pk, fetched_at, p.text, p.like_count, p.reply_count, p.repost_count, p.quote_count),
                )
                snap_count += 1
        return {"new": new_count, "updated": updated_count, "snapshots": snap_count}

    # --- queries -----------------------------------------------------------
    def get_account(self, username: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM accounts WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        return dict(row) if row else None

    def list_posts(self, username: str, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM posts WHERE username = ? COLLATE NOCASE
               ORDER BY COALESCE(taken_at, first_seen_at) DESC LIMIT ?""",
            (username, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for col in ("image_urls", "video_urls", "link_previews"):
                if d.get(col):
                    try:
                        d[col] = json.loads(d[col])
                    except json.JSONDecodeError:
                        pass
            out.append(d)
        return out

    def follower_history(self, user_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT fetched_at, follower_count, following_count FROM account_snapshots WHERE user_id=? ORDER BY fetched_at",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def post_history(self, pk: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM post_snapshots WHERE pk=? ORDER BY fetched_at", (pk,)
        ).fetchall()
        return [dict(r) for r in rows]
