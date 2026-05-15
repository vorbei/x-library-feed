#!/usr/bin/env python3
"""Translate English X article + linked-content excerpts to Chinese via DeepSeek.

Reads public/x-bookmarks-favorites.json, finds English-language bodies that
either lack a translation or have changed since the last run, and writes the
result back as `article_text_zh` / `text_excerpt_zh` alongside a `*_zh_hash`
fingerprint so repeat runs can skip unchanged content.

Designed for low-frequency cron (every 6h-ish): caps the number of API calls
per run via DEEPSEEK_TRANSLATE_MAX_CALLS so quota stays predictable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-chat"
SYSTEM_PROMPT = (
    "You are a precise English-to-Chinese (Simplified) technical translator. "
    "Translate the user's text faithfully into natural, fluent Simplified Chinese. "
    "Preserve technical terms, library/product names, and acronyms in English "
    "(e.g. agent, harness, LLM, Claude Code, Firecracker, MCP). "
    "Preserve markdown structure: headings (#), lists (-, 1.), links "
    "([text](url)), code (`code` and ```blocks```), blockquotes (>). "
    "Translate link text and inline prose; leave URLs unchanged. "
    "Do NOT add commentary, preamble, or any explanation. "
    "Output ONLY the translated content."
)


def is_english_dominant(text: str, threshold: float = 0.7, min_len: int = 40) -> bool:
    """Return True when the text is mostly Latin script (≥threshold of non-space chars).

    `min_len` gates very-short strings as noise. Default 40 chars works for
    tweet/article bodies; callers can lower it for known headline-y fields
    like poche.app titles where a single English phrase is meaningful.
    """
    text = text.strip()
    if len(text) < min_len:
        return False
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    if cjk / max(len(text), 1) > 0.05:
        return False
    latin = sum(1 for c in text if c.isascii() and (c.isalpha() or c.isspace() or c in "-_.,:;'\"()[]/"))
    return latin / max(len(text), 1) >= threshold


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def call_deepseek(
    api_key: str,
    text: str,
    model: str,
    timeout: int = 120,
    max_attempts: int = 3,
) -> str | None:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    data = json.dumps(body).encode("utf-8")
    last_err: str | None = None
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(
            DEEPSEEK_API_URL,
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            choice = (payload.get("choices") or [{}])[0]
            msg = (choice.get("message") or {}).get("content") or ""
            stripped = msg.strip()
            if stripped:
                return stripped
            last_err = "empty completion"
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last_err = f"HTTP {e.code}: {detail}"
            # 5xx → retry with backoff; 4xx → abort
            if 400 <= e.code < 500 and e.code not in (408, 429):
                break
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        if attempt < max_attempts:
            time.sleep(min(2 ** attempt, 8))
    print(f"::warning::DeepSeek call failed after {max_attempts} attempts: {last_err}", file=sys.stderr)
    return None


def maybe_translate(
    text: str,
    existing_zh: str | None,
    existing_hash: str | None,
    api_key: str,
    model: str,
) -> tuple[str | None, str | None, bool]:
    """Return (translation, hash, performed_call)."""
    if not text or not text.strip():
        return None, None, False
    if not is_english_dominant(text):
        return None, None, False
    new_hash = content_hash(text)
    if existing_zh and existing_hash == new_hash:
        return existing_zh, existing_hash, False
    translation = call_deepseek(api_key, text, model)
    if not translation:
        return existing_zh, existing_hash, False
    return translation, new_hash, True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default="public/x-bookmarks-favorites.json")
    parser.add_argument("--recent", default="public/feed-recent.json")
    parser.add_argument("--archive", default="public/feed-archive.json")
    parser.add_argument("--poche", default="public/feed-poche.json")
    parser.add_argument(
        "--max-calls",
        type=int,
        default=int(os.environ.get("DEEPSEEK_TRANSLATE_MAX_CALLS", "30")),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
    )
    parser.add_argument(
        "--priority-ids",
        default=os.environ.get("TRANSLATE_PRIORITY_IDS", ""),
        help="Comma-separated item ids translated first (bypasses --max-calls).",
    )
    args = parser.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("::error::DEEPSEEK_API_KEY not set; nothing to do.", file=sys.stderr)
        return 1

    index_path = Path(args.json)
    recent_path = Path(args.recent)
    archive_path = Path(args.archive)
    if not index_path.exists():
        print(f"::error::Feed index not found at {index_path}", file=sys.stderr)
        return 1

    # Phase 2 chunked the JSON into index + recent + archive. Load all three
    # back into a unified store so the translator can patch
    # article_text / linked_content / referenced_articles in one place; we'll
    # write the chunks back out at the end.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sync_x_library import read_chunked_feed, write_chunked_feed  # noqa: E402

    store = read_chunked_feed(index_path, recent_path, archive_path, {})
    items: list[dict[str, Any]] = store.get("items") or []
    lc: dict[str, dict[str, Any]] = store.get("linked_content_by_url") or {}
    refs: dict[str, dict[str, Any]] = store.get("referenced_articles_by_id") or {}

    # Also load the poche feed (if present) so its title/description fields
    # share the same DeepSeek pipeline.
    poche_path = Path(args.poche)
    poche_doc: dict[str, Any] = {}
    poche_items: list[dict[str, Any]] = []
    if poche_path.exists():
        try:
            poche_doc = json.loads(poche_path.read_text(encoding="utf-8"))
            poche_items = poche_doc.get("items") or []
        except Exception as e:
            print(f"::warning::could not read {poche_path}: {e}", file=sys.stderr)

    concurrency = max(1, int(os.environ.get("DEEPSEEK_TRANSLATE_CONCURRENCY", "3")))

    priority_ids = {p.strip() for p in args.priority_ids.split(",") if p.strip()}
    # Linked-content URLs / referenced-tweet ids touched by the priority items
    # — translations for them should also bypass the max-calls cap.
    priority_urls: set[str] = set()
    priority_ref_ids: set[str] = set()
    if priority_ids:
        for item in items:
            if item.get("id") not in priority_ids:
                continue
            for u in item.get("linked_content_urls") or []:
                if u:
                    priority_urls.add(u)
            for u in item.get("primary_urls") or []:
                m = re.match(
                    r"^https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/(?:status|article)/(\d+)",
                    u or "",
                    re.I,
                )
                if m:
                    priority_ref_ids.add(m.group(1))
            for r in item.get("referenced_tweets") or []:
                rid = r.get("id") if isinstance(r, dict) else None
                if rid:
                    priority_ref_ids.add(str(rid))

    # 1) Collect pending jobs (each = which dict to patch + which keys).
    Job = tuple[dict[str, Any], str, str, str]
    priority_jobs: list[Job] = []
    regular_jobs: list[Job] = []

    def maybe_enqueue(
        record: dict[str, Any],
        src_key: str,
        zh_key: str,
        hash_key: str,
        is_priority: bool,
        min_len: int = 40,
    ) -> None:
        text = record.get(src_key) or ""
        if not text or not text.strip() or not is_english_dominant(text, min_len=min_len):
            return
        new_hash = content_hash(text)
        if record.get(zh_key) and record.get(hash_key) == new_hash:
            return
        bucket = priority_jobs if is_priority else regular_jobs
        if not is_priority and len(regular_jobs) >= args.max_calls:
            return
        bucket.append((record, src_key, zh_key, hash_key))

    for item in items:
        is_pri = item.get("id") in priority_ids
        maybe_enqueue(item, "article_text", "article_text_zh", "article_text_zh_hash", is_pri)
        # Tweet bodies themselves need translation too — the summary
        # blockquote falls back to English when text_zh is missing.
        maybe_enqueue(item, "text", "text_zh", "text_zh_hash", is_pri)
    for url, entry in lc.items():
        is_pri = url in priority_urls
        maybe_enqueue(entry, "text_excerpt", "text_excerpt_zh", "text_excerpt_zh_hash", is_pri)
    for rid, ref in refs.items():
        is_pri = rid in priority_ref_ids
        maybe_enqueue(ref, "article_text", "article_text_zh", "article_text_zh_hash", is_pri)
        maybe_enqueue(ref, "text", "text_zh", "text_zh_hash", is_pri)
    # Poche items: title + description (no priority queue for poche yet).
    # Poche titles are headline-y and often short ("Poppy", "A Technical Deep
    # Dive into the New Raycast") — drop the 40-char floor for them, keep the
    # English-dominant gate so brand names like "Muxy" still skip.
    for p in poche_items:
        maybe_enqueue(p, "title", "title_zh", "title_zh_hash", False, min_len=12)
        maybe_enqueue(p, "description", "description_zh", "description_zh_hash", False)

    jobs: list[Job] = priority_jobs + regular_jobs

    print(
        f"::notice::translate_feed.py queued {len(jobs)} translation jobs "
        f"(max_calls={args.max_calls}, concurrency={concurrency})",
        file=sys.stderr,
    )

    if not jobs:
        return 0

    # 2) Run translations concurrently.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    completed = 0
    touched = 0

    def run_job(job: Job) -> tuple[Job, str | None]:
        record, src_key, _, _ = job
        text = record[src_key]
        return job, call_deepseek(api_key, text, args.model)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(run_job, j): j for j in jobs}
        for fut in as_completed(futures):
            job, translation = fut.result()
            record, src_key, zh_key, hash_key = job
            completed += 1
            if translation:
                record[zh_key] = translation
                record[hash_key] = content_hash(record[src_key])
                touched += 1
                print(
                    f"::notice::[{completed}/{len(jobs)}] translated {src_key} "
                    f"({len(record[src_key])} chars)",
                    file=sys.stderr,
                )
            else:
                print(
                    f"::warning::[{completed}/{len(jobs)}] failed {src_key} "
                    f"({len(record[src_key])} chars)",
                    file=sys.stderr,
                )

    print(
        f"::notice::translate_feed.py done — {touched}/{len(jobs)} succeeded.",
        file=sys.stderr,
    )
    calls = touched

    if touched == 0:
        return 0

    store["items"] = items
    store["linked_content_by_url"] = lc
    store["referenced_articles_by_id"] = refs
    write_chunked_feed(store, index_path, recent_path, archive_path)
    if poche_doc and poche_items:
        poche_doc["items"] = poche_items
        poche_path.write_text(
            json.dumps(poche_doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
