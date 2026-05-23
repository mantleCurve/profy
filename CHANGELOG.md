# Changelog

## 0.1.1

- First non-alpha release. Same code as 0.1.0a2; dropping the `aN` suffix to publish a stable PyPI version.

## 0.1.0a2

- Merged additional English wordlists from leo-profanity (MIT), zautumnz/profane-words (WTFPL), web-mech/badwords-list (MIT), and LDNOOBW (CC BY 4.0). `profy/data/languages/english.json` now carries ~2663 base profanities (up from ~1316). New entries default to `Severity.HIGH`; pre-existing curated `severity.mild`/`moderate`/`extreme` classifications are preserved.
- Added vowel-elision matching for single-vowel profanities of length ≥4 with ≥3 consonants, so obfuscations like `fck`, `sht`, `dmn`, `hll` are caught. Constrained to interior vowels to avoid false positives on short tokens (`fc`, `ss`, `ck`, `miss`, `FC Barcelona`).
- Added `scripts/sync_external_sources.py` to re-merge external lists and drop suffix-variant duplicates (e.g. skip `fuckings` when `fucking` is already classified).
- Added `NOTICE.md` with full attribution for every bundled source.

## 0.1.0a1

- Initial alpha release of Profy.
- Replaced the PHP/Laravel fork contents with a standalone Python package.
- Ported Blasp's core obfuscation-aware profanity matching, masking, severity scoring, allow/block lists, and result objects.
- Bundled English, Spanish, German, and French dictionaries exported from Blasp's PHP config.
- Added Python CI, release workflow, and upstream Blasp sync tooling.
