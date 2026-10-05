#!/usr/bin/env python3
"""Assemble Profy's bundled language files from layered word-list sources.

Layers (deterministic JSON kept in the repository, not shipped in the wheel):

- ``sources/upstream/<language>.json``: the word lists exported from
  Blaspsoft/blasp by ``scripts/sync_from_blasp.py`` (``severity``,
  ``profanities`` and ``false_positives``).
- ``sources/external/<language>.json``: normalized candidate words per external
  source, fetched by ``scripts/sync_external_sources.py``.

In the shipped bundle ``profy/data/languages/<language>.json`` the word-list keys
are always regenerated from the layers, while every other key (``substitutions``
and anything else Profy owns) is kept exactly as edited locally. The bundle is a
pure function of the layers, so running either sync script alone, in any order,
produces the same bundle.

Run this module directly to re-assemble after editing a layer by hand::

    python scripts/wordlist_layers.py [--check]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
WORDLIST_KEYS = ("severity", "profanities", "false_positives")
SUFFIX_VARIANTS = ("s", "es", "ed", "er", "ers", "ly", "y", "ing", "ings")


def bundle_dir(root: Path) -> Path:
    return root / "profy" / "data" / "languages"


def upstream_dir(root: Path) -> Path:
    return root / "sources" / "upstream"


def external_dir(root: Path) -> Path:
    return root / "sources" / "external"


def dumps(data: object) -> str:
    return json.dumps(data, indent=4, ensure_ascii=False) + "\n"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_if_changed(path: Path, text: str, *, check: bool = False) -> bool:
    """Write ``text`` unless the file already holds it; report whether it differs."""
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    if not check:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return True


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


def merge_external(
    profanities: Sequence[str],
    false_positives: Sequence[str],
    severity: Mapping[str, Sequence[str]],
    sources: Mapping[str, Sequence[str]],
) -> tuple[list[str], dict[str, int]]:
    """Append external words that are new, not false positives, and not mere
    suffix variants; returns the merged list and the number added per source."""
    index = {word.lower() for word in profanities}
    for bucket in severity.values():
        index.update(word.lower() for word in bucket)
    excluded = {word.lower() for word in false_positives}

    merged = list(profanities)
    added: dict[str, int] = {}
    for name, words in sources.items():
        count = 0
        for word in words:
            if word in excluded or word in index or is_redundant_suffix_variant(word, index):
                continue
            merged.append(word)
            index.add(word)
            count += 1
        added[name] = count
    return merged, added


def external_sources(layer: Mapping[str, object]) -> dict[str, list[str]]:
    return {name: list(source["words"]) for name, source in dict(layer.get("sources", {})).items()}


def assemble_language(
    upstream: Mapping[str, object],
    external: Optional[Mapping[str, object]],
    existing: Mapping[str, object],
) -> dict:
    wordlists = {key: upstream[key] for key in WORDLIST_KEYS if key in upstream}
    if external is not None:
        wordlists["profanities"], _ = merge_external(
            wordlists.get("profanities", []),
            wordlists.get("false_positives", []),
            wordlists.get("severity", {}),
            external_sources(external),
        )

    # Keep the bundle's key order; word-list keys upstream no longer provides go.
    bundle: dict = {}
    for key, value in existing.items():
        if key not in WORDLIST_KEYS:
            bundle[key] = value
        elif key in wordlists:
            bundle[key] = wordlists[key]
    for key in WORDLIST_KEYS:
        if key in wordlists and key not in bundle:
            bundle[key] = wordlists[key]
    return bundle


def _layers(directory: Path) -> dict[str, dict]:
    return {path.stem: read_json(path) for path in sorted(directory.glob("*.json"))}


def assemble(
    root: Path,
    *,
    check: bool = False,
    upstream: Optional[Mapping[str, Mapping[str, object]]] = None,
    external: Optional[Mapping[str, Mapping[str, object]]] = None,
) -> list[Path]:
    """Regenerate every bundle that has upstream word lists.

    ``upstream``/``external`` replace the on-disk layers (used by ``--check``
    runs, which must not write the new layers first). Returns the bundle files
    that changed, or would change when ``check`` is true.
    """
    upstream = _layers(upstream_dir(root)) if upstream is None else upstream
    external = _layers(external_dir(root)) if external is None else external
    changed: list[Path] = []
    for language in sorted(upstream):
        target = bundle_dir(root) / f"{language}.json"
        existing = read_json(target) if target.is_file() else {}
        bundle = assemble_language(upstream[language], external.get(language), existing)
        if write_if_changed(target, dumps(bundle), check=check):
            changed.append(target)
    return changed


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 if the bundle is out of date; write nothing")
    args = parser.parse_args(argv)

    changed = assemble(ROOT, check=args.check)
    for path in changed:
        print(f"{'out of date' if args.check else 'assembled'}: {path.relative_to(ROOT).as_posix()}")
    if not changed:
        print("Bundled language files are up to date.")
    return 1 if args.check and changed else 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
