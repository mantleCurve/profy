# Third-party data attributions

Profy bundles profanity wordlists derived from the following upstream sources.
All sources are used under permissive licenses; copies of those licenses are
preserved in their respective repositories.

## Original code lineage

- **Blaspsoft/blasp** — MIT License — https://github.com/Blaspsoft/blasp
  Profy's regex engine, substitution machinery and separator handling are
  derived from the Blasp PHP implementation. The per-language word lists
  (`profanities`, `false_positives` and the curated `severity` buckets) are
  synced from Blasp's `config/languages/*.php` by `scripts/sync_from_blasp.py`
  into `sources/upstream/`.

## Bundled wordlist sources (merged via `scripts/sync_external_sources.py`)

- **jojoee/leo-profanity** — MIT License —
  https://github.com/jojoee/leo-profanity
  Source: `dictionary/default.json`.

- **zautumnz/profane-words** (formerly zacanger/profane-words) — WTFPL —
  https://github.com/zautumnz/profane-words
  Source: `words.json`.

- **web-mech/badwords-list** — MIT License —
  https://github.com/web-mech/badwords-list
  Source: `lib/array.ts`.

- **LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words** —
  Creative Commons Attribution 4.0 International (CC BY 4.0) —
  https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words
  Source: `en`.
  License text: https://creativecommons.org/licenses/by/4.0/legalcode

The normalized candidates of each source are kept in
`sources/external/english.json`; `scripts/wordlist_layers.py` appends the ones
new to the upstream English lists to the `profanities` array of
`profy/data/languages/english.json`. Words classified in the `severity` buckets
retain their original severity; everything else defaults to `Severity.HIGH` per
`profy.core._build_severity_map`.
