"""Tests for the TWSE/TPEx OpenAPI parsing layer.

We mock the HTTP layer so the tests don't depend on the live endpoints, but
the field names + shapes here are the *real* ones TWSE/TPEx return — verified
against `data/fundamentals.json` produced by a successful CI run.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from threads_tracker import fundamentals as fund


def _twse_price_row(code, name, price):
    return {"Code": code, "Name": name, "ClosingPrice": str(price), "Change": "0.5", "Date": "1150518"}


def _twse_valuation_row(code, name, pe, pb, dy):
    return {"Code": code, "Name": name, "PEratio": str(pe), "PBratio": str(pb),
            "DividendYield": str(dy), "Date": "1150518"}


def _twse_eps_row(code, eps):
    return {"公司代號": code, "基本每股盈餘(元)": str(eps), "年度": "115", "季別": "1"}


def _twse_revenue_row(code, revenue, yoy):
    """The real field name uses the long prefix; we test that pattern."""
    return {
        "公司代號": code,
        "資料年月": "11504",
        "營業收入-當月營收": str(revenue),
        "營業收入-去年同月增減(%)": str(yoy),
    }


def _twse_income_row(code, revenue, gross, net):
    return {
        "公司代號": code,
        "營業收入": str(revenue),
        "營業毛利（毛損）": str(gross),
        "本期淨利（淨損）": str(net),
    }


def _twse_balance_row(code, assets, liab, equity):
    return {
        "公司代號": code,
        "資產總計": str(assets),
        "負債總計": str(liab),
        "權益總計": str(equity),
    }


def test_revenue_yoy_uses_long_field_name() -> None:
    """The bug we fixed: prior code looked for 「去年同月增減(%)」 but the real
    TWSE field is 「營業收入-去年同月增減(%)」, so revenueYoY was None for
    every code. This test guards against regression."""
    fake_responses = {
        fund.TWSE_PRICE: [_twse_price_row("2330", "台積電", 1100)],
        fund.TWSE_VALUATION: [_twse_valuation_row("2330", "台積電", 28.5, 7.0, 1.8)],
        fund.TWSE_EPS: [_twse_eps_row("2330", 38.6)],
        fund.TWSE_REVENUE: [_twse_revenue_row("2330", 300_000, 15.3)],
        fund.TPEX_PRICE: [],
        fund.TPEX_VALUATION: [],
        fund.TPEX_EPS: [],
        fund.TPEX_REVENUE: [],
    }
    # Income/balance endpoints (12 of them) return empty for this test.
    def fake_fetch(url):
        return fake_responses.get(url, [])

    with patch.object(fund, "_fetch_json", side_effect=fake_fetch):
        result = fund.fetch_fundamentals()

    row = result["2330"]
    assert row["price"] == 1100.0, row
    assert row["peRatio"] == 28.5
    assert row["pbRatio"] == 7.0
    assert row["dividendYield"] == 1.8
    assert row["eps"] == 38.6
    assert row["revenue"] == 300_000
    # ★ the regression check
    assert row["revenueYoY"] == 15.3, f"revenueYoY missing — got {row!r}"


def test_legacy_field_names_still_work() -> None:
    """If TWSE renames a field back, we accept the legacy name too."""
    legacy_row = {
        "公司代號": "2330",
        "資料年月": "11504",
        "營業收入-當月營收": "300000",
        "去年同月增減(%)": "12.0",  # legacy short form
    }

    def fake_fetch(url):
        if url == fund.TWSE_REVENUE:
            return [legacy_row]
        return []

    with patch.object(fund, "_fetch_json", side_effect=fake_fetch):
        result = fund.fetch_fundamentals()
    assert result["2330"]["revenueYoY"] == 12.0


def test_derived_roe_from_income_and_balance() -> None:
    """gross margin from income statement, debt ratio from balance sheet,
    ROE derived from net income / equity."""
    responses = {
        fund.TWSE_PRICE: [_twse_price_row("2330", "台積電", 1100)],
        fund.TWSE_INCOME.format(c="basi"): [_twse_income_row("2330", 1_000_000, 600_000, 300_000)],
        fund.TWSE_BALANCE.format(c="basi"): [_twse_balance_row("2330", 5_000_000, 2_000_000, 3_000_000)],
    }
    # Other categories return empty.
    def fake_fetch(url):
        return responses.get(url, [])

    with patch.object(fund, "_fetch_json", side_effect=fake_fetch):
        result = fund.fetch_fundamentals()

    row = fund.with_derived(result["2330"])
    assert row["grossMargin"] == 60.0      # 600k / 1M
    assert row["debtRatio"] == 40.0        # 2M / 5M
    assert row["roe"] == 10.0              # 300k / 3M


def test_tpex_revenue_uses_securities_company_code() -> None:
    """Regression for audit #30: TPEx revenue endpoint emits the English key
    SecuritiesCompanyCode, not 公司代號. Prior code only looked for the
    Chinese key so 6,656 TPEx rows had revenueYoY=None.
    """
    tpex_revenue_row = {
        "SecuritiesCompanyCode": "6488",
        "DataYearMonth": "11504",
        "MonthlyRevenue": "1234567",
        "營業收入-去年同月增減(%)": "25.5",
    }

    def fake_fetch(url):
        if url == fund.TPEX_REVENUE:
            return [tpex_revenue_row]
        return []

    with patch.object(fund, "_fetch_json", side_effect=fake_fetch):
        result = fund.fetch_fundamentals()
    assert "6488" in result, f"TPEx code not enriched: {list(result.keys())}"
    assert result["6488"]["revenueYoY"] == 25.5
    assert result["6488"]["revenue"] == 1234567


def test_tpex_dividend_yield_uses_yield_ratio() -> None:
    """Regression for audit #2: TPEx valuation's DividendPerShare is an NTD
    amount per share, not a yield. The right field is YieldRatio.
    """
    tpex_valuation_row = {
        "SecuritiesCompanyCode": "6488",
        "CompanyName": "環球晶",
        "PERatio": "18.5",
        "PBRatio": "3.2",
        "YieldRatio": "4.5",          # the real yield, in %
        "DividendPerShare": "12.5",   # NTD amount, NOT a yield
    }

    def fake_fetch(url):
        if url == fund.TPEX_VALUATION:
            return [tpex_valuation_row]
        return []

    with patch.object(fund, "_fetch_json", side_effect=fake_fetch):
        result = fund.fetch_fundamentals()
    assert result["6488"]["dividendYield"] == 4.5, result["6488"]


def test_permanent_4xx_short_circuits_retry() -> None:
    """Issue #19: a 403 should not waste 3 retries × 1.5s backoff."""
    call_count = {"n": 0}

    class FakeResponse:
        status_code = 403
        text = "forbidden"

        def json(self):
            return {}

    def fake_get(url, **kwargs):
        call_count["n"] += 1
        return FakeResponse()

    with patch.object(fund.requests, "get", side_effect=fake_get):
        try:
            fund._fetch_json("https://example.com/x", retries=3)
        except RuntimeError:
            pass
    assert call_count["n"] == 1, f"should not retry on 403; got {call_count['n']} calls"


def test_numeric_helpers_handle_garbage() -> None:
    assert fund._num(None) is None
    assert fund._num("") is None
    assert fund._num("-") is None
    assert fund._num("N/A") is None
    assert fund._num("1,234.5") == 1234.5
    assert fund._num("not a number") is None


if __name__ == "__main__":
    test_revenue_yoy_uses_long_field_name()
    test_legacy_field_names_still_work()
    test_derived_roe_from_income_and_balance()
    test_tpex_revenue_uses_securities_company_code()
    test_tpex_dividend_yield_uses_yield_ratio()
    test_permanent_4xx_short_circuits_retry()
    test_numeric_helpers_handle_garbage()
    print("OK: all fundamentals tests passed")
