# Profy

Profy is a Python fork of [Blaspsoft/blasp](https://github.com/Blaspsoft/blasp), focused on the core profanity filtering engine rather than Laravel integration. It ports Blasp's dictionary-driven, obfuscation-aware matching into a standalone Python package.

The repository lives at [mantleCurve/profy](https://github.com/mantleCurve/profy).

## Install

```bash
pip install profy-filter
```

The import package is `profy`:

```python
from profy import filter_text, clean_text, check_text

result = filter_text("This is f-uuck!ng noisy")

print(result.is_offensive)      # True
print(result.clean)             # This is ********* noisy
print(result.unique_words)      # ['fucking']
print(result.score)             # severity-weighted score from 0 to 100

assert check_text("f**k")
assert clean_text("shit") == "****"
```

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest --cov --cov-report=term-missing   # CI enforces 100% line and branch coverage
```

## Reuse a Filter

Create a `ProfanityFilter` when you need to process many strings with the same options. Compiled dictionaries are cached per option set (languages, allow/block lists, driver), so constructing filters and calling the one-shot helpers repeatedly is cheap after the first use; masks and `minimum_severity` are applied per call and never shared.

```python
from profy import ProfanityFilter, Severity

shield = ProfanityFilter(
    languages=["english", "spanish"],
    mask="#",
    minimum_severity=Severity.MODERATE,
    allow=["heck"],
    block=["internal-ban-word"],
)

result = shield.check("clean this text")
print(result.to_dict())
```

## Options

`filter_text()` and `ProfanityFilter()` accept:

- `languages`: a language name or iterable. Bundled data includes `english`, `spanish`, `german`, and `french`. Names are case-insensitive; an unknown name (or an empty selection) raises `ValueError` listing the available languages instead of silently disabling detection.
- `all_languages`: use every bundled language dictionary (overrides `languages`). Combined dictionaries only merge accent/diacritic substitutions, as in Blasp, so one language's letter rules (German `c` -> `s`) do not leak into the others.
- `allow`: words that should never be flagged.
- `block`: extra words that should always be flagged.
- `mask`: a replacement character (only the first character is used; an empty string falls back to `*`) or a callback `(word, length) -> str`.
- `minimum_severity`: `mild`, `moderate`, `high`, or `extreme`.
- `driver`: `"regex"` (default) for obfuscation-aware matching, or `"pattern"` for Blasp's literal `PatternDriver`: exact, case-insensitive, whole-word matches only, with no substitutions, separators or normalization. It is faster to build and never matches inside other words, but it does not catch `f*ck` or `sh1t` unless they are listed literally.

### Allow and Block Semantics

- Entries are case-insensitive and stripped of surrounding whitespace; blank entries are ignored. A single string counts as one word.
- `block` adds a word to the dictionary. A word that is already in the dictionary keeps its curated severity (blocking `coon` keeps it `extreme`); new words default to `high`, like Blasp. An explicit block also overrides a bundled false positive for that exact word, so `block=["class"]` masks `class` (while `classroom` stays clean).
- `allow` removes a word from the dictionary and wins over `block`: a word in both lists is never flagged (Blasp applies the block list first and the allow list last).

### Unicode

Invisible characters (zero-width spaces and joiners, bidi marks, soft hyphens, tag characters, variation selectors) are ignored for matching only. `result.original` is always the exact input and `result.clean` is the input with only the matched spans masked, so emoji ZWJ sequences, flags and skin tones survive untouched, while `f\u200bu\u200bc\u200bk` is still detected and masked across its whole original span.

### Repeated Letters

Repeated letters are expanded (`fuuuuck`, `shiiiit`), but ordinary double letters never manufacture a profanity on their own: `cook`, `cookie` or `good` are no longer read as `cok`/`god`. Three or more identical letters in a row are treated as deliberate stretching and stay caught, and so is a doubled letter English almost never doubles (`a h i j k q u v w x y`, as in `fuuckin` or `shiit`) when the match is the whole word or the word plus an inflection; inside a longer word it is real spelling (`vacuum`). A doubled letter that merely extends a literal profanity (`fuckk`, `anally`) is judged as if it were not doubled. A repeated letter may also be split by separators (`coo-on`, `cell*lule`); a space splits it only when the letter repeats on both sides (`koo oon`), so `butt today` stays `butt` + `today`.

A match only continues into the next word through deliberate obfuscation: `shit, said` and `fuck - yourself` mask just the profanity, while `f u c k`, `f, u, c, k`, `f, u, c, kheads`, `sh, itheads` and `@ss holes` are still caught. A word containing a profanity is treated as a compound of profanities only when another profanity directly abuts that occurrence (`hellfuck`, `shitx|shit|hell`), not when one merely occurs elsewhere in the word (`ero` in `zerowidth`).

A profanity followed by an inflection stays flagged inside a longer word: `-s`, `-es`, `-ed`, `-er(s)`, `-est`, `-ing(s)`, `-ly`, `-y`, and the y -> i forms `-ies`, `-ied`, `-ier(s)`, `-iest`, `-ily`, `-iness`. The y -> i forms count after a doubled consonant (`shittier`, `crappiest`) or when the y-adjective is itself a dictionary word (`bitchier`), so `tardiness` and `spicily` stay clean.

Matching time is linear in the input length, including adversarial input such as `"*" * 10000` and long tokens without spaces (`"a55" * 4000`, hex strings): every context lookup goes through run indexes built once per check. Very long runs of one character are shortened for matching only (the last kept character stands for the rest of the run, so masks still cover it and matches never overlap; a run of a one-character block word longer than the limit is reported with its tail in the last match), and a letter inside a word matches at most 64 mixed variants in a row (`uüuü...`) or 32 stretches of censoring characters (`f*f*f*...`).

## Result Shape

```python
{
    "original": "This is shit",
    "clean": "This is ****",
    "is_offensive": True,
    "score": 40,
    "count": 1,
    "unique_words": ["shit"],
    "severity": "high",
    "words": [
        {
            "text": "shit",
            "base": "shit",
            "severity": "high",
            "position": 8,
            "length": 4,
            "language": "english",
        }
    ],
}
```

## Scope

This fork intentionally ports the profanity filter itself, not Blasp's Laravel service provider, middleware, Eloquent helpers, events, facades, or cache layer.

## Upstream Sync

The bundled word lists are assembled from layers kept under `sources/` (not shipped in the wheel):

- `sources/upstream/<language>.json`: the per-language word lists (`profanities`, `false_positives`, `severity`) pulled from Blasp's `config/languages/*.php` by `scripts/sync_from_blasp.py`. Nothing else is pulled from upstream; separators, substitutions, global false positives and the matching engine are Profy-owned and edited locally (`profy/data/global.json` and the non-word-list keys of `profy/data/languages/*.json`).
- `sources/external/english.json`: normalized candidates from the external English lists in `NOTICE.md`, refreshed by `scripts/sync_external_sources.py`.

Both scripts finish by re-assembling `profy/data/languages/*.json` from the layers, so running either one alone, in any order, produces the same bundle. `profy/data/upstream.json` and `docs/upstream-sync-report.md` change only when the upstream commit or its word lists change.

```bash
python scripts/sync_from_blasp.py            # pull upstream word lists (needs git and php)
python scripts/sync_from_blasp.py --check    # exit 1 if a sync would change anything
python scripts/sync_external_sources.py      # refresh the external English lists
python scripts/wordlist_layers.py [--check]  # re-assemble after editing a layer by hand
```

## Release

Alpha releases use PEP 440 versions such as `0.1.0a1` and Git tags such as `v0.1.0a1`. Pushing a `v*` tag runs the release workflow: it fails unless the tag, `pyproject.toml` and `profy.__version__` agree, runs the tests, builds the source distribution and wheel, smoke-tests the wheel in a clean virtualenv, attaches them to a GitHub Release (marked as a pre-release only for PEP 440 pre-release versions), and publishes to PyPI. A manual run of the workflow on a branch is a dry run that publishes nothing.

## Attribution

Profy is a Python fork of [Blaspsoft/blasp](https://github.com/Blaspsoft/blasp), originally authored by Michael Deeming and released under the MIT license.

## License

MIT. See [LICENSE.md](LICENSE.md).
