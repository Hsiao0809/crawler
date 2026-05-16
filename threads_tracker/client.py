"""HTTP client for Threads.

Threads serves a public, server-rendered profile page that already contains
the user's recent posts as embedded JSON. We prefer that path because it
needs no auth and is the most resilient to API changes. As a secondary path
we also support the GraphQL endpoint that the site itself uses, which can
paginate further than what the HTML embeds.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger(__name__)


class ThreadsError(RuntimeError):
    pass


USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
]

PROFILE_HOSTS = ["www.threads.com", "www.threads.net"]

DEFAULT_DOC_IDS = {
    "profile": "23996318473300828",
    "threads": "6232751443445612",
    "replies": "6307072669391286",
}

LSD_RE = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"\}')
DTSG_RE = re.compile(r'"DTSGInitialData",\[\],\{"token":"([^"]+)"\}')


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)


class ThreadsClient:
    def __init__(
        self,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff: float = 1.5,
        user_agent: str | None = None,
        proxies: dict[str, str] | None = None,
        debug_dump_dir: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "User-Agent": user_agent or random.choice(USER_AGENTS),
            }
        )
        if proxies:
            self.session.proxies.update(proxies)
        self.lsd_token: str | None = None
        self.app_id = "238260118697367"
        self.debug_dump_dir = debug_dump_dir

    def _dump(self, name: str, content: str) -> None:
        if not self.debug_dump_dir:
            return
        import os

        os.makedirs(self.debug_dump_dir, exist_ok=True)
        path = os.path.join(self.debug_dump_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        log.info("Dumped %s (%d bytes)", path, len(content))

    def _do_request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                log.debug("%s %s (attempt %d)", method, url, attempt)
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise ThreadsError(f"server status {resp.status_code}")
                return resp
            except (requests.RequestException, ThreadsError) as exc:
                last_exc = exc
                sleep_for = self.backoff ** attempt + random.uniform(0, 0.5)
                log.warning("attempt %d failed (%s); sleeping %.1fs", attempt, exc, sleep_for)
                time.sleep(sleep_for)
        raise ThreadsError(f"request failed after {self.max_retries} attempts: {last_exc}")

    def fetch_profile_html(self, username: str) -> FetchResult:
        """Fetch the public profile HTML. Tries threads.com then threads.net."""
        last_err: Exception | None = None
        for host in PROFILE_HOSTS:
            url = f"https://{host}/@{username}"
            try:
                resp = self._do_request("GET", url)
            except ThreadsError as exc:
                last_err = exc
                continue
            text = resp.text
            self._dump(f"profile_{username}_{host}.html", text)
            if resp.status_code == 404:
                raise ThreadsError(f"user @{username} not found (404 from {host})")
            if resp.status_code != 200:
                last_err = ThreadsError(f"{host} returned {resp.status_code}")
                continue
            self._capture_tokens(text)
            return FetchResult(url=url, status=resp.status_code, text=text, headers=dict(resp.headers))
        raise ThreadsError(f"all profile hosts failed: {last_err}")

    def _capture_tokens(self, html: str) -> None:
        m = LSD_RE.search(html)
        if m:
            self.lsd_token = m.group(1)
            log.debug("captured LSD token (len=%d)", len(self.lsd_token))

    def graphql(
        self,
        doc_id: str,
        variables: dict[str, Any],
        friendly_name: str = "BarcelonaProfileThreadsTabQuery",
    ) -> dict[str, Any]:
        if not self.lsd_token:
            raise ThreadsError(
                "no LSD token captured yet — call fetch_profile_html() first to harvest it"
            )
        headers = {
            "Accept": "*/*",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.threads.com",
            "Referer": "https://www.threads.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-ASBD-ID": "129477",
            "X-FB-LSD": self.lsd_token,
            "X-IG-App-ID": self.app_id,
            "X-FB-Friendly-Name": friendly_name,
        }
        data = {
            "lsd": self.lsd_token,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": friendly_name,
            "variables": json.dumps(variables, separators=(",", ":")),
            "doc_id": doc_id,
        }
        url = "https://www.threads.com/api/graphql"
        resp = self._do_request("POST", url, headers=headers, data=data)
        self._dump(f"graphql_{friendly_name}_{doc_id}.json", resp.text)
        if resp.status_code != 200:
            raise ThreadsError(f"graphql {friendly_name} returned {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ThreadsError(f"graphql response was not JSON: {exc}: {resp.text[:200]}") from exc
        if "errors" in payload:
            raise ThreadsError(f"graphql errors: {payload['errors']}")
        return payload

    def fetch_user_threads_graphql(self, user_id: str, doc_id: str | None = None) -> dict[str, Any]:
        return self.graphql(
            doc_id=doc_id or DEFAULT_DOC_IDS["threads"],
            variables={"userID": user_id},
            friendly_name="BarcelonaProfileThreadsTabQuery",
        )

    def fetch_user_replies_graphql(self, user_id: str, doc_id: str | None = None) -> dict[str, Any]:
        return self.graphql(
            doc_id=doc_id or DEFAULT_DOC_IDS["replies"],
            variables={"userID": user_id},
            friendly_name="BarcelonaProfileRepliesTabQuery",
        )
