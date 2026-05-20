#!/usr/bin/env python3
"""Fetch bookmarks / liked tweets via X's web GraphQL endpoints instead of
the metered X API v2.

The official `/2/users/{id}/bookmarks` and `/2/users/{id}/liked_tweets`
endpoints bill per read against a credit pool that runs dry (HTTP 402
CreditsDepleted). The X web client reads the same data through private
GraphQL endpoints authenticated by the logged-in session cookies
(`auth_token` + `ct0`) and the public web bearer token — which consume
no API credits.

This module replicates those requests with stdlib urllib and converts
the GraphQL tweet objects back into the same v2-shaped dicts the rest of
sync_x_library.py already consumes (id / text / created_at / author_id /
entities / attachments + _media / conversation_id / referenced_tweets /
public_metrics), so it's a drop-in for collect_tweets().

Fragility note: queryId and the features object are scraped from the
live web client and change every few months. When X rotates them, the
endpoint returns 404 / "BadRequest" and the caller should fall back to
the API. Re-capture by watching the /Bookmarks and /Likes XHRs in a
logged-in browser session.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

# Public web client bearer — same constant the x.com SPA ships; not a secret.
WEB_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# Scraped from the live web client 2026-05-20. Bump when X rotates them.
BOOKMARKS_QUERY_ID = "XD0ViOeSOW4YoeNTGjVaYw"
LIKES_QUERY_ID = "CDWHmpZeSdIJ3HGeRbNm0w"

GRAPHQL_FEATURES: dict[str, bool] = {
    "rweb_video_screen_enabled": False,
    "rweb_cashtags_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "rweb_cashtags_composer_attachment_enabled": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "rweb_conversational_replies_downvote_enabled": False,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
    "responsive_web_grok_analysis_button_from_backend": True,
    "post_ctas_fetch_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": False,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class GraphQLError(RuntimeError):
    """Raised when the GraphQL endpoint rejects us (rotated queryId, expired
    cookie, rate limit). The caller can catch this and fall back to the API."""


def _iso(created_at: str | None) -> str | None:
    """Twitter's legacy 'Tue May 19 05:06:30 +0000 2026' → ISO 8601 to match
    the v2 API's created_at so downstream string sorting stays correct."""
    if not created_at:
        return None
    try:
        dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except ValueError:
        return created_at


def _graphql_get(
    query_id: str,
    name: str,
    variables: dict[str, Any],
    auth_token: str,
    ct0: str,
    timeout: int = 30,
    max_attempts: int = 3,
) -> dict[str, Any]:
    qs = urllib.parse.urlencode(
        {"variables": json.dumps(variables), "features": json.dumps(GRAPHQL_FEATURES)}
    )
    url = f"https://x.com/i/api/graphql/{query_id}/{name}?{qs}"
    headers = {
        "authorization": WEB_BEARER,
        "x-csrf-token": ct0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "content-type": "application/json",
        "cookie": f"auth_token={auth_token}; ct0={ct0}",
        "user-agent": _UA,
    }
    last_err: str | None = None
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("errors") and not payload.get("data"):
                raise GraphQLError(f"{name} GraphQL errors: {payload['errors'][:1]}")
            return payload
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last_err = f"HTTP {e.code}: {detail}"
            # 404 (rotated queryId) / 401/403 (bad cookie) → no point retrying.
            if e.code in (400, 401, 403, 404):
                raise GraphQLError(f"{name} {last_err}") from e
        except GraphQLError:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        if attempt < max_attempts:
            time.sleep(min(2 ** attempt, 8))
    raise GraphQLError(f"{name} failed after {max_attempts} attempts: {last_err}")


def _unwrap_result(result: dict[str, Any]) -> dict[str, Any] | None:
    """A timeline tweet_results.result is either a Tweet or a
    TweetWithVisibilityResults wrapping the real tweet under .tweet."""
    if not isinstance(result, dict):
        return None
    if result.get("__typename") == "TweetWithVisibilityResults":
        return result.get("tweet") or None
    if result.get("__typename") in (None, "Tweet", "TweetTombstone"):
        return result if result.get("__typename") != "TweetTombstone" else None
    return result


def _user_to_v2(user_result: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """core.user_results.result → (user_id, v2 user dict)."""
    uid = user_result.get("rest_id")
    legacy = user_result.get("legacy") or {}
    core = user_result.get("core") or {}
    username = core.get("screen_name") or legacy.get("screen_name") or ""
    name = core.get("name") or legacy.get("name") or ""
    return uid, {
        "id": uid,
        "username": username,
        "name": name,
        "profile_image_url": legacy.get("profile_image_url_https") or "",
    }


def _media_to_v2(legacy: dict[str, Any]) -> list[dict[str, Any]]:
    media_list = (legacy.get("extended_entities") or {}).get("media") or (
        legacy.get("entities") or {}
    ).get("media") or []
    out: list[dict[str, Any]] = []
    for m in media_list:
        info = m.get("original_info") or {}
        out.append(
            {
                "type": m.get("type"),
                "url": m.get("media_url_https"),
                "preview_image_url": m.get("media_url_https"),
                "width": info.get("width"),
                "height": info.get("height"),
                "alt_text": m.get("ext_alt_text") or "",
            }
        )
    return out


def _entities_with_unwound(legacy: dict[str, Any], note: dict[str, Any] | None) -> dict[str, Any]:
    """Prefer the note_tweet entity_set for long-form posts (richer URLs),
    falling back to the legacy entities."""
    if note:
        es = note.get("entity_set") or {}
        if es.get("urls"):
            return es
    return legacy.get("entities") or {}


def tweet_result_to_v2(
    result: dict[str, Any],
    users: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Convert one GraphQL tweet_results.result into the v2 tweet shape and
    register its author into `users`. Returns None for tombstones / ads."""
    tw = _unwrap_result(result)
    if not tw or tw.get("rest_id") is None:
        return None
    legacy = tw.get("legacy") or {}
    rest_id = tw.get("rest_id")

    # Author.
    author_id = None
    user_result = (tw.get("core") or {}).get("user_results", {}).get("result")
    if isinstance(user_result, dict):
        author_id, user_v2 = _user_to_v2(user_result)
        if author_id:
            users[author_id] = user_v2

    # Long-form note_tweet body wins over the truncated legacy.full_text.
    note = (
        (tw.get("note_tweet") or {})
        .get("note_tweet_results", {})
        .get("result")
    )
    text = (note or {}).get("text") or legacy.get("full_text") or ""

    # referenced_tweets: quoted / retweeted / replied_to.
    refs: list[dict[str, Any]] = []
    q = (tw.get("quoted_status_result") or {}).get("result")
    qid = (_unwrap_result(q) or {}).get("rest_id") if q else legacy.get("quoted_status_id_str")
    if qid:
        refs.append({"type": "quoted", "id": str(qid)})
    if legacy.get("retweeted_status_result") or legacy.get("retweeted_status_id_str"):
        rid = legacy.get("retweeted_status_id_str")
        if rid:
            refs.append({"type": "retweeted", "id": str(rid)})
    if legacy.get("in_reply_to_status_id_str"):
        refs.append({"type": "replied_to", "id": str(legacy["in_reply_to_status_id_str"])})

    pm = {
        "like_count": legacy.get("favorite_count"),
        "retweet_count": legacy.get("retweet_count"),
        "reply_count": legacy.get("reply_count"),
        "quote_count": legacy.get("quote_count"),
        "bookmark_count": legacy.get("bookmark_count"),
    }

    tweet_v2: dict[str, Any] = {
        "id": str(rest_id),
        "text": text,
        "created_at": _iso(legacy.get("created_at")),
        "author_id": author_id,
        "entities": _entities_with_unwound(legacy, note),
        "conversation_id": legacy.get("conversation_id_str"),
        "referenced_tweets": refs,
        "public_metrics": {k: v for k, v in pm.items() if v is not None},
        "_media": _media_to_v2(legacy),
    }
    return tweet_v2


def _iter_timeline_tweets(payload: dict[str, Any], timeline_path: list[str]):
    """Walk instructions[].entries[], yielding (tweet_results.result, kind)
    and returning the bottom cursor."""
    node: Any = payload.get("data") or {}
    for key in timeline_path:
        node = (node or {}).get(key) or {}
    instructions = (node.get("timeline") or node).get("instructions") or []
    cursor_bottom = None
    for ins in instructions:
        for entry in ins.get("entries") or []:
            eid = entry.get("entryId") or ""
            content = entry.get("content") or {}
            if eid.startswith("cursor-bottom") or content.get("cursorType") == "Bottom":
                cursor_bottom = content.get("value")
                continue
            if not eid.startswith("tweet-"):
                continue
            result = (
                (content.get("itemContent") or {})
                .get("tweet_results", {})
                .get("result")
            )
            if result:
                yield result
    return cursor_bottom


def _collect(
    query_id: str,
    name: str,
    base_variables: dict[str, Any],
    timeline_path: list[str],
    source: str,
    auth_token: str,
    ct0: str,
    max_pages: int,
    known_ids: set[str] | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    tweets: list[dict[str, Any]] = []
    users: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    overlap_exit = False
    page = 0
    for page in range(1, max_pages + 1):
        variables = dict(base_variables)
        if cursor:
            variables["cursor"] = cursor
        payload = _graphql_get(query_id, name, variables, auth_token, ct0)

        # Drive the generator and capture its return value (bottom cursor).
        gen = _iter_timeline_tweets(payload, timeline_path)
        next_cursor = None
        page_results = []
        try:
            while True:
                page_results.append(next(gen))
        except StopIteration as stop:
            next_cursor = stop.value

        page_count = 0
        hit_known = False
        for result in page_results:
            v2 = tweet_result_to_v2(result, users)
            if not v2:
                continue
            v2["_source"] = source
            v2["_source_rank"] = (page - 1) * 100 + page_count
            page_count += 1
            tweets.append(v2)
            if known_ids and v2["id"] in known_ids:
                hit_known = True

        if hit_known:
            overlap_exit = True
            break
        if not next_cursor or page_count == 0:
            break
        cursor = next_cursor
        time.sleep(0.3)

    if overlap_exit:
        print(
            f"::notice::{source} graphql overlap-exit after page {page} "
            f"({len(tweets)} tweets)",
            file=sys.stderr,
        )
    return tweets, users


def collect_bookmarks(
    auth_token: str,
    ct0: str,
    max_pages: int,
    known_ids: set[str] | None = None,
    source: str = "bookmark",
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    return _collect(
        BOOKMARKS_QUERY_ID,
        "Bookmarks",
        {"count": 100, "includePromotedContent": False},
        ["bookmark_timeline_v2"],
        source,
        auth_token,
        ct0,
        max_pages,
        known_ids,
    )


def collect_likes(
    user_id: str,
    auth_token: str,
    ct0: str,
    max_pages: int,
    known_ids: set[str] | None = None,
    source: str = "favorite",
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    return _collect(
        LIKES_QUERY_ID,
        "Likes",
        {
            "userId": str(user_id),
            "count": 100,
            "includePromotedContent": False,
            "withClientEventToken": False,
            "withBirdwatchNotes": False,
            "withVoice": True,
        },
        ["user", "result", "timeline"],
        source,
        auth_token,
        ct0,
        max_pages,
        known_ids,
    )
