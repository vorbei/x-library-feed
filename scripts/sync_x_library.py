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
from datetime import datetime, timedelta, timezone
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
        self.token_suffix = token_suffix
        suffix = f"_{token_suffix}" if token_suffix else ""
        self.env_suffix = suffix
        self.access_token = os.environ.get(f"X_USER_ACCESS_TOKEN{suffix}", "").strip()
        self.refresh_token = os.environ.get(f"X_REFRESH_TOKEN{suffix}", "").strip()
        self.client_id = os.environ.get(f"X_CLIENT_ID{suffix}", "").strip()
        self.client_secret = os.environ.get(f"X_CLIENT_SECRET{suffix}", "").strip()
        if token_suffix and not self.access_token and not self.refresh_token:
            self.env_suffix = ""
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
            state_file = os.environ.get("X_TOKEN_STATE_FILE", "").strip()
            secret_key = f"X_REFRESH_TOKEN{self.env_suffix}"
            if state_file:
                with open(state_file, "a", encoding="utf-8") as f:
                    f.write(f"{secret_key}={new_refresh}\n")
                print(
                    f"::notice::Rotated {secret_key}; queued for secret update.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"::warning::X returned a rotated refresh token for {secret_key}. "
                    "Update the GH secret manually, or set X_TOKEN_STATE_FILE so a "
                    "post-step can persist it.",
                    file=sys.stderr,
                )
            self.refresh_token = new_refresh
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
    path = parsed.path or ""
    # X long-form articles have real article body content — let them through so
    # CF Browser Rendering can pull the markdown. Other X URLs are still skipped.
    if ("x.com" in host or "twitter.com" in host) and "/article/" in path:
        return True
    if any(domain in host for domain in ["x.com", "twitter.com", "t.co", "pic.x.com"]):
        return False
    return True


CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
CF_API_BASE = "https://api.cloudflare.com/client/v4"
TEXT_USEFUL_MIN_CHARS = 200


def _x_cookies_for_cf() -> list[dict[str, Any]] | None:
    raw = os.environ.get("X_COOKIES_JSON", "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    normalised: list[dict[str, Any]] = []
    for c in parsed:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if not name or not value:
            continue
        entry: dict[str, Any] = {
            "name": str(name),
            "value": str(value),
            "domain": str(c.get("domain") or ".x.com"),
            "path": str(c.get("path") or "/"),
            "secure": True,
            "sameSite": "None",
        }
        if str(name) == "auth_token":
            entry["httpOnly"] = True
        normalised.append(entry)
    return normalised or None


def _trafilatura_extract(markup: str, url: str) -> dict[str, str] | None:
    try:
        import trafilatura  # type: ignore
    except ImportError:
        return None
    try:
        record = trafilatura.bare_extraction(
            markup,
            url=url,
            include_links=True,
            include_comments=False,
            favor_recall=True,
            output_format="python",
        )
    except Exception:
        return None
    if not record:
        return None
    body = record.get("text") if isinstance(record, dict) else getattr(record, "text", None)
    if not body:
        return None
    title = record.get("title") if isinstance(record, dict) else getattr(record, "title", None)
    description = record.get("description") if isinstance(record, dict) else getattr(record, "description", None)
    return {"text": body, "title": title or "", "description": description or ""}


_CF_REAL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def _cf_browser_rendering(url: str) -> dict[str, str] | None:
    if not (CF_ACCOUNT_ID and CF_API_TOKEN):
        return None
    endpoint = f"{CF_API_BASE}/accounts/{CF_ACCOUNT_ID}/browser-rendering/markdown"
    body_dict: dict[str, Any] = {
        "url": url,
        "userAgent": _CF_REAL_UA,
        "bestAttempt": True,
        "gotoOptions": {"waitUntil": "networkidle0", "timeout": 30000},
    }
    parsed_host = urllib.parse.urlparse(url).netloc.lower()
    if "x.com" in parsed_host or "twitter.com" in parsed_host:
        cookies = _x_cookies_for_cf()
        if cookies:
            body_dict["cookies"] = cookies
    body = json.dumps(body_dict).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {CF_API_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:300]}
    if not payload.get("success"):
        return {"error": str(payload.get("errors") or payload)[:300]}
    text = (payload.get("result") or "").strip()
    if not text:
        return {"error": "empty result"}
    return {"text": text}


def fetch_link_content(url: str) -> dict[str, Any]:
    parsed_host = urllib.parse.urlparse(url).netloc.lower()
    # X article pages are SPAs - urllib only gets the "enable JavaScript"
    # shell, which trafilatura then "successfully" extracts. Skip straight to
    # the CF Browser Rendering path so we capture the actual article body.
    if "x.com" in parsed_host or "twitter.com" in parsed_host:
        result: dict[str, Any] = {
            "url": url,
            "final_url": url,
            "content_type": "",
            "title": "",
            "description": "",
            "text_excerpt": "",
            "image_urls": [],
            "frameable": False,  # X actively blocks framing
        }
        cf = _cf_browser_rendering(url)
        if cf:
            if cf.get("text"):
                result["text_excerpt"] = cf["text"][:MAX_TEXT_EXCERPT_CHARS]
                result["extraction_source"] = "cf_browser_rendering"
            elif cf.get("error"):
                result["cf_fallback_error"] = cf["error"]
        result["kind"] = classify_link(url, result)
        return result

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
    markup: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            final_url = resp.geturl()
            content_type = resp.headers.get("content-type", "")
            xfo = resp.headers.get("x-frame-options", "")
            csp = resp.headers.get("content-security-policy", "")
            raw = resp.read(MAX_HTML_BYTES)
        result["final_url"] = final_url
        result["content_type"] = content_type
        result["frameable"] = _is_frameable(xfo, csp)
        if content_type.lower().startswith("image/"):
            result["image_urls"] = [final_url]
            result["kind"] = "image"
            return result
        if "html" in content_type.lower() or "xml" in content_type.lower():
            charset_match = re.search(r"charset=([^;]+)", content_type, re.I)
            encoding = charset_match.group(1).strip() if charset_match else "utf-8"
            markup = raw.decode(encoding, "replace")
    except Exception as e:
        result["fetch_error"] = str(e)[:300]

    if markup is not None:
        parser = PageExtractor(result["final_url"])
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
        extracted = _trafilatura_extract(markup, result["final_url"])
        if extracted:
            body = html.unescape(extracted["text"]).strip()
            if len(body) > len(result["text_excerpt"]):
                result["text_excerpt"] = body[:MAX_TEXT_EXCERPT_CHARS]
                result["extraction_source"] = "trafilatura"
            if extracted.get("title") and not result["title"]:
                result["title"] = extracted["title"]
            if extracted.get("description") and not result["description"]:
                result["description"] = extracted["description"]

    if len(result.get("text_excerpt") or "") < TEXT_USEFUL_MIN_CHARS:
        cf = _cf_browser_rendering(url)
        if cf:
            if cf.get("text") and len(cf["text"]) > len(result.get("text_excerpt") or ""):
                result["text_excerpt"] = cf["text"][:MAX_TEXT_EXCERPT_CHARS]
                result["extraction_source"] = "cf_browser_rendering"
            elif cf.get("error"):
                result["cf_fallback_error"] = cf["error"]

    result["kind"] = classify_link(url, result)
    if result["kind"] == "page":
        # Page cards don't need the text dump.
        result["text_excerpt"] = ""
    # Per-site cleanup: strip nav prefix, accessibility skip links, footer
    # rails, etc. Idempotent — also safe to call on already-cleaned text.
    try:
        from clean_article import clean_article_text, CLEANER_VERSION  # noqa: E402
        if result.get("text_excerpt"):
            cleaned = clean_article_text(result["text_excerpt"], url)
            if cleaned:
                result["text_excerpt"] = cleaned
                result["cleaner_v"] = CLEANER_VERSION
    except Exception:
        # Cleaning is best-effort; never fail the fetch over it.
        pass
    return result


# Hosts where the path layout makes the homepage / repo root / profile feel like
# an interactive page rather than a readable article. Listed as suffix matches.
_PAGE_LIKE_HOSTS = {
    "github.com",
    "gist.github.com",
    "huggingface.co",
    "gitlab.com",
    "bitbucket.org",
    "codeberg.org",
    "sourceforge.net",
    "npmjs.com",
    "pypi.org",
    "crates.io",
    "rubygems.org",
    "hub.docker.com",
    "producthunt.com",
    "linkedin.com",
}

# Hosts whose pages are almost always long-form prose worth rendering as markdown.
_ARTICLE_HOSTS = {
    "simonwillison.net",
    "every.to",
    "martinfowler.com",
    "emilkowal.ski",
    "yage.ai",
    "blog.google",
    "blog.cloudflare.com",
    "engineering.atspotify.com",
    "stratechery.com",
    "platformer.news",
    "ben-evans.com",
    "danluu.com",
    "paulgraham.com",
    "lwn.net",
    "openai.com",
    "anthropic.com",
    "www.anthropic.com",
    "developers.openai.com",
    "research.google",
    "ai.googleblog.com",
    "deepmind.google",
}

_ARTICLE_PATH_HINTS = (
    "/blog/", "/posts/", "/post/", "/article/", "/articles/", "/p/", "/news/",
    "/research/", "/papers/", "/docs/", "/wiki/", "/issues/", "/pull/", "/discussions/",
)
_ARTICLE_PATH_RE = re.compile(r"/(19|20)\d{2}/")
_NAV_SLOP_PHRASES = (
    "skip to content",
    "navigation menu",
    "toggle navigation",
    "appearance settings",
    "sign in",
    "sign up",
    "main navigation",
)


def _host_matches(host: str, hosts: set[str]) -> bool:
    host = host.lower().lstrip(".")
    return any(host == h or host.endswith("." + h) for h in hosts)


def _is_frameable(x_frame_options: str, content_security_policy: str) -> bool | None:
    """Return True/False/None based on standard anti-framing headers.

    None means we couldn't determine it (no headers received) — viewer treats
    that as "fall back to a preview card to be safe".
    """
    if not x_frame_options and not content_security_policy:
        return None
    xfo = (x_frame_options or "").strip().lower()
    if xfo in {"deny", "sameorigin"} or xfo.startswith("allow-from"):
        return False
    csp = (content_security_policy or "").lower()
    # Find a frame-ancestors directive if present.
    for directive in csp.split(";"):
        directive = directive.strip()
        if directive.startswith("frame-ancestors"):
            value = directive[len("frame-ancestors"):].strip()
            tokens = value.split()
            if not tokens:
                return False
            # 'none' or only self/scheme-restricted sources → not frameable
            if "'none'" in tokens:
                return False
            if all(t in {"'self'", "'none'"} for t in tokens):
                return False
            if "*" in tokens:
                return True
            # any explicit allowlist that isn't us → treat as not frameable
            return False
    return True


def classify_link(url: str, entry: dict[str, Any]) -> str:
    """Return one of 'article', 'page', 'image', or 'unknown'.

    URL heuristics win when they match; otherwise we look at the extracted text
    shape (nav-slop ratio, paragraph length) to decide.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path or "/"

    content_type = (entry.get("content_type") or "").lower()
    if content_type.startswith("image/"):
        return "image"
    if re.search(r"\.(jpg|jpeg|png|gif|webp|svg)(\?|$)", path, re.I):
        return "image"

    # X URLs that reach the classifier are /article/... long-form posts (the
    # only X URLs that pass should_fetch_link). Treat them as articles.
    if "x.com" in host or "twitter.com" in host:
        return "article" if "/article/" in path else "page"

    segments = [s for s in path.split("/") if s]

    # github / gist / similar dev-hub homepages → page
    if _host_matches(host, _PAGE_LIKE_HOSTS):
        if len(segments) <= 2:
            return "page"
        # github.com/<u>/<r>/<rest> — third segment decides
        if host.endswith("github.com") and len(segments) >= 3:
            kind_seg = segments[2].lower()
            if kind_seg in {"tree", "actions", "projects", "settings", "releases", "stargazers", "network", "graphs", "pulse", "commits", "branches"}:
                return "page"
            if kind_seg in {"blob", "raw", "wiki", "issues", "pull", "discussions"}:
                return "article"
            return "page"
        # other dev hubs: anything beyond /<u>/<r> we treat as article (file/issue/PR view)
        return "article"

    # explicit article hosts
    if _host_matches(host, _ARTICLE_HOSTS):
        return "article"

    # arxiv / hf paper landings
    if host.endswith("arxiv.org") and path.startswith(("/abs/", "/pdf/", "/html/")):
        return "article"
    if host.endswith("huggingface.co") and path.startswith("/papers/"):
        return "article"

    # wechat article URLs
    if host.endswith("mp.weixin.qq.com") and path.startswith("/s"):
        return "article"

    # Path hints that strongly suggest a dated article
    if any(hint in path for hint in _ARTICLE_PATH_HINTS) or _ARTICLE_PATH_RE.search(path):
        return "article"

    # Root or near-root paths on unknown hosts → likely a landing page
    if path in {"", "/"} or path.rstrip("/").lower() in {"/index", "/index.html", "/index.htm"}:
        return "page"

    # Fallback: look at extracted text shape
    text = (entry.get("text_excerpt") or "").strip()
    if not text:
        return "unknown"
    lowered = text.lower()
    nav_hits = sum(1 for phrase in _NAV_SLOP_PHRASES if phrase in lowered)
    if nav_hits >= 3:
        return "page"
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if paragraphs:
        median_len = sorted(len(p) for p in paragraphs)[len(paragraphs) // 2]
        if median_len < 80:
            return "page"
        if median_len >= 200:
            return "article"

    return "unknown"


def clean_text(tweet: dict[str, Any]) -> str:
    text = tweet.get("text") or ""
    for item in (tweet.get("entities") or {}).get("urls") or []:
        short = item.get("url")
        expanded = item.get("expanded_url") or ""
        if not short:
            continue
        if "pic.x.com" in expanded or "pic.twitter.com" in expanded:
            text = text.replace(short, "").strip()
            continue
        real = item.get("unwound_url") or expanded
        if real:
            text = text.replace(short, real)
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
    article_text = html.unescape(article.get("plain_text") or "").strip()
    article_preview = html.unescape(article.get("preview_text") or "").strip()
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
        "article_text": article_text,
        "article_preview_text": article_preview,
        "article_urls": extract_article_urls(tweet),
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
        for key in ["article_title", "article_url", "article_text", "article_preview_text", "article_urls"]:
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

    def sort_key(item: dict[str, Any]) -> tuple[str, int, str]:
        rank = fetched_order.get(item.get("id", ""), 999999)
        return (item.get("first_seen_at", ""), -rank, item.get("created_at", ""))

    return sorted(by_id.values(), key=sort_key, reverse=True)


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


_X_USER_TWEET_URL_RE = re.compile(
    r'^https?://(?:www\.)?(?:x\.com|twitter\.com)/[^/i][^/]*/(?:status|article)/(\d+)',
    re.IGNORECASE,
)


def extract_x_article_id(url: str) -> str | None:
    """Return the tweet id for an X tweet URL whose path is
    /<username>/(status|article)/<tweet_id>. The /i/article/<opaque_id> form
    is *not* handled here because that trailing id is X's internal article
    id, not a tweet id - those URLs are instead routed through the linked-
    content path so CF Browser Rendering can pull the article body directly.

    Status URLs return the same kind of id as the article URLs, so callers
    can batch them through /tweets?ids= and get back tweet text plus any
    embedded article and media without distinguishing.
    """
    m = _X_USER_TWEET_URL_RE.match(url)
    return m.group(1) if m else None


def fetch_referenced_x_articles(
    items: list[dict[str, Any]],
    existing: dict[str, dict[str, Any]],
    tokens: TokenProvider,
    users: dict[str, dict[str, Any]],
    max_fetches: int,
) -> dict[str, dict[str, Any]]:
    refs = dict(existing)
    own_ids = {it["id"] for it in items if it.get("id")}
    needed: list[str] = []
    seen_needed: set[str] = set()
    for item in items:
        for url in item.get("primary_urls") or []:
            tid = extract_x_article_id(url)
            if not tid or tid in refs or tid in own_ids or tid in seen_needed:
                continue
            seen_needed.add(tid)
            needed.append(tid)
            if len(needed) >= max_fetches:
                break
        if len(needed) >= max_fetches:
            break

    for start in range(0, len(needed), 100):
        chunk = needed[start : start + 100]
        params = {
            "ids": ",".join(chunk),
            "tweet.fields": TWEET_FIELDS,
            "expansions": EXPANSIONS,
            "user.fields": USER_FIELDS,
            "media.fields": MEDIA_FIELDS,
        }
        try:
            payload = api_get("/tweets", params, tokens)
        except RuntimeError as e:
            print(f"::warning::Referenced article fetch failed for batch: {e}", file=sys.stderr)
            continue
        for user in payload.get("includes", {}).get("users", []):
            users[user["id"]] = user
        media_map = {
            m["media_key"]: m
            for m in payload.get("includes", {}).get("media", [])
            if m.get("media_key")
        }
        for tweet in payload.get("data") or []:
            tweet["_media"] = [
                media_map[k]
                for k in (tweet.get("attachments") or {}).get("media_keys") or []
                if k in media_map
            ]
            author = users.get(tweet.get("author_id") or "", {})
            article = tweet.get("article") or {}
            refs[tweet["id"]] = {
                "id": tweet["id"],
                "url": tweet_url(tweet["id"], users, tweet.get("author_id")),
                "author": {
                    "username": author.get("username", ""),
                    "name": author.get("name", ""),
                },
                "text": clean_text(tweet),
                "article_title": article.get("title"),
                "article_text": html.unescape(article.get("plain_text") or "").strip(),
                "image_urls": media_urls(tweet),
                "fetched_at": iso_now(),
            }
        time.sleep(0.2)
    return refs


LINKED_FETCH_CONCURRENCY = int(os.environ.get("X_LINKED_FETCH_CONCURRENCY", "2"))


def update_linked_content(
    items: list[dict[str, Any]],
    existing_content: dict[str, dict[str, Any]],
    max_fetches: int,
    thread_urls_by_conv: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    content = dict(existing_content)
    thread_urls_by_conv = thread_urls_by_conv or {}

    # First pass: collect every URL referenced by an item, dedup, decide which
    # ones still need fetching (capped by max_fetches).
    item_url_lists: list[list[str]] = []
    to_fetch: list[str] = []
    seen_to_fetch: set[str] = set()
    for item in items:
        seen: set[str] = set()
        urls: list[str] = []
        item_thread_urls = thread_urls_by_conv.get(item.get("conversation_id", ""), [])
        for url in [
            *(item.get("primary_urls") or []),
            *(item.get("image_urls") or []),
            *item_thread_urls,
        ]:
            if url not in seen:
                seen.add(url)
                urls.append(url)
        item_url_lists.append(urls)
        for url in urls:
            if not should_fetch_link(url):
                continue
            if url in content or url in seen_to_fetch:
                continue
            if len(to_fetch) >= max_fetches:
                break
            seen_to_fetch.add(url)
            to_fetch.append(url)
        if len(to_fetch) >= max_fetches:
            pass  # let outer loop still gather item_url_lists for rebuild

    # Concurrent fetch.
    workers = max(1, min(LINKED_FETCH_CONCURRENCY, len(to_fetch))) if to_fetch else 0
    if workers:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(fetch_link_content, u): u for u in to_fetch}
            for fut in as_completed(futures):
                u = futures[fut]
                try:
                    content[u] = fut.result()
                except Exception as e:
                    content[u] = {"url": u, "final_url": u, "fetch_error": str(e)[:300]}

    # Backfill `kind`/`frameable` and strip nav-slop bodies from page-kind
    # entries. Cached entries that predate the classifier get their kind
    # computed in-place; `frameable` defaults to None (viewer treats that as
    # "render a preview card" to be safe).
    try:
        from clean_article import clean_article_text, CLEANER_VERSION
    except Exception:
        clean_article_text = None
        CLEANER_VERSION = 0
    backfilled_clean = 0
    for u, entry in content.items():
        if not isinstance(entry, dict):
            continue
        if "kind" not in entry:
            entry["kind"] = classify_link(u, entry)
        if "frameable" not in entry:
            entry["frameable"] = None
        if entry.get("kind") == "page":
            # Page cards only need title/description/image_urls — the long
            # nav-littered text dumps were the bulk of the JSON and aren't
            # rendered anymore.
            entry["text_excerpt"] = ""
            entry["text_excerpt_zh"] = ""
            entry.pop("text_excerpt_zh_hash", None)
            continue
        # Per-site cleanup backfill on the cached body. Run once per
        # CLEANER_VERSION bump so we never re-clean unchanged text.
        if clean_article_text and entry.get("cleaner_v") != CLEANER_VERSION:
            text = entry.get("text_excerpt") or ""
            if text.strip():
                new_text = clean_article_text(text, u)
                if new_text and new_text != text:
                    entry["text_excerpt"] = new_text
                    entry["text_excerpt_zh"] = ""
                    entry.pop("text_excerpt_zh_hash", None)
                    backfilled_clean += 1
            entry["cleaner_v"] = CLEANER_VERSION
    if backfilled_clean:
        print(
            f"::notice::clean_article: {backfilled_clean} linked-content entries re-cleaned",
            file=sys.stderr,
        )

    # Second pass: store URL lists per item (the viewer / markdown renderers
    # look up the full entry from `linked_content_by_url`). This drops ~2.7 MB
    # of duplicated data from the JSON payload.
    for item, urls in zip(items, item_url_lists):
        item_urls = []
        for url in urls:
            if not should_fetch_link(url):
                continue
            if url in content:
                item_urls.append(url)
        item["linked_content_urls"] = item_urls
        item.pop("linked_content", None)
    return content


# --- Phase 2: chunked JSON output -------------------------------------------
#
# The viewer used to fetch one ~8 MB JSON. With chunking we publish three
# files: a small index for the list pane (all items, no heavy bodies), a
# "recent" detail bundle for items first-seen in the last 30 days plus the
# linked-content / referenced-article context they touch, and a matching
# "archive" bundle for older items. Each detail bundle is self-contained so
# the viewer can load it independently. See viewer entry point for the
# fetch sequence (`index.html`'s onLoad handler).

RECENT_WINDOW_DAYS = 30

# Fields removed from items[] for the index file. These are big and only used
# in the right-pane "full content" view, so we hand them out lazily via the
# recent/archive bundles instead.
_INDEX_OMIT_FIELDS = (
    "article_text",
    "article_text_zh",
    "article_text_zh_hash",
)


def _index_item(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k not in _INDEX_OMIT_FIELDS}


def _detail_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """Minimal detail payload — only the heavy fields the index dropped. If
    the item has none of them (just a tweet, no article body), skip it so the
    detail bundle stays as small as possible."""
    delta: dict[str, Any] = {}
    for f in _INDEX_OMIT_FIELDS:
        v = item.get(f)
        if v is not None and v != "":
            delta[f] = v
    if not delta:
        return None
    delta["id"] = item.get("id")
    return delta


def _slice_context(
    items: list[dict[str, Any]],
    linked_content_by_url: dict[str, dict[str, Any]],
    referenced_articles_by_id: dict[str, dict[str, Any]],
    thread_urls_by_conversation: dict[str, list[str]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, list[str]]]:
    """Return the linked-content / referenced-article / thread-url subsets that
    the given item slice actually references. Keeps each detail bundle
    self-contained without duplicating the entire global dicts."""
    wanted_urls: set[str] = set()
    wanted_article_ids: set[str] = set()
    wanted_conv_ids: set[str] = set()
    for item in items:
        for url in item.get("linked_content_urls") or []:
            if url:
                wanted_urls.add(url)
        # Some items embed legacy linked_content blobs — keep working during
        # the rollover by walking those URLs too.
        for entry in item.get("linked_content") or []:
            u = entry.get("url") or entry.get("final_url") or ""
            if u:
                wanted_urls.add(u)
        for url in item.get("primary_urls") or []:
            article_id = extract_x_article_id(url)
            if article_id:
                wanted_article_ids.add(article_id)
        for ref in item.get("referenced_tweets") or []:
            rid = ref.get("id") if isinstance(ref, dict) else None
            if rid:
                wanted_article_ids.add(rid)
        conv = item.get("conversation_id")
        if conv:
            wanted_conv_ids.add(conv)
    linked = {u: linked_content_by_url[u] for u in wanted_urls if u in linked_content_by_url}
    refs = {i: referenced_articles_by_id[i] for i in wanted_article_ids if i in referenced_articles_by_id}
    threads = {c: thread_urls_by_conversation[c] for c in wanted_conv_ids if c in thread_urls_by_conversation}
    return linked, refs, threads


def _split_recent_archive(
    items: list[dict[str, Any]],
    cutoff: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    recent: list[dict[str, Any]] = []
    archive: list[dict[str, Any]] = []
    for item in items:
        ts = item.get("first_seen_at") or item.get("last_seen_at") or item.get("created_at") or ""
        try:
            seen = parse_datetime(ts) if ts else cutoff
        except Exception:
            seen = cutoff
        if seen >= cutoff:
            recent.append(item)
        else:
            archive.append(item)
    return recent, archive


def write_chunked_feed(
    store: dict[str, Any],
    index_path: Path,
    recent_path: Path,
    archive_path: Path,
) -> dict[str, int]:
    """Write index.json + feed-recent.json + feed-archive.json. Returns byte
    sizes for the manifest / commit log."""
    items = list(store.get("items") or [])
    linked = store.get("linked_content_by_url") or {}
    refs = store.get("referenced_articles_by_id") or {}
    threads = store.get("thread_urls_by_conversation") or {}

    cutoff = utc_now() - timedelta(days=RECENT_WINDOW_DAYS)
    recent_items, archive_items = _split_recent_archive(items, cutoff)
    recent_linked, recent_refs, recent_threads = _slice_context(
        recent_items, linked, refs, threads
    )
    archive_linked, archive_refs, archive_threads = _slice_context(
        archive_items, linked, refs, threads
    )

    index_doc = {
        "schema_version": store.get("schema_version"),
        "format": "feed-index/1",
        "updated_at": store.get("updated_at"),
        "accounts": store.get("accounts") or [],
        "recent_window_days": RECENT_WINDOW_DAYS,
        "recent_cutoff_at": cutoff.isoformat().replace("+00:00", "Z"),
        "recent_count": len(recent_items),
        "archive_count": len(archive_items),
        # Per-item: identical shape to the legacy items[] minus the heavy
        # article_text fields — viewer falls back to the legacy shape if the
        # detail bundles aren't fetched yet.
        "items": [_index_item(it) for it in items],
        "detail_files": {
            "recent": recent_path.name,
            "archive": archive_path.name,
        },
    }
    recent_detail = [d for d in (_detail_item(it) for it in recent_items) if d]
    archive_detail = [d for d in (_detail_item(it) for it in archive_items) if d]
    recent_doc = {
        "format": "feed-detail/1",
        "updated_at": store.get("updated_at"),
        "window": "recent",
        "recent_window_days": RECENT_WINDOW_DAYS,
        "items": recent_detail,
        "linked_content_by_url": recent_linked,
        "referenced_articles_by_id": recent_refs,
        "thread_urls_by_conversation": recent_threads,
    }
    archive_doc = {
        "format": "feed-detail/1",
        "updated_at": store.get("updated_at"),
        "window": "archive",
        "items": archive_detail,
        "linked_content_by_url": archive_linked,
        "referenced_articles_by_id": archive_refs,
        "thread_urls_by_conversation": archive_threads,
    }

    write_json(index_path, index_doc)
    write_json(recent_path, recent_doc)
    write_json(archive_path, archive_doc)
    return {
        "index": index_path.stat().st_size,
        "recent": recent_path.stat().st_size,
        "archive": archive_path.stat().st_size,
    }


def read_chunked_feed(
    index_path: Path,
    recent_path: Path,
    archive_path: Path,
    default: dict[str, Any],
) -> dict[str, Any]:
    """Reassemble the legacy single-store dict from the chunked layout.

    Falls back gracefully if any chunk is missing — including the very first
    run (no files) and the rollover run (legacy full JSON still present at
    index_path with shape `{items, linked_content_by_url, ...}`)."""
    index_doc = read_json(index_path, {})
    if not index_doc:
        return default
    fmt = (index_doc.get("format") or "").lower()
    if fmt != "feed-index/1":
        # Legacy full-store JSON predating chunking — return as-is.
        return index_doc
    recent_doc = read_json(recent_path, {})
    archive_doc = read_json(archive_path, {})

    # Items: the index has the canonical ordering but missing heavy fields.
    # Merge article_text / article_text_zh back in from detail bundles.
    detail_by_id: dict[str, dict[str, Any]] = {}
    for doc in (recent_doc, archive_doc):
        for item in doc.get("items") or []:
            iid = item.get("id")
            if iid:
                detail_by_id[iid] = item

    merged_items: list[dict[str, Any]] = []
    for item in index_doc.get("items") or []:
        detail = detail_by_id.get(item.get("id"))
        if detail:
            merged = {**item}
            for f in _INDEX_OMIT_FIELDS:
                if f in detail and detail[f] is not None:
                    merged[f] = detail[f]
            merged_items.append(merged)
        else:
            merged_items.append(item)

    linked: dict[str, dict[str, Any]] = {}
    refs: dict[str, dict[str, Any]] = {}
    threads: dict[str, list[str]] = {}
    for doc in (recent_doc, archive_doc):
        linked.update(doc.get("linked_content_by_url") or {})
        refs.update(doc.get("referenced_articles_by_id") or {})
        threads.update(doc.get("thread_urls_by_conversation") or {})

    return {
        "schema_version": index_doc.get("schema_version") or default.get("schema_version"),
        "updated_at": index_doc.get("updated_at") or default.get("updated_at"),
        "accounts": index_doc.get("accounts") or default.get("accounts") or [],
        "items": merged_items,
        "thread_urls_by_conversation": threads,
        "linked_content_by_url": linked,
        "referenced_articles_by_id": refs,
    }


def resolve_linked_content(
    item: dict[str, Any],
    linked_content_by_url: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the list of linked-content dicts for an item.

    Prefers the new `linked_content_urls` list (post Phase-1) and looks each
    URL up in `linked_content_by_url`. Falls back to a legacy embedded
    `linked_content` list for items that haven't been re-synced yet.
    """
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for url in item.get("linked_content_urls") or []:
        if url in seen:
            continue
        seen.add(url)
        entry = linked_content_by_url.get(url)
        if entry:
            entries.append(entry)
    for entry in item.get("linked_content") or []:
        u = entry.get("url") or entry.get("final_url") or ""
        if u and u in seen:
            continue
        if u:
            seen.add(u)
        entries.append(entry)
    return entries


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
        if item.get("article_text"):
            excerpt = textwrap.shorten(item.get("article_text"), width=300, placeholder="...")
            lines.append(f"- X article text: {excerpt}")
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
        linked_content = resolve_linked_content(item, store.get("linked_content_by_url") or {})
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
        if item.get("article_text"):
            lines.append("")
            lines.append("#### X Article")
            lines.append("")
            lines.append(markdown_quote(item.get("article_text") or "", width=4000))
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def clean_heading(text: str) -> str:
    # Strip standalone URLs so titles aren't dominated by raw status / article
    # links when the tweet body is mostly a quote-link.
    stripped = re.sub(r"https?://\S+", "", text or "")
    first_line = " ".join(stripped.split())
    if not first_line:
        return ""
    first_line = first_line.replace("[", "").replace("]", "")
    return textwrap.shorten(first_line, width=90, placeholder="...")


def best_item_title(item: dict[str, Any], referenced_articles: dict[str, dict[str, Any]]) -> str:
    """Pick a meaningful title for an item: article title > referenced article
    title > first sentence of tweet text > URL-only fallback."""
    if item.get("article_title"):
        return clean_heading(item["article_title"]) or item["article_title"]
    text_part = clean_heading(item.get("text") or "")
    if text_part:
        return text_part
    for url in item.get("primary_urls") or []:
        tid = extract_x_article_id(url)
        if tid:
            ref = referenced_articles.get(tid)
            if ref and ref.get("article_title"):
                return clean_heading(ref["article_title"]) or ref["article_title"]
            if ref and (ref.get("text") or "").strip():
                return clean_heading(ref["text"])
    primary = (item.get("primary_urls") or [""])[0]
    if primary:
        return clean_heading(primary)
    return ""


def write_markdown(path: Path, store: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(store), encoding="utf-8")


_LINK_RE = re.compile(r"https?://[^\s<>\"']+")


def _html_escape_text(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _linkify(text: str) -> str:
    parts: list[str] = []
    last = 0
    for m in _LINK_RE.finditer(text):
        parts.append(_html_escape_text(text[last : m.start()]))
        url = m.group(0).rstrip(".,;:!?")
        parts.append(f'<a href="{_html_escape_text(url)}">{_html_escape_text(url)}</a>')
        last = m.start() + len(url)
    parts.append(_html_escape_text(text[last:]))
    return "".join(parts).replace("\n", "<br/>")


def render_item_html(
    item: dict[str, Any],
    referenced_articles: dict[str, dict[str, Any]] | None = None,
    linked_content_by_url: dict[str, dict[str, Any]] | None = None,
) -> str:
    referenced_articles = referenced_articles or {}
    linked_content_by_url = linked_content_by_url or {}
    resolved_linked = resolve_linked_content(item, linked_content_by_url)
    author = item.get("author") or {}
    username = author.get("username") or "unknown"
    display_name = author.get("name") or username
    item_url = item.get("url") or ""
    parts: list[str] = []

    parts.append(
        f'<p><strong>{_html_escape_text(display_name)}</strong> '
        f'(<a href="https://x.com/{_html_escape_text(username)}">@{_html_escape_text(username)}</a>)</p>'
    )

    text = (item.get("text") or "").strip()
    if text:
        parts.append(f"<blockquote><p>{_linkify(text)}</p></blockquote>")

    if item.get("article_url"):
        title = item.get("article_title") or item.get("article_url")
        parts.append(
            f'<p>📄 X Article: <a href="{_html_escape_text(item["article_url"])}">'
            f'{_html_escape_text(title)}</a></p>'
        )
    article_text = (item.get("article_text") or "").strip()
    if article_text:
        body = "<br/>".join(_html_escape_text(p) for p in article_text.split("\n") if p.strip())
        if len(article_text) <= 1500:
            parts.append(f'<blockquote>{body}</blockquote>')
        else:
            parts.append(
                f'<details><summary>X Article full text ({len(article_text):,} chars)</summary>'
                + f'<div>{body}</div></details>'
            )

    referenced_blocks: list[str] = []
    referenced_extra_images: list[str] = []
    for url in item.get("primary_urls") or []:
        tid = extract_x_article_id(url)
        if not tid:
            continue
        ref = referenced_articles.get(tid)
        if not ref:
            continue
        ref_author = (ref.get("author") or {}).get("username", "")
        ref_url = ref.get("url") or url
        ref_article_text = (ref.get("article_text") or "").strip()
        ref_tweet_text = (ref.get("text") or "").strip()
        ref_title = ref.get("article_title") or ("X Article" if ref_article_text else "X Tweet")
        icon = "📄" if ref_article_text else "💬"
        header = (
            f'<p>{icon} Referenced X {"Article" if ref_article_text else "Tweet"} '
            f'<a href="{_html_escape_text(ref_url)}">{_html_escape_text(ref_title)}</a>'
            + (f' (@{_html_escape_text(ref_author)})' if ref_author else '')
            + '</p>'
        )
        body_text = ref_article_text or ref_tweet_text
        if body_text:
            body = "<br/>".join(_html_escape_text(p) for p in body_text.split("\n") if p.strip())
            # Short referenced tweets / short articles → inline directly so RSS
            # readers that don't expand <details> still surface the content.
            # Long article bodies stay collapsed to keep the feed compact.
            if len(body_text) <= 1500:
                referenced_blocks.append(
                    header + f'<blockquote>{body}</blockquote>'
                )
            else:
                summary = "Article full text" if ref_article_text else "Tweet text"
                referenced_blocks.append(
                    header
                    + f'<details><summary>{summary} ({len(body_text):,} chars)</summary>'
                    + f'<div>{body}</div></details>'
                )
        else:
            referenced_blocks.append(header)
        for img in ref.get("image_urls") or []:
            if img not in referenced_extra_images:
                referenced_extra_images.append(img)

    image_urls = item.get("image_urls") or []
    linked_image_urls = []
    for linked in resolved_linked:
        for url in linked.get("image_urls") or []:
            if url not in linked_image_urls and url not in image_urls:
                linked_image_urls.append(url)
    for img in referenced_extra_images:
        if img not in image_urls and img not in linked_image_urls:
            linked_image_urls.append(img)
    gallery = [*image_urls, *linked_image_urls][:8]
    if gallery:
        imgs = "".join(
            f'<img src="{_html_escape_text(u)}" alt="" style="max-width:480px;margin:4px"/>'
            for u in gallery
        )
        parts.append(f'<div>{imgs}</div>')

    if referenced_blocks:
        parts.append("<hr/>")
        parts.extend(referenced_blocks)

    linked_content = resolved_linked
    rendered_linked = []
    for linked in linked_content:
        url = linked.get("final_url") or linked.get("url") or ""
        if not url or url in gallery:
            continue
        title = linked.get("title") or linked.get("description") or url
        block = [
            f'<p>🔗 <a href="{_html_escape_text(url)}">{_html_escape_text(title)}</a></p>'
        ]
        excerpt = (linked.get("text_excerpt") or "").strip()
        if excerpt:
            body = "<br/>".join(_html_escape_text(p) for p in excerpt.split("\n") if p.strip())
            block.append(f'<blockquote>{body}</blockquote>')
        rendered_linked.append("".join(block))
    if rendered_linked:
        parts.append("<hr/>")
        parts.extend(rendered_linked)

    metrics = item.get("public_metrics") or {}
    metric_bits = []
    for key, label in [("like_count", "♥"), ("retweet_count", "🔁"), ("reply_count", "💬"), ("bookmark_count", "🔖"), ("impression_count", "👁")]:
        if metrics.get(key):
            metric_bits.append(f"{label} {metrics[key]:,}")
    if metric_bits:
        parts.append(f'<p style="color:#777;font-size:0.9em">{" · ".join(metric_bits)}</p>')

    if item_url:
        parts.append(
            f'<p><a href="{_html_escape_text(item_url)}">View on X →</a></p>'
        )
    return "".join(parts)


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
    referenced_articles = store.get("referenced_articles_by_id") or {}
    linked_content_by_url = store.get("linked_content_by_url") or {}
    updated = parse_datetime(store.get("updated_at"))

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">',
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
        title = best_item_title(item, referenced_articles)
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
        if item.get("article_text"):
            description_lines.append("X article text:")
            description_lines.append(textwrap.shorten(item.get("article_text"), width=2000, placeholder="..."))
        primary_urls = item.get("primary_urls") or []
        if primary_urls:
            description_lines.append("Primary URLs:")
            description_lines.extend(f"- {url}" for url in primary_urls)
        image_urls = item.get("image_urls") or []
        linked_content = resolve_linked_content(item, linked_content_by_url)
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
        author = item.get("author") or {}
        author_name = author.get("name") or author.get("username") or "unknown"
        html_body = render_item_html(item, referenced_articles, linked_content_by_url).replace("]]>", "]]&gt;")
        lines.extend(
            [
                "    <item>",
                f"      <title>{escape(title)}</title>",
                f"      <link>{escape(item_url)}</link>",
                f"      <guid isPermaLink=\"false\">{escape(str(guid))}</guid>",
                f"      <pubDate>{format_datetime(created, usegmt=True)}</pubDate>",
                f"      <dc:creator>{escape(author_name)}</dc:creator>",
                f"      <description>{escape(chr(10).join(description_lines))}</description>",
                f"      <content:encoded><![CDATA[{html_body}]]></content:encoded>",
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
    import sys as _sys; print("::notice::sync.py main() entered", file=_sys.stderr, flush=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", action="append", default=[], help="Account as user_id:username:xurl_user[:token_suffix]. Can be repeated.")
    parser.add_argument("--user-id", default=os.environ.get("X_USER_ID", ""), help="Legacy single-account user id.")
    parser.add_argument("--max-pages", type=int, default=int(os.environ.get("X_LIBRARY_MAX_PAGES", "5")))
    parser.add_argument("--max-thread-fetches", type=int, default=int(os.environ.get("X_THREAD_MAX_FETCHES", "40")))
    parser.add_argument("--json-out", default="public/x-bookmarks-favorites.json")
    parser.add_argument("--recent-out", default="public/feed-recent.json")
    parser.add_argument("--archive-out", default="public/feed-archive.json")
    parser.add_argument("--md-out", default="public/x-bookmarks-favorites.md")
    parser.add_argument("--archive-dir", default="archive/x-bookmarks-favorites")
    parser.add_argument("--rss-out", default="rss.xml")
    parser.add_argument("--public-base-url", default=os.environ.get("PUBLIC_BASE_URL", "https://vorbei.github.io/research-routine"))
    parser.add_argument("--auth-mode", choices=["auto", "oauth", "xurl"], default=os.environ.get("X_AUTH_MODE", "auto"))
    parser.add_argument("--max-linked-url-fetches", type=int, default=int(os.environ.get("X_LINKED_URL_MAX_FETCHES", "80")))
    parser.add_argument(
        "--max-referenced-article-fetches",
        type=int,
        default=int(os.environ.get("X_REFERENCED_ARTICLE_MAX_FETCHES", "100")),
    )
    parser.add_argument("--skip-referenced-articles", action="store_true")
    parser.add_argument("--skip-thread-urls", action="store_true")
    parser.add_argument("--skip-linked-content", action="store_true")
    args = parser.parse_args()

    accounts = load_accounts(args.account, args.user_id)
    if not accounts:
        raise SystemExit("Missing X account configuration")

    now = iso_now()
    json_path = Path(args.json_out)
    recent_path = Path(args.recent_out)
    archive_path = Path(args.archive_out)
    md_path = Path(args.md_out)
    archive_dir = Path(args.archive_dir)
    rss_path = Path(args.rss_out)

    existing = read_chunked_feed(
        json_path,
        recent_path,
        archive_path,
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
            print(f"Fetched {len(tweets)} {source} tweets for @{account['username']}", flush=True)

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
        linked_content = update_linked_content(
            items, linked_content, args.max_linked_url_fetches, thread_urls
        )

    referenced_articles = existing.get("referenced_articles_by_id") or {}
    if not args.skip_referenced_articles:
        referenced_articles = fetch_referenced_x_articles(
            items, referenced_articles, tokens, all_users, args.max_referenced_article_fetches
        )

    store = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now,
        "accounts": [{"user_id": a["user_id"], "username": a["username"]} for a in accounts],
        "items": items,
        "thread_urls_by_conversation": thread_urls,
        "linked_content_by_url": linked_content,
        "referenced_articles_by_id": referenced_articles,
    }
    chunk_sizes = write_chunked_feed(store, json_path, recent_path, archive_path)
    write_markdown(md_path, store)
    write_rss(rss_path, store, args.public_base_url)

    today_iso = utc_now().date().isoformat()
    todays_items = [
        item for item in store["items"]
        if (item.get("first_seen_at") or "")[:10] == today_iso
    ]
    todays_store = {**store, "items": todays_items}
    daily_md_path = archive_dir / f"{today_iso}.md"
    write_markdown(daily_md_path, todays_store)
    size_summary = ", ".join(f"{k}={v/1024/1024:.2f}MB" for k, v in chunk_sizes.items())
    print(
        f"Wrote {md_path}, {json_path} (+{recent_path.name}/{archive_path.name}: {size_summary}), "
        f"{rss_path}, and {daily_md_path} ({len(todays_items)} items first-seen today)"
    )


if __name__ == "__main__":
    main()
