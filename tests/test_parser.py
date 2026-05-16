"""Parser tests with a synthetic HTML fixture that mirrors Threads' shape.

We don't have a real network response in CI, so we build one based on what
threads.com actually embeds: a <script> with a nested __bbox.result.data tree
that holds the user record plus a list of thread items, each of which has a
``text_post_app_info`` dict.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from threads_tracker.parser import parse_profile_html, parse_graphql_response


def _build_html(username: str, user_id: str) -> str:
    user_obj = {
        "pk": user_id,
        "pk_id": user_id,
        "username": username,
        "full_name": "Eva Chien 千千",
        "biography": "test bio line 1\ntest bio line 2 #分享",
        "follower_count": 12345,
        "following_count": 678,
        "is_verified": False,
        "profile_pic_url": "https://scontent.cdninstagram.com/pp.jpg",
        "hd_profile_pic_url_info": {"url": "https://scontent.cdninstagram.com/hd.jpg"},
    }

    def post(pk, code, text, like, reply, taken_at, has_image=False, has_video=False):
        media = {}
        if has_image:
            media["image_versions2"] = {
                "candidates": [
                    {"url": f"https://scontent.cdninstagram.com/img_{pk}.jpg", "width": 1080, "height": 1080}
                ]
            }
        if has_video:
            media["video_versions"] = [
                {"url": f"https://scontent.cdninstagram.com/vid_{pk}.mp4", "type": 101}
            ]
        return {
            "pk": pk,
            "code": code,
            "user": user_obj,
            "caption": {"text": text},
            "taken_at": taken_at,
            "like_count": like,
            "text_post_app_info": {
                "direct_reply_count": reply,
                "repost_count": 1,
                "quote_count": 0,
                "link_preview_attachment": None,
            },
            **media,
        }

    data = {
        "data": {
            "userData": {"user": user_obj},
            "mediaData": {
                "threads": [
                    {
                        "thread_items": [
                            post(
                                "3193456789012345001",
                                "C9aaaaaaaaA",
                                "今天天氣很好 ☀️ #日常 https://example.com",
                                42,
                                3,
                                1715800000,
                                has_image=True,
                            )
                        ]
                    },
                    {
                        "thread_items": [
                            post(
                                "3193456789012345002",
                                "C9aaaaaaaaB",
                                "另一篇貼文，沒有圖片 @friend",
                                17,
                                1,
                                1715900000,
                            )
                        ]
                    },
                ]
            },
        }
    }

    bbox = {"__bbox": {"complete": True, "result": data}}
    script = (
        '<script type="application/json">'
        + json.dumps(bbox, ensure_ascii=False)
        + "</script>"
        + '<script>requireLazy(["abc"],function(){var x='
        + json.dumps(bbox, ensure_ascii=False)
        + ";});</script>"
    )
    html = (
        '<!doctype html><html><head>'
        f'<meta property="og:title" content="@{username} on Threads"/>'
        '<meta name="description" content="test bio"/>'
        f'</head><body>{script}</body></html>'
    )
    return html


def test_parses_user_and_posts() -> None:
    html = _build_html("evachien.chien", "1234567890")
    profile, posts = parse_profile_html(html, "evachien.chien")

    assert profile is not None
    assert profile.username == "evachien.chien"
    assert profile.user_id == "1234567890"
    assert profile.full_name == "Eva Chien 千千"
    assert profile.follower_count == 12345
    assert profile.following_count == 678
    assert profile.profile_pic_url and "cdninstagram" in profile.profile_pic_url

    assert len(posts) == 2
    by_pk = {p.pk: p for p in posts}
    p1 = by_pk["3193456789012345001"]
    assert p1.code == "C9aaaaaaaaA"
    assert p1.text.startswith("今天天氣很好")
    assert p1.like_count == 42
    assert p1.reply_count == 3
    assert p1.image_urls and p1.image_urls[0].endswith(".jpg")
    assert p1.url == "https://www.threads.com/@evachien.chien/post/C9aaaaaaaaA"

    # Newest-first ordering.
    assert posts[0].pk == "3193456789012345002"


def test_parses_graphql_payload() -> None:
    user_obj = {
        "pk": "1234567890",
        "username": "evachien.chien",
        "full_name": "Eva",
        "follower_count": 1,
        "biography": "",
    }
    payload = {
        "data": {
            "mediaData": {
                "threads": [
                    {
                        "thread_items": [
                            {
                                "pk": "3193456789012345003",
                                "code": "C9aaaaaaaaC",
                                "user": user_obj,
                                "caption": {"text": "graphql post"},
                                "taken_at": 1716000000,
                                "like_count": 99,
                                "text_post_app_info": {"direct_reply_count": 0},
                            }
                        ]
                    }
                ]
            }
        }
    }
    posts = parse_graphql_response(payload, username_hint="evachien.chien")
    assert len(posts) == 1
    assert posts[0].like_count == 99
    assert posts[0].text == "graphql post"


def test_falls_back_to_meta_when_json_missing() -> None:
    html = (
        '<!doctype html><html><head>'
        '<meta property="og:title" content="@evachien.chien on Threads"/>'
        '<meta name="description" content="bio text"/>'
        '</head><body><script>console.log("no useful data here");</script></body></html>'
    )
    profile, posts = parse_profile_html(html, "evachien.chien")
    # We get *something* back instead of failing hard.
    assert profile is not None
    assert profile.username == "evachien.chien"
    assert posts == []


if __name__ == "__main__":
    test_parses_user_and_posts()
    test_parses_graphql_payload()
    test_falls_back_to_meta_when_json_missing()
    print("OK: all parser tests passed")
