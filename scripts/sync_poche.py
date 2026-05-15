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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONVEX_URL = "https://cloud.poche.app/api/query"
QUERY_PATH = "links/queries:getRecommendedLinks"
CATEGORIES_PATH = "links/queries:getExploreCategories"
TEAMS_PATH = "links/queries:getExploreTeams"

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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="public/feed-poche.json")
    parser.add_argument("--max-items", type=int, default=int(os.environ.get("POCHE_MAX_ITEMS", "2500")))
    parser.add_argument("--page-size", type=int, default=int(os.environ.get("POCHE_PAGE_SIZE", "100")))
    args = parser.parse_args()

    print("::notice::sync_poche.py start", file=sys.stderr)
    raw_items = fetch_paginated_links(max_items=args.max_items, page_size=args.page_size)
    items = [normalize_item(r) for r in raw_items if (r.get("url") or "").strip()]
    cats, teams = fetch_meta()

    doc = {
        "format": "poche-feed/1",
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_url": "https://poche.app/explore",
        "item_count": len(items),
        "categories": cats,
        "teams": teams,
        "items": items,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"::notice::wrote {out_path} ({out_path.stat().st_size/1024:.0f} KB, {len(items)} items)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
