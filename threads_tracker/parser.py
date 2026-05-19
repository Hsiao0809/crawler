"""Parse Threads profile HTML and GraphQL responses.

The profile HTML at https://www.threads.com/@<username> contains:
  - a meta description with follower count
  - one or more <script> blobs of the form:
      requireLazy(["..."], function(){ ... __bbox: {"complete":true,"result":{"data":{...}}} ... });
    where `data` carries the user record and recent thread items.

We brute-force scan every <script> for `"text_post_app_info"` (the marker for a
Threads post in Meta's JSON) and for `"user_id":"<digits>"` to find the account
record. This is intentionally schema-agnostic: Meta reshuffles GraphQL shapes
often, so we walk the JSON looking for known leaves rather than following a
fixed path.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

log = logging.getLogger(__name__)


USER_ID_RES = [
    re.compile(r'"props":\{"user_id":"(\d+)"\}'),
    re.compile(r'"user_id":"(\d+)","username":"%s"'),  # template; format() in code
    re.compile(r'"pk":"(\d+)","username":"%s"'),
]
SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
META_DESC_RE = re.compile(
    r'<meta[^>]+(?:name|property)="(?:og:)?description"[^>]+content="([^"]*)"',
    re.IGNORECASE,
)
META_TITLE_RE = re.compile(
    r'<meta[^>]+(?:name|property)="(?:og:)?title"[^>]+content="([^"]*)"', re.IGNORECASE
)
# "12,345 個粉絲" / "12,345 followers" / "12.3K followers" patterns Threads
# embeds in the description meta tag.
FOLLOWER_RES = [
    re.compile(r'(\d[\d,]*)\s*(?:個)?\s*粉絲'),
    re.compile(r'(\d[\d,]*\.?\d*[KkMm]?)\s+[Ff]ollowers?'),
]


@dataclass
class ThreadPost:
    """One Threads post (a single item in a thread)."""

    pk: str                       # post primary key, like "3193456789012345678"
    code: str | None              # short shareable code, used in /@user/post/<code>
    user_id: str | None
    username: str | None
    text: str = ""
    taken_at: int | None = None   # unix seconds
    like_count: int = 0
    reply_count: int = 0
    repost_count: int = 0
    quote_count: int = 0
    image_urls: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
    link_previews: list[str] = field(default_factory=list)
    is_repost: bool = False
    raw: dict[str, Any] | None = None

    @property
    def url(self) -> str | None:
        if self.username and self.code:
            return f"https://www.threads.com/@{self.username}/post/{self.code}"
        return None

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "raw"}
        d["url"] = self.url
        return d


@dataclass
class UserProfile:
    user_id: str
    username: str
    full_name: str | None = None
    biography: str | None = None
    follower_count: int | None = None
    following_count: int | None = None
    is_verified: bool | None = None
    profile_pic_url: str | None = None
    raw: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "raw"}
        return d


# ---------------------------------------------------------------------------
# JSON scanning helpers
# ---------------------------------------------------------------------------


def _iter_json_objects(text: str) -> Iterator[dict[str, Any]]:
    """Yield every well-formed top-level JSON object found in `text`.

    Threads' inline scripts embed many separate JSON blobs concatenated with
    other code. We find each `{` and try to parse forward with json.JSONDecoder.
    """
    decoder = json.JSONDecoder()
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "{":
            i += 1
            continue
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict):
            yield obj
        i = end


def _walk(node: Any) -> Iterator[Any]:
    """Yield every dict/list contained in `node`, including `node` itself."""
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _find_user_id(html: str, username: str) -> str | None:
    """Best-effort regex search for the account's numeric user_id."""
    for pat in USER_ID_RES:
        pattern = pat.pattern
        if "%s" in pattern:
            pat = re.compile(pattern % re.escape(username))
        m = pat.search(html)
        if m:
            return m.group(1)
    # Fallback: look for {"username":"X","pk":"<digits>"} pairings anywhere.
    fallback = re.search(
        r'"username":"%s"[^{}]{0,200}?"(?:pk|id|pk_id|user_id)":"?(\d{8,})"?' % re.escape(username),
        html,
    )
    if fallback:
        return fallback.group(1)
    return None


# ---------------------------------------------------------------------------
# Post extraction
# ---------------------------------------------------------------------------


def _extract_post(raw: dict[str, Any]) -> ThreadPost | None:
    """Build a ThreadPost from a Threads post dict, or None if it doesn't look like one."""
    pk = raw.get("pk") or raw.get("pk_id") or raw.get("id")
    if not pk:
        return None
    pk = str(pk)
    code = raw.get("code") or raw.get("shortcode")
    text_caption = raw.get("caption") or {}
    text = ""
    if isinstance(text_caption, dict):
        text = text_caption.get("text") or ""
    text_post_info = raw.get("text_post_app_info") or {}
    user = raw.get("user") or {}
    username = user.get("username")
    user_id = str(user.get("pk") or user.get("id") or user.get("pk_id") or "") or None

    taken_at = raw.get("taken_at") or raw.get("device_timestamp")
    if isinstance(taken_at, str) and taken_at.isdigit():
        taken_at = int(taken_at)

    like_count = raw.get("like_count") or 0
    reply_count = (
        text_post_info.get("direct_reply_count")
        or raw.get("direct_reply_count")
        or text_post_info.get("reply_count")
        or 0
    )
    repost_count = text_post_info.get("repost_count") or raw.get("repost_count") or 0
    quote_count = text_post_info.get("quote_count") or raw.get("quote_count") or 0

    images: list[str] = []
    videos: list[str] = []

    def _collect_media(media: dict[str, Any]) -> None:
        v = media.get("video_versions") or []
        if isinstance(v, list):
            for vv in v:
                u = vv.get("url") if isinstance(vv, dict) else None
                if u and u not in videos:
                    videos.append(u)
        ic = media.get("image_versions2") or {}
        if isinstance(ic, dict):
            for cand in ic.get("candidates") or []:
                u = cand.get("url") if isinstance(cand, dict) else None
                if u and u not in images:
                    images.append(u)

    _collect_media(raw)
    carousel = raw.get("carousel_media") or []
    if isinstance(carousel, list):
        for child in carousel:
            if isinstance(child, dict):
                _collect_media(child)

    links: list[str] = []
    tpinfo_link = text_post_info.get("link_preview_attachment") or {}
    if isinstance(tpinfo_link, dict):
        u = tpinfo_link.get("url")
        if u:
            links.append(u)

    return ThreadPost(
        pk=pk,
        code=code,
        user_id=user_id,
        username=username,
        text=text,
        taken_at=taken_at if isinstance(taken_at, int) else None,
        like_count=int(like_count or 0),
        reply_count=int(reply_count or 0),
        repost_count=int(repost_count or 0),
        quote_count=int(quote_count or 0),
        image_urls=images,
        video_urls=videos,
        link_previews=links,
        is_repost=bool(text_post_info.get("share_info") or {}),
        raw=raw,
    )


def _looks_like_post(d: dict[str, Any]) -> bool:
    if not isinstance(d, dict):
        return False
    # Threads posts always carry a text_post_app_info dict (even on photo/video posts).
    return "text_post_app_info" in d and ("caption" in d or "code" in d or "pk" in d)


def _extract_user_profile(d: dict[str, Any]) -> UserProfile | None:
    if not isinstance(d, dict):
        return None
    username = d.get("username")
    pk = d.get("pk") or d.get("pk_id") or d.get("id")
    if not username or not pk:
        return None
    # Filter out partial user references that lack the things only a full
    # profile carries (follower count or biography).
    if (
        "follower_count" not in d
        and "biography" not in d
        and "biography_with_entities" not in d
    ):
        return None
    return UserProfile(
        user_id=str(pk),
        username=username,
        full_name=d.get("full_name"),
        biography=d.get("biography"),
        follower_count=d.get("follower_count"),
        following_count=d.get("following_count"),
        is_verified=d.get("is_verified"),
        profile_pic_url=d.get("hd_profile_pic_url_info", {}).get("url")
        if isinstance(d.get("hd_profile_pic_url_info"), dict)
        else d.get("profile_pic_url"),
        raw=d,
    )


def _scan_json_for_posts_and_user(
    objs: Iterable[dict[str, Any]],
    username_hint: str | None,
) -> tuple[list[ThreadPost], UserProfile | None]:
    posts: dict[str, ThreadPost] = {}
    profile: UserProfile | None = None
    for top in objs:
        for node in _walk(top):
            if isinstance(node, dict):
                if _looks_like_post(node):
                    p = _extract_post(node)
                    if p and (
                        username_hint is None
                        or (p.username and p.username.lower() == username_hint.lower())
                    ):
                        # Keep the version with the most info if we see this pk twice.
                        existing = posts.get(p.pk)
                        if not existing or len(p.text) >= len(existing.text):
                            posts[p.pk] = p
                if profile is None:
                    up = _extract_user_profile(node)
                    if up and (username_hint is None or up.username.lower() == username_hint.lower()):
                        profile = up
    return list(posts.values()), profile


def parse_profile_html(html: str, username: str) -> tuple[UserProfile | None, list[ThreadPost]]:
    """Extract profile + posts from a fetched profile HTML page."""
    # Pull JSON out of every <script> block. The Threads page also has top-level
    # JSON-LD; we scan the raw HTML too as a backstop.
    candidates: list[dict[str, Any]] = []
    for m in SCRIPT_RE.finditer(html):
        script_body = m.group(1)
        if "text_post_app_info" not in script_body and "BarcelonaProfile" not in script_body and "follower_count" not in script_body:
            continue
        candidates.extend(_iter_json_objects(script_body))

    posts, profile = _scan_json_for_posts_and_user(candidates, username_hint=username)

    if not profile:
        uid = _find_user_id(html, username)
        if uid:
            profile = UserProfile(user_id=uid, username=username)
            _enrich_profile_from_meta(profile, html)
        else:
            # Even without a numeric user_id, try the meta tags so the caller
            # has something to display.
            profile = _build_profile_from_meta(username, html)

    # Sort newest first.
    posts.sort(key=lambda p: p.taken_at or 0, reverse=True)
    return profile, posts


def parse_graphql_response(payload: dict[str, Any], username_hint: str | None = None) -> list[ThreadPost]:
    """Extract posts from a GraphQL JSON payload."""
    posts, _ = _scan_json_for_posts_and_user([payload], username_hint=username_hint)
    posts.sort(key=lambda p: p.taken_at or 0, reverse=True)
    return posts


def _parse_follower_count(text: str) -> int | None:
    """Parse '12,345', '12.3K', '1.2M' (zh-TW or en) into an int."""
    for pat in FOLLOWER_RES:
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1).replace(",", "")
        try:
            if raw.endswith(("K", "k")):
                return int(float(raw[:-1]) * 1_000)
            if raw.endswith(("M", "m")):
                return int(float(raw[:-1]) * 1_000_000)
            return int(float(raw))
        except ValueError:
            continue
    return None


def _enrich_profile_from_meta(profile: UserProfile, html: str) -> None:
    """Best-effort: harvest follower_count and biography from <meta> when the
    structured JSON path didn't provide them."""
    desc_match = META_DESC_RE.search(html)
    desc = desc_match.group(1) if desc_match else ""
    if desc:
        follower = _parse_follower_count(desc)
        if follower is not None and profile.follower_count is None:
            profile.follower_count = follower
        # Description has shape "FullName (@user) on Threads. 12,345 個粉絲. <bio>".
        # Strip the leading metadata so we don't write that into biography.
        if profile.biography is None:
            bio = re.sub(r'^.+? \(@[\w.]+\) on Threads\.\s*', '', desc)
            bio = re.sub(r'^\d[\d,]*\s*(?:個)?\s*粉絲[。\.]?\s*', '', bio)
            bio = re.sub(r'^\d[\d,]*\.?\d*[KkMm]?\s+[Ff]ollowers?[。\.]?\s*', '', bio)
            if bio and bio != desc:
                profile.biography = bio.strip() or None


def _build_profile_from_meta(username: str, html: str) -> UserProfile | None:
    """Construct a UserProfile from meta tags alone. Used when no JSON
    payload is recoverable. Returns None if there's nothing usable so the
    caller can decide not to clobber the DB with garbage."""
    title_m = META_TITLE_RE.search(html)
    desc_m = META_DESC_RE.search(html)
    if not title_m and not desc_m:
        return None
    log.warning("could not find structured profile JSON; falling back to <meta> tags only")
    profile = UserProfile(user_id="", username=username)
    # og:title for Threads looks like "FullName (@user) on Threads". Strip
    # the "(@user) on Threads" trailer so full_name is just the name.
    if title_m:
        title = title_m.group(1)
        m = re.match(r'^(.*?)\s*\(@[\w.]+\)\s*(?:on Threads)?', title)
        if m and m.group(1):
            profile.full_name = m.group(1).strip()
        elif "@" + username.lower() not in title.lower():
            profile.full_name = title.strip()
    _enrich_profile_from_meta(profile, html)
    # If we couldn't get any real signal beyond echoing the username,
    # return None so the storage layer doesn't write a placeholder row
    # that would later overwrite a real fetch's fields.
    if (
        not profile.full_name
        and profile.follower_count is None
        and not profile.biography
    ):
        return None
    return profile
