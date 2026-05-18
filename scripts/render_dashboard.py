#!/usr/bin/env python3
"""Render data/latest.json into public/index.html.

Tiny single-file dashboard: top-mentioned stocks table with action breakdown
and fundamentals, plus the post timeline below.
"""
from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ACTION_LABELS = {
    "buy": "買",
    "sell": "賣",
    "limitUp": "漲停",
    "limitDown": "跌停",
    "watch": "觀察",
    "regret": "後悔",
}
ACTION_COLORS = {
    "buy": "#0a7f3f",
    "sell": "#b8002b",
    "limitUp": "#0a7f3f",
    "limitDown": "#b8002b",
    "watch": "#6a5cff",
    "regret": "#a05a00",
}


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.2f}"
    return _esc(v)


def render(report: dict) -> str:
    summary = report.get("summary", {})
    acc = report.get("account") or {}
    stocks = report.get("stocks") or []
    posts = report.get("posts") or []
    generated = report.get("generatedAt") or datetime.now(timezone.utc).isoformat()

    def render_actions(actions: dict) -> str:
        chips = []
        for k, n in actions.items():
            label = ACTION_LABELS.get(k, k)
            color = ACTION_COLORS.get(k, "#444")
            chips.append(
                f'<span class="chip" style="background:{color}">{_esc(label)}×{n}</span>'
            )
        return "".join(chips)

    stock_rows = []
    for s in stocks:
        f = s.get("fundamentals") or {}
        url_query = s.get("code") or s.get("name")
        link = f"https://tw.stock.yahoo.com/quote/{url_query}" if s.get("code") else "#"
        stock_rows.append(
            f"""
            <tr>
              <td><a href="{_esc(link)}" target="_blank">{_esc(s.get('code') or '—')}</a></td>
              <td>{_esc(s.get('name') or '?')}</td>
              <td>{_esc(s.get('market') or '')}</td>
              <td class="num">{s.get('count')}</td>
              <td>{render_actions(s.get('actions') or {})}</td>
              <td class="num">{_fmt(f.get('price'))}</td>
              <td class="num">{_fmt(f.get('peRatio'))}</td>
              <td class="num">{_fmt(f.get('pbRatio'))}</td>
              <td class="num">{_fmt(f.get('dividendYield'))}</td>
              <td class="num">{_fmt(f.get('eps'))}</td>
              <td class="num">{_fmt(f.get('revenueYoY'))}</td>
              <td class="num">{_fmt(f.get('grossMargin'))}</td>
              <td class="num">{_fmt(f.get('roe'))}</td>
              <td class="num">{_fmt(f.get('debtRatio'))}</td>
            </tr>
            """
        )

    post_cards = []
    for p in posts[:50]:
        mentions = p.get("mentions") or []
        if not mentions:
            continue
        chips = []
        for m in mentions:
            ms = (m.get("code") or "") + (m.get("name") or "")
            acts = render_actions({a: 1 for a in m.get("actions") or []})
            chips.append(f'<span class="stock-chip">{_esc(ms)}{acts}</span>')
        when = p.get("taken_at") or "—"
        post_url = p.get("url") or "#"
        text = _esc(p.get("text") or "")
        post_cards.append(
            f"""
            <article class="post">
              <header>
                <a href="{_esc(post_url)}" target="_blank">{_esc(when)}</a>
                <div class="chips">{''.join(chips)}</div>
              </header>
              <p>{text}</p>
            </article>
            """
        )

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@{_esc(acc.get('username') or 'threads')} · Stock Watch</title>
<style>
  body {{ font: 14px/1.5 -apple-system, "Noto Sans TC", system-ui, sans-serif; margin: 0; background:#fafafa; color:#111; }}
  header.top {{ background:#111; color:#fafafa; padding: 16px 24px; }}
  header.top h1 {{ margin:0; font-size:20px; }}
  header.top .sub {{ opacity:0.7; font-size:13px; }}
  main {{ max-width: 1280px; margin: 0 auto; padding: 16px 24px 64px; }}
  .stats {{ display:flex; gap: 16px; flex-wrap:wrap; margin: 12px 0 24px; }}
  .stat {{ background:#fff; border:1px solid #eee; border-radius:8px; padding:10px 14px; min-width:120px; }}
  .stat .v {{ font-size:22px; font-weight:600; }}
  .stat .k {{ font-size:11px; color:#777; text-transform:uppercase; letter-spacing:0.05em; }}
  table {{ width:100%; border-collapse:collapse; background:#fff; font-size:13px; }}
  th, td {{ padding:8px 10px; border-bottom:1px solid #eee; text-align:left; }}
  th {{ background:#f3f3f3; font-weight:600; position:sticky; top:0; }}
  td.num {{ text-align:right; font-variant-numeric: tabular-nums; }}
  .chip {{ display:inline-block; color:#fff; padding:2px 6px; border-radius:4px; font-size:11px; margin-right:4px; }}
  .stock-chip {{ display:inline-flex; gap:4px; align-items:center; background:#f0f0f0; padding:2px 6px; border-radius:4px; margin-right:6px; }}
  .post {{ background:#fff; border:1px solid #eee; border-radius:8px; padding:12px 14px; margin: 8px 0; }}
  .post header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; font-size:12px; color:#666; }}
  .post p {{ margin: 0; white-space: pre-wrap; }}
  .chips {{ display:flex; flex-wrap:wrap; gap:4px; }}
</style>
</head>
<body>
<header class="top">
  <h1>@{_esc(acc.get('username') or '?')} · Threads Stock Watch</h1>
  <div class="sub">{_esc(acc.get('full_name') or '')} — generated {_esc(generated)}</div>
</header>
<main>
  <section class="stats">
    <div class="stat"><div class="v">{summary.get('totalPosts', 0)}</div><div class="k">Posts</div></div>
    <div class="stat"><div class="v">{summary.get('postsWithStocks', 0)}</div><div class="k">With stocks</div></div>
    <div class="stat"><div class="v">{summary.get('uniqueStocks', 0)}</div><div class="k">Unique stocks</div></div>
    <div class="stat"><div class="v">{summary.get('fundamentalsCoverage', 0)}</div><div class="k">Fundamentals</div></div>
  </section>

  <h2>Top stocks</h2>
  <div style="overflow-x:auto">
  <table>
    <thead><tr>
      <th>代碼</th><th>名稱</th><th>市場</th><th>提及</th><th>動作</th>
      <th>收盤</th><th>P/E</th><th>P/B</th><th>殖利率</th><th>EPS</th>
      <th>營收YoY%</th><th>毛利率%</th><th>ROE%</th><th>負債%</th>
    </tr></thead>
    <tbody>{''.join(stock_rows)}</tbody>
  </table>
  </div>

  <h2>Recent posts</h2>
  {''.join(post_cards)}
</main>
</body>
</html>
"""


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: render_dashboard.py <input.json> <output.html>", file=sys.stderr)
        return 1
    report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    out = render(report)
    out_path = Path(argv[2])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out, encoding="utf-8")
    print(f"wrote {out_path} ({len(out):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
