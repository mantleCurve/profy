#!/usr/bin/env python3
"""Merge external profanity wordlists into profy/data/languages/english.json.

Sources (fetched fresh on each run):
  - jojoee/leo-profanity            (MIT)
  - zautumnz/profane-words          (WTFPL, was zacanger/profane-words)
  - web-mech/badwords-list          (MIT)
  - LDNOOBW/List-of-...-Bad-Words   (CC-BY-4.0)

Words land in the `profanities` array, which defaults to Severity.HIGH per
profy.core._build_severity_map. Words already classified in the `severity`
buckets keep their existing severity (mild/moderate/extreme are not overwritten).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGLISH_PATH = ROOT / "profy" / "data" / "languages" / "english.json"

SOURCES = {
    "leo-profanity": "https://raw.githubusercontent.com/jojoee/leo-profanity/master/dictionary/default.json",
    "profane-words": "https://raw.githubusercontent.com/zautumnz/profane-words/master/words.json",
    "badwords-list": "https://raw.githubusercontent.com/web-mech/badwords-list/main/lib/array.ts",
    "ldnoobw-en": "https://raw.githubusercontent.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words/master/en",
}

ALLOWED = re.compile(r"^[a-z0-9 _\-@$*!\.]+$")


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode("utf-8")


def parse_leo(text: str) -> list[str]:
    return list(json.loads(text))


def parse_zautumn(text: str) -> list[str]:
    return list(json.loads(text))


def parse_badwords_ts(text: str) -> list[str]:
    return re.findall(r"'([^']+)'", text)


def parse_ldnoobw(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


PARSERS = {
    "leo-profanity": parse_leo,
    "profane-words": parse_zautumn,
    "badwords-list": parse_badwords_ts,
    "ldnoobw-en": parse_ldnoobw,
}


def normalize(word: str) -> str | None:
    word = word.strip().lower()
    if len(word) < 2 or len(word) > 40:
        return None
    if not ALLOWED.match(word):
        return None
    if word.isdigit():
        return None
    return word


SUFFIX_VARIANTS = ("s", "es", "ed", "er", "ers", "ly", "y", "ing", "ings")


def is_redundant_suffix_variant(word: str, index: set[str]) -> bool:
    """Skip imports like 'fuckings' when 'fucking' is already classified.

    These add no signal and trigger boundary-bleed false positives because
    the trailing `s+` can attach to the next word's leading `s`.
    """
    for suffix in SUFFIX_VARIANTS:
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            stem = word[: -len(suffix)]
            if stem in index:
                return True
    return False


def main() -> int:
    existing = json.loads(ENGLISH_PATH.read_text(encoding="utf-8"))
    profanities: list[str] = list(existing.get("profanities", []))
    false_positives = {w.lower() for w in existing.get("false_positives", [])}
    existing_index = {w.lower() for w in profanities}
    for bucket in existing.get("severity", {}).values():
        existing_index.update(w.lower() for w in bucket)

    per_source_counts: dict[str, int] = {}
    skipped: dict[str, int] = {}

    for name, url in SOURCES.items():
        print(f"[fetch] {name} <- {url}", file=sys.stderr)
        raw = fetch(url)
        candidates = PARSERS[name](raw)
        added = 0
        dropped = 0
        for candidate in candidates:
            normalized = normalize(candidate)
            if normalized is None:
                dropped += 1
                continue
            if normalized in false_positives:
                dropped += 1
                continue
            if normalized in existing_index:
                continue
            if is_redundant_suffix_variant(normalized, existing_index):
                dropped += 1
                continue
            profanities.append(normalized)
            existing_index.add(normalized)
            added += 1
        per_source_counts[name] = added
        skipped[name] = dropped
        print(f"[merge] {name}: +{added} new, dropped {dropped}", file=sys.stderr)

    existing["profanities"] = profanities

    ENGLISH_PATH.write_text(
        json.dumps(existing, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"[done] total profanities now: {len(profanities)} "
        f"(added: {sum(per_source_counts.values())})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
