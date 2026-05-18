"""Extract Taiwan stock mentions and action tags from post text.

Two-pass match:
  1. Build a "universe" of known stocks (code+name+aliases). We seed this from
     a bundled JSON, but at runtime we refresh from TWSE/TPEx OpenAPI for full
     coverage of every listed company.
  2. For each post, scan the text for any alias. Resolve overlapping matches
     by preferring the longer alias (so "貿聯-KY" beats "貿聯").

After we know which stocks were mentioned, we walk a window of context around
each mention and look for action keywords (buy / sell / limit-up / limit-down
/ watch / regret), only attaching an action when the keyword lives in the
same sentence as the mention.

This mirrors the taxonomy used by Hsiao0809/threads-stock-watch.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


# Stock-code patterns: pure 4-digit, optionally 5-6 digit for warrants / ETFs.
TW_CODE_RE = re.compile(r"\b(\d{4,6})\b")

# Action keyword regexes, applied within a context window around each mention.
BUY_RE = re.compile(r"(買|進場|加碼|低接|建倉|抄底|承接|攤平)")
SELL_RE = re.compile(r"(賣|出場|出掉|賣飛|減碼|停利|出清)")
LIMIT_UP_RE = re.compile(r"漲停")
LIMIT_DOWN_RE = re.compile(r"跌停")
WATCH_RE = re.compile(r"(看好|觀察|佈局|布局|等待|驗證|追蹤|留意)")
REGRET_RE = re.compile(r"(可惜|哭|唉|捨不得|賣飛|早知道|跑掉|後悔)")

# Splitters for cutting text into "near a mention" context windows.
LEFT_BREAKS = "。！？!?\n；"
RIGHT_BREAKS = "。！？!?\n；"


@dataclass
class StockEntry:
    code: str
    name: str
    market: str
    aliases: list[str] = field(default_factory=list)

    def all_aliases(self) -> list[str]:
        out = {self.name, *self.aliases}
        for pattern in ("-KY", "*", "－KY", "-ky"):
            if self.name.endswith(pattern):
                out.add(self.name.rsplit(pattern, 1)[0])
        # Code itself is searchable
        if self.code:
            out.add(self.code)
        return sorted({a for a in out if a and len(a) >= 2}, key=len, reverse=True)


@dataclass
class Mention:
    code: str
    name: str
    market: str
    aliases: list[str]
    contexts: list[str]
    actions: list[str]


def load_universe(seed_path: str | Path | None = None) -> list[StockEntry]:
    """Load the bundled seed universe; users can replace it at runtime."""
    seed_path = Path(seed_path or Path(__file__).parent / "stock_aliases_seed.json")
    if not seed_path.exists():
        return []
    rows = json.loads(seed_path.read_text(encoding="utf-8"))
    out = []
    for r in rows:
        out.append(
            StockEntry(
                code=r.get("code", ""),
                name=r.get("name", ""),
                market=r.get("market", "TW"),
                aliases=list(r.get("aliases") or []),
            )
        )
    return out


def merge_universe(*pools: Iterable[StockEntry]) -> list[StockEntry]:
    """Combine universes from multiple sources, dedup by (market, code|name)."""
    by_key: dict[tuple[str, str], StockEntry] = {}
    for pool in pools:
        for entry in pool:
            key = (entry.market, entry.code or entry.name)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = StockEntry(
                    code=entry.code,
                    name=entry.name,
                    market=entry.market,
                    aliases=list(set(entry.aliases)),
                )
            else:
                existing.aliases = sorted({*existing.aliases, *entry.aliases})
    # Sort so longer aliases match first.
    result = list(by_key.values())
    result.sort(key=lambda e: sum(len(a) for a in e.aliases), reverse=True)
    return result


def _is_excluded_context(text: str, start: int, end: int) -> bool:
    """Skip price-format matches like '聯亞-12元' where '聯亞' looks like a mention
    but is actually a dash-separated label."""
    before = text[max(0, start - 3) : start]
    after = text[end : end + 8]
    if re.search(r"[-－—]\s*$", before) and re.search(r"^\s*\d+\s*元", after):
        return True
    return False


def _context_window(text: str, start: int, end: int, span: int = 80) -> str:
    left = max(0, start - span)
    right = min(len(text), end + span)
    # Snap to sentence boundary if we can find one inside the window.
    for i in range(start, max(0, start - 120), -1):
        if text[i - 1] in LEFT_BREAKS:
            left = i
            break
    for i in range(end, min(len(text), end + 120)):
        if text[i] in RIGHT_BREAKS:
            right = i + 1
            break
    return text[left:right].strip()


def find_mentions(text: str, universe: list[StockEntry]) -> list[Mention]:
    """Return stock mentions in `text`, with contexts and inferred actions."""
    if not text:
        return []
    matches = []
    for stock in universe:
        for alias in stock.all_aliases():
            # Skip pure-code aliases of <4 digits (too noisy); allow the canonical
            # code (>=4 digits) since stock-influencer posts usually do include it.
            if alias.isdigit() and len(alias) < 4:
                continue
            start = -1
            while True:
                start = text.find(alias, start + 1)
                if start < 0:
                    break
                end = start + len(alias)
                if _is_excluded_context(text, start, end):
                    continue
                # If alias is a number, require it to not be part of a longer number.
                if alias.isdigit():
                    if start > 0 and text[start - 1].isdigit():
                        continue
                    if end < len(text) and text[end].isdigit():
                        continue
                matches.append((start, end, alias, stock))

    # Resolve overlaps: prefer the longest alias.
    matches.sort(key=lambda m: (m[1] - m[0]), reverse=True)
    chosen: list[tuple[int, int, str, StockEntry]] = []
    for m in matches:
        if any(not (m[1] <= c[0] or m[0] >= c[1]) for c in chosen):
            continue
        chosen.append(m)

    # Group by stock.
    by_code: dict[str, dict[str, Any]] = {}
    for start, end, alias, stock in sorted(chosen, key=lambda m: m[0]):
        key = stock.code or stock.name
        bucket = by_code.setdefault(
            key,
            {"stock": stock, "aliases": set(), "contexts": []},
        )
        bucket["aliases"].add(alias)
        bucket["contexts"].append(_context_window(text, start, end))

    out: list[Mention] = []
    for entry in by_code.values():
        stock: StockEntry = entry["stock"]
        contexts = list(dict.fromkeys(entry["contexts"]))[:5]
        actions = classify_actions(contexts, list(entry["aliases"]))
        out.append(
            Mention(
                code=stock.code,
                name=stock.name,
                market=stock.market,
                aliases=sorted(entry["aliases"], key=len, reverse=True),
                contexts=contexts,
                actions=actions,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Action classification
# ---------------------------------------------------------------------------


def classify_actions(contexts: list[str], aliases: list[str]) -> list[str]:
    """Tag a list of contexts with buy/sell/limitUp/limitDown/watch/regret."""
    joined = " ".join(contexts)
    actions: list[str] = []

    def near_alias(regex: re.Pattern, distance: int = 60) -> bool:
        for ctx in contexts:
            for alias in aliases:
                alias_positions = _all_positions(ctx, alias)
                if not alias_positions:
                    continue
                for m in regex.finditer(ctx):
                    kw = m.group(0)
                    # "買股" / "買股票" is generic — skip.
                    if kw == "買" and ctx[m.end(): m.end() + 2] in ("股", "股票"):
                        continue
                    kw_start, kw_end = m.start(), m.end()
                    for ap in alias_positions:
                        ap_end = ap + len(alias)
                        if (
                            abs(ap - kw_end) <= distance
                            or abs(kw_start - ap_end) <= distance
                        ):
                            return True
        return False

    def action_word_applies(word: str) -> bool:
        """For limit-up/down: require an alias to appear in the same sentence
        before the word, since '今天大盤一堆跌停, 但xxx撐住' shouldn't tag xxx
        as limitDown."""
        for ctx in contexts:
            cursor = -1
            while True:
                cursor = ctx.find(word, cursor + 1)
                if cursor < 0:
                    break
                # Cut at preceding sentence boundary.
                seg_start = 0
                for break_ch in LEFT_BREAKS:
                    pos = ctx.rfind(break_ch, 0, cursor)
                    if pos > seg_start:
                        seg_start = pos + 1
                seg = ctx[seg_start:cursor]
                if any(alias in seg for alias in aliases):
                    return True
        return False

    if near_alias(BUY_RE):
        actions.append("buy")
    if near_alias(SELL_RE):
        actions.append("sell")
    if action_word_applies("漲停"):
        actions.append("limitUp")
    if action_word_applies("跌停"):
        actions.append("limitDown")
    if WATCH_RE.search(joined):
        actions.append("watch")
    if REGRET_RE.search(joined):
        actions.append("regret")
    return actions


def _all_positions(text: str, needle: str) -> list[int]:
    out = []
    cursor = -1
    while True:
        cursor = text.find(needle, cursor + 1)
        if cursor < 0:
            return out
        out.append(cursor)
