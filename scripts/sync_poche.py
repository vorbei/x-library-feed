#!/usr/bin/env python3
"""Sync poche.app/explore recommendations into a public JSON feed.

Poche (https://poche.app) is a curated link-discovery app. Its /explore page
runs on Convex; the HTTP query at `https://cloud.poche.app/api/query` accepts
`{"path": "links/queries:getRecommendedLinks", "args": {"paginationOpts":
{"numItems": N, "cursor": <prev>}}, "format": "json"}` and returns paginated
items keyed by Convex doc id. We paginate until we hit `isDone` or a cap,
then write the result to `public/feed-poche.json` for the Lire viewer to load
alongside the X library feed.

The output schema is deliberately close to the X items schema so the viewer
can merge both into one list with minimal special-casing:

    {
      "format": "poche-feed/1",
      "updated_at": "<ISO8601>",
      "categories": [{name, count}, ...],
      "teams": [{id, name, slug, count}, ...],
      "items": [
        {
          "id": "<convex doc id>",          # globally unique per poche item
          "source_type": "poche",
          "source": "poche@<team-slug>",     # mirrors X's "bookmark@user"
          "url": "<canonical>",
          "domain": "<host>",
          "title": "<...>",
          "description": "<...>",
          "category": "<...>",
          "tags": [...],
          "og_image_url": "<...>",
          "embed_type": "twitter|web|...",
          "recommended_at": "<ISO8601>",
          "created_at": "<ISO8601>",
          "teams": [{name, slug}, ...],
          "x_status_id": "<id or null>",   # filled when url is x.com/.../status/<id>
        },
        ...
      ]
    }

The Lire viewer dedupes against its X library by comparing `x_status_id` to
existing item ids, and as a final fallback by URL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CONVEX_URL = "https://cloud.poche.app/api/query"
QUERY_PATH = "links/queries:getRecommendedLinks"
CATEGORIES_PATH = "links/queries:getExploreCategories"
TEAMS_PATH = "links/queries:getExploreTeams"

# Where to look for already-fetched URL content so we don't double-pay
# CF Browser Rendering calls. These files are written by sync_x_library.py
# and hold both the bulk linked_content_by_url cache and per-X-tweet
# referenced_articles_by_id bodies.
X_FEED_RECENT = Path("public/feed-recent.json")
X_FEED_ARCHIVE = Path("public/feed-archive.json")

X_STATUS_RE = re.compile(
    r"^https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/(\d+)", re.I
)


def convex_query(path: str, args: dict[str, Any]) -> Any:
    body = json.dumps({"path": path, "args": args, "format": "json"}).encode("utf-8")
    req = urllib.request.Request(
        CONVEX_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "lire-poche-sync/1.0 (+https://vorbei.github.io/x-library-feed)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("status") != "success":
        raise RuntimeError(f"Convex query {path!r} failed: {payload}")
    return payload["value"]


def fetch_paginated_links(max_items: int = 2500, page_size: int = 100) -> list[dict[str, Any]]:
    """Walk the Convex paginated query until isDone or max_items reached."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    while True:
        opts: dict[str, Any] = {"numItems": page_size, "cursor": cursor}
        try:
            res = convex_query(QUERY_PATH, {"paginationOpts": opts})
        except Exception as e:
            print(f"::warning::poche page {pages + 1} failed: {e}", file=sys.stderr)
            break
        page = res.get("page") or []
        out.extend(page)
        pages += 1
        print(
            f"::notice::poche page {pages}: +{len(page)} items (total {len(out)}, isDone={res.get('isDone')})",
            file=sys.stderr,
        )
        if res.get("isDone") or not page:
            break
        if len(out) >= max_items:
            print(f"::notice::poche cap reached at {max_items}", file=sys.stderr)
            break
        cursor = res.get("continueCursor")
        if not cursor:
            break
        time.sleep(0.25)  # be polite
    return out[:max_items]


def normalize_item(raw: dict[str, Any]) -> dict[str, Any]:
    """Map a Convex doc into the schema we publish."""
    url = (raw.get("url") or "").strip()
    domain = (raw.get("domain") or "").strip().lower()
    if not domain and url:
        try:
            domain = urllib.parse.urlparse(url).netloc.lower().lstrip(".")
        except Exception:
            domain = ""
    teams = []
    for t in raw.get("teams") or []:
        if not isinstance(t, dict):
            continue
        teams.append(
            {
                "id": t.get("_id"),
                "name": t.get("name") or "",
                "slug": t.get("slug") or "",
                "recommended_by": (t.get("recommendedByUser") or {}).get("name") or "",
                "recommended_at": _ms_to_iso(t.get("recommendedAt")),
            }
        )
    team_slug = teams[0]["slug"] if teams else "unknown"

    x_status_id = None
    m = X_STATUS_RE.match(url) if url else None
    if m:
        x_status_id = m.group(1)

    return {
        "id": raw.get("_id") or "",
        "source_type": "poche",
        "source": f"poche@{team_slug}" if team_slug else "poche",
        "url": url,
        "domain": domain,
        "title": (raw.get("title") or "").strip(),
        "description": (raw.get("description") or "").strip(),
        "category": (raw.get("category") or "").strip(),
        "tags": list(raw.get("tags") or []),
        "og_image_url": raw.get("ogImageUrl") or "",
        "embed_type": raw.get("embedType") or "",
        "recommended_at": _ms_to_iso(raw.get("recommendedAt")),
        "created_at": _ms_to_iso(raw.get("_creationTime")),
        "teams": teams,
        "x_status_id": x_status_id,
    }


def _ms_to_iso(ms: float | int | None) -> str:
    if ms is None:
        return ""
    try:
        return (
            datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OSError):
        return ""


def fetch_meta() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        cats = convex_query(CATEGORIES_PATH, {}) or []
    except Exception as e:
        print(f"::warning::failed to fetch categories: {e}", file=sys.stderr)
        cats = []
    try:
        teams = convex_query(TEAMS_PATH, {}) or []
    except Exception as e:
        print(f"::warning::failed to fetch teams: {e}", file=sys.stderr)
        teams = []
    norm_cats = [{"name": c.get("name") or c.get("_id"), "count": int(c.get("count") or 0)} for c in cats]
    norm_teams = [
        {
            "id": t.get("_id") or "",
            "name": t.get("name") or "",
            "slug": t.get("slug") or "",
            "count": int(t.get("count") or 0),
        }
        for t in teams
    ]
    return norm_cats, norm_teams


def _load_x_lib_cache() -> dict[str, Any]:
    """Read the X library's recent + archive bundles for content URLs we may
    already have fetched. Used to avoid duplicate CF Browser Rendering calls
    when a poche URL is also linked from one of our X tweets."""
    linked: dict[str, dict[str, Any]] = {}
    refs: dict[str, dict[str, Any]] = {}
    for path in (X_FEED_RECENT, X_FEED_ARCHIVE):
        if not path.exists():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        linked.update(doc.get("linked_content_by_url") or {})
        refs.update(doc.get("referenced_articles_by_id") or {})
    return {"linked_content_by_url": linked, "referenced_articles_by_id": refs}


# Snippets we know come from page chrome rather than article content. When
# the scraped body starts with one of these the extractor lost the article
# boundary — better to keep nothing than to publish navigation menus as
# "article content". Includes both English originals and the Chinese
# translations the translator produced before this filter existed.
_NAV_SIGNATURES = (
    # English
    "Home Explore Notifications Chat Grok Premium",
    "To view keyboard shortcuts, press question mark",
    "View keyboard shortcuts",
    "Skip to content Navigation Menu",
    "Sign in Sign up",
    "Toggle navigation",
    "Loading...",
    "JavaScript is not available",
    "Enable JavaScript",
    # Chinese — produced by translating the X.com / github.com SPA shell
    "查看键盘快捷键，请按问号键",
    "首页 探索 通知 聊天 Grok",
    "首页](",  # markdown-link form: [首页](url)
    "查看新帖子",
    "跳过导航",
    "切换导航",
    "登录 注册",
)


def is_garbage_scrape(text: str) -> bool:
    """Return True when the scraped text is dominated by nav chrome rather
    than article prose. Cheap heuristic — exact-match on a few signature
    phrases at the head of the document covers the major culprits."""
    head = (text or "").strip()[:800]
    if not head:
        return True
    if any(sig in head for sig in _NAV_SIGNATURES):
        return True
    return False


_X_STATUS_RE = re.compile(
    r"^https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/\d+", re.I
)


def url_should_have_empty_body(url: str) -> bool:
    """X tweet pages (non-article) only render usefully through the X API,
    not through generic scraping. Any cached article_text on these URLs is
    SPA-shell garbage by definition."""
    if not url:
        return False
    return bool(_X_STATUS_RE.match(url) and "/article/" not in url)


def enrich_with_content(
    items: list[dict[str, Any]],
    cache: dict[str, dict[str, Any]],
    max_fetches: int,
) -> int:
    """Populate each item with article_text / article_title pulled either
    from the X library's existing fetch cache, or from a fresh CF Browser
    Rendering call (capped by max_fetches)."""
    # Lazy import — only need fetch_link_content when we actually run.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sync_x_library import fetch_link_content, should_fetch_link  # noqa: E402
    from clean_article import clean_article_text, CLEANER_VERSION  # noqa: E402

    # Negative-fetch cache: URLs that consistently return no body
    # (paywalled SPA shells, marketing pages with no scrapable prose,
    # YouTube/GitHub/App Store pages, etc.) were retried every run before
    # this, eating the per-run fetch budget so genuinely new items never
    # reached the front of the queue. We stamp items we attempted but
    # couldn't body with article_fetch_attempted_at and skip re-fetch
    # within this window — long enough to stop the bleeding, short enough
    # to recover from transient outages.
    fetch_retry_window = timedelta(days=7)
    now = datetime.now(timezone.utc)

    def attempted_recently(rec: dict[str, Any]) -> bool:
        ts = rec.get("article_fetch_attempted_at")
        if not ts:
            return False
        try:
            parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return False
        return (now - parsed) < fetch_retry_window

    linked = cache.get("linked_content_by_url") or {}
    refs = cache.get("referenced_articles_by_id") or {}
    fetched = 0
    reused = 0
    cleared_garbage = 0
    cleared_x_status = 0
    backfilled = 0
    skipped_negative_cache = 0
    for it in items:
        url = (it.get("url") or "").strip()
        # x.com / twitter.com status pages were fetched by older code that
        # didn't yet gate against the SPA shell — any cached body on those
        # URLs is nav garbage regardless of what is_garbage_scrape thinks.
        if url_should_have_empty_body(url) and (it.get("article_text") or "").strip():
            it["article_text"] = ""
            it["article_text_zh"] = ""
            it.pop("article_text_zh_hash", None)
            it.pop("article_text_zh", None)
            cleared_x_status += 1
        # Detect and clear garbage scrapes that slipped through earlier runs
        # (X.com SPA shell, github nav menus, etc.) before we look at the
        # body — leaving them in place defeats the whole point.
        existing = (it.get("article_text") or "").strip()
        if existing and is_garbage_scrape(existing):
            it["article_text"] = ""
            it["article_text_zh"] = ""
            it.pop("article_text_zh_hash", None)
            existing = ""
            cleared_garbage += 1
        # Backfill: run the per-site cleaner over any existing body whose
        # cleaner_v predates this build. Clean-on-clean is a no-op so this
        # stays safe across reruns.
        if existing and it.get("cleaner_v") != CLEANER_VERSION:
            new_text = clean_article_text(existing, url)
            if new_text != existing:
                it["article_text"] = new_text
                # Source text changed — invalidate the translation so the
                # next translate cron re-runs with the cleaned body.
                it["article_text_zh"] = ""
                it.pop("article_text_zh_hash", None)
                backfilled += 1
            it["cleaner_v"] = CLEANER_VERSION
        if (it.get("article_text") or "").strip():
            continue  # already populated from a previous run
        url = (it.get("url") or "").strip()
        if not url:
            continue
        # x.com / twitter.com URLs that aren't /article/ long-form posts
        # can't be cleanly scraped — the page is an SPA shell, we get back
        # the nav. Skip them; if the X library has the tweet body cached
        # under referenced_articles_by_id (the next check) that still wins.
        if not should_fetch_link(url):
            # should_fetch_link returns False for all non-article x.com /
            # twitter.com URLs (and a few media hosts). Fall through to the
            # X-cache check below; if that misses, leave the body empty.
            pass
        x_status_id = it.get("x_status_id")
        # 1) For X tweets we already cache the referenced article body.
        if x_status_id and x_status_id in refs:
            ref = refs[x_status_id]
            body = (ref.get("article_text") or ref.get("text") or "").strip()
            if body:
                it["article_text"] = body
                if ref.get("article_title"):
                    it["article_title_extracted"] = ref["article_title"]
                if ref.get("image_urls"):
                    it["article_image_urls"] = ref["image_urls"]
                reused += 1
                continue
        # 2) Anything else: try the X library's general link cache.
        lc = linked.get(url)
        if lc and (lc.get("text_excerpt") or "").strip():
            it["article_text"] = lc["text_excerpt"]
            if lc.get("title"):
                it["article_title_extracted"] = lc["title"]
            if lc.get("image_urls"):
                it["article_image_urls"] = lc["image_urls"]
            if lc.get("extraction_source"):
                it["article_extraction"] = lc["extraction_source"]
            reused += 1
            continue
        # 3) Fresh fetch via the same trafilatura → CF Browser Rendering path
        #    that sync_x_library uses, capped per run so the workflow stays
        #    under the runner timeout. Skip URLs the X library deliberately
        #    won't scrape (x.com status pages render as nav-only SPA shells).
        if not should_fetch_link(url):
            continue
        # Negative-cache skip: we tried this URL recently and got nothing
        # back. No point burning the budget on it again.
        if attempted_recently(it):
            skipped_negative_cache += 1
            continue
        if fetched >= max_fetches:
            continue
        try:
            res = fetch_link_content(url)
        except Exception as e:
            print(f"::warning::poche fetch {url}: {e}", file=sys.stderr)
            it["article_fetch_attempted_at"] = now.isoformat().replace("+00:00", "Z")
            it["article_fetch_error"] = str(e)[:200]
            continue
        fetched += 1
        body = (res.get("text_excerpt") or "").strip()
        if body:
            body = clean_article_text(body, url)
        if body and is_garbage_scrape(body):
            body = ""
        if body:
            it["article_text"] = body
            it["cleaner_v"] = CLEANER_VERSION
            # Real body landed — clear any prior negative-cache stamp so a
            # late-arriving article doesn't stay marked.
            it.pop("article_fetch_attempted_at", None)
        else:
            it["article_fetch_attempted_at"] = now.isoformat().replace("+00:00", "Z")
        if res.get("title"):
            it["article_title_extracted"] = res["title"]
        if res.get("image_urls"):
            it["article_image_urls"] = res["image_urls"]
        if res.get("extraction_source"):
            it["article_extraction"] = res["extraction_source"]
        if res.get("fetch_error"):
            it["article_fetch_error"] = res["fetch_error"]
    print(
        f"::notice::poche enrich: {fetched} fresh fetches, {reused} reused from X cache, "
        f"{cleared_garbage} prior garbage scrapes cleared, "
        f"{cleared_x_status} x.com SPA-shell bodies cleared by URL, "
        f"{backfilled} bodies re-cleaned by per-site rules, "
        f"{skipped_negative_cache} skipped by negative-fetch cache",
        file=sys.stderr,
    )
    return fetched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="public/feed-poche.json")
    parser.add_argument("--max-items", type=int, default=int(os.environ.get("POCHE_MAX_ITEMS", "2500")))
    parser.add_argument("--page-size", type=int, default=int(os.environ.get("POCHE_PAGE_SIZE", "100")))
    parser.add_argument("--max-fetches", type=int, default=int(os.environ.get("POCHE_MAX_FETCHES", "150")),
                        help="Upper bound on fresh URL fetches per run.")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Don't fetch article bodies for poche URLs.")
    args = parser.parse_args()

    print("::notice::sync_poche.py start", file=sys.stderr)
    raw_items = fetch_paginated_links(max_items=args.max_items, page_size=args.page_size)
    items = [normalize_item(r) for r in raw_items if (r.get("url") or "").strip()]
    cats, teams = fetch_meta()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Carry over previously-fetched article_text / translations so we don't
    # re-fetch the same URL or lose its zh translation just because the
    # poche API surfaced a slightly newer "recommendedAt" timestamp.
    prior_by_id: dict[str, dict[str, Any]] = {}
    if out_path.exists():
        try:
            prior_doc = json.loads(out_path.read_text(encoding="utf-8"))
            for p in prior_doc.get("items") or []:
                if p.get("id"):
                    prior_by_id[p["id"]] = p
        except Exception as e:
            print(f"::warning::could not read prior {out_path}: {e}", file=sys.stderr)
    carry_keys = (
        "article_text",
        "article_title_extracted",
        "article_image_urls",
        "article_extraction",
        "article_fetch_error",
        "article_fetch_attempted_at",
        "cleaner_v",
        "article_text_zh",
        "article_text_zh_hash",
        "article_text_zh_translator_v",
        "title_zh",
        "title_zh_hash",
        "title_zh_translator_v",
        "description_zh",
        "description_zh_hash",
        "description_zh_translator_v",
    )
    for it in items:
        old = prior_by_id.get(it.get("id"))
        if not old:
            continue
        for k in carry_keys:
            if old.get(k) and not it.get(k):
                it[k] = old[k]

    # Pull article bodies for as many URLs as the budget allows.
    if not args.skip_fetch:
        cache = _load_x_lib_cache()
        enrich_with_content(items, cache, args.max_fetches)

    doc = {
        "format": "poche-feed/1",
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_url": "https://poche.app/explore",
        "item_count": len(items),
        "categories": cats,
        "teams": teams,
        "items": items,
    }

    out_path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with_body = sum(1 for it in items if (it.get("article_text") or "").strip())
    print(
        f"::notice::wrote {out_path} ({out_path.stat().st_size/1024:.0f} KB, "
        f"{len(items)} items, {with_body} with article body)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
