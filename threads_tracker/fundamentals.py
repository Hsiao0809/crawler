"""Fetch TWSE / TPEx public OpenAPI for stock universe + fundamentals.

Two stages:
  1. universe   — list of every TWSE-listed + TPEx-listed company with their
                  official Chinese name, used to seed the mention matcher.
  2. fundamentals — per-code price, P/E, P/B, dividend yield, EPS, monthly
                    revenue YoY, gross margin, ROE, debt ratio.

All endpoints are documented at https://openapi.twse.com.tw/ and
https://www.tpex.org.tw/openapi/v1/.

The functions cache to disk so subsequent runs don't hammer the OpenAPI on
every invocation.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

from .stock_extractor import StockEntry

log = logging.getLogger(__name__)


TWSE_PRICE = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_VALUATION = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
TWSE_EPS = "https://openapi.twse.com.tw/v1/opendata/t187ap14_L"
TWSE_REVENUE = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_PRICE = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TPEX_VALUATION = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"
TPEX_EPS = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap14_O"
TPEX_REVENUE = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"

FINANCIAL_CATEGORIES = ["basi", "bd", "ci", "fh", "ins", "mim"]
TWSE_INCOME = "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_{c}"
TWSE_BALANCE = "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_{c}"
TPEX_INCOME = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_{c}"
TPEX_BALANCE = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_{c}"


def _fetch_json(url: str, timeout: float = 30.0, retries: int = 3) -> Any:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; threads-stock-tracker/1.0)",
                    "Accept": "application/json,*/*",
                },
                timeout=timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            raise RuntimeError(f"{url} -> HTTP {resp.status_code}")
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_exc = exc
            wait = 1.5 * attempt
            log.warning("fetch %s failed (attempt %d): %s — sleeping %.1fs", url, attempt, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"failed to fetch {url}: {last_exc}")


def _num(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s in ("-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _clean(v: Any) -> str:
    return str(v or "").strip()


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------


def fetch_universe() -> list[StockEntry]:
    """Build a universe of every TWSE + TPEx listed name from official APIs."""
    out: list[StockEntry] = []

    log.info("fetching TWSE universe…")
    for row in _fetch_json(TWSE_PRICE) or []:
        code, name = _clean(row.get("Code")), _clean(row.get("Name"))
        if code and name:
            out.append(StockEntry(code=code, name=name, market="TWSE", aliases=[name]))

    log.info("fetching TPEx universe…")
    for row in _fetch_json(TPEX_PRICE) or []:
        code = _clean(row.get("SecuritiesCompanyCode"))
        name = _clean(row.get("CompanyName"))
        if code and name:
            out.append(StockEntry(code=code, name=name, market="TPEx", aliases=[name]))

    log.info("universe size: %d", len(out))
    return out


# ---------------------------------------------------------------------------
# Fundamentals
# ---------------------------------------------------------------------------


def _add(byc: dict[str, dict[str, Any]], code: str, **fields: Any) -> None:
    if not code:
        return
    if code not in byc:
        byc[code] = {"code": code}
    for k, v in fields.items():
        if v is None and k in byc[code]:
            continue
        byc[code][k] = v


def fetch_fundamentals(target_codes: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """Pull price + valuation + EPS + revenue + income + balance for every code.

    Returns {code: {price, peRatio, pbRatio, dividendYield, eps, revenueYoY,
                    grossMargin, roe, debtRatio, name, market, ...}}.
    `target_codes` is informational only — endpoints return full lists
    regardless, so this is just for log accounting.
    """
    byc: dict[str, dict[str, Any]] = {}

    log.info("fetching TWSE+TPEx prices + valuations + EPS + revenue…")
    # Run sequentially to keep dependency-free. Each call retries internally.
    twse_p = _fetch_json(TWSE_PRICE)
    twse_v = _fetch_json(TWSE_VALUATION)
    twse_e = _fetch_json(TWSE_EPS)
    twse_r = _fetch_json(TWSE_REVENUE)
    tpex_p = _fetch_json(TPEX_PRICE)
    tpex_v = _fetch_json(TPEX_VALUATION)
    tpex_e = _fetch_json(TPEX_EPS)
    tpex_r = _fetch_json(TPEX_REVENUE)

    for row in twse_p or []:
        _add(
            byc,
            _clean(row.get("Code")),
            name=_clean(row.get("Name")),
            market="TWSE",
            price=_num(row.get("ClosingPrice")),
            change=_num(row.get("Change")),
            priceDate=_clean(row.get("Date")),
        )
    for row in twse_v or []:
        _add(
            byc,
            _clean(row.get("Code")),
            name=_clean(row.get("Name")),
            market="TWSE",
            peRatio=_num(row.get("PEratio")),
            dividendYield=_num(row.get("DividendYield")),
            pbRatio=_num(row.get("PBratio")),
            valuationDate=_clean(row.get("Date")),
        )
    for row in twse_e or []:
        _add(
            byc,
            _clean(row.get("公司代號") or row.get("Code")),
            eps=_num(row.get("基本每股盈餘(元)") or row.get("EPS")),
            epsPeriod=_clean(row.get("年度") or "") + _clean(row.get("季別") or ""),
        )
    for row in twse_r or []:
        _add(
            byc,
            _clean(row.get("公司代號") or row.get("Code")),
            revenueYoY=_num(row.get("去年同月增減(%)")),
            revenue=_num(row.get("營業收入-當月營收")),
            revenuePeriod=_clean(row.get("資料年月") or ""),
        )

    for row in tpex_p or []:
        _add(
            byc,
            _clean(row.get("SecuritiesCompanyCode")),
            name=_clean(row.get("CompanyName")),
            market="TPEx",
            price=_num(row.get("Close")),
            change=_num(row.get("Change")),
            priceDate=_clean(row.get("Date")),
        )
    for row in tpex_v or []:
        _add(
            byc,
            _clean(row.get("SecuritiesCompanyCode")),
            name=_clean(row.get("CompanyName")),
            market="TPEx",
            peRatio=_num(row.get("PERatio")),
            dividendYield=_num(row.get("DividendPerShare")),
            pbRatio=_num(row.get("PBRatio")),
        )
    for row in tpex_e or []:
        _add(
            byc,
            _clean(row.get("公司代號") or row.get("SecuritiesCompanyCode")),
            eps=_num(row.get("基本每股盈餘(元)") or row.get("EPS")),
        )
    for row in tpex_r or []:
        _add(
            byc,
            _clean(row.get("公司代號") or row.get("SecuritiesCompanyCode")),
            revenueYoY=_num(row.get("去年同月增減(%)")),
        )

    log.info("fetching income statements (gross margin, ROE, debt ratio)…")
    for cat in FINANCIAL_CATEGORIES:
        for row in _fetch_json(TWSE_INCOME.format(c=cat)) or []:
            _enrich_income(byc, row)
        for row in _fetch_json(TPEX_INCOME.format(c=cat)) or []:
            _enrich_income(byc, row)
        for row in _fetch_json(TWSE_BALANCE.format(c=cat)) or []:
            _enrich_balance(byc, row)
        for row in _fetch_json(TPEX_BALANCE.format(c=cat)) or []:
            _enrich_balance(byc, row)

    log.info("fundamentals coverage: %d codes", len(byc))
    if target_codes:
        covered = [c for c in target_codes if c in byc]
        log.info("of %d target codes, %d enriched", len(target_codes), len(covered))
    return byc


def _enrich_income(byc: dict[str, dict[str, Any]], row: dict[str, Any]) -> None:
    code = _clean(row.get("公司代號"))
    if not code:
        return
    revenue = _num(row.get("營業收入"))
    gross = _num(row.get("營業毛利（毛損）") or row.get("營業毛利"))
    if revenue and gross is not None and revenue > 0:
        _add(byc, code, grossMargin=round(gross / revenue * 100, 2))
    net_income = _num(row.get("本期淨利（淨損）") or row.get("本期淨利"))
    if net_income is not None:
        _add(byc, code, netIncome=net_income)


def _enrich_balance(byc: dict[str, dict[str, Any]], row: dict[str, Any]) -> None:
    code = _clean(row.get("公司代號"))
    if not code:
        return
    total_assets = _num(row.get("資產總計") or row.get("資產總額"))
    total_liab = _num(row.get("負債總計") or row.get("負債總額"))
    equity = _num(row.get("權益總計") or row.get("權益總額"))
    if total_assets and total_liab is not None and total_assets > 0:
        _add(byc, code, debtRatio=round(total_liab / total_assets * 100, 2))
    if equity and equity != 0:
        # ROE is computed in the analyzer later, where we have netIncome.
        _add(byc, code, equity=equity)


def compute_roe(fund: dict[str, Any]) -> float | None:
    ni = fund.get("netIncome")
    eq = fund.get("equity")
    if ni is None or eq is None or eq == 0:
        return None
    return round(ni / eq * 100, 2)


def with_derived(fund: dict[str, Any]) -> dict[str, Any]:
    """Attach derived metrics (ROE) to a fundamentals row."""
    out = dict(fund)
    if "roe" not in out:
        roe = compute_roe(out)
        if roe is not None:
            out["roe"] = roe
    return out


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def load_cached(path: str | Path) -> Any | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_cached(path: str | Path, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
