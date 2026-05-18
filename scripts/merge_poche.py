#!/usr/bin/env python3
"""Merge sync_poche's just-produced feed-poche.json onto a freshly
fetched origin/main, preserving any translation fields the translator
may have added in the meantime.

Sync Poche Feed runs take ~15 minutes (fetching link content for new
items). Translate Feed can finish and push during that window, adding
title_zh / description_zh / article_text_zh fields to feed-poche.json.
A plain `git rebase` on top of those translation commits hits content-
level conflicts in the JSON that git can't resolve, and the whole sync
run is discarded (see run 26014267321 for an example).

This script is the inverse of merge_translations.py:
- `feed-poche.json.poche-update` = sync_poche's output (has fresh
  items list + new article_text + cleaner_v bumps + top-level
  updated_at / item_count / categories / teams)
- `public/feed-poche.json` = fresh origin/main (has whatever
  translation fields translate has added since we started)

We take the sidecar as the base (because sync_poche's content
mutations are what we're trying to land), then copy translation-
shaped fields (suffix _zh, _zh_hash, _translator_v) from each fresh-
main item into the matching sidecar item by id. Result lands as
public/feed-poche.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

TRANSLATION_SUFFIXES = ("_zh", "_zh_hash", "_zh_translator_v")


def is_translation_field(key: str) -> bool:
    return any(key.endswith(s) for s in TRANSLATION_SUFFIXES)


def copy_translation_fields(src: dict[str, Any], dst: dict[str, Any]) -> int:
    """Copy translation-shaped fields from src into dst when src has a
    truthy value. Returns number of fields written."""
    updated = 0
    for k, v in src.items():
        if not is_translation_field(k):
            continue
        # Only carry over actually-populated translations. An empty
        # article_text_zh on main usually means sync_poche cleared it
        # because article_text changed — we shouldn't reinstate it.
        if v in (None, ""):
            continue
        if dst.get(k) != v:
            dst[k] = v
            updated += 1
    return updated


def main() -> int:
    out_path = Path("public/feed-poche.json")
    sidecar = out_path.with_suffix(out_path.suffix + ".poche-update")
    if not sidecar.exists():
        print("::warning::merge_poche: no sidecar at", sidecar, file=sys.stderr)
        return 0
    if not out_path.exists():
        # No prior translations to preserve — sidecar is the answer.
        out_path.write_bytes(sidecar.read_bytes())
        print("::notice::merge_poche: no main file; promoted sidecar as-is", file=sys.stderr)
        return 0

    fresh = json.loads(out_path.read_text(encoding="utf-8"))
    update = json.loads(sidecar.read_text(encoding="utf-8"))

    fresh_by_id = {it.get("id"): it for it in (fresh.get("items") or []) if it.get("id")}
    update_items = update.get("items") or []
    updated_field_count = 0
    for it in update_items:
        f = fresh_by_id.get(it.get("id"))
        if f:
            updated_field_count += copy_translation_fields(f, it)

    update["items"] = update_items
    out_path.write_text(
        json.dumps(update, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"::notice::merge_poche: applied to {out_path} "
        f"({len(update_items)} items, {updated_field_count} translation fields preserved)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
