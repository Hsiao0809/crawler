"""Stock-focused analysis: extract mentions per post, aggregate by stock,
attach fundamentals, produce a report.

Compatible JSON shape with Hsiao0809/threads-stock-watch's `data/latest.json`
so downstream tools can swap implementations.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .fundamentals import with_derived
from .stock_extractor import Mention, StockEntry, find_mentions

log = logging.getLogger(__name__)


def analyze_posts_for_stocks(
    posts: list[dict[str, Any]],
    universe: list[StockEntry],
    fundamentals: dict[str, dict[str, Any]] | None = None,
    account: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the full stock-analysis report.

    `posts` is the list of dicts emitted by storage.list_posts() (so any
    persisted post can go through this, whether fetched by HTTP or browser).
    """
    fundamentals = fundamentals or {}
    analyzed_posts: list[dict[str, Any]] = []
    for idx, p in enumerate(posts):
        text = p.get("text") or ""
        mentions = find_mentions(text, universe)
        analyzed_posts.append(
            {
                "index": idx,
                "pk": p.get("pk"),
                "url": _post_url(p),
                "taken_at": _iso(p.get("taken_at")),
                "text": text,
                "like_count": p.get("like_count"),
                "reply_count": p.get("reply_count"),
                "mentions": [_mention_to_dict(m) for m in mentions],
            }
        )

    stocks = _aggregate(analyzed_posts, fundamentals)
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "account": account or {},
        "summary": _summarize(analyzed_posts, stocks),
        "stocks": stocks,
        "posts": analyzed_posts,
    }


def _mention_to_dict(m: Mention) -> dict[str, Any]:
    return {
        "code": m.code,
        "name": m.name,
        "market": m.market,
        "aliases": m.aliases,
        "actions": m.actions,
        "contexts": m.contexts,
    }


def _post_url(p: dict[str, Any]) -> str | None:
    user = p.get("username")
    code = p.get("code")
    if user and code:
        return f"https://www.threads.com/@{user}/post/{code}"
    return None


def _iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _aggregate(
    posts: list[dict[str, Any]],
    fundamentals: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_code: dict[str, dict[str, Any]] = {}
    for post in posts:
        for mention in post["mentions"]:
            key = mention["code"] or mention["name"]
            bucket = by_code.setdefault(
                key,
                {
                    "code": mention["code"],
                    "name": mention["name"],
                    "market": mention["market"],
                    "count": 0,
                    "actions": {},
                    "posts": [],
                },
            )
            bucket["count"] += 1
            for a in mention["actions"]:
                bucket["actions"][a] = bucket["actions"].get(a, 0) + 1
            bucket["posts"].append(
                {
                    "pk": post["pk"],
                    "url": post["url"],
                    "taken_at": post["taken_at"],
                    "text": post["text"][:240],
                    "actions": mention["actions"],
                    "contexts": mention["contexts"],
                }
            )

    # Sort by mention count desc.
    out = list(by_code.values())
    for entry in out:
        fund = fundamentals.get(entry["code"]) if entry["code"] else None
        entry["fundamentals"] = with_derived(fund) if fund else None
    out.sort(key=lambda e: (e["count"], len(e["actions"])), reverse=True)
    return out


def _summarize(posts: list[dict[str, Any]], stocks: list[dict[str, Any]]) -> dict[str, Any]:
    posts_with_stocks = sum(1 for p in posts if p["mentions"])
    action_totals: dict[str, int] = {}
    for s in stocks:
        for a, c in s["actions"].items():
            action_totals[a] = action_totals.get(a, 0) + c
    fundamentals_coverage = sum(1 for s in stocks if s.get("fundamentals"))
    return {
        "totalPosts": len(posts),
        "postsWithStocks": posts_with_stocks,
        "uniqueStocks": len(stocks),
        "topStocks": [
            {"code": s["code"], "name": s["name"], "count": s["count"], "actions": s["actions"]}
            for s in stocks[:10]
        ],
        "actionTotals": action_totals,
        "fundamentalsCoverage": fundamentals_coverage,
    }


# ---------------------------------------------------------------------------
# Pretty-print
# ---------------------------------------------------------------------------


ACTION_LABELS = {
    "buy": "買",
    "sell": "賣",
    "limitUp": "漲停",
    "limitDown": "跌停",
    "watch": "觀察",
    "regret": "後悔",
}


def format_report(report: dict[str, Any], top_n: int = 20) -> str:
    lines: list[str] = []
    summary = report.get("summary", {})
    acc = report.get("account") or {}
    lines.append("=" * 78)
    lines.append(
        f"@{acc.get('username') or '?'}  ({acc.get('full_name') or ''})    "
        f"generated {report.get('generatedAt')}"
    )
    lines.append("-" * 78)
    lines.append(
        f"posts={summary.get('totalPosts')}  with-stocks={summary.get('postsWithStocks')}"
        f"  unique-stocks={summary.get('uniqueStocks')}"
        f"  fundamentals-covered={summary.get('fundamentalsCoverage')}"
    )
    act = summary.get("actionTotals") or {}
    if act:
        breakdown = "  ".join(f"{ACTION_LABELS.get(k, k)}:{v}" for k, v in act.items())
        lines.append(f"actions: {breakdown}")
    lines.append("-" * 78)
    lines.append(f"Top {top_n} stocks:")
    lines.append(
        f"{'CODE':<6} {'NAME':<14} {'CNT':<4} {'PRICE':<7} {'P/E':<6} {'P/B':<5}"
        f" {'YoY%':<7} {'GM%':<6} {'ROE%':<6} ACTIONS"
    )
    for s in report.get("stocks", [])[:top_n]:
        f = s.get("fundamentals") or {}
        actions_str = ",".join(
            f"{ACTION_LABELS.get(a, a)}×{n}" for a, n in (s.get("actions") or {}).items()
        )
        lines.append(
            f"{(s.get('code') or '-'):<6} {(s.get('name') or '?')[:13]:<14}"
            f" {s.get('count'):<4} {_fmt(f.get('price')):<7} {_fmt(f.get('peRatio')):<6}"
            f" {_fmt(f.get('pbRatio')):<5} {_fmt(f.get('revenueYoY')):<7}"
            f" {_fmt(f.get('grossMargin')):<6} {_fmt(f.get('roe')):<6} {actions_str}"
        )
    lines.append("-" * 78)
    lines.append("Recent mentions (text + actions):")
    for p in report.get("posts", [])[:10]:
        if not p["mentions"]:
            continue
        mt = ", ".join(
            f"{m.get('code') or ''}{m.get('name') or ''}["
            + ",".join(ACTION_LABELS.get(a, a) for a in m.get("actions") or [])
            + "]"
            for m in p["mentions"]
        )
        lines.append(f"  [{p.get('taken_at') or '?'}] {mt}")
        lines.append(f"    {p['text'][:120].replace(chr(10), ' ')}")
    return "\n".join(lines)


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)
