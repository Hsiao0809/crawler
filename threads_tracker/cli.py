"""Command-line interface for the Threads tracker."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .analyzer import analyze_account
from .client import ThreadsClient, ThreadsError
from .parser import parse_graphql_response, parse_profile_html
from .storage import TrackerStore

log = logging.getLogger("threads_tracker")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _strip_at(username: str) -> str:
    return username.lstrip("@").strip()


def cmd_track(args: argparse.Namespace) -> int:
    username = _strip_at(args.username)
    store = TrackerStore(args.db)
    client = ThreadsClient(debug_dump_dir=args.dump_dir)

    print(f"[*] Fetching profile page for @{username} ...", flush=True)
    try:
        result = client.fetch_profile_html(username)
    except ThreadsError as exc:
        print(f"[x] failed to fetch profile: {exc}", file=sys.stderr)
        return 2

    print(f"[*] Loaded {result.url} ({len(result.text):,} bytes, HTTP {result.status})")
    profile, posts = parse_profile_html(result.text, username)

    if not profile:
        print(
            "[x] Could not extract a profile object from the page. "
            "Run with --dump-dir to save raw HTML for inspection.",
            file=sys.stderr,
        )
        return 3
    print(
        f"[+] Profile: user_id={profile.user_id} full_name={profile.full_name!r} "
        f"followers={profile.follower_count} bio_len={len(profile.biography or '')}"
    )
    print(f"[+] Posts extracted from HTML: {len(posts)}")

    # Optional: also pull from GraphQL for deeper history.
    if args.graphql and profile.user_id:
        print("[*] Calling GraphQL for additional history ...")
        try:
            payload = client.fetch_user_threads_graphql(profile.user_id)
            extra = parse_graphql_response(payload, username_hint=username)
            print(f"[+] Posts from GraphQL: {len(extra)}")
            # Merge by pk.
            seen = {p.pk for p in posts}
            for p in extra:
                if p.pk not in seen:
                    posts.append(p)
                    seen.add(p.pk)
        except ThreadsError as exc:
            print(f"[!] GraphQL fetch failed (continuing with HTML data): {exc}", file=sys.stderr)

    fetched_at = int(time.time())
    store.upsert_account(profile, fetched_at=fetched_at)
    counts = store.upsert_posts(posts, fetched_at=fetched_at)

    print(
        f"[+] Stored to {args.db}: new={counts['new']} updated={counts['updated']} "
        f"snapshots={counts['snapshots']}"
    )

    if args.show:
        _print_post_table(posts[: args.show])

    store.close()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    username = _strip_at(args.username)
    store = TrackerStore(args.db)
    acc = store.get_account(username)
    if not acc:
        print(f"[x] No data for @{username}. Run `track` first.", file=sys.stderr)
        return 1
    print(json.dumps(acc, ensure_ascii=False, indent=2))
    posts = store.list_posts(username, limit=args.limit)
    print(f"\n[+] Most recent {len(posts)} stored posts:")
    _print_post_table(posts)
    store.close()
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    username = _strip_at(args.username)
    store = TrackerStore(args.db)
    acc = store.get_account(username)
    posts = store.list_posts(username, limit=args.limit or 1000)
    report = analyze_account(acc, posts)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    store.close()
    return 0


def cmd_parse_file(args: argparse.Namespace) -> int:
    """Parse a saved HTML file. Useful when the user can fetch the page in a
    browser (via DevTools 'Save Page As') but the script can't reach Threads
    directly — for instance when running behind a corporate firewall."""
    username = _strip_at(args.username)
    html = Path(args.html).read_text(encoding="utf-8")
    profile, posts = parse_profile_html(html, username)
    if not profile:
        print("[x] No profile object found in file.", file=sys.stderr)
        return 3
    print(
        f"[+] Profile: user_id={profile.user_id} full_name={profile.full_name!r} "
        f"followers={profile.follower_count}"
    )
    print(f"[+] Posts: {len(posts)}")
    if args.store:
        store = TrackerStore(args.db)
        store.upsert_account(profile)
        counts = store.upsert_posts(posts)
        print(
            f"[+] Stored: new={counts['new']} updated={counts['updated']} "
            f"snapshots={counts['snapshots']}"
        )
        store.close()
    if args.show:
        _print_post_table(posts[: args.show])
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    username = _strip_at(args.username)
    store = TrackerStore(args.db)
    posts = store.list_posts(username, limit=args.limit or 1000)
    out_path = Path(args.out)
    if out_path.suffix.lower() == ".csv":
        import csv

        with open(out_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "pk",
                    "code",
                    "url",
                    "taken_at",
                    "text",
                    "like_count",
                    "reply_count",
                    "repost_count",
                    "image_urls",
                ]
            )
            for p in posts:
                ts = p.get("taken_at")
                w.writerow(
                    [
                        p.get("pk"),
                        p.get("code"),
                        f"https://www.threads.com/@{p.get('username')}/post/{p.get('code')}"
                        if p.get("username") and p.get("code")
                        else "",
                        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else "",
                        p.get("text"),
                        p.get("like_count"),
                        p.get("reply_count"),
                        p.get("repost_count"),
                        ";".join(p.get("image_urls") or []),
                    ]
                )
    else:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(posts, f, ensure_ascii=False, indent=2, default=str)
    print(f"[+] Wrote {len(posts)} posts to {out_path}")
    store.close()
    return 0


def _print_post_table(posts: list) -> None:
    for p in posts:
        # Posts can come in as ThreadPost dataclasses or plain dicts (from store).
        if hasattr(p, "to_dict"):
            p = p.to_dict()
        ts = p.get("taken_at")
        ts_str = (
            datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
            if ts
            else "        ?       "
        )
        text = (p.get("text") or "").replace("\n", " ").strip()
        if len(text) > 90:
            text = text[:87] + "..."
        media = ""
        if p.get("image_urls"):
            media += f" [img×{len(p['image_urls'])}]"
        if p.get("video_urls"):
            media += f" [video×{len(p['video_urls'])}]"
        print(
            f"  {ts_str}  ♥{p.get('like_count', 0):>5}  💬{p.get('reply_count', 0):>4}"
            f"  ↻{p.get('repost_count', 0):>3}  {text}{media}"
        )


def _print_report(r: dict) -> None:
    acc = r.get("account") or {}
    print("=" * 70)
    print(f"Account @{acc.get('username') or '?'}  ({acc.get('full_name') or ''})")
    print(f"  followers={acc.get('follower_count')}  following={acc.get('following_count')}")
    if acc.get("biography"):
        print(f"  bio: {acc['biography'][:200]}")
    print("-" * 70)
    print(f"Posts tracked: {r['post_count']}")
    if r.get("note"):
        print(r["note"])
        return
    print(f"Window: {r.get('first_post')} → {r.get('last_post')}  ({r.get('span_days')} days)")
    print(f"Cadence: ~{r.get('posts_per_week')} posts/week")
    eng = r.get("engagement", {})
    likes = eng.get("likes", {})
    print(
        f"Likes:   mean={likes.get('mean')}  median={likes.get('median')}  "
        f"max={likes.get('max')}  total={likes.get('total')}"
    )
    replies = eng.get("replies", {})
    print(f"Replies: mean={replies.get('mean')}  max={replies.get('max')}  total={replies.get('total')}")
    content = r.get("content", {})
    print(
        f"Content: avg_chars={content.get('avg_chars')}  median={content.get('median_chars')}  "
        f"media_share={content.get('media_share')}  url_share={content.get('url_share')}"
    )
    print("-" * 70)
    print("Top hashtags: ", ", ".join(f"#{t}({n})" for t, n in r.get("top_hashtags") or []) or "—")
    print("Top mentions: ", ", ".join(f"@{m}({n})" for m, n in r.get("top_mentions") or []) or "—")
    print(
        "Top words   : ",
        ", ".join(f"{w}({n})" for w, n in (r.get("top_words") or [])[:10]) or "—",
    )
    print("Posting hours:", " ".join(f"{h:02d}:{n}" for h, n in r.get("hour_distribution") or []))
    print("Weekdays    :", " ".join(f"{d[:3]}:{n}" for d, n in r.get("weekday_distribution") or []))
    print("-" * 70)
    print("Top posts by likes:")
    for tp in r.get("top_posts_by_likes") or []:
        print(
            f"  ♥{tp.get('like_count'):>5}  💬{tp.get('reply_count'):>4}  "
            f"{tp.get('taken_at') or '?'}  {tp.get('url') or ''}"
        )
        print(f"      {tp.get('text_preview')}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="threads-tracker",
        description="Fetch, store, and analyse a Threads account's public posts.",
    )
    p.add_argument("--db", default=os.environ.get("THREADS_DB", "data/threads.db"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("track", help="Fetch latest profile + posts from threads.com")
    sp.add_argument("username")
    sp.add_argument("--graphql", action="store_true", help="Also call the GraphQL endpoint for more history")
    sp.add_argument("--dump-dir", help="Save raw responses here for debugging")
    sp.add_argument("--show", type=int, default=10, help="Print N posts after fetching")
    sp.set_defaults(func=cmd_track)

    sp = sub.add_parser("show", help="Show stored account + posts")
    sp.add_argument("username")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("analyze", help="Print analysis report")
    sp.add_argument("username")
    sp.add_argument("--limit", type=int, default=500)
    sp.add_argument("--json", action="store_true", help="Print as JSON instead of formatted text")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser(
        "parse-file",
        help="Parse a locally-saved profile HTML file (e.g. saved from a browser)",
    )
    sp.add_argument("username")
    sp.add_argument("html", help="Path to saved HTML file")
    sp.add_argument("--store", action="store_true", help="Also persist to DB")
    sp.add_argument("--show", type=int, default=20)
    sp.set_defaults(func=cmd_parse_file)

    sp = sub.add_parser("export", help="Dump stored posts to JSON or CSV")
    sp.add_argument("username")
    sp.add_argument("out", help="Output path (.csv or .json)")
    sp.add_argument("--limit", type=int, default=0)
    sp.set_defaults(func=cmd_export)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
