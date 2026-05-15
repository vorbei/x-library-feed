#!/usr/bin/env python3
"""Per-site cleanup of scraped article bodies.

CF Browser Rendering and trafilatura both leave a lot of page chrome in the
extracted markdown — top navigation, accessibility skip links, "Sign up" CTAs,
footers, related-post grids, podcast timecodes. This module applies a few
generic rules plus per-host nav-vocabulary detectors so the reader sees actual
article prose.

The shape is one pure function: `clean_article_text(text, url) -> text`. Run
it from any sync pipeline before storing `article_text` (and / or backfill
across previously-stored records — `clean_article_text` is idempotent).

Tag records with `cleaner_v` = `CLEANER_VERSION` after running. The sync
script can then skip re-cleaning until a new version ships.
"""

from __future__ import annotations

import re
import urllib.parse


CLEANER_VERSION = 1


# Per-host nav-vocabulary tokens. A line at the top of the document that is
# under ~240 chars and has ≥3 of these tokens, or whose words are mostly
# these tokens, is treated as a navigation row and dropped. Order them
# longest-first so substring matching catches them.
_PREFIX_NAV_TOKENS_BY_HOST = {
    "apple.com": [
        "iPhone 专区", "Apple Store", "iPhone", "iPad", "Mac", "Vision", "Watch",
        "TV", "AirPods", "Search", "Today", "Game", "App", "搜索", "游戏",
    ],
    "apps.apple.com": [
        "iPhone 专区", "iPhone", "iPad", "Mac", "Vision", "Watch", "TV", "AirPods",
        "Search", "Today", "Game", "App", "搜索", "游戏",
    ],
    "anthropic.com": [
        "Meet Claude", "Claude Code", "Claude Cowork", "Try Claude",
        "Get API access", "Products", "Claude", "API", "Solutions",
        "Research", "Commitments", "Learn", "News", "Company", "Login",
    ],
    "claude.com": [
        "Meet Claude", "Claude Code", "Claude Cowork", "Try Claude",
        "Get API access", "Products", "Claude", "API", "Solutions",
        "Research", "Commitments", "Learn", "News", "Company", "Login",
    ],
    "x.ai": [
        "Try Grok", "Grok", "API", "Company", "Colossus", "Careers",
        "News", "Shop", "SpaceX",
    ],
    "openai.com": [
        "OpenAI", "Sora", "ChatGPT", "API", "Research", "Safety", "Company",
        "Stories", "News", "Log in", "Sign up", "Pricing",
    ],
    "developers.openai.com": [
        "Documentation", "API reference", "Cookbook", "Forum", "Log in",
        "Sign up", "Solutions",
    ],
    "figma.com": [
        "Brand Guidelines", "Copy Logo as SVG", "Product Team", "Log in",
        "Sign up", "FigJam", "Slides", "Sites", "Design", "Make",
    ],
    "github.com": [
        "Skip to content", "Navigation Menu", "Toggle navigation",
        "Sign in", "Sign up", "Appearance settings", "Star this repository",
        "Watch this repository", "Fork this repository",
    ],
    "blog.cloudflare.com": [
        "Subscribe", "Cloudflare Blog", "Topics", "Recent posts",
    ],
    "blog.google": [
        "All stories", "Product news", "Company news", "Search Search",
        "Try Gemini", "Subscribe",
    ],
    "linear.app": [
        "Pricing", "Customers", "Now", "Method", "Changelog", "Docs",
        "Log in", "Sign up",
    ],
    "tailscale.com": [
        "Solutions", "Customers", "Pricing", "Docs", "Blog", "Log in",
        "Sign up", "Download",
    ],
    "raycast.com": [
        "Store", "Pricing", "Manual", "Blog", "Changelog", "Log in",
        "Download for free", "Download",
    ],
    "zed.dev": [
        "Download", "Features", "Pricing", "Docs", "Blog", "Community",
        "Log in",
    ],
    "framer.com": [
        "Templates", "Plugins", "Components", "Marketplace", "Resources",
        "Pricing", "Log in", "Sign up",
    ],
    "mercury.com": [
        "Products", "Business Banking", "Customers", "Pricing", "Resources",
        "Sign in", "Apply now",
    ],
}


# Patterns that, anywhere in the doc, are noise and get dropped per-line.
_SKIP_LINK_RE = re.compile(r"^\s*\[Skip to[^\]]+\]\([^)]+\)\s*$", re.I)
_POD_TIMECODE_RE = re.compile(r"^\s*\d+:\d{2}\s*(?:/\s*\d+:\d{2})?\s*$")
_NAV_BURGER_RE = re.compile(r"^\s*(Toggle\s+\w+|Open\s+menu|Close\s+menu)\s*$", re.I)
_SUBSCRIBE_INLINE_RE = re.compile(
    r"^\s*Subscribe(?:\s+to\s+\w+)?\s*$", re.I
)


# Once a line starts with one of these phrases (or is exactly one), treat
# everything below it as page footer / related-content rail. Be conservative:
# only lines that are clearly cut-offs, not phrases that could appear inside
# real prose ("Subscribe to" is too broad, "Subscribe to receive our newsletter"
# is fine).
_SUFFIX_KILLSWITCH_PREFIXES = (
    "More from ",
    "Recommended from Medium",
    "Sign up to discover",
    "Continue reading on ",
    "Subscribe to receive ",
    "Get the newsletter",
    "Get the latest",
    "Share this post",
    "Related posts",
    "Related articles",
    "You might also like",
    "Read more on ",
    "Footer",
    "© 20",  # © 2024, © 2025, etc.
    "Copyright ©",
    "All rights reserved",
)
_SUFFIX_KILLSWITCH_EXACT = {
    "Comments",
    "Discussion",
    "Tags",
    "About    Privacy    Terms",
    "About  Privacy  Terms",
}


def host_of(url: str) -> str:
    if not url:
        return ""
    try:
        h = urllib.parse.urlparse(url).netloc.lower()
        if h.startswith("www."):
            h = h[4:]
        return h
    except Exception:
        return ""


def _host_matches(host: str, target: str) -> bool:
    return host == target or host.endswith("." + target)


def _prefix_tokens_for(host: str) -> list[str]:
    if not host:
        return []
    for pattern, tokens in _PREFIX_NAV_TOKENS_BY_HOST.items():
        if _host_matches(host, pattern):
            return tokens
    return []


_SENTENCE_END_RE = re.compile(r"[.!?。！？:]$")
_WORD_RE = re.compile(r"[A-Za-z一-鿿]+")


def _line_is_prefix_nav(line: str, tokens: list[str]) -> bool:
    """A nav line: short, no sentence-final punctuation, made up of tokens
    we know the site uses as menu labels. Errs toward keeping content when
    the signal is weak."""
    s = line.strip()
    if not s:
        return True  # blank prefix lines get eaten while we're still skipping
    if _SKIP_LINK_RE.match(s) or _POD_TIMECODE_RE.match(s) or _NAV_BURGER_RE.match(s):
        return True
    if len(s) > 240:
        return False  # real prose
    if not tokens:
        return False
    # Strong signal: ≥3 distinct nav tokens appearing in the line.
    distinct_hits = sum(1 for tok in tokens if tok in s)
    if distinct_hits >= 3:
        return True
    # Or: line is all words and ≥50% of them are nav tokens (case-insensitive
    # substring match against any token).
    words = _WORD_RE.findall(s)
    if 2 <= len(words) <= 14 and not _SENTENCE_END_RE.search(s):
        nav_hits = 0
        for w in words:
            wl = w.lower()
            if any(t.lower() in wl or wl in t.lower() for t in tokens):
                nav_hits += 1
        if nav_hits >= max(2, (len(words) + 1) // 2):
            return True
    return False


def _line_is_suffix_killswitch(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if s in _SUFFIX_KILLSWITCH_EXACT:
        return True
    return any(s.startswith(p) for p in _SUFFIX_KILLSWITCH_PREFIXES)


def clean_article_text(text: str, url: str = "") -> str:
    """Return text with per-host prefix nav, suffix chrome, and generic
    skip-links / podcast timecodes / sub-burgers stripped. Idempotent."""
    if not text:
        return ""

    host = host_of(url)
    prefix_tokens = _prefix_tokens_for(host)
    lines = text.split("\n")

    # 1) Drop leading nav lines until the first real prose line.
    start = 0
    while start < len(lines) and _line_is_prefix_nav(lines[start], prefix_tokens):
        start += 1

    # 2) Find the suffix killswitch position.
    end = len(lines)
    for i in range(start, len(lines)):
        if _line_is_suffix_killswitch(lines[i]):
            end = i
            break

    body = lines[start:end]

    # 3) Inline cleanup — drop skip links / timecodes / burger-menu lines
    #    that appear inside the body, plus standalone "Subscribe" CTAs.
    body = [
        ln
        for ln in body
        if not _SKIP_LINK_RE.match(ln)
        and not _POD_TIMECODE_RE.match(ln)
        and not _NAV_BURGER_RE.match(ln)
        and not _SUBSCRIBE_INLINE_RE.match(ln)
    ]

    cleaned = "\n".join(body)
    # Collapse 3+ blank lines and trim.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


if __name__ == "__main__":
    # Quick smoke test from the command line:
    #   echo "..." | python3 clean_article.py https://example.com
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    text = sys.stdin.read()
    print(clean_article_text(text, url))
