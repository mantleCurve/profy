#!/usr/bin/env python3
"""Refresh the external English word-list layer and re-assemble the bundle.

Sources (fetched fresh on each run):
  - jojoee/leo-profanity            (MIT)
  - zautumnz/profane-words          (WTFPL, was zacanger/profane-words)
  - web-mech/badwords-list          (MIT)
  - LDNOOBW/List-of-...-Bad-Words   (CC-BY-4.0)

The normalized candidates of each source are stored in
``sources/external/english.json``; ``scripts/wordlist_layers.py`` then appends
the ones that are new to the upstream English word lists, in source order, to
``profy/data/languages/english.json``. New words default to Severity.HIGH per
profy.core._build_severity_map; words already classified in the upstream
`severity` buckets keep their severity, and false positives and mere suffix
variants of known words are skipped.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
import wordlist_layers as layers

ROOT = layers.ROOT
LANGUAGE = "english"

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


def collect() -> tuple[dict, dict[str, int]]:
    """Fetch every source before anything is written, so a failed download
    leaves the layer and the bundle untouched."""
    layer: dict = {"sources": {}}
    dropped: dict[str, int] = {}
    for name, url in SOURCES.items():
        print(f"[fetch] {name} <- {url}", file=sys.stderr)
        try:
            candidates = PARSERS[name](fetch(url))
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise SystemExit(f"failed to fetch {name} from {url}: {error}") from error
        words: dict[str, None] = {}
        dropped[name] = 0
        for candidate in candidates:
            normalized = normalize(candidate)
            if normalized is None:
                dropped[name] += 1
            else:
                words.setdefault(normalized, None)
        layer["sources"][name] = {"url": url, "words": list(words)}
    return layer, dropped


def main() -> int:
    upstream_path = layers.upstream_dir(ROOT) / f"{LANGUAGE}.json"
    if not upstream_path.is_file():
        raise SystemExit(f"{upstream_path.relative_to(ROOT)} is missing; run scripts/sync_from_blasp.py first")
    layer, dropped = collect()
    layers.write_if_changed(layers.external_dir(ROOT) / f"{LANGUAGE}.json", layers.dumps(layer))
    changed = layers.assemble(ROOT)

    upstream = layers.read_json(upstream_path)
    merged, added = layers.merge_external(
        upstream.get("profanities", []),
        upstream.get("false_positives", []),
        upstream.get("severity", {}),
        layers.external_sources(layer),
    )
    for name in SOURCES:
        print(f"[merge] {name}: +{added[name]} new, dropped {dropped[name]}", file=sys.stderr)
    print(
        f"[done] total profanities now: {len(merged)} "
        f"(added: {sum(added.values())}; bundle {'updated' if changed else 'unchanged'})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
