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
from .stock_analyzer import analyze_posts_for_stocks, format_report
from .stock_extractor import load_universe, merge_universe

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

    if args.browser:
        try:
            from .browser import collect_graphql_bodies, fetch_with_browser
        except ImportError as exc:
            print(f"[x] cannot import browser fetcher: {exc}", file=sys.stderr)
            return 2

        print(f"[*] Launching Chromium for @{username} ...", flush=True)
        try:
            br = fetch_with_browser(
                username,
                scroll_rounds=args.scroll_rounds,
                settle_ms=args.settle_ms,
                post_pages=args.post_pages,
                headless=not args.headful,
                timeout_ms=args.timeout_ms,
                user_data_dir=args.profile_dir,
            )
        except Exception as exc:
            print(f"[x] browser fetch failed: {exc}", file=sys.stderr)
            return 2

        print(
            f"[*] Loaded {br.final_url}  title={br.title!r}  "
            f"html={len(br.html):,} bytes  graphql-responses={len(br.graphql_responses)}"
        )
        profile, posts = parse_profile_html(br.html, username)
        # Merge in posts from captured GraphQL bodies (covers infinite scroll).
        gql_bodies = collect_graphql_bodies(br)
        if gql_bodies:
            print(f"[*] Parsing {len(gql_bodies)} GraphQL JSON bodies ...")
            for body in gql_bodies:
                extra = parse_graphql_response(body, username_hint=username)
                seen = {p.pk for p in posts}
                for p in extra:
                    if p.pk not in seen:
                        posts.append(p)
                        seen.add(p.pk)

        if args.dump_dir:
            _dump_browser_result(args.dump_dir, username, br)
    else:
        client = ThreadsClient(debug_dump_dir=args.dump_dir)
        print(f"[*] Fetching profile page for @{username} (HTTP) ...", flush=True)
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


def cmd_stocks(args: argparse.Namespace) -> int:
    username = _strip_at(args.username)
    store = TrackerStore(args.db)
    acc = store.get_account(username)
    posts = store.list_posts(username, limit=args.limit)
    if not posts:
        print(f"[x] No posts stored for @{username}. Run `track` first.", file=sys.stderr)
        store.close()
        return 1

    # Universe: bundled seed → optionally refresh from TWSE/TPEx.
    universe = load_universe()
    print(f"[*] Universe (seed): {len(universe)} stocks")
    if args.refresh_universe:
        from .fundamentals import fetch_universe, load_cached, save_cached

        try:
            fresh = fetch_universe()
            universe = merge_universe(universe, fresh)
            save_cached(args.universe_cache, [vars(e) for e in universe])
            print(f"[+] Refreshed universe: {len(universe)} stocks (cached at {args.universe_cache})")
        except Exception as exc:
            print(f"[!] Universe refresh failed, using seed only: {exc}", file=sys.stderr)
    else:
        from .fundamentals import load_cached

        cached = load_cached(args.universe_cache)
        if cached:
            from .stock_extractor import StockEntry

            cached_entries = [StockEntry(**r) for r in cached]
            universe = merge_universe(universe, cached_entries)
            print(f"[+] Loaded cached universe: {len(universe)} stocks")

    # Fundamentals: load from cache, optionally refresh.
    fundamentals: dict[str, dict] = {}
    if args.refresh_fundamentals:
        from .fundamentals import fetch_fundamentals, save_cached

        try:
            fundamentals = fetch_fundamentals()
            save_cached(args.fundamentals_cache, fundamentals)
            print(f"[+] Refreshed fundamentals for {len(fundamentals)} codes")
        except Exception as exc:
            print(f"[!] Fundamentals refresh failed: {exc}", file=sys.stderr)
    else:
        from .fundamentals import load_cached

        cached = load_cached(args.fundamentals_cache)
        if cached:
            fundamentals = cached
            print(f"[+] Loaded cached fundamentals: {len(fundamentals)} codes")

    print(f"[*] Analysing {len(posts)} posts ...")
    report = analyze_posts_for_stocks(posts, universe, fundamentals, account=acc)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[+] Wrote analysis to {args.out}")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report, top_n=args.top))

    store.close()
    return 0


def _dump_browser_result(dump_dir: str, username: str, br) -> None:
    """Save browser-fetch artifacts for offline inspection."""
    import os

    os.makedirs(dump_dir, exist_ok=True)
    with open(os.path.join(dump_dir, f"browser_{username}.html"), "w", encoding="utf-8") as f:
        f.write(br.html)
    with open(os.path.join(dump_dir, f"browser_{username}_graphql.json"), "w", encoding="utf-8") as f:
        json.dump(br.graphql_responses, f, ensure_ascii=False, indent=2)


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
    # Shared parser so the same flags work either before or after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help="(also accepted as global flag)")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("track", parents=[common], help="Fetch latest profile + posts from threads.com")
    sp.add_argument("username")
    sp.add_argument("--graphql", action="store_true", help="Also call the GraphQL endpoint for more history (HTTP mode)")
    sp.add_argument("--dump-dir", help="Save raw responses here for debugging")
    sp.add_argument("--show", type=int, default=10, help="Print N posts after fetching")
    sp.add_argument(
        "--browser",
        action="store_true",
        help="Drive Playwright Chromium instead of plain HTTP (more robust against Threads anti-scraping)",
    )
    sp.add_argument("--scroll-rounds", type=int, default=4, help="Browser mode: how many times to scroll the timeline")
    sp.add_argument("--settle-ms", type=int, default=4000, help="Browser mode: wait after navigate / scroll")
    sp.add_argument("--post-pages", type=int, default=0, help="Browser mode: also visit N individual post pages")
    sp.add_argument("--headful", action="store_true", help="Browser mode: show the window")
    sp.add_argument("--timeout-ms", type=int, default=45000)
    sp.add_argument("--profile-dir", help="Browser mode: persistent user-data directory")
    sp.set_defaults(func=cmd_track)

    sp = sub.add_parser("show", parents=[common], help="Show stored account + posts")
    sp.add_argument("username")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("analyze", parents=[common], help="Print analysis report")
    sp.add_argument("username")
    sp.add_argument("--limit", type=int, default=500)
    sp.add_argument("--json", action="store_true", help="Print as JSON instead of formatted text")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser(
        "parse-file",
        parents=[common],
        help="Parse a locally-saved profile HTML file (e.g. saved from a browser)",
    )
    sp.add_argument("username")
    sp.add_argument("html", help="Path to saved HTML file")
    sp.add_argument("--store", action="store_true", help="Also persist to DB")
    sp.add_argument("--show", type=int, default=20)
    sp.set_defaults(func=cmd_parse_file)

    sp = sub.add_parser("export", parents=[common], help="Dump stored posts to JSON or CSV")
    sp.add_argument("username")
    sp.add_argument("out", help="Output path (.csv or .json)")
    sp.add_argument("--limit", type=int, default=0)
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser(
        "stocks",
        parents=[common],
        help="Extract stock mentions + action tags + TWSE/TPEx fundamentals",
    )
    sp.add_argument("username")
    sp.add_argument("--limit", type=int, default=500, help="How many recent posts to analyse")
    sp.add_argument(
        "--refresh-universe",
        action="store_true",
        help="Fetch full universe from TWSE/TPEx OpenAPI (else use bundled seed)",
    )
    sp.add_argument(
        "--refresh-fundamentals",
        action="store_true",
        help="Fetch price/P/E/EPS/revenue/income/balance from TWSE/TPEx OpenAPI",
    )
    sp.add_argument(
        "--universe-cache",
        default="data/universe.json",
        help="Where to cache the universe across runs",
    )
    sp.add_argument(
        "--fundamentals-cache",
        default="data/fundamentals.json",
        help="Where to cache fundamentals across runs",
    )
    sp.add_argument("--out", help="Also write the analysis JSON to this path")
    sp.add_argument("--json", action="store_true", help="Print JSON instead of formatted report")
    sp.add_argument("--top", type=int, default=20)
    sp.set_defaults(func=cmd_stocks)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
