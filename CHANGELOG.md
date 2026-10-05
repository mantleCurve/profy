# Changelog

## Unreleased

### Behavior changes

- Unknown language names now raise `ValueError` listing the available languages (for example `languages="englsh"` used to silently disable detection); an empty selection also raises. Language names are case-insensitive.
- `ShieldResult.original` is now always the exact input, and `clean` is the input with only matched spans masked. Invisible characters (zero-width spaces/joiners, bidi marks, soft hyphens, tag characters, variation selectors) are ignored for matching only, so emoji ZWJ sequences such as `👩‍💻` are no longer altered. Invisible-character obfuscation (`f\u200bu\u200bc\u200bk`) is still caught and the mask now covers the whole original span, invisible characters included; `Match.position`/`length` index the original text.
- Ordinary double letters no longer create matches on their own: `cook`, `cookie`, `good`, `annals` and ~1,000 other dictionary words were flagged because repeated-letter expansion read them as `cok`, `god`, `anal`. Runs of three or more identical letters (`fuuuck`, `shiiiit`) and doubles of letters English almost never doubles (`fuuckin`, `shiit`; derived from double-letter frequencies in /usr/share/dict/words) are still treated as deliberate obfuscation.
- Matches no longer bleed into the next word across punctuation: `shit, said` was reported as `shits` and masked `*******aid`; `fuck, yourself` and `f*ck yourself` masked `rself`'s prefix. Deliberate spaced obfuscation (`f u c k`, `f, u, c, k`, `@ss holes`) is still caught.
- The compound-word rule only counts a profanity that directly abuts the match, so ordinary words are no longer flagged because a short entry occurs elsewhere in them (`zero\u200bwidth` masked `dth`, `anticipate`, `advertisement`, `holster`, ...). Combined with the change above, 2,355 of 234,456 dictionary words were flagged before and 830 are now, none of them newly.
- `Shiite`/`Shiites` were added to the bundled false positives.
- `block` keeps the curated severity of words already in the dictionary (blocking `coon` no longer lowers it from `extreme` to `high`); new words default to `high` as before. An explicit `block` now overrides a bundled false positive for that word (`block=["class"]` masks `class`). `allow` still wins over `block`.
- Blank or whitespace-only `allow`/`block` entries are ignored (`block=[""]` used to hang forever), entries are stripped, and a single string counts as one word. Non-string entries raise `TypeError`.
- Combining languages (`languages=[...]` with more than one entry, or `all_languages=True`) only merges accent/diacritic substitutions, as upstream Blasp does, so `sock` is no longer masked in an English+German filter.

### New

- `driver="pattern"` option on `ProfanityFilter`, `filter_text`, `check_text` and `clean_text`: a port of Blasp's literal `PatternDriver` (exact, whole-word, case-insensitive matches). The default `driver="regex"` is unchanged.

### Fixes

- Fixed catastrophic regex backtracking: `check("*" * 22)` took ~29s and `"*" * 24` never finished. Matching is now linear in the input length: 500-character adversarial inputs built from every bundled separator and substitution character finish in well under a second in every language. Very long runs of one character are shortened for matching only (masks still cover the whole run); a letter inside a word matches at most 64 mixed variants (`uüuü...`) or 32 censoring stretches (`f*f*f*...`) in a row.
- Scanning long tokens was quadratic (`"a55" * 2000` took ~4.5s, `"shit" * 4000` ~33s): word, hex-token and letter contexts are now looked up in run indexes built once per check, overlap checks use bisect, and masking no longer copies the position map per match.
- Inflections are handled completely: `shittier`, `crappier` and `crappiest` were no longer caught; the y -> i endings (`-ies/-ied/-ier/-iest/-ily/-iness`) now count after a doubled consonant or when the y-adjective is a dictionary word.
- Obfuscated plurals across punctuation (`f, u, c, kheads`, `sh, itheads`) are caught again, without reintroducing `shit, s|ing` bleed.
- The compound-word guard judges the actual occurrence instead of the first one in the word (`shitxshithell` masked only `hell`).
- Repeated letters split by separators (`coo-on`, `pimm-mel`, `cell*lule`) are caught again, and `butt today` no longer loses its match to the following word.
- Block words long enough to need more than 99 regex groups (for example a 101-character word) matched nothing.
- Runs of a one-character block word (`"💩" * 8`) produced overlapping matches; with a callback mask, text after the run was deleted.
- `filter_text`, `check_text` and `clean_text` no longer rebuild the dictionary on every call (~1.5s each); compiled dictionaries are cached per option set (bounded, thread-safe) and shared with `ProfanityFilter` instances, and filters that only differ by their allow/block lists reuse each other's compiled expressions.

### Tooling

- `scripts/sync_from_blasp.py` now pulls only the per-language word lists from upstream and no longer overwrites `profy/data/global.json` or per-language substitutions, which are Profy-owned. Upstream and external word lists are stored as separate layers under `sources/` and assembled deterministically, so the two sync scripts no longer clobber each other. Sync metadata is rewritten only when upstream changes, and `--check` reports pending changes without writing.
- The release workflow verifies that the tag, `pyproject.toml` and `profy.__version__` agree, marks GitHub Releases as pre-releases only for PEP 440 pre-release versions, smoke-tests the built wheel in a clean virtualenv, and no longer skips already-published files silently. Manual runs are dry runs.
- CI tests Python 3.9-3.14 and enforces 100% line and branch coverage.

## 0.1.2

- Fixed a false positive where a legitimate word adjacent to digits was masked because of a contained profanity. For example `hello9` was cleaned to `****o9` (matching `hell`). The Scunthorpe-style substring guard previously only applied when the entire surrounding `\w`-context was purely alphabetic, so a trailing/leading digit disabled it. The guard now inspects the surrounding alphabetic run (via `_alpha_word_context`), so words like `hello9`, `9hello`, `shell9`, and `scunthorpe9` stay clean while bare profanities with appended digits (`hell9`, `ass9`) are still masked.

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
