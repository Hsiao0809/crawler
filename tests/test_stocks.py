"""Tests for stock extraction + analysis."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from threads_tracker.stock_analyzer import analyze_posts_for_stocks
from threads_tracker.stock_extractor import (
    StockEntry,
    classify_actions,
    find_mentions,
    load_universe,
)


def _universe() -> list[StockEntry]:
    return [
        StockEntry(code="2330", name="台積電", market="TWSE", aliases=["台積電", "台積"]),
        StockEntry(code="3008", name="大立光", market="TWSE", aliases=["大立光"]),
        StockEntry(code="3665", name="貿聯-KY", market="TWSE", aliases=["貿聯-KY", "貿聯"]),
        StockEntry(code="3037", name="欣興", market="TWSE", aliases=["欣興"]),
        StockEntry(code="5439", name="高技", market="TPEx", aliases=["高技"]),
    ]


def test_seed_universe_loads() -> None:
    seed = load_universe()
    assert len(seed) >= 5, "bundled seed should have at least a handful of stocks"
    codes = {e.code for e in seed}
    assert "2330" in codes, "台積電 should be in the seed"


def test_finds_simple_mentions() -> None:
    text = "今天 2330 台積電 大爆發，買進 5 張，目標價 1200。"
    mentions = find_mentions(text, _universe())
    assert any(m.code == "2330" for m in mentions)
    # Should infer 'buy' from "買進".
    tsmc = next(m for m in mentions if m.code == "2330")
    assert "buy" in tsmc.actions


def test_action_classification_buy_and_sell() -> None:
    contexts = ["決定加碼 台積電 2330，看好它今年。"]
    actions = classify_actions(contexts, aliases=["台積電", "台積", "2330"])
    assert "buy" in actions
    assert "watch" in actions

    contexts = ["昨天把 大立光 出掉了，後悔莫及。"]
    actions = classify_actions(contexts, aliases=["大立光"])
    assert "sell" in actions
    assert "regret" in actions


def test_limit_up_down_attached_to_correct_stock() -> None:
    # 漲停 should bind to 貿聯-KY since they're in the same sentence;
    # the second sentence has 跌停 but binds to 欣興.
    text = "貿聯-KY 今天漲停太爽了。欣興則是跌停。"
    mentions = find_mentions(text, _universe())
    by_code = {m.code: m for m in mentions}
    assert "limitUp" in by_code["3665"].actions
    assert "limitDown" in by_code["3037"].actions
    assert "limitDown" not in by_code["3665"].actions
    assert "limitUp" not in by_code["3037"].actions


def test_longer_alias_wins_over_shorter() -> None:
    # "貿聯-KY" should match instead of double-matching "貿聯" too.
    text = "貿聯-KY 持續看好"
    mentions = find_mentions(text, _universe())
    assert len(mentions) == 1
    assert mentions[0].code == "3665"


def test_full_report_against_posts() -> None:
    posts = [
        {
            "pk": "1",
            "code": "abc",
            "username": "evachien.chien",
            "text": "台積電 2330 今天漲停, 加碼一張。",
            "taken_at": 1715000000,
            "like_count": 100,
            "reply_count": 10,
        },
        {
            "pk": "2",
            "code": "def",
            "username": "evachien.chien",
            "text": "大立光 賣飛了, 唉早知道再多抱。",
            "taken_at": 1715100000,
            "like_count": 80,
            "reply_count": 5,
        },
        {
            "pk": "3",
            "code": "ghi",
            "username": "evachien.chien",
            "text": "今天大盤好弱, 但 貿聯-KY 撐住, 觀察中。",
            "taken_at": 1715200000,
            "like_count": 50,
            "reply_count": 2,
        },
    ]
    fundamentals = {
        "2330": {"code": "2330", "name": "台積電", "market": "TWSE", "price": 1100.0, "peRatio": 28.5, "roe": 30.0},
    }
    report = analyze_posts_for_stocks(posts, _universe(), fundamentals, account={"username": "evachien.chien"})
    s = report["summary"]
    assert s["totalPosts"] == 3
    assert s["postsWithStocks"] == 3
    assert s["uniqueStocks"] == 3

    by_code = {st["code"]: st for st in report["stocks"]}
    assert by_code["2330"]["actions"].get("buy") == 1
    assert by_code["2330"]["actions"].get("limitUp") == 1
    assert by_code["2330"]["fundamentals"]["price"] == 1100.0
    assert by_code["3008"]["actions"].get("sell") == 1
    assert by_code["3008"]["actions"].get("regret") == 1
    assert by_code["3665"]["actions"].get("watch") == 1


if __name__ == "__main__":
    test_seed_universe_loads()
    test_finds_simple_mentions()
    test_action_classification_buy_and_sell()
    test_limit_up_down_attached_to_correct_stock()
    test_longer_alias_wins_over_shorter()
    test_full_report_against_posts()
    print("OK: all stock tests passed")
