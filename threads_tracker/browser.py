"""Playwright-based fetcher for Threads.

The reference repo Hsiao0809/threads-stock-watch drives Chrome via CDP. This
module does the same in Python with Playwright. Threads' web app loads posts
via XHR to /api/graphql; we intercept those responses and also dump the
final rendered HTML, then hand both to the existing parser.

Used by `threads-tracker track ... --browser`. Falls back to the HTTP path
if Playwright isn't installed.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class BrowserFetchResult:
    url: str
    final_url: str
    title: str
    html: str
    graphql_responses: list[dict[str, Any]] = field(default_factory=list)


async def _fetch_async(
    username: str,
    *,
    scroll_rounds: int,
    settle_ms: int,
    post_pages: int,
    headless: bool,
    timeout_ms: int,
    user_data_dir: str | None,
) -> BrowserFetchResult:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright not installed. Run: pip install playwright && playwright install chromium"
        ) from exc

    target = f"https://www.threads.com/@{username.lstrip('@')}"
    graphql_responses: list[dict[str, Any]] = []

    async with async_playwright() as pw:
        if user_data_dir:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=headless,
                locale="zh-TW",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                extra_http_headers={"Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"},
            )
            page = await context.new_page()
        else:
            browser = await pw.chromium.launch(headless=headless)
            context = await browser.new_context(
                locale="zh-TW",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                extra_http_headers={"Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"},
            )
            page = await context.new_page()

        async def on_response(response):
            try:
                url = response.url
                if "/api/graphql" not in url and "/graphql" not in url:
                    return
                ct = (response.headers or {}).get("content-type", "")
                # Skip non-JSON like CSP reports.
                if "json" not in ct and "javascript" not in ct and "text" not in ct:
                    return
                body = await response.text()
                graphql_responses.append({"url": url, "status": response.status, "body": body})
            except Exception as exc:  # pragma: no cover — defensive
                log.debug("response capture failed: %s", exc)

        page.on("response", on_response)

        page.set_default_timeout(timeout_ms)
        log.info("navigating to %s", target)
        await page.goto(target, wait_until="domcontentloaded")
        await page.wait_for_timeout(settle_ms)

        # Click "view more" / cookie banners if present.
        for selector in (
            'div[role="button"]:has-text("接受")',
            'div[role="button"]:has-text("Allow all")',
            'button:has-text("Allow")',
        ):
            try:
                el = await page.query_selector(selector)
                if el:
                    await el.click(timeout=1500)
                    await page.wait_for_timeout(800)
            except Exception:
                pass

        # Scroll to load more posts.
        for _ in range(max(scroll_rounds, 1)):
            await page.evaluate(
                "window.scrollBy(0, Math.max(document.documentElement.clientHeight, 900))"
            )
            await page.wait_for_timeout(1200)

        await page.wait_for_timeout(2000)

        # Optionally visit individual post pages to pick up full text the
        # profile timeline truncated.
        if post_pages > 0:
            post_hrefs = await page.evaluate(
                """() => {
                    const handle = location.pathname.replace(/^\\//, '').toLowerCase();
                    return Array.from(document.querySelectorAll('a[href*="/post/"]'))
                        .map(a => a.href)
                        .filter(h => h.toLowerCase().includes('/' + handle + '/post/'))
                        .filter((v, i, arr) => arr.indexOf(v) === i);
                }"""
            )
            for href in (post_hrefs or [])[:post_pages]:
                try:
                    await page.goto(href, wait_until="domcontentloaded")
                    await page.wait_for_timeout(settle_ms)
                except Exception as exc:
                    log.warning("post page %s failed: %s", href, exc)

        final_url = page.url
        title = await page.title()
        html = await page.content()

        await context.close()
        if not user_data_dir:
            await browser.close()  # type: ignore[name-defined]

    return BrowserFetchResult(
        url=target,
        final_url=final_url,
        title=title,
        html=html,
        graphql_responses=graphql_responses,
    )


def fetch_with_browser(
    username: str,
    scroll_rounds: int = 4,
    settle_ms: int = 4000,
    post_pages: int = 0,
    headless: bool = True,
    timeout_ms: int = 45000,
    user_data_dir: str | None = None,
) -> BrowserFetchResult:
    """Synchronous wrapper around the async Playwright fetcher."""
    return asyncio.run(
        _fetch_async(
            username,
            scroll_rounds=scroll_rounds,
            settle_ms=settle_ms,
            post_pages=post_pages,
            headless=headless,
            timeout_ms=timeout_ms,
            user_data_dir=user_data_dir,
        )
    )


# ---------------------------------------------------------------------------
# GraphQL body → posts
# ---------------------------------------------------------------------------


def collect_graphql_bodies(result: BrowserFetchResult) -> list[dict[str, Any]]:
    """Parse each captured GraphQL response into a list of top-level JSON
    objects. Some Threads endpoints return JSONL (one object per line)."""
    import json

    bodies: list[dict[str, Any]] = []
    for resp in result.graphql_responses:
        text = (resp.get("body") or "").strip()
        if not text:
            continue
        # Single JSON document.
        if text.startswith("{") or text.startswith("["):
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    bodies.append(obj)
                elif isinstance(obj, list):
                    bodies.extend(o for o in obj if isinstance(o, dict))
                continue
            except json.JSONDecodeError:
                pass
        # JSONL or partial.
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    bodies.append(obj)
            except json.JSONDecodeError:
                continue
    return bodies
