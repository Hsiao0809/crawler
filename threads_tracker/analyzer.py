"""Lightweight analytics over a tracked account's posts."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from statistics import mean, median
from typing import Any

HASHTAG_RE = re.compile(r"#([\w一-鿿぀-ヿ㐀-䶿]+)")
MENTION_RE = re.compile(r"@([A-Za-z0-9._]+)")
URL_RE = re.compile(r"https?://\S+")


def _has_media(p: dict[str, Any]) -> bool:
    return bool(p.get("image_urls")) or bool(p.get("video_urls"))


def _ts(p: dict[str, Any]) -> int:
    return int(p.get("taken_at") or p.get("first_seen_at") or 0)


def analyze_account(account: dict[str, Any] | None, posts: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute a summary report dict from the stored account + posts."""
    if not posts:
        return {
            "account": account,
            "post_count": 0,
            "note": "No posts stored. Run `track` first.",
        }

    likes = [p.get("like_count") or 0 for p in posts]
    replies = [p.get("reply_count") or 0 for p in posts]
    reposts = [p.get("repost_count") or 0 for p in posts]
    quotes = [p.get("quote_count") or 0 for p in posts]

    timestamps = sorted(_ts(p) for p in posts if _ts(p))
    span_days = None
    posts_per_week = None
    if len(timestamps) >= 2:
        span = timestamps[-1] - timestamps[0]
        span_days = span / 86400 if span > 0 else None
        if span_days and span_days > 0:
            posts_per_week = round(len(timestamps) / (span_days / 7), 2)

    hour_counter: Counter[int] = Counter()
    weekday_counter: Counter[str] = Counter()
    for ts in timestamps:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        hour_counter[dt.hour] += 1
        weekday_counter[dt.strftime("%A")] += 1

    hashtags: Counter[str] = Counter()
    mentions: Counter[str] = Counter()
    word_counter: Counter[str] = Counter()
    char_lengths: list[int] = []
    media_posts = 0
    has_url_posts = 0

    for p in posts:
        text = p.get("text") or ""
        char_lengths.append(len(text))
        hashtags.update(HASHTAG_RE.findall(text))
        mentions.update(MENTION_RE.findall(text))
        if URL_RE.search(text):
            has_url_posts += 1
        if _has_media(p):
            media_posts += 1
        words = re.findall(r"[A-Za-z]{3,}|[一-鿿]+", text)
        for w in words:
            word_counter[w.lower()] += 1

    top_posts_by_likes = sorted(posts, key=lambda p: p.get("like_count") or 0, reverse=True)[:5]

    def _post_brief(p: dict[str, Any]) -> dict[str, Any]:
        ts = _ts(p)
        return {
            "pk": p.get("pk"),
            "code": p.get("code"),
            "url": f"https://www.threads.com/@{p.get('username')}/post/{p.get('code')}"
            if p.get("username") and p.get("code")
            else None,
            "taken_at": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            if ts
            else None,
            "like_count": p.get("like_count"),
            "reply_count": p.get("reply_count"),
            "repost_count": p.get("repost_count"),
            "text_preview": (p.get("text") or "")[:140],
        }

    return {
        "account": account,
        "post_count": len(posts),
        "first_post": datetime.fromtimestamp(timestamps[0], tz=timezone.utc).isoformat()
        if timestamps
        else None,
        "last_post": datetime.fromtimestamp(timestamps[-1], tz=timezone.utc).isoformat()
        if timestamps
        else None,
        "span_days": round(span_days, 1) if span_days else None,
        "posts_per_week": posts_per_week,
        "engagement": {
            "likes": {
                "mean": round(mean(likes), 1) if likes else 0,
                "median": median(likes) if likes else 0,
                "max": max(likes) if likes else 0,
                "total": sum(likes),
            },
            "replies": {
                "mean": round(mean(replies), 1) if replies else 0,
                "max": max(replies) if replies else 0,
                "total": sum(replies),
            },
            "reposts_total": sum(reposts),
            "quotes_total": sum(quotes),
        },
        "content": {
            "avg_chars": round(mean(char_lengths), 1) if char_lengths else 0,
            "median_chars": median(char_lengths) if char_lengths else 0,
            "media_share": round(media_posts / len(posts), 2),
            "url_share": round(has_url_posts / len(posts), 2),
        },
        "top_hashtags": hashtags.most_common(15),
        "top_mentions": mentions.most_common(10),
        "top_words": [(w, n) for w, n in word_counter.most_common(20) if n > 1],
        "hour_distribution": sorted(hour_counter.items()),
        "weekday_distribution": [
            (d, weekday_counter.get(d, 0))
            for d in [
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Saturday",
                "Sunday",
            ]
        ],
        "top_posts_by_likes": [_post_brief(p) for p in top_posts_by_likes],
    }
