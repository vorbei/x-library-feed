#!/usr/bin/env python3
"""Merge translation fields from `.translated` sidecar files onto the
current versions of feed JSONs.

The Translate Feed workflow runs `translate_feed.py` which can take an
hour. While it's running, Sync X Library Feed pushes its own commits to
the same JSON files (adding new tweets, refreshing media etc.). A plain
`git rebase` on top of those commits hits content-level conflicts in
the JSON that git cannot resolve, and the whole translation run gets
discarded.

This script avoids that. The workflow snapshots the translated files as
`<path>.translated`, resets the working tree to a fresh `origin/main`,
then calls us. We re-apply ONLY the translation-shaped fields
(suffix `_zh`, `_zh_hash`, `_translator_v`) from the sidecar onto the
fresh files, preserving every change Sync X Library just landed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

TRANSLATION_SUFFIXES = ("_zh", "_zh_hash", "_zh_translator_v")


def is_translation_field(key: str) -> bool:
    return any(key.endswith(s) for s in TRANSLATION_SUFFIXES)


def merge_dict_translations(fresh: dict[str, Any], translated: dict[str, Any]) -> int:
    """Copy translation-shaped fields from `translated` into `fresh` in
    place. Returns the number of fields updated."""
    updated = 0
    for k, v in translated.items():
        if is_translation_field(k) and fresh.get(k) != v:
            fresh[k] = v
            updated += 1
    return updated


def merge_items_list(fresh_items: list[dict[str, Any]], translated_items: list[dict[str, Any]]) -> int:
    """Match items by id and merge translation fields. Items that exist
    only in `translated` (e.g. tweets the sync workflow has since deleted)
    are dropped — fresh main is the source of truth for membership."""
    by_id = {it.get("id"): it for it in translated_items if it.get("id")}
    updated = 0
    for it in fresh_items:
        t = by_id.get(it.get("id"))
        if t:
            updated += merge_dict_translations(it, t)
    return updated


def merge_keyed_dict(
    fresh: dict[str, dict[str, Any]],
    translated: dict[str, dict[str, Any]],
) -> int:
    """For dicts keyed by url / article_id (e.g. linked_content_by_url,
    referenced_articles_by_id). Keys that only exist in `translated` are
    dropped for the same reason as merge_items_list."""
    updated = 0
    for k, fresh_entry in fresh.items():
        t = translated.get(k)
        if isinstance(t, dict) and isinstance(fresh_entry, dict):
            updated += merge_dict_translations(fresh_entry, t)
    return updated


def merge_file(fresh_path: Path, translated_path: Path) -> int:
    """Merge one JSON file. Returns total field count updated."""
    fresh = json.loads(fresh_path.read_text(encoding="utf-8"))
    translated = json.loads(translated_path.read_text(encoding="utf-8"))
    updated = 0

    fresh_items = fresh.get("items")
    t_items = translated.get("items")
    if isinstance(fresh_items, list) and isinstance(t_items, list):
        updated += merge_items_list(fresh_items, t_items)

    for key in ("linked_content_by_url", "referenced_articles_by_id"):
        if isinstance(fresh.get(key), dict) and isinstance(translated.get(key), dict):
            updated += merge_keyed_dict(fresh[key], translated[key])

    if updated:
        fresh_path.write_text(
            json.dumps(fresh, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return updated


def main() -> int:
    paths = [
        Path("public/x-bookmarks-favorites.json"),
        Path("public/feed-recent.json"),
        Path("public/feed-archive.json"),
        Path("public/feed-poche.json"),
    ]
    total = 0
    for p in paths:
        sidecar = p.with_suffix(p.suffix + ".translated")
        if not p.exists() or not sidecar.exists():
            continue
        updated = merge_file(p, sidecar)
        print(f"::notice::merge_translations: {p} +{updated} fields", file=sys.stderr)
        total += updated
    print(f"::notice::merge_translations: total {total} fields applied", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
