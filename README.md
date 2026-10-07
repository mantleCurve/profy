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
- `driver`: `"regex"` (default) for obfuscation-aware matching, or `"pattern"` for Blasp's literal `PatternDriver`: exact, case-insensitive, whole-word matches only (not preceded or followed by a letter, digit or underscore, so words that start or end with a symbol work too), with no substitutions, separators or normalization. It is faster to build and never matches inside other words, but it does not catch `f*ck` or `sh1t` unless they are listed literally.

### Allow and Block Semantics

- Entries are case-insensitive and stripped of surrounding whitespace; blank entries are ignored. A single string counts as one word. With the default `regex` driver, invisible characters in entries are ignored like in the text (see Unicode), so `block=["❤\ufe0f"]` matches the heart with or without its variation selector, and an entry with nothing visible left counts as blank; the `pattern` driver matches entries literally.
- A block word always masks its own exact text: hyphens, dots, underscores, apostrophes, digits, symbols, spaces and non-Latin scripts included, at its edges too (`-12345678`, `deadbeef1-`). A match that is the entry as typed (case-insensitive, ignoring invisible characters and the amount of whitespace) and not part of a longer word or number, or that lies in a token made of nothing but the entry repeated (`12121212` for `1212`), skips every heuristic: the hex/UUID, substring, compound, cross-word and vowel-elision guards and the bundled false positives. Such matches are found in the text as typed, before language normalization and run shortening, so normalization cannot change their letters (German `block=["Fischhändler"]`, whose `schh` German otherwise reads as `sh`). Inside a longer token (`ab` in `ab12cd34ef`) or in an obfuscated form the guards still apply; internal whitespace in an entry matches any whitespace in the text. Block entries are matched separately from the bundled words and from each other: the bundled detection runs exactly as without a block list, and each entry finds its own text and every reading of its obfuscated forms that holds on its own (read as the language normalizes the text, with runs as long as the entry needs: `block=["z" + "o" * 10 + "q"]` catches `"z-" + "o" * 10 + "q"`; the bundled words and the other entries count as compound parts). The result masks everything any of them finds, overlapping or not (`x-y-z` with `block=["xy", "yz"]`, `12-34-5678` with `block=["12-34", "34-5678"]`, with either driver). So adding a block entry never unmasks a character, and a bundled word next to a block word is judged as without it. Matches are reported in text order, longer first, an entry's own text before its other readings and before bundled matches of the same span; each reports what no earlier one holds, so a longer match holding an entry is reported instead of it (`n!gger` with `block=["!"]` is still `nigger`, `ass hole` with `block=["ass"]` is still `asshole`). Every reported match has the highest severity found where it is (the overlapping findings around it), and the score counts every distinct span found, so adding a block entry never lowers a severity or the score: `n!gger` with `block=["n!gger"]` stays extreme. An entry below `minimum_severity` finds nothing (`wanker` with `block=["wanker"]` at `high` still masks `wank`). Every occurrence of an entry is masked, overlapping ones included, and one rejected inside a longer word or number never hides the next: `x1234-1234-1234` with `block=["1234-1234"]` masks the last nine characters, `"-" * 9` with `block=["--"]` all nine; a rest no occurrence covers stays (`-x-` with `block=["-x"]` masks `-x`).
- `block` adds a word to the dictionary. A word that is already in the dictionary keeps its curated severity (blocking `coon` keeps it `extreme`); new words default to `high`, like Blasp. An explicit block also overrides a bundled false positive for that exact word, so `block=["class"]` masks `class` (while `classroom` stays clean).
- `allow` removes a word from the dictionary and wins over `block`: a word in both lists is never flagged (Blasp applies the block list first and the allow list last).

### Unicode

Invisible characters (zero-width spaces and joiners, bidi marks, soft hyphens, tag characters, variation selectors) are ignored for matching only. `result.original` is always the exact input and `result.clean` is the input with only the matched spans masked, so emoji ZWJ sequences, flags and skin tones survive untouched, while `f\u200bu\u200bc\u200bk` is still detected and masked across its whole original span. Variation selectors and emoji tag characters right after a match belong to its last character and are masked with it.

### Repeated Letters

Repeated letters are expanded (`fuuuuck`, `shiiiit`), but ordinary double letters never manufacture a profanity on their own: `cook`, `cookie` or `good` are no longer read as `cok`/`god`. Three or more identical letters in a row are treated as deliberate stretching and stay caught, and so is a doubled letter English almost never doubles (`a h i j k q u v w x y`, as in `fuuckin` or `shiit`) when the match is the whole word or the word plus an inflection; inside a longer word it is real spelling (`vacuum`). A doubled letter that merely extends a literal profanity (`fuckk`, `anally`) is judged as if it were not doubled. A repeated letter may also be split by separators (`coo-on`, `cell*lule`); a space splits it only when the letter repeats on both sides (`koo oon`), so `butt today` stays `butt` + `today`.

A profanity repeated, glued to itself or to another one, is masked completely. Each pass first collects every reading of every entry: the matches themselves, shorter readings that give a stretched run's last letters to a copy that follows (`twatt|wat`), readings starting at any split near the end of a run of their first letter that another reading ends at, a run being any characters that letter accepts (`as|sshit`, `bitttch|hell`, `bitchhh|hhell`, `bitchΗ|hell` with a Greek `Η`), readings ending inside a deliberate final run (three or more characters, or a substitute among them: `bitchΗ|ell`); a double that is the word's own, as in `gittite`, makes no such split), and the copies such readings free. Each match is judged once per pass, however many others lead to it, so glued copies (`"twat" * 1000`) cost linear time. It then chooses the non-overlapping set of readings that covers the most letters and digits, then the most characters that spell something (symbols standing for letters, not whitespace or separators), then the one with the fewest matches, longer entries and earlier positions; a match that would be reported under `minimum_severity` is never dropped for one that would not. So `jihadjihad`, `5h1t5h1t` (`shit|shit` rather than `shits|h1t`), `baastard` × 3, `@ne @ne` (French, not `@|ne @ne` as `nene`) and glued phrases (`beef curtainsbeef curtains`, `raging bonerraging boner`) are masked as their copies, `fuccckd` as one match, and a reading across the separator between copies (`kum|s,kum|s`) does not win. Every letter of a multi-letter substitution key (German `sch`, `ck`, `ie`, Spanish `ll`, French `qu`) can also be obfuscated or stretched on its own (`5chwuler`, `schhmähliches`), and German `sch` and Spanish `rr` are normalized in their stretched forms too (`sschh`, `rrr`).

A match only continues into the next word through deliberate obfuscation: `shit, said` and `fuck - yourself` mask just the profanity, while `f u c k`, `f, u, c, k`, `f, u, c, kheads`, `sh, itheads` and `@ss holes` are still caught. A word containing a profanity is treated as a compound of profanities only when another profanity (or another match, such as `biitch` in `biitchfuck`, or a chain of such candidates like `biitchbiitch` or `bitchhbiitch`, each leaning on whichever neighbour it has; a chain link must be a word of three or more letters, and one that leaves its vowel out needs a word edge or match on both sides, as in `shtdck`) directly abuts that occurrence (`hellfuck`, `shitx|shit|hell`), not when one merely occurs elsewhere in the word (`ero` in `zerowidth`); the selection decides such neighbours together. The end of a word is never read into the next one, even next to a profanity: a letter of it (`cabron|a $Naa` as `asna`, `shit|s hit`) or what runs up to a lone separator (`bourrin|es @`). A match that would run into the next word is retried before the gap between the words (`hell, Lloyd`, `hell - Lloyd` and `hell / Lloyd` mask `hell`). Words joined by a separator stay apart too: `hell-Lloyd`, `shit_tom` and `ass/Sasha` mask only the profanity, while symbols inside an obfuscated word keep it one word (`fu-ck`, `a*s*s`, `sh-itzilla`), as does a separator inside a dictionary word (`cock*kblocker`) or another profanity continuing the word (`ass-holefuck`). A match that leaves the vowel out (`fck`, `f--ck`, `$ht`) must spell every other letter; a `*` stands for any letter, so `*Lloyd` is not read as `hll`.

A profanity followed by an inflection stays flagged inside a longer word: `-s`, `-es`, `-ed`, `-er(s)`, `-est`, `-ing(s)`, `-ly`, `-y`, and the y -> i forms `-ies`, `-ied`, `-ier(s)`, `-iest`, `-ily`, `-iness`. The y -> i forms count after a doubled consonant (`shittier`, `crappiest`) or when the y-adjective is itself a dictionary word (`bitchier`), so `tardiness` and `spicily` stay clean.

Matching time is linear in the input length, including adversarial input such as `"*" * 10000`, long tokens without spaces (`"a55" * 4000`, hex strings) and match-dense text (`"shit " * 500000`): context lookups go through run indexes built once per pass, a pass's readings are selected by one dynamic program linear in their number, and matches are masked once per pass. The constant factor is one regex scan of the text per dictionary entry (about 2,660 for English), roughly 25 ms per 1,000 characters of English text. When the text contains matches a second, confirming pass runs; it only rescans the regions the masks can affect (or a compacted copy of the text when matches are dense), so on long texts with few matches it is nearly free. Very long runs of interchangeable characters (characters no expression tells apart, such as `u`, `U` and `ü` in English) are shortened for matching only: the kept character after the cut stands for the removed ones, so masks still cover the whole run and matches never overlap, and complete occurrences of a literal run (the `00` of `b00bs`) stay complete, so a long run may be reported as fewer, longer matches. Block entries are matched with their own shortening, so they do not change the bundled words' (`block=["u" * 100]` does not stop `"f" + "u" * 100 + "ck"`). Runs of characters no letter accepts (separators, emoji) are matched as they are. A letter inside a word matches at most 64 substitutes in a row that are not interchangeable (`uυuυ...`, as the Greek `υ` also stands for `v`) or 32 stretches of censoring characters (`f*f*f*...`).

## Known Limitations

Sweeps that stretch, obfuscate and repeat every bundled entry still find these cases:

- Doubling a letter English often doubles makes a different word unless the entry is still spelled in it: `cocck` stays clean (`ccock` is masked). Doubling a letter the entry already doubles elsewhere is read as spelling too: `cockknockker` (for `cockknocker`) stays clean.
- Tokens of eight or more hexadecimal characters are treated as identifiers: `caa4aaca` (a stretched `caca`) stays clean.
- Spanish reads `ll` and `rr` as letters of their own: `llucifer` (read as `yucifer`) stays clean, and a substitute inside such a run breaks it (`zorr®a`).
- A substitute inside a stretched letter that the next letter also accepts is left to the letters after it when the letter after the next one is the stretched letter again, or when more than three such characters stand together: `ani!ilingus` (the `!` may be the `l`) and German `lecccc¢cccken` stay clean.
- A digit or symbol the entry spells literally does not stretch: `masterb88` masks only `masterb8`.
- Spaced-out letters are read across the spaces: `f u c k i n g it` also masks `it` (as a spaced `git`), while a doubled letter spaced out is not read as one (`s h i i t` stays clean).
- Copies glued without a separator are not always split into their copies when other entries read their letters better: `big breeeastsbig breeeasts` and `\umb ass-\umb ass` are only partly masked.

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
