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


def is_english_dominant(text: str, threshold: float = 0.7) -> bool:
    """Return True when the text is mostly Latin script (≥threshold of non-space chars)."""
    text = text.strip()
    if len(text) < 40:
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
    parser.add_argument(
        "--max-calls",
        type=int,
        default=int(os.environ.get("DEEPSEEK_TRANSLATE_MAX_CALLS", "30")),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
    )
    args = parser.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("::error::DEEPSEEK_API_KEY not set; nothing to do.", file=sys.stderr)
        return 1

    path = Path(args.json)
    if not path.exists():
        print(f"::error::JSON not found at {path}", file=sys.stderr)
        return 1

    store = json.loads(path.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = store.get("items") or []
    lc: dict[str, dict[str, Any]] = store.get("linked_content_by_url") or {}
    refs: dict[str, dict[str, Any]] = store.get("referenced_articles_by_id") or {}

    calls = 0
    touched = 0

    def perform(record: dict[str, Any], src_key: str, zh_key: str, hash_key: str) -> bool:
        nonlocal calls, touched
        if calls >= args.max_calls:
            return False
        text = record.get(src_key) or ""
        new_text, new_hash, called = maybe_translate(
            text,
            record.get(zh_key),
            record.get(hash_key),
            api_key,
            args.model,
        )
        if called:
            calls += 1
            touched += 1
            record[zh_key] = new_text
            record[hash_key] = new_hash
            print(
                f"::notice::translated {src_key} ({len(text)} chars) — {calls}/{args.max_calls}",
                file=sys.stderr,
            )
            return True
        return False

    # Items' own article_text
    for item in items:
        if calls >= args.max_calls:
            break
        perform(item, "article_text", "article_text_zh", "article_text_zh_hash")

    # Linked content excerpts (X /article/ markdown bodies + external articles)
    for url, entry in lc.items():
        if calls >= args.max_calls:
            break
        perform(entry, "text_excerpt", "text_excerpt_zh", "text_excerpt_zh_hash")

    # Referenced articles fetched via X API
    for tid, ref in refs.items():
        if calls >= args.max_calls:
            break
        perform(ref, "article_text", "article_text_zh", "article_text_zh_hash")
        if calls >= args.max_calls:
            break
        perform(ref, "text", "text_zh", "text_zh_hash")

    print(
        f"::notice::translate_feed.py done — performed {calls} DeepSeek calls, "
        f"touched {touched} fields.",
        file=sys.stderr,
    )

    if touched == 0:
        return 0

    store["items"] = items
    store["linked_content_by_url"] = lc
    store["referenced_articles_by_id"] = refs
    path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
