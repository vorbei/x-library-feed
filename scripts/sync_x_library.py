#!/usr/bin/env python3
"""Sync X bookmarks and favorites into a public Markdown data source.

The script is designed for GitHub Actions: it uses only the Python standard
library, reads an OAuth 2.0 user access token from secrets, merges newly fetched
items with the checked-in JSON cache, writes a public Markdown file, and updates
today's archive snapshot.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


API_BASE = "https://api.x.com/2"
OAUTH_TOKEN_URL = "https://api.x.com/2/oauth2/token"
SCHEMA_VERSION = 1
DEFAULT_XURL_ACCOUNTS = [
    {"user_id": "643123", "username": "chumsdock", "xurl_user": "chumsdock", "token_suffix": "1"},
    {
        "user_id": "2017610375295086592",
        "username": "CatHanami97880",
        "xurl_user": "CatHanami97880",
        "token_suffix": "2",
    },
]

TWEET_FIELDS = ",".join(
    [
        "created_at",
        "author_id",
        "text",
        "entities",
        "article",
        "attachments",
        "conversation_id",
        "referenced_tweets",
        "public_metrics",
    ]
)
EXPANSIONS = "author_id,attachments.media_keys"
USER_FIELDS = "username,name"
MEDIA_FIELDS = "media_key,type,url,preview_image_url,width,height,alt_text"
MAX_HTML_BYTES = 1_500_000
MAX_TEXT_EXCERPT_CHARS = 4000
FETCH_USER_AGENT = "Mozilla/5.0 (compatible; x-library-feed/1.0; +https://github.com/vorbei/x-library-feed)"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


class TokenProvider:
    def __init__(self, auth_mode: str = "auto", token_suffix: str = "") -> None:
        self.auth_mode = auth_mode
        suffix = f"_{token_suffix}" if token_suffix else ""
        self.access_token = os.environ.get(f"X_USER_ACCESS_TOKEN{suffix}", "").strip()
        self.refresh_token = os.environ.get(f"X_REFRESH_TOKEN{suffix}", "").strip()
        self.client_id = os.environ.get(f"X_CLIENT_ID{suffix}", "").strip()
        self.client_secret = os.environ.get(f"X_CLIENT_SECRET{suffix}", "").strip()
        if token_suffix and not self.access_token and not self.refresh_token:
            self.access_token = os.environ.get("X_USER_ACCESS_TOKEN", "").strip()
            self.refresh_token = os.environ.get("X_REFRESH_TOKEN", "").strip()
            self.client_id = os.environ.get("X_CLIENT_ID", "").strip()
            self.client_secret = os.environ.get("X_CLIENT_SECRET", "").strip()

    def get(self) -> str:
        if self.access_token:
            return self.access_token
        if self.refresh_token and self.client_id:
            self.access_token = self.refresh()
            return self.access_token
        raise SystemExit(
            "Missing X auth. Set X_USER_ACCESS_TOKEN, or set X_REFRESH_TOKEN "
            "and X_CLIENT_ID as GitHub Actions secrets. For local runs, use "
            "--auth-mode xurl when the xurl CLI is already authenticated."
        )

    def use_xurl(self) -> bool:
        if self.auth_mode == "xurl":
            return True
        if self.auth_mode == "oauth":
            return False
        return not (self.access_token or (self.refresh_token and self.client_id))

    def refresh(self) -> str:
        body = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.client_secret:
            raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
            import base64

            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")

        req = urllib.request.Request(
            OAUTH_TOKEN_URL,
            data=urllib.parse.urlencode(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:800]
            raise SystemExit(f"X token refresh failed ({e.code}): {detail}") from e

        new_refresh = payload.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            print(
                "::warning::X returned a rotated refresh token. Update the "
                "X_REFRESH_TOKEN repository secret if later runs start failing.",
                file=sys.stderr,
            )
        token = payload.get("access_token")
        if not token:
            raise SystemExit(f"X token refresh returned no access_token: {payload}")
        return token

    def refresh_after_unauthorized(self) -> bool:
        if not (self.refresh_token and self.client_id):
            return False
        self.access_token = self.refresh()
        return True


def api_get(
    path: str,
    params: dict[str, str | int],
    tokens: TokenProvider,
    retry_auth: bool = True,
) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    if tokens.use_xurl():
        endpoint = f"/2{path}?{query}"
        result = subprocess.run(["xurl", endpoint], capture_output=True, text=True)
        output = result.stdout.strip() or result.stderr.strip()
        if result.returncode != 0:
            raise RuntimeError(f"xurl GET {path} failed ({result.returncode}): {output[:1000]}")
        try:
            return json.loads(output)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"xurl GET {path} returned non-JSON output: {output[:1000]}") from e

    url = f"{API_BASE}{path}?{query}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {tokens.get()}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        if e.code == 401 and retry_auth and tokens.refresh_after_unauthorized():
            return api_get(path, params, tokens, retry_auth=False)
        raise RuntimeError(f"X API GET {path} failed ({e.code}): {detail[:1000]}") from e


def parse_account_spec(spec: str) -> dict[str, str]:
    parts = spec.split(":")
    if len(parts) == 1:
        return {"user_id": parts[0], "username": parts[0], "xurl_user": parts[0], "token_suffix": ""}
    if len(parts) == 2:
        return {"user_id": parts[0], "username": parts[1], "xurl_user": parts[1], "token_suffix": ""}
    if len(parts) == 3:
        return {"user_id": parts[0], "username": parts[1], "xurl_user": parts[2], "token_suffix": ""}
    return {"user_id": parts[0], "username": parts[1], "xurl_user": parts[2], "token_suffix": parts[3]}


def load_accounts(account_args: list[str], legacy_user_id: str) -> list[dict[str, str]]:
    accounts_json = os.environ.get("X_ACCOUNTS_JSON", "").strip()
    if accounts_json:
        raw = json.loads(accounts_json)
        return [
            {
                "user_id": str(item["user_id"]),
                "username": str(item.get("username") or item["user_id"]),
                "xurl_user": str(item.get("xurl_user") or item.get("username") or item["user_id"]),
                "token_suffix": str(item.get("token_suffix") or ""),
            }
            for item in raw
        ]
    if account_args:
        return [parse_account_spec(spec) for spec in account_args]
    if legacy_user_id:
        username = os.environ.get("X_USERNAME", legacy_user_id)
        return [{"user_id": legacy_user_id, "username": username, "xurl_user": username, "token_suffix": ""}]
    return DEFAULT_XURL_ACCOUNTS


def switch_xurl_user(xurl_user: str) -> None:
    result = subprocess.run(
        ["xurl", "auth", "default", os.environ.get("XURL_APP", "maxgent"), xurl_user],
        capture_output=True,
        text=True,
    )
    output = result.stdout.strip() or result.stderr.strip()
    if result.returncode != 0:
        raise RuntimeError(f"Failed to switch xurl to {xurl_user}: {output[:1000]}")


def tweet_url(tweet_id: str, users: dict[str, dict[str, Any]], author_id: str | None) -> str:
    username = users.get(author_id or "", {}).get("username")
    if username:
        return f"https://x.com/{username}/status/{tweet_id}"
    return f"https://x.com/i/web/status/{tweet_id}"


def label_for_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    label = parsed.netloc + parsed.path
    if parsed.query:
        label += "?" + parsed.query
    return label.rstrip("/") or url


def extract_urls(tweet: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    entities = tweet.get("entities") or {}
    for item in entities.get("urls") or []:
        url = item.get("unwound_url") or item.get("expanded_url") or item.get("url")
        if not url or not str(url).startswith("http"):
            continue
        if "t.co/" in url or "pic.x.com" in url or "pic.twitter.com" in url:
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def extract_article_urls(tweet: dict[str, Any]) -> list[str]:
    article = tweet.get("article") or {}
    urls: list[str] = []
    seen: set[str] = set()
    for item in (article.get("entities") or {}).get("urls") or []:
        url = item.get("unwound_url") or item.get("expanded_url") or item.get("url")
        if url and str(url).startswith("http") and url not in seen:
            seen.add(url)
            urls.append(url)
    for url in re_find_urls(article.get("plain_text") or ""):
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def re_find_urls(text: str) -> list[str]:
    urls = []
    for match in re.finditer(r"https?://[^\s<>)\"']+", text):
        urls.append(match.group(0).rstrip(".,;:!?]"))
    return urls


class PageExtractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.description = ""
        self.images: list[str] = []
        self.text_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag in {"script", "style", "svg", "noscript"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            key = (attrs_dict.get("name") or attrs_dict.get("property") or "").lower()
            content = attrs_dict.get("content", "").strip()
            if key in {"description", "og:description", "twitter:description"} and content and not self.description:
                self.description = content
            if key in {"og:title", "twitter:title"} and content and not self.title:
                self.title = content
            if key in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"} and content:
                self.add_image(content)
            return
        if tag == "img":
            src = attrs_dict.get("src") or attrs_dict.get("data-src") or attrs_dict.get("data-original")
            if src:
                self.add_image(src)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self.title = (self.title + " " + text).strip()
            return
        if len(" ".join(self.text_parts)) < MAX_TEXT_EXCERPT_CHARS * 2:
            self.text_parts.append(text)

    def add_image(self, url: str) -> None:
        absolute = urllib.parse.urljoin(self.base_url, html.unescape(url.strip()))
        if absolute.startswith("http") and absolute not in self.images:
            self.images.append(absolute)

    def text_excerpt(self) -> str:
        text = " ".join(self.text_parts)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:MAX_TEXT_EXCERPT_CHARS]


def should_fetch_link(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower()
    if any(domain in host for domain in ["x.com", "twitter.com", "t.co", "pic.x.com"]):
        return False
    return True


def fetch_link_content(url: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": FETCH_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/*;q=0.8,*/*;q=0.5",
        },
        method="GET",
    )
    result: dict[str, Any] = {
        "url": url,
        "final_url": url,
        "content_type": "",
        "title": "",
        "description": "",
        "text_excerpt": "",
        "image_urls": [],
    }
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            final_url = resp.geturl()
            content_type = resp.headers.get("content-type", "")
            raw = resp.read(MAX_HTML_BYTES)
    except Exception as e:
        result["fetch_error"] = str(e)[:300]
        return result

    result["final_url"] = final_url
    result["content_type"] = content_type
    if content_type.lower().startswith("image/"):
        result["image_urls"] = [final_url]
        return result
    if "html" not in content_type.lower() and "xml" not in content_type.lower():
        return result

    charset_match = re.search(r"charset=([^;]+)", content_type, re.I)
    encoding = charset_match.group(1).strip() if charset_match else "utf-8"
    markup = raw.decode(encoding, "replace")
    parser = PageExtractor(final_url)
    try:
        parser.feed(markup)
    except Exception as e:
        result["fetch_error"] = f"HTML parse error: {e}"[:300]
    result.update(
        {
            "title": html.unescape(parser.title).strip(),
            "description": html.unescape(parser.description).strip(),
            "text_excerpt": html.unescape(parser.text_excerpt()).strip(),
            "image_urls": parser.images[:12],
        }
    )
    return result


def clean_text(tweet: dict[str, Any]) -> str:
    text = tweet.get("text") or ""
    for item in (tweet.get("entities") or {}).get("urls") or []:
        short = item.get("url")
        expanded = item.get("expanded_url") or ""
        if short and ("pic.x.com" in expanded or "pic.twitter.com" in expanded):
            text = text.replace(short, "").strip()
    return text


def collect_tweets(
    endpoint: str,
    user_id: str,
    source: str,
    tokens: TokenProvider,
    max_pages: int,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    tweets: list[dict[str, Any]] = []
    users: dict[str, dict[str, Any]] = {}
    pagination_token = None
    for page in range(1, max_pages + 1):
        params: dict[str, str | int] = {
            "max_results": 100,
            "tweet.fields": TWEET_FIELDS,
            "expansions": EXPANSIONS,
            "user.fields": USER_FIELDS,
            "media.fields": MEDIA_FIELDS,
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        payload = api_get(endpoint.format(user_id=user_id), params, tokens)
        for user in payload.get("includes", {}).get("users", []):
            users[user["id"]] = user
        media_map = {
            media["media_key"]: media
            for media in payload.get("includes", {}).get("media", [])
            if media.get("media_key")
        }
        batch = payload.get("data") or []
        for index, tweet in enumerate(batch):
            tweet["_source"] = source
            tweet["_source_rank"] = (page - 1) * 100 + index
            media_keys = (tweet.get("attachments") or {}).get("media_keys") or []
            tweet["_media"] = [media_map[key] for key in media_keys if key in media_map]
            tweets.append(tweet)
        next_token = payload.get("meta", {}).get("next_token")
        if not next_token:
            break
        pagination_token = next_token
        time.sleep(0.2)
    return tweets, users


def fetch_thread_urls(
    conversation_id: str,
    author_id: str | None,
    tokens: TokenProvider,
    users: dict[str, dict[str, Any]],
) -> list[str]:
    params = {
        "query": f"conversation_id:{conversation_id}",
        "max_results": 100,
        "tweet.fields": TWEET_FIELDS,
        "expansions": EXPANSIONS,
        "user.fields": USER_FIELDS,
        "media.fields": MEDIA_FIELDS,
    }
    try:
        payload = api_get("/tweets/search/recent", params, tokens)
    except RuntimeError as e:
        print(f"::warning::Thread URL fetch skipped for {conversation_id}: {e}", file=sys.stderr)
        return []
    for user in payload.get("includes", {}).get("users", []):
        users[user["id"]] = user
    media_map = {
        media["media_key"]: media
        for media in payload.get("includes", {}).get("media", [])
        if media.get("media_key")
    }
    urls: list[str] = []
    seen: set[str] = set()
    for tweet in payload.get("data") or []:
        if author_id and tweet.get("author_id") != author_id:
            continue
        media_keys = (tweet.get("attachments") or {}).get("media_keys") or []
        tweet["_media"] = [media_map[key] for key in media_keys if key in media_map]
        status_url = tweet_url(tweet["id"], users, tweet.get("author_id"))
        for url in [status_url, *extract_urls(tweet), *extract_article_urls(tweet), *media_urls(tweet)]:
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def media_entries(tweet: dict[str, Any]) -> list[dict[str, Any]]:
    entries = []
    for media in tweet.get("_media") or []:
        url = media.get("url") or media.get("preview_image_url")
        if not url:
            continue
        entries.append(
            {
                "type": media.get("type"),
                "url": url,
                "preview_url": media.get("preview_image_url"),
                "width": media.get("width"),
                "height": media.get("height"),
                "alt_text": media.get("alt_text", ""),
            }
        )
    return entries


def media_urls(tweet: dict[str, Any]) -> list[str]:
    urls = []
    seen: set[str] = set()
    for media in media_entries(tweet):
        url = media.get("url")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def item_from_tweet(
    tweet: dict[str, Any],
    users: dict[str, dict[str, Any]],
    source: str,
    account: dict[str, str],
    now: str,
) -> dict[str, Any]:
    author = users.get(tweet.get("author_id") or "", {})
    tweet_id = tweet["id"]
    primary_urls = []
    seen_urls: set[str] = set()
    for url in [*extract_urls(tweet), *extract_article_urls(tweet)]:
        if url not in seen_urls:
            seen_urls.add(url)
            primary_urls.append(url)
    article = tweet.get("article") or {}
    article_url = None
    if article:
        username = author.get("username")
        article_url = f"https://x.com/{username}/article/{tweet_id}" if username else None
    return {
        "id": tweet_id,
        "url": tweet_url(tweet_id, users, tweet.get("author_id")),
        "sources": [source],
        "accounts": [account["username"]],
        "created_at": tweet.get("created_at"),
        "first_seen_at": now,
        "last_seen_at": now,
        "author": {
            "id": tweet.get("author_id"),
            "username": author.get("username", ""),
            "name": author.get("name", ""),
        },
        "text": clean_text(tweet),
        "primary_urls": primary_urls,
        "media": media_entries(tweet),
        "image_urls": media_urls(tweet),
        "article_title": article.get("title"),
        "article_url": article_url,
        "conversation_id": tweet.get("conversation_id"),
        "referenced_tweets": tweet.get("referenced_tweets") or [],
        "public_metrics": tweet.get("public_metrics") or {},
    }


def merge_items(
    existing_items: list[dict[str, Any]],
    fetched_tweets: list[dict[str, Any]],
    users: dict[str, dict[str, Any]],
    now: str,
) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in existing_items if item.get("id")}
    fetched_order: dict[str, int] = {}
    for tweet in fetched_tweets:
        source = tweet["_source"]
        account = tweet["_account"]
        tweet_id = tweet["id"]
        fetched_order.setdefault(tweet_id, tweet.get("_source_rank", 999999))
        incoming = item_from_tweet(tweet, users, source, account, now)
        if tweet_id not in by_id:
            by_id[tweet_id] = incoming
            continue

        current = by_id[tweet_id]
        current["last_seen_at"] = now
        sources = sorted(set(current.get("sources", [])) | {source})
        current["sources"] = sources
        accounts = sorted(set(current.get("accounts", [])) | {account["username"]})
        current["accounts"] = accounts
        for key in ["url", "created_at", "text", "conversation_id", "referenced_tweets", "public_metrics"]:
            if incoming.get(key):
                current[key] = incoming[key]
        if incoming.get("author", {}).get("username"):
            current["author"] = incoming["author"]
        for key in ["article_title", "article_url"]:
            if incoming.get(key):
                current[key] = incoming[key]
        merged_urls = []
        seen_urls: set[str] = set()
        for url in [*(current.get("primary_urls") or []), *incoming["primary_urls"]]:
            if url not in seen_urls:
                seen_urls.add(url)
                merged_urls.append(url)
        current["primary_urls"] = merged_urls
        merged_media = []
        seen_media_urls: set[str] = set()
        for media in [*(current.get("media") or []), *incoming.get("media", [])]:
            url = media.get("url")
            if url and url not in seen_media_urls:
                seen_media_urls.add(url)
                merged_media.append(media)
        current["media"] = merged_media
        current["image_urls"] = [media["url"] for media in merged_media if media.get("url")]

    for item in by_id.values():
        sources = list(item.get("sources") or [])
        if any(source.startswith("bookmark@") for source in sources):
            sources = [source for source in sources if source != "bookmark"]
        if any(source.startswith("favorite@") for source in sources):
            sources = [source for source in sources if source != "favorite"]
        item["sources"] = sorted(set(sources))

    def sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
        rank = fetched_order.get(item.get("id", ""), 999999)
        return (rank, item.get("first_seen_at", ""), item.get("created_at", ""))

    return sorted(by_id.values(), key=sort_key, reverse=False)


def update_thread_urls(
    items: list[dict[str, Any]],
    existing_threads: dict[str, list[str]],
    tokens: TokenProvider,
    users: dict[str, dict[str, Any]],
    max_threads: int,
) -> dict[str, list[str]]:
    threads = dict(existing_threads)
    fetched = 0
    for item in items:
        conv_id = item.get("conversation_id")
        if not conv_id:
            continue
        if fetched >= max_threads:
            break
        author_id = (item.get("author") or {}).get("id")
        urls = fetch_thread_urls(conv_id, author_id, tokens, users)
        if urls:
            threads[conv_id] = urls
        fetched += 1
        time.sleep(0.2)
    return threads


def update_linked_content(
    items: list[dict[str, Any]],
    existing_content: dict[str, dict[str, Any]],
    max_fetches: int,
) -> dict[str, dict[str, Any]]:
    content = dict(existing_content)
    fetched = 0
    for item in items:
        urls = []
        seen: set[str] = set()
        for url in [*(item.get("primary_urls") or []), *(item.get("image_urls") or [])]:
            if url not in seen:
                seen.add(url)
                urls.append(url)
        item_content = []
        for url in urls:
            if not should_fetch_link(url):
                continue
            if url not in content:
                if fetched >= max_fetches:
                    continue
                content[url] = fetch_link_content(url)
                fetched += 1
                time.sleep(0.2)
            item_content.append(content[url])
        item["linked_content"] = item_content
    return content


def markdown_quote(text: str, width: int = 1000) -> str:
    collapsed = "\n".join(line.rstrip() for line in text.strip().splitlines()).strip()
    if len(collapsed) > width:
        collapsed = collapsed[: width - 1].rstrip() + "..."
    if not collapsed:
        return "> _(no text)_"
    return "\n".join(f"> {line}" if line else ">" for line in collapsed.splitlines())


def write_link_list(lines: list[str], urls: list[str]) -> None:
    if not urls:
        lines.append("- Primary URLs: none")
        return
    lines.append("- Primary URLs:")
    for url in urls:
        lines.append(f"  - [{label_for_url(url)}]({url})")


def render_markdown(store: dict[str, Any]) -> str:
    items = store.get("items") or []
    threads = store.get("thread_urls_by_conversation") or {}
    lines = [
        "# X Bookmarks + Favorites",
        "",
        f"Updated: {store.get('updated_at', '')}",
        f"Total items: {len(items)}",
        "",
        "This file is generated hourly from X bookmarks and favorites. It is intended to be a public, linkable Markdown data source.",
        "",
        "## Items",
        "",
    ]

    for item in items:
        author = item.get("author") or {}
        username = author.get("username") or "unknown"
        name = author.get("name") or ""
        created = item.get("created_at") or ""
        title = clean_heading(item.get("text") or item.get("url") or item.get("id"))
        lines.append(f"### @{username} {title}")
        lines.append("")
        if name:
            lines.append(f"- Author: {name} [@{username}](https://x.com/{username})")
        else:
            lines.append(f"- Author: [@{username}](https://x.com/{username})")
        lines.append(f"- Tweet URL: [{item.get('url')}]({item.get('url')})")
        if item.get("article_url"):
            title = item.get("article_title") or item.get("article_url")
            lines.append(f"- X article: [{title}]({item.get('article_url')})")
        lines.append(f"- Sources: {', '.join(item.get('sources') or [])}")
        if item.get("accounts"):
            lines.append(f"- Saved by: {', '.join(item.get('accounts') or [])}")
        if created:
            lines.append(f"- Tweet created: {created}")
        lines.append(f"- First seen: {item.get('first_seen_at', '')}")
        write_link_list(lines, item.get("primary_urls") or [])
        media = item.get("media") or []
        if media:
            lines.append("- Media URLs:")
            for media_item in media:
                url = media_item.get("url")
                if not url:
                    continue
                label = media_item.get("alt_text") or media_item.get("type") or label_for_url(url)
                lines.append(f"  - [{label}]({url})")
        linked_content = item.get("linked_content") or []
        if linked_content:
            lines.append("- Linked content:")
            for linked in linked_content:
                url = linked.get("final_url") or linked.get("url")
                title = linked.get("title") or linked.get("description") or label_for_url(url)
                lines.append(f"  - [{title}]({url})")
                if linked.get("image_urls"):
                    lines.append(f"    Images: {', '.join(linked.get('image_urls')[:4])}")
                if linked.get("text_excerpt"):
                    excerpt = textwrap.shorten(linked.get("text_excerpt"), width=240, placeholder="...")
                    lines.append(f"    Text: {excerpt}")

        thread_urls = threads.get(item.get("conversation_id") or "") or []
        if thread_urls:
            lines.append("- Thread URLs:")
            for url in thread_urls:
                lines.append(f"  - [{label_for_url(url)}]({url})")
        else:
            lines.append("- Thread URLs: none captured")

        metrics = item.get("public_metrics") or {}
        if metrics:
            metric_bits = [f"{key}={value}" for key, value in sorted(metrics.items())]
            lines.append(f"- Public metrics: {', '.join(metric_bits)}")
        lines.append("")
        lines.append(markdown_quote(item.get("text") or ""))
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def clean_heading(text: str) -> str:
    first_line = " ".join(text.strip().split())
    if not first_line:
        return ""
    first_line = first_line.replace("[", "").replace("]", "")
    return textwrap.shorten(first_line, width=90, placeholder="...")


def write_markdown(path: Path, store: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(store), encoding="utf-8")


def parse_datetime(value: str | None) -> datetime:
    if not value:
        return utc_now()
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
    except ValueError:
        return utc_now()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def render_rss(store: dict[str, Any], public_base_url: str, max_items: int = 100) -> str:
    base = public_base_url.rstrip("/")
    feed_url = f"{base}/rss.xml"
    markdown_url = f"{base}/public/x-bookmarks-favorites.md"
    archive_url = f"{base}/archive/x-bookmarks-favorites/{utc_now().date().isoformat()}.md"
    items = list(store.get("items") or [])[:max_items]
    updated = parse_datetime(store.get("updated_at"))

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">',
        "  <channel>",
        "    <title>X Bookmarks + Favorites</title>",
        f"    <link>{escape(markdown_url)}</link>",
        "    <description>Hourly generated X bookmarks and favorites feed.</description>",
        f"    <lastBuildDate>{format_datetime(updated, usegmt=True)}</lastBuildDate>",
        f"    <atom:link xmlns:atom=\"http://www.w3.org/2005/Atom\" href=\"{escape(feed_url)}\" rel=\"self\" type=\"application/rss+xml\" />",
        f"    <docs>{escape(archive_url)}</docs>",
    ]
    for item in items:
        author = item.get("author") or {}
        username = author.get("username") or "unknown"
        title = clean_heading(item.get("text") or item.get("url") or item.get("id"))
        title = f"@{username}: {title}" if title else f"@{username}"
        item_url = item.get("url") or markdown_url
        guid = item.get("id") or item_url
        created = parse_datetime(item.get("created_at") or item.get("first_seen_at"))
        description_lines = [
            markdown_quote(item.get("text") or "").replace("> ", "", 1),
            "",
            f"Tweet: {item_url}",
        ]
        if item.get("article_url"):
            description_lines.append(f"X article: {item.get('article_url')}")
        primary_urls = item.get("primary_urls") or []
        if primary_urls:
            description_lines.append("Primary URLs:")
            description_lines.extend(f"- {url}" for url in primary_urls)
        image_urls = item.get("image_urls") or []
        linked_content = item.get("linked_content") or []
        linked_image_urls = []
        for linked in linked_content:
            for url in linked.get("image_urls") or []:
                if url not in linked_image_urls:
                    linked_image_urls.append(url)
        if image_urls:
            description_lines.append("Image URLs:")
            description_lines.extend(f"- {url}" for url in image_urls)
        if linked_content:
            description_lines.append("Linked content:")
            for linked in linked_content[:5]:
                linked_url = linked.get("final_url") or linked.get("url")
                label = linked.get("title") or linked.get("description") or linked_url
                description_lines.append(f"- {label}: {linked_url}")
                if linked.get("text_excerpt"):
                    description_lines.append(textwrap.shorten(linked.get("text_excerpt"), width=500, placeholder="..."))
        media_xml = []
        rss_image_urls = []
        for url in [*image_urls, *linked_image_urls]:
            if url not in rss_image_urls:
                rss_image_urls.append(url)
        for url in rss_image_urls[:8]:
            media_xml.append(f"      <media:content url=\"{escape(url)}\" medium=\"image\" />")
        lines.extend(
            [
                "    <item>",
                f"      <title>{escape(title)}</title>",
                f"      <link>{escape(item_url)}</link>",
                f"      <guid isPermaLink=\"false\">{escape(str(guid))}</guid>",
                f"      <pubDate>{format_datetime(created, usegmt=True)}</pubDate>",
                f"      <description>{escape(chr(10).join(description_lines))}</description>",
                *media_xml,
                "    </item>",
            ]
        )
    lines.extend(["  </channel>", "</rss>", ""])
    return "\n".join(lines)


def write_rss(path: Path, store: dict[str, Any], public_base_url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_rss(store, public_base_url), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", action="append", default=[], help="Account as user_id:username:xurl_user[:token_suffix]. Can be repeated.")
    parser.add_argument("--user-id", default=os.environ.get("X_USER_ID", ""), help="Legacy single-account user id.")
    parser.add_argument("--max-pages", type=int, default=int(os.environ.get("X_LIBRARY_MAX_PAGES", "5")))
    parser.add_argument("--max-thread-fetches", type=int, default=int(os.environ.get("X_THREAD_MAX_FETCHES", "40")))
    parser.add_argument("--json-out", default="public/x-bookmarks-favorites.json")
    parser.add_argument("--md-out", default="public/x-bookmarks-favorites.md")
    parser.add_argument("--archive-dir", default="archive/x-bookmarks-favorites")
    parser.add_argument("--rss-out", default="rss.xml")
    parser.add_argument("--public-base-url", default=os.environ.get("PUBLIC_BASE_URL", "https://vorbei.github.io/research-routine"))
    parser.add_argument("--auth-mode", choices=["auto", "oauth", "xurl"], default=os.environ.get("X_AUTH_MODE", "auto"))
    parser.add_argument("--max-linked-url-fetches", type=int, default=int(os.environ.get("X_LINKED_URL_MAX_FETCHES", "80")))
    parser.add_argument("--skip-thread-urls", action="store_true")
    parser.add_argument("--skip-linked-content", action="store_true")
    args = parser.parse_args()

    accounts = load_accounts(args.account, args.user_id)
    if not accounts:
        raise SystemExit("Missing X account configuration")

    now = iso_now()
    json_path = Path(args.json_out)
    md_path = Path(args.md_out)
    archive_dir = Path(args.archive_dir)
    rss_path = Path(args.rss_out)

    existing = read_json(
        json_path,
        {
            "schema_version": SCHEMA_VERSION,
            "items": [],
            "thread_urls_by_conversation": {},
        },
    )

    all_tweets: list[dict[str, Any]] = []
    all_users: dict[str, dict[str, Any]] = {}
    for account in accounts:
        tokens = TokenProvider(args.auth_mode, account.get("token_suffix", ""))
        if tokens.use_xurl():
            switch_xurl_user(account["xurl_user"])
        for source, endpoint in [
            ("bookmark", "/users/{user_id}/bookmarks"),
            ("favorite", "/users/{user_id}/liked_tweets"),
        ]:
            tweets, users = collect_tweets(endpoint, account["user_id"], source, tokens, args.max_pages)
            for tweet in tweets:
                tweet["_account"] = account
                tweet["_source"] = f"{source}@{account['username']}"
            all_tweets.extend(tweets)
            all_users.update(users)
            print(f"Fetched {len(tweets)} {source} tweets for @{account['username']}")

    items = merge_items(existing.get("items") or [], all_tweets, all_users, now)
    thread_urls = existing.get("thread_urls_by_conversation") or {}
    if not args.skip_thread_urls:
        thread_urls = update_thread_urls(
            items,
            thread_urls,
            tokens,
            all_users,
            args.max_thread_fetches,
        )
    linked_content = existing.get("linked_content_by_url") or {}
    if not args.skip_linked_content:
        linked_content = update_linked_content(items, linked_content, args.max_linked_url_fetches)

    store = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now,
        "accounts": [{"user_id": a["user_id"], "username": a["username"]} for a in accounts],
        "items": items,
        "thread_urls_by_conversation": thread_urls,
        "linked_content_by_url": linked_content,
    }
    write_json(json_path, store)
    write_markdown(md_path, store)
    write_rss(rss_path, store, args.public_base_url)

    archive_path = archive_dir / f"{utc_now().date().isoformat()}.md"
    write_markdown(archive_path, store)
    print(f"Wrote {md_path}, {json_path}, {rss_path}, and {archive_path}")


if __name__ == "__main__":
    main()
