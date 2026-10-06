from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
import json
from importlib import resources
import re
from types import MappingProxyType
import unicodedata
from typing import Callable, Iterable, Mapping, NamedTuple, Optional, Union


class Severity(str, Enum):
    MILD = "mild"
    MODERATE = "moderate"
    HIGH = "high"
    EXTREME = "extreme"

    @property
    def weight(self) -> int:
        return {
            Severity.MILD: 5,
            Severity.MODERATE: 15,
            Severity.HIGH: 30,
            Severity.EXTREME: 50,
        }[self]

    def is_at_least(self, minimum: "Severity") -> bool:
        return self.weight >= minimum.weight


@dataclass(frozen=True)
class Match:
    text: str
    base: str
    severity: Severity
    position: int
    length: int
    language: str = "english"

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "base": self.base,
            "severity": self.severity.value,
            "position": self.position,
            "length": self.length,
            "language": self.language,
        }


@dataclass(frozen=True)
class ShieldResult:
    original: str
    clean: str
    matches: tuple[Match, ...]
    score: int

    @property
    def is_clean(self) -> bool:
        return not self.matches

    @property
    def is_offensive(self) -> bool:
        return bool(self.matches)

    @property
    def count(self) -> int:
        return len(self.matches)

    @property
    def unique_words(self) -> list[str]:
        seen: dict[str, None] = {}
        for match in self.matches:
            seen.setdefault(match.base, None)
        return list(seen)

    @property
    def severity(self) -> Optional[Severity]:
        if not self.matches:
            return None
        return max((match.severity for match in self.matches), key=lambda item: item.weight)

    def to_dict(self) -> dict[str, object]:
        return {
            "original": self.original,
            "clean": self.clean,
            "is_offensive": self.is_offensive,
            "score": self.score,
            "count": self.count,
            "unique_words": self.unique_words,
            "severity": self.severity.value if self.severity else None,
            "words": [match.to_dict() for match in self.matches],
        }

    def __str__(self) -> str:
        return self.clean


Mask = Union[str, Callable[[str, int], str]]

# "regex" is the obfuscation-aware Blasp RegexDriver port; "pattern" is the
# literal, word-boundary-only Blasp PatternDriver port.
DRIVERS = ("regex", "pattern")

# Compiled dictionaries are expensive (~2s for English) and immutable once built,
# so ProfanityFilter instances and the one-shot helpers share them per option set.
_DICTIONARY_CACHE_SIZE = 16


class ProfanityFilter:
    """Reusable profanity filter with Blasp-style obfuscation detection."""

    def __init__(
        self,
        *,
        languages: Union[str, Iterable[str]] = "english",
        all_languages: bool = False,
        allow: Iterable[str] = (),
        block: Iterable[str] = (),
        mask: Mask = "*",
        minimum_severity: Optional[Union[Severity, str]] = None,
        driver: str = "regex",
    ) -> None:
        self.languages = list(_available_languages()) if all_languages else _coerce_languages(languages)
        self.allow = set(_coerce_words(allow, "allow"))
        self.block = set(_coerce_words(block, "block"))
        self.mask = mask
        self.minimum_severity = _coerce_severity(minimum_severity)
        self.driver = _coerce_driver(driver)
        self.dictionary = _cached_dictionary(
            tuple(self.languages),
            all_languages or len(self.languages) > 1,
            tuple(sorted(self.allow)),
            tuple(sorted(self.block)),
            self.driver,
        )

    def check(self, text: Optional[str]) -> ShieldResult:
        original = text or ""
        if self.driver == "pattern":
            return self._check_literal(original)

        # Invisible characters are removed only from the matching representation;
        # every span maps back to the untouched input so the output stays intact.
        text, text_map = _strip_invisible_with_mapping(original)
        if not text:
            return ShieldResult(original, original, (), 0)

        normalized, normalized_map = self.dictionary.normalize_with_mapping(text, text_map)
        normalized, normalized_map = _collapse_whitespace_with_mapping(normalized, normalized_map)
        # A run of one character longer than any dictionary word needs
        # ("fuuuuuuuuu...ck", "*" * 1000) is shortened for matching only; the
        # last kept character maps to the rest of the original run, so masks
        # still cover it.
        normalized, normalized_map = _shorten_runs_with_mapping(
            normalized,
            normalized_map,
            self.dictionary.long_run_pattern,
            self.dictionary.longest_run,
        )
        immutable_normalized = normalized
        expressions = self.dictionary.sorted_expressions
        dictionary = self.dictionary
        window = dictionary.context_window

        # Letters of the unmasked text, for the Scunthorpe guard.
        letters = _RunIndex(normalized, r"[a-zA-Z]+")

        matches: list[Match] = []
        # Original characters already claimed by a match (several normalized
        # characters can come from one original character).
        claimed = bytearray(len(original))
        working = normalized
        working_map = normalized_map
        keep_scanning = True

        # Each expression's matches in the previous pass, and the spans that pass
        # masked: a confirming pass only rescans where those masks can matter.
        previous: dict[int, list[tuple[int, int, str]]] = {}
        changed: list[tuple[int, int]] = []

        # ``working`` is already whitespace-collapsed and masking never adds
        # whitespace, so (unlike Blasp) it is not re-collapsed on every pass.
        while keep_scanning:
            keep_scanning = False
            finder = _CandidateFinder(working, changed, dictionary.stops)
            # Every context lookup is a bisect into runs computed once per pass,
            # and accepted spans are masked (with \x01) once at the end of the
            # pass, so each candidate costs time independent of the text length.
            # The next pass sees this pass's masks, so it re-checks anything
            # they could change.
            words = _RunIndex(working, r"\w+")
            hex_runs = _RunIndex(working, r"[0-9a-fA-F-]+")
            hex_verdicts: dict[tuple[int, int], bool] = {}
            taken = bytearray(len(working))
            accepted: list[tuple[int, int]] = []
            pending: list[tuple[int, int, str, bool, bool]] = []

            def masked(index: int) -> bool:
                return 0 <= index < len(working) and (working[index] == "\x01" or bool(taken[index]))

            def is_false_positive(start: int, end: int) -> bool:
                word_start, word_end = words.around(start, end)
                return (
                    word_end - word_start <= dictionary.longest_false_positive
                    and working[word_start:word_end].lower() in dictionary.false_positives
                )

            def accept(start: int, end: int, base: str) -> None:
                taken[start:end] = b"\x01" * (end - start)
                accepted.append((start, end))

                severity = dictionary.severity_for(base)
                if self.minimum_severity is not None and not severity.is_at_least(self.minimum_severity):
                    return
                original_start, original_end = _original_span(working_map, start, end)
                if claimed.find(1, original_start, original_end) != -1:
                    return
                claimed[original_start:original_end] = b"\x01" * (original_end - original_start)
                matches.append(
                    Match(
                        text=original[original_start:original_end],
                        base=base,
                        severity=severity,
                        position=original_start,
                        length=original_end - original_start,
                        language=",".join(self.languages),
                    )
                )

            for number, (base, expression) in enumerate(expressions):
                if finder.mode == "full":
                    candidates = [(found.start(), found.end(), found.group(0)) for found in expression.finditer(working)]
                else:
                    candidates = finder.candidates(expression, previous.get(number))
                previous[number] = candidates
                for start, end, matched_text in candidates:

                    if _is_spanning_word_boundary(matched_text, working, start):
                        # Rejected for running across a phrase break ("hell, Ll|oyd");
                        # the same profanity may still match before the break.
                        cut = _PHRASE_BREAK.search(matched_text)
                        retry = expression.match(working, start, start + cut.start()) if cut else None
                        if retry is None or _is_spanning_word_boundary(retry.group(0), working, start):
                            continue
                        end = retry.end()
                        matched_text = retry.group(0)

                    # A zero-length match can never be masked and would keep the
                    # scan loop alive forever, so it is never accepted. Masked
                    # characters (\x01) and this pass's matches are never reused.
                    if start == end or "\x01" in matched_text or taken.find(1, start, end) != -1:
                        continue
                    token = hex_runs.around(start, end)
                    if token not in hex_verdicts:
                        hex_verdicts[token] = _is_hex_token(working[token[0] : token[1]])
                    # An explicit block word that is the whole token is masked
                    # anyway ("deadbeef1", "12345678").
                    if hex_verdicts[token] and not (
                        base in dictionary.blocked and working[token[0] : token[1]].strip("-") == matched_text
                    ):
                        continue

                    # For the Scunthorpe-style substring guard we use the
                    # surrounding *alphabetic run* rather than the full \w-context.
                    # A trailing/leading digit (e.g. "hello9") must not strip the
                    # protection that keeps a real word like "hello" from being
                    # masked just because it contains the profanity "hell".
                    word_start, word_end = letters.around(start, end)
                    guard = (
                        matched_text,
                        immutable_normalized[max(word_start, start - window) : start],
                        immutable_normalized[end : min(word_end, end + window)],
                        base,
                        dictionary.compound_parts,
                    )
                    # Directly touching an accepted match also makes a compound
                    # ("biitch|fuck", where "biitch" is no literal entry).
                    touches_before = masked(start - 1)
                    touches_after = masked(end)
                    if _is_pure_alpha_substring(*guard, touches_before=touches_before, touches_after=touches_after):
                        # It may still be one link of a chain of candidates that
                        # only qualify together ("biitch|biitch"); see below.
                        need = _chain_need(guard, touches_before, touches_after)
                        if need is not None and not is_false_positive(start, end):
                            pending.append((start, end, base, need[0], need[1]))
                    elif not is_false_positive(start, end):
                        keep_scanning = True
                        accept(start, end, base)

            # Accept the chains of protected candidates whose missing neighbours
            # are each other ("biitch|biitch", "biitch|biitch|fuck").
            for start, end, base in _resolve_chains(pending, masked):
                keep_scanning = True
                accept(start, end, base)

            working = _mask_spans(working, accepted)
            changed = sorted(accepted)

        return ShieldResult(original, self._apply_masks(original, matches), tuple(matches), _score(matches, text))

    def clean(self, text: Optional[str]) -> str:
        return self.check(text).clean

    def _check_literal(self, text: str) -> ShieldResult:
        """Port of Blasp's PatternDriver: exact, case-insensitive, word-bounded matches."""
        if not text:
            return ShieldResult(text, text, (), 0)

        found: list[Match] = []
        for base, expression in self.dictionary.sorted_expressions:
            if base.lower() in self.dictionary.false_positives:
                continue
            for hit in expression.finditer(text):
                found.append(
                    Match(
                        text=hit.group(0),
                        base=base,
                        severity=self.dictionary.severity_for(base),
                        position=hit.start(),
                        length=hit.end() - hit.start(),
                        language=",".join(self.languages),
                    )
                )

        # Severity filtering happens before de-duplication so a shorter,
        # high-severity match is not swallowed by a longer filtered-out one.
        if self.minimum_severity is not None:
            found = [match for match in found if match.severity.is_at_least(self.minimum_severity)]

        matches: list[Match] = []
        covered_end = -1
        for match in sorted(found, key=lambda item: (item.position, -item.length)):
            if match.position >= covered_end:
                matches.append(match)
                covered_end = match.position + match.length

        return ShieldResult(text, self._apply_masks(text, matches), tuple(matches), _score(matches, text))

    def _apply_masks(self, text: str, matches: list[Match]) -> str:
        pieces: list[str] = []
        cursor = 0
        for match in sorted(matches, key=lambda item: item.position):
            pieces.append(text[cursor : match.position])
            pieces.append(self._mask(match.text, match.length))
            cursor = match.position + match.length
        pieces.append(text[cursor:])
        return "".join(pieces)

    def _mask(self, word: str, length: int) -> str:
        if callable(self.mask):
            return self.mask(word, length)
        character = (self.mask or "*")[0]
        return character * length


def filter_text(
    text: Optional[str],
    *,
    languages: Union[str, Iterable[str]] = "english",
    all_languages: bool = False,
    allow: Iterable[str] = (),
    block: Iterable[str] = (),
    mask: Mask = "*",
    minimum_severity: Optional[Union[Severity, str]] = None,
    driver: str = "regex",
) -> ShieldResult:
    return ProfanityFilter(
        languages=languages,
        all_languages=all_languages,
        allow=allow,
        block=block,
        mask=mask,
        minimum_severity=minimum_severity,
        driver=driver,
    ).check(text)


def check_text(text: Optional[str], **options: object) -> bool:
    return filter_text(text, **options).is_offensive


def clean_text(text: Optional[str], **options: object) -> str:
    return filter_text(text, **options).clean


@lru_cache(maxsize=_DICTIONARY_CACHE_SIZE)
def _cached_dictionary(
    languages: tuple[str, ...],
    combined: bool,
    allow: tuple[str, ...],
    block: tuple[str, ...],
    driver: str,
) -> "_Dictionary":
    return _Dictionary.for_languages(
        list(languages),
        combined=combined,
        allow=frozenset(allow),
        block=block,
        driver=driver,
    )


class _Dictionary:
    def __init__(
        self,
        *,
        profanities: list[str],
        false_positives: set[str],
        severity_map: dict[str, Severity],
        substitutions: Mapping[str, list[str]],
        separators: list[str],
        languages: list[str],
        driver: str = "regex",
        blocked: Iterable[str] = (),
    ) -> None:
        # Instances are cached and shared between filters, so expose read-only views.
        self.profanities = tuple(profanities)
        self.false_positives = frozenset(false_positives)
        self.severity_map = MappingProxyType(dict(severity_map))
        self.languages = list(languages)
        self.longest_run = _longest_needed_run(profanities, substitutions)
        # Parts that make a word a compound of profanities ("hell|fuck"), and how
        # far around a match the substring guard needs to look.
        self.compound_parts = frozenset(word.lower() for word in profanities if len(word) >= 3)
        self.context_window = max([len(part) for part in self.compound_parts] + [_LONGEST_SUFFIX + 1])
        self.longest_false_positive = max([0] + [len(word) for word in self.false_positives])
        self.long_run_pattern = rf"(.)\1{{{self.longest_run},}}"
        # Stop classes of the generated expressions (see _compile_profanity),
        # keyed by id(): hashing a compiled pattern re-hashes its whole program.
        # The expressions live as long as this dictionary, so ids stay unique.
        self.stops: dict[int, re.Pattern[str]] = {}
        if driver == "pattern":
            self.expressions = _generate_literal_expressions(profanities)
        else:
            self.expressions, stops = _generate_reaching_expressions(profanities, separators, substitutions)
            self.stops = {id(expression): stop for expression, stop in stops.items()}
        # Longest first; among equally long words an explicitly blocked one goes
        # first, so it always matches its own text ("*6zy" before "fagz").
        blocked = frozenset(blocked)
        self.blocked = blocked
        self.sorted_expressions = tuple(
            sorted(self.expressions.items(), key=lambda item: (len(item[0]), item[0] in blocked), reverse=True)
        )

    @classmethod
    def for_languages(
        cls,
        languages: list[str],
        *,
        combined: bool = False,
        allow: frozenset[str] = frozenset(),
        block: Iterable[str] = (),
        driver: str = "regex",
    ) -> "_Dictionary":
        global_data = _load_json("global.json")
        profanities: list[str] = []
        false_positives = {item.lower() for item in global_data.get("false_positives", [])}
        severity_map: dict[str, Severity] = {}
        substitutions = {
            key: list(values) for key, values in dict(global_data.get("substitutions", {})).items()
        }

        for language in languages:
            data = _load_language(language)
            profanities.extend(data.get("profanities", []))
            false_positives.update(item.lower() for item in data.get("false_positives", []))
            severity_map.update(_build_severity_map(data))

            for key, values in data.get("substitutions", {}).items():
                # Mirrors Blasp's Dictionary::forLanguages(): a combined dictionary
                # only merges accent/diacritic substitutions. Multi-character keys
                # and plain ASCII letters (German "c" -> "s", French "k" -> "c", ...)
                # would otherwise leak one language's phonetics into the others.
                plain = key.strip("/")
                if combined and (len(plain) > 1 or re.fullmatch(r"[a-zA-Z]", plain)):
                    continue
                existing = substitutions.setdefault(key, [])
                for value in values:
                    if value not in existing:
                        existing.append(value)

        known = {word.lower() for word in profanities}
        for word in block:
            # Like Blasp, only words new to the dictionary default to HIGH; blocking
            # an existing word keeps its curated severity. An explicit block also
            # overrides a bundled false positive for the same word.
            if word not in known:
                profanities.append(word)
                known.add(word)
                severity_map[word] = Severity.HIGH
            false_positives.discard(word)

        profanities = list(dict.fromkeys(word for word in profanities if word.lower() not in allow))
        return cls(
            profanities=profanities,
            false_positives=false_positives,
            severity_map=severity_map,
            substitutions=substitutions,
            separators=global_data.get("separators", []),
            languages=languages,
            driver=driver,
            blocked=block,
        )

    def severity_for(self, word: str) -> Severity:
        return self.severity_map.get(word.lower(), Severity.HIGH)

    def normalize(self, text: str) -> str:
        normalized, _ = self.normalize_with_mapping(text)
        return normalized

    def normalize_with_mapping(
        self,
        text: str,
        span_map: Optional[list[tuple[int, int]]] = None,
    ) -> tuple[str, list[tuple[int, int]]]:
        normalized = text
        if span_map is None:
            span_map = [(index, index + 1) for index in range(len(text))]
        if self.languages == ["spanish"]:
            normalized, span_map = _translate_with_mapping(normalized, span_map, _SPANISH_MAP)
            normalized, span_map = _replace_with_mapping(
                normalized,
                span_map,
                r"\bll(?=[aeiouáéíóúü])",
                lambda match: _case_like(match.group(0), "y"),
            )
            normalized, span_map = _replace_with_mapping(
                normalized,
                span_map,
                r"rr",
                lambda match: _case_like(match.group(0), "r"),
            )
        elif self.languages == ["german"]:
            normalized, span_map = _translate_with_mapping(normalized, span_map, _GERMAN_MAP)
            normalized, span_map = _replace_with_mapping(
                normalized,
                span_map,
                r"sch",
                lambda match: _case_like(match.group(0), "sh"),
            )
        elif self.languages == ["french"]:
            normalized, span_map = _translate_with_mapping(normalized, span_map, _FRENCH_MAP)
        return normalized, span_map


def _longest_needed_run(profanities: Iterable[str], substitutions: Mapping[str, list[str]]) -> int:
    """How many identical characters in a row matching may need to see.

    Every dictionary word must still fit, and a run kept for a repeated-letter
    option ("ll", "ss") must be able to hold two of them, so shortening a longer
    run never leaves an odd character over.
    """

    def runs(text: str) -> list[int]:
        return [len(run.group(0)) for run in re.finditer(r"(.)\1+", text.lower())]

    word_runs = [length for word in profanities for length in runs(word)]
    option_runs = [
        2 * length for options in substitutions.values() for option in options if len(option) > 1 for length in runs(option)
    ]
    return max([3] + word_runs + option_runs)


_VOWEL_KEYS = frozenset("aeiou")
_MIN_CONSONANTS_FOR_ELISION = 3
# Upper bound of separator characters between two letters ("f-*-ck").
_SEPARATOR_LIMIT = 3
# How many ambiguous characters in a row a letter run may keep when one of its
# own characters follows them; longer stretches are left to the next letter.
_AMBIGUOUS_LOOKAHEAD = 3
# Bounds for one letter run inside a profanity: _RUN_LENGTH_LIMIT characters in a
# row (identical characters are already shortened, so this only limits mixed
# variants such as "uüuü...") and _RUN_STEP_LIMIT stretches of ambiguous
# characters ("f*f*f*...") or multi-character options. They keep the work per
# start position constant, so matching stays linear in the input length.
_RUN_LENGTH_LIMIT = 64
_RUN_STEP_LIMIT = 32


class _Token(NamedTuple):
    """One position of a profanity: a substitution set, or a literal character."""

    chars: frozenset
    multi: tuple
    optional: bool = False
    literal: Optional[str] = None


def _substitution_token(options: list[str], optional: bool) -> _Token:
    # Same split as Blasp: if every option is a single character (or a regex escape
    # such as "\\$"), all characters form one class; otherwise escapes stand for
    # the escaped character and longer options are matched literally.
    has_multi = any(len(option) > 1 and not re.fullmatch(r"\\.", option) for option in options)
    chars: set[str] = set()
    multi: list[str] = []
    for option in options:
        if not has_multi:
            chars.update(option)
        elif re.fullmatch(r"\\.", option):
            chars.add(option[1])
        elif len(option) == 1:
            chars.add(option)
        elif option not in multi:
            multi.append(option)
    multi.sort(key=len, reverse=True)
    return _Token(frozenset(chars), tuple(multi), optional)


def _generate_expressions(
    profanities: list[str],
    separators: list[str],
    substitutions: Mapping[str, list[str]],
) -> dict[str, re.Pattern[str]]:
    return _generate_reaching_expressions(profanities, separators, substitutions)[0]


def _generate_reaching_expressions(
    profanities: list[str],
    separators: list[str],
    substitutions: Mapping[str, list[str]],
) -> tuple[dict[str, re.Pattern[str]], dict[re.Pattern[str], re.Pattern[str]]]:
    """The expressions, and for each one the class of characters it never
    reads past (see ``_compile_profanity``)."""
    options_by_key = {
        character.strip("/"): tuple(options)
        for character, options in substitutions.items()
        if character.strip("/")
    }
    ordered = tuple(sorted(options_by_key.items(), key=lambda item: len(item[0]), reverse=True))
    compiled = {profanity: _compile_profanity(profanity, ordered, tuple(separators)) for profanity in profanities}
    return (
        {profanity: pair[0] for profanity, pair in compiled.items()},
        {pair[0]: pair[1] for pair in compiled.values()},
    )


# Compiled expressions are shared between dictionaries, so a filter that only
# differs by its allow/block list reuses everything except the changed words.
@lru_cache(maxsize=16384)
def _compile_profanity(
    profanity: str,
    ordered: tuple[tuple[str, tuple[str, ...]], ...],
    separators: tuple[str, ...],
) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """The expression for one profanity and its *stop* class.

    The stop class matches every character that no part of the expression can
    consume or test positively: all token classes, multi-character options and
    literals, every separator and whitespace are excluded from it. Each
    construct reads forward only through characters it accepts, and the only
    look-behind (the start guard) reads one character before the match, so an
    attempt starting at ``p`` reads nothing outside ``[p - 1, s]``, where ``s``
    is the first stop character at or after ``p``. The confirming passes rely
    on this to rescan only where masking changed the text.
    """
    lowered = profanity.lower()
    vowel_positions = [index for index, character in enumerate(lowered) if character in _VOWEL_KEYS]
    consonant_letters = sum(
        1 for character in lowered if character.isalpha() and character not in _VOWEL_KEYS
    )
    elidable = (
        len(vowel_positions) == 1
        and 0 < vowel_positions[0] < len(lowered) - 1
        and consonant_letters >= _MIN_CONSONANTS_FOR_ELISION
    )
    tokens: list[_Token] = []
    i = 0
    while i < len(profanity):
        for key, options in ordered:
            if profanity.startswith(key, i):
                tokens.append(_substitution_token(options, elidable and key in _VOWEL_KEYS))
                i += len(key)
                break
        else:
            tokens.append(_Token(frozenset(), (), literal=profanity[i]))
            i += 1
    readable: set[str] = set(separators)
    for token in tokens:
        readable.update(token.chars)
        readable.update(character for option in token.multi for character in option)
        if token.literal is not None:
            readable.add(token.literal)
    stop = re.compile(r"[^\s" + "".join(_class_members(readable)) + "]", re.IGNORECASE | re.UNICODE)
    return re.compile(_tokens_expression(tokens, separators), re.IGNORECASE | re.UNICODE), stop


def _tokens_expression(tokens: list[_Token], separators: Iterable[str]) -> str:
    """Build a backtracking-safe expression for one profanity.

    Blasp's original expression (``[f*]+sep{0,3}?[u*]*sep{0,3}?...``) lets every
    letter run, separator gap and neighbouring run compete for the same characters
    ("*" belongs to every letter *and* to the separators), so a failing attempt on
    input like ``"*" * 24`` explores an exponential number of splits. Here every
    character has one owner, decided without re-splitting:

    * a letter run keeps the characters only it can use, takes a character the
      next letter could also use only when one of its own follows shortly, and is
      committed through an atomic group (``(?=(...))\\N``, portable to Python 3.9);
    * a gap takes separators the next letter cannot start with, so giving one
      back can never let the next letter match it.

    Backtracking is therefore limited to an elided vowel's two-way choice and a
    few constant-time gap retries. Letter runs are bounded and the first letter
    never restarts inside its own run, so each start position costs constant
    work and a whole scan stays linear in the input length.
    """
    pieces: list[str] = []
    groups = 0

    def atomic(body: str) -> str:
        # Named groups: a numbered backreference past \99 would be read as an
        # octal escape, breaking long (block-list) words.
        nonlocal groups
        groups += 1
        return f"(?=(?P<r{groups}>{body}))(?P=r{groups})"

    for index, token in enumerate(tokens):
        if token.literal is not None:
            pieces.append(re.escape(token.literal))
            continue

        follow_chars = _follow_chars(tokens, index)
        if follow_chars is None:
            # Final letter: nothing competes for its repeats.
            any_option = _options_expression(token.chars, token.multi)
            piece = atomic(f"{any_option}{{1,{_RUN_STEP_LIMIT}}}") if token.multi else any_option + "+"
        else:
            own, shared, absorbing = _split_options(token, follow_chars, _later_chars(tokens, index))
            # Shared characters ("*" in "fu**uck") stay in this run only when one
            # of its own characters follows within a few positions -- and only an
            # own character no later letter could start with ("b*bo" keeps "*" for
            # the "o" because the next "b" may be the profanity's second "b").
            # Runs are bounded (except the first letter's plain run, which the
            # start guard keeps linear) so that a run reached from any start
            # position does constant work: up to _RUN_LENGTH_LIMIT characters of
            # one letter (one option per step for multi-character letters), and
            # up to _RUN_STEP_LIMIT stretches of shared characters inside it.
            own_run = own if token.multi else f"{own}{{1,{_RUN_LENGTH_LIMIT}}}"
            if not own:
                repeats = ""
            elif not shared or not absorbing:
                repeats = f"{own}{{1,{_RUN_STEP_LIMIT}}}" if token.multi else own_run
            else:
                lead = f"{shared}{{1,{_AMBIGUOUS_LOOKAHEAD}}}" + ("" if absorbing == own else f"(?={absorbing})")
                if token.multi:
                    repeats = f"(?:(?:{lead})?{own}){{1,{_RUN_STEP_LIMIT}}}"
                else:
                    repeats = f"(?:{lead})?{own_run}(?:{lead}{own_run}){{0,{_RUN_STEP_LIMIT - 1}}}"
            # Every repeat could equally start the next letter ("ss" in "ass"),
            # so such a run keeps one character and leaves the rest to the next
            # -- unless a separator splits the run ("coo-on", "pimm-mel"): then it
            # keeps everything up to that separator.
            split_run = "" if own else _split_run_expression(token, shared, separators, follow_chars, tokens[index + 1 :])
            if index == 0 and not token.multi:
                # The very first character stays a plain class so the regex
                # engine can skip ahead to candidate positions.
                run = _options_expression(token.chars, token.multi) + _start_guard(token, own, follow_chars)
                if repeats == own_run:
                    run += atomic(f"{own}*")
                elif repeats:
                    run += atomic(f"(?:{repeats})?")
                elif split_run:
                    run += atomic(split_run)
            elif not own:
                run = (atomic(shared) if token.multi else shared) + (atomic(split_run) if split_run else "")
            else:
                run = atomic(f"{repeats}|{shared}" if shared else repeats)
            piece = run + _gap_expression(separators, follow_chars)
        pieces.append(f"(?:{piece})?" if token.optional else piece)
    return "".join(pieces)


def _follow_chars(tokens: list[_Token], index: int) -> Optional[frozenset]:
    """Case-folded characters that can start whatever follows ``tokens[index]``."""
    chars: set[str] = set()
    for token in tokens[index + 1 :]:
        if token.literal is not None:
            chars.add(token.literal.lower())
            return frozenset(chars)
        chars.update(character.lower() for character in token.chars)
        chars.update(option[0].lower() for option in token.multi)
        if not token.optional:
            return frozenset(chars)
    return None


def _later_chars(tokens: list[_Token], index: int) -> frozenset:
    """Case-folded characters that can start any letter after ``tokens[index]``."""
    chars: set[str] = set()
    for token in tokens[index + 1 :]:
        if token.literal is not None:
            chars.add(token.literal.lower())
        chars.update(character.lower() for character in token.chars)
        chars.update(option[0].lower() for option in token.multi)
    return frozenset(chars)


def _split_options(token: _Token, follow_chars: frozenset, later_chars: frozenset) -> tuple[str, str, str]:
    """Expressions (empty when there are none) for the options only this letter
    can use, those it shares with whatever follows it, and the own options that
    no later letter can start with either."""

    def expression(chars: list[str], multi: list[str]) -> str:
        return _options_expression(chars, multi) if chars or multi else ""

    own_chars = [character for character in token.chars if character.lower() not in follow_chars]
    own_multi = [option for option in token.multi if option[0].lower() not in follow_chars]
    shared_chars = [character for character in token.chars if character not in own_chars]
    shared_multi = [option for option in token.multi if option not in own_multi]
    absorbing_chars = [character for character in own_chars if character.lower() not in later_chars]
    absorbing_multi = [option for option in own_multi if option[0].lower() not in later_chars]
    return (
        expression(own_chars, own_multi),
        expression(shared_chars, shared_multi),
        expression(absorbing_chars, absorbing_multi),
    )


def _start_guard(token: _Token, own: str, follow_chars: frozenset) -> str:
    # A match cannot usefully start in the middle of the first letter's run: if
    # this character and the one before it both continue that run, the attempt
    # from the earlier position consumes exactly the same characters from here on.
    # Skipping such starts keeps long runs ("$" * 1000) linear instead of quadratic.
    # The current character already matched the letter, so "continues the run"
    # is the cheaper "is not shared with the next letter".
    if not own:
        return ""
    shared = _class_members(character for character in token.chars if character.lower() in follow_chars)
    current = "[^" + "".join(shared) + "]" if shared else r"[\s\S]"
    return f"(?<!{own}{current})"


def _split_run_expression(
    token: _Token,
    shared: str,
    separators: Iterable[str],
    follow_chars: frozenset,
    later: list[_Token],
) -> str:
    """Further characters of a run the next letter shares completely, ending in
    separators -- taken only when one of the next letter's own (non-separator)
    characters follows them ("coo-on", "pimm-mel", "cell*lule")."""
    letters = _class_members(character for character in follow_chars if character not in separators)
    if not letters:
        return ""
    following = "[" + "".join(letters) + "]"
    limit = _RUN_STEP_LIMIT if token.multi else _RUN_LENGTH_LIMIT
    # The run is atomic, so it must leave alone any separator the word itself
    # spells later ("full-length", "niggardliness's").
    reserved = frozenset(item.literal.lower() for item in later if item.literal is not None)
    # Whitespace splits a run only when the letter repeats after it too
    # ("koo oon"); "butt today" must stay "butt" + "today", not "but|tt t|oday".
    symbols = _gap_expression(separators, frozenset(" ") | reserved, minimum=1)
    spaced = _gap_expression(separators, reserved, minimum=1)
    return f"(?:{shared}{{0,{limit}}}(?:{symbols}(?={following})|{spaced}(?={following}{{2}})))?"


def _gap_expression(separators: Iterable[str], follow_chars: frozenset, minimum: int = 0) -> str:
    # Up to three separators between letters, as in Blasp; a "." only counts when
    # a word character follows it. Separators the next letter can start with
    # ("*", "@", "!", ...) are left to that letter.
    whitespace = "" if any(character.isspace() for character in follow_chars) else r"\s"
    members = whitespace + "".join(
        _class_members(
            character for character in separators if character != "." and character.lower() not in follow_chars
        )
    )
    options = ["[" + members + "]"] if members else []
    if "." not in follow_chars:
        options.append(r"\.(?=\w)")
    if not options:
        return ""
    return "(?:" + "|".join(options) + f"){{{minimum},{_SEPARATOR_LIMIT}}}"


def _options_expression(chars: Iterable[str], multi: Iterable[str]) -> str:
    parts = [re.escape(option) for option in multi]
    members = _class_members(chars)
    if members:
        single = len(members) == 1 and "-" not in members[0][1:]
        parts.append(members[0] if single else "[" + "".join(members) + "]")
    if len(parts) == 1 and members:
        return parts[0]
    return "(?:" + "|".join(parts) + ")"


def _class_members(chars: Iterable[str]) -> list[str]:
    """Compact character-class body: one case per letter (expressions are
    case-insensitive) and ranges for consecutive code points, which keeps the
    thousands of expressions quick to compile."""
    points = sorted({ord(character.lower()) if len(character.lower()) == 1 else ord(character) for character in chars})
    members: list[str] = []
    i = 0
    while i < len(points):
        j = i
        while j + 1 < len(points) and points[j + 1] == points[j] + 1:
            j += 1
        if j - i >= 2:
            members.append(re.escape(chr(points[i])) + "-" + re.escape(chr(points[j])))
        else:
            members.extend(re.escape(chr(point)) for point in points[i : j + 1])
        i = j + 1
    return members


def _generate_literal_expressions(profanities: list[str]) -> dict[str, re.Pattern[str]]:
    return {profanity: _compile_literal(profanity) for profanity in profanities}


@lru_cache(maxsize=16384)
def _compile_literal(profanity: str) -> re.Pattern[str]:
    # Not inside a longer word on either side; unlike \b this also works for
    # words that start or end with a symbol ("c++", "100%"). The literal driver
    # checks the raw text, so a space matches any run of whitespace.
    return re.compile(
        r"(?<!\w)" + r"\s+".join(re.escape(part) for part in profanity.lower().split(" ")) + r"(?!\w)",
        re.IGNORECASE | re.UNICODE,
    )


def _chain_need(guard: tuple, touches_before: bool, touches_after: bool) -> Optional[tuple[bool, bool]]:
    """Which side(s) of a protected candidate would have to touch another match
    for the candidate to count as part of a compound; None if that is not
    enough to lift the protection."""
    for need_before, need_after in ((True, False), (False, True), (True, True)):
        if not _is_pure_alpha_substring(
            *guard,
            touches_before=touches_before or need_before,
            touches_after=touches_after or need_after,
        ):
            return need_before and not touches_before, need_after and not touches_after
    return None


def _resolve_chains(
    pending: list[tuple[int, int, str, bool, bool]],
    masked: Callable[[int], bool],
) -> list[tuple[int, int, str]]:
    """The greatest set of pending candidates whose needed sides each touch a
    match or another candidate of the set, without overlaps.

    Linear-time support counting: a candidate whose needed neighbour count
    drops to zero is removed and its neighbours lose its support in turn.
    """
    alive = [not any(masked(index) for index in range(start, end)) for start, end, _, _, _ in pending]

    def settle() -> None:
        ending: dict[int, list[int]] = {}
        starting: dict[int, list[int]] = {}
        for index, (start, end, _, _, _) in enumerate(pending):
            if alive[index]:
                ending.setdefault(end, []).append(index)
                starting.setdefault(start, []).append(index)
        support = []
        for index, (start, end, _, need_before, need_after) in enumerate(pending):
            before = len(ending.get(start, ())) if need_before and not masked(start - 1) else -1
            after = len(starting.get(end, ())) if need_after and not masked(end) else -1
            support.append([before, after])
        queue = [index for index in range(len(pending)) if alive[index] and 0 in support[index]]
        while queue:
            index = queue.pop()
            if not alive[index]:
                continue
            alive[index] = False
            start, end = pending[index][0], pending[index][1]
            for neighbour in ending.get(start, ()):
                if alive[neighbour] and support[neighbour][1] > 0:
                    support[neighbour][1] -= 1
                    if support[neighbour][1] == 0:
                        queue.append(neighbour)
            for neighbour in starting.get(end, ()):
                if alive[neighbour] and support[neighbour][0] > 0:
                    support[neighbour][0] -= 1
                    if support[neighbour][0] == 0:
                        queue.append(neighbour)

    settle()
    # Overlapping survivors (different words over the same letters): keep the
    # first (longer words come first), then settle the supports again.
    claimed: set[int] = set()
    for index, (start, end, _, _, _) in enumerate(pending):
        if alive[index]:
            if claimed.intersection(range(start, end)):
                alive[index] = False
            else:
                claimed.update(range(start, end))
    settle()
    return [(start, end, base) for index, (start, end, base, _, _) in enumerate(pending) if alive[index]]


# The windowed rescan does a few searches per masked span and expression; past
# about one masked span per this many characters a scan of the compacted text
# is cheaper. Either way the result is the same.
_WINDOW_SPACING = 256
_SHORTCUT_MINIMUM = 512


class _CandidateFinder:
    """``expression.finditer(text)`` for every expression of one pass, as
    (start, end, text) tuples, computed cheaply on confirming passes.

    A confirming pass sees the previous pass's text with ``changed`` spans
    replaced by \x01, which no generated expression reads past (it is in every
    stop class). Two exact shortcuts follow:

    * Windowed rescan: an attempt starting at ``p`` reads only ``[p - 1, s]``
      (``s`` = first stop character at or after ``p``), so only attempts in a
      *zone* -- from the start of the readable run before a changed span up to
      its end -- can differ from the previous pass. Outside the zones the
      previous pass's matches are reused, and real searches are bounded by
      ``endpos`` just past the stop character ending the zone, which no
      attempt in the zone reads beyond.
    * Compaction: runs of \x01 are collapsed to one character (an attempt
      reads at most the first of them, and the look-behind at most the last),
      the compacted text is scanned, and positions are mapped back.

    Expressions without a known stop class (anything not generated here) are
    always scanned in full.
    """

    def __init__(self, text: str, changed: list[tuple[int, int]], stops: Mapping[int, re.Pattern[str]]):
        self.text = text
        self.stops = stops
        self.blocks: list[list[int]] = []
        for start, end in changed:
            if self.blocks and start <= self.blocks[-1][1]:
                self.blocks[-1][1] = max(self.blocks[-1][1], end)
            else:
                self.blocks.append([start, end])
        self.reversed = ""
        self.compact: Optional[tuple[str, list[int]]] = None
        # Chosen once per pass; every strategy gives the same result. On short
        # texts a plain scan is cheaper than either shortcut.
        if not self.blocks or len(text) < _SHORTCUT_MINIMUM:
            self.mode = "full"
        elif len(self.blocks) * _WINDOW_SPACING <= len(text):
            self.mode = "windowed"
            self.reversed = text[::-1]
        else:
            self.mode = "compacted"

    def candidates(
        self,
        expression: re.Pattern[str],
        previous: Optional[list[tuple[int, int, str]]],
    ) -> list[tuple[int, int, str]]:
        if previous is None:
            return self._full(expression)
        stop = self.stops.get(id(expression))
        if stop is None:
            return self._full(expression)
        if self.mode == "windowed":
            return self._windowed(expression, stop, previous)
        return self._compacted(expression)

    def _full(self, expression: re.Pattern[str]) -> list[tuple[int, int, str]]:
        return [(found.start(), found.end(), found.group(0)) for found in expression.finditer(self.text)]

    def _compacted(self, expression: re.Pattern[str]) -> list[tuple[int, int, str]]:
        if self.compact is None:
            pieces: list[str] = []
            origin: list[int] = []
            cursor = 0
            for run in re.finditer("\x01{2,}", self.text):
                pieces.append(self.text[cursor : run.start() + 1])
                origin.extend(range(cursor, run.start() + 1))
                cursor = run.end()
            pieces.append(self.text[cursor:])
            origin.extend(range(cursor, len(self.text) + 1))
            self.compact = ("".join(pieces), origin)
        text, origin = self.compact
        # Matches never contain \x01, so both ends map back directly.
        return [
            (origin[found.start()], origin[found.end() - 1] + 1, found.group(0)) for found in expression.finditer(text)
        ]

    def _windowed(
        self,
        expression: re.Pattern[str],
        stop: re.Pattern[str],
        previous: list[tuple[int, int, str]],
    ) -> list[tuple[int, int, str]]:
        text = self.text
        size = len(text)
        zones: list[list[int]] = []
        for start, end in self.blocks:
            # Attempts reaching the span start inside the readable run before it.
            before = stop.search(self.reversed, size - start) if start else None
            first = size - before.start() if before else 0
            if zones and first <= zones[-1][1] + 1:
                zones[-1][1] = end
            else:
                zones.append([first, end])

        found: list[tuple[int, int, str]] = []
        position = 0
        index = 0
        zone = 0
        while position <= size:
            while index < len(previous) and previous[index][0] < position:
                index += 1
            while zone < len(zones) and zones[zone][1] < position:
                zone += 1
            next_zone = zones[zone][0] if zone < len(zones) else size + 1
            if index == 0 or previous[index - 1][1] <= position:
                # In step with the previous pass: its next match stands unless
                # a zone comes first.
                if index < len(previous) and previous[index][0] < next_zone:
                    if previous[index][0] == previous[index][1]:
                        # Empty matches advance differently; generated
                        # expressions never produce them.
                        return self._full(expression)
                    found.append(previous[index])
                    position = previous[index][1]
                    continue
                if zone == len(zones):
                    break
                low, high = max(position, zones[zone][0]), zones[zone][1]
            else:
                # Resuming inside a match of the previous pass: its later
                # starts were never tried there.
                low, high = position, previous[index - 1][1] - 1
            ending = stop.search(text, high)
            limit = min(size, (ending.start() if ending else size) + 1)
            position = high + 1
            for match in expression.finditer(text, low, limit):
                if match.start() > high:
                    break
                if match.end() == match.start():
                    return self._full(expression)
                found.append((match.start(), match.end(), match.group(0)))
                position = max(position, match.end())
        return found


def _mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """``text`` with every (disjoint) span replaced by \x01 characters."""
    pieces: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        pieces.append(text[cursor:start])
        pieces.append("\x01" * (end - start))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


class _RunIndex:
    """Maximal runs of ``pattern`` in a text, for O(log n) context lookups."""

    def __init__(self, text: str, pattern: str) -> None:
        spans = [found.span() for found in re.finditer(pattern, text)]
        self.starts = [start for start, _ in spans]
        self.ends = [end for _, end in spans]

    def around(self, start: int, end: int) -> tuple[int, int]:
        """``[start, end)`` widened over the runs touching it on either side."""
        left, right = start, end
        before = bisect_right(self.starts, start - 1) - 1
        if start and before >= 0 and self.ends[before] >= start:
            left = self.starts[before]
        after = bisect_right(self.starts, end) - 1
        if after >= 0 and self.ends[after] > end:
            right = self.ends[after]
        return left, right


def _is_hex_token(token: str) -> bool:
    """Whether a token (a match plus the hex characters around it) is a UUID or
    a long hexadecimal identifier rather than a word."""
    token = token.strip("-")
    uuid_pattern = (
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    )
    if re.fullmatch(uuid_pattern, token):
        return True
    stripped = token.replace("-", "")
    return (
        len(stripped) >= 8
        and bool(re.fullmatch(r"[0-9a-fA-F]+", stripped))
        and bool(re.search(r"\d", stripped))
    )


def _is_spanning_word_boundary(matched: str, full_text: str, start: int) -> bool:
    if not re.search(r"\s+", matched):
        return False
    parts = re.split(r"\s+", matched)
    if len(parts) <= 1:  # pragma: no cover - unreachable: ``matched`` contains whitespace
        return False
    if sum(1 for part in parts if len(part) == 1 and re.search(r"[a-z]", part, re.I)) == len(parts):
        return False

    end = start + len(matched)
    embedded_start = start > 0 and bool(re.match(r"\w", full_text[start - 1], re.UNICODE))
    embedded_end = end < len(full_text) and bool(re.match(r"\w", full_text[end], re.UNICODE))

    if embedded_start and embedded_end:
        return True
    # Punctuation touching the whitespace ends a phrase ("shit, said",
    # "fuck, yourself"): a plain word followed by it is complete, so the match
    # must not run on into the next word ("shits", "fuckyou").
    phrase_break = bool(_PHRASE_BREAK.search(matched))
    if embedded_start:
        return phrase_break or not _has_letter_and_nonletter(parts[1:])
    if not embedded_end:
        return False
    # The match may only stop short of the next word's end by an inflection
    # ("@ss hole|s"), not part-way through another word ("f*ck you|rself").
    rest = _WORD_HEAD.match(full_text, end).group(0).lower()
    if rest not in _INFLECTION_SUFFIXES:
        return True
    if phrase_break:
        # Across a phrase break only deliberate splitting continues: single
        # letters ("f, u, c, k|s") or a stem of two or more letters before the
        # inflection ("sh, ithead|s"); "shit, s|ing" is two words.
        stem = re.sub(r"[^a-z]", "", parts[-1], flags=re.I)
        return len(stem) < 2 and not all(_is_letter_piece(part) for part in parts[:-1])
    return not _has_letter_and_nonletter(parts[:-1])


_PHRASE_BREAK = re.compile(r"[,.;:?!]\s|\s[,.;:?!]")
# Inflectional endings (plural, past, participle, comparative, superlative,
# adverb) including the y -> i spellings ("shittier", "crappiest"), plus "-y"
# and "-iness" adjective/noun forms.
_INFLECTION_SUFFIXES = frozenset(
    [
        "s", "es", "ed", "er", "ers", "est", "ing", "ings", "ly", "y",
        "ies", "ied", "ier", "iers", "iest", "ily", "iness",
    ]
)
_Y_ENDINGS = frozenset(["ies", "ied", "ier", "iers", "iest", "ily", "iness"])
_LONGEST_SUFFIX = max(len(suffix) for suffix in _INFLECTION_SUFFIXES)
_WORD_HEAD = re.compile(rf"\w{{0,{_LONGEST_SUFFIX + 1}}}")


def _is_inflection(stem: str, ending: str, base: str, words: Iterable[str]) -> bool:
    # The y -> i endings inflect a y-adjective. English forms those from short
    # stems by doubling the final consonant ("shit" -> "shitty" -> "shitt|ier"),
    # which is evidence enough; otherwise the adjective must be a dictionary
    # word: "bitch|ier" (bitchy) is an inflection, "tard|iness" (tardy) and
    # "hell|ier" (the "ll" is the word's own) are not.
    if ending in _Y_ENDINGS:
        doubled = stem == base + base[-1] and base[-1] not in "aeiouy"
        return doubled or stem + "y" in words
    return ending in _INFLECTION_SUFFIXES


def _is_letter_piece(part: str) -> bool:
    return len(re.sub(r"[^a-z]", "", part, flags=re.I)) == 1


def _has_letter_and_nonletter(parts: list[str]) -> bool:
    # Obfuscation evidence is a word mixing letters and symbols ("@ss", "f*ck");
    # a lone separator between words ("fuck - yourself") is not.
    return any(re.search(r"[a-z]", part, re.I) and re.search(r"[^a-z]", part, re.I) for part in parts)


# Three identical letters in a row never occur in ordinary English spelling, so
# they mark a deliberately stretched word ("fuuuck", "shiiiit").
_STRETCHED_LETTERS = re.compile(r"(.)\1\1")
# Letters English almost never doubles, so a doubled one is deliberate stretching
# too ("fuuckin", "shiit"). Derived from /usr/share/dict/words (234k words): each
# of these is doubled in under 0.5% of the words containing it (x 0%, y 0.008%,
# q 0.03%, u 0.04%, v 0.06%, j 0.07%, a 0.09%, h 0.13%, i 0.29%, w 0.43%,
# k 0.48%); the next letter, d, is doubled in 1.7% and s, l, f, o in 4-13%.
_RARELY_DOUBLED = re.compile(r"([ahijkquvwxy])\1", re.IGNORECASE)


def _squeeze(text: str) -> str:
    return re.sub(r"(.)\1+", r"\1", text)


def _is_pure_alpha_substring(
    matched: str,
    before: str,
    after: str,
    base: str,
    compound_parts: Iterable[str],
    *,
    touches_before: bool = False,
    touches_after: bool = False,
) -> bool:
    """Whether ``matched`` is ordinary letters of a longer word (Scunthorpe).

    ``before``/``after`` are the letters directly around the match within its
    word (the caller may cut them to the longest profanity: nothing further away
    changes the verdict). ``compound_parts`` are the lower-cased profanities of
    three or more letters; ``touches_before``/``touches_after`` say an accepted
    match directly abuts this one on that side.
    """
    if not re.fullmatch(r"[a-zA-Z]+", matched):
        return False

    match_lower = matched.lower()
    before = before.lower()
    after = after.lower()
    base_lower = base.lower()
    # A compound of profanities ("hellfuck", "dumbass") is not a clean word: the
    # match stays flagged when another profanity directly abuts it. Profanities
    # merely contained somewhere in the rest of the word ("ero" in "zerowidth")
    # are ordinary letters and do not count.
    left_profane = touches_before or _ends_with_profanity(before, compound_parts)
    right_profane = touches_after or _starts_with_profanity(after, compound_parts)
    # (stem, ending) readings of the letters after the match.
    endings = [(match_lower, after)]
    if len(match_lower) > len(base_lower) and _squeeze(match_lower) == _squeeze(base_lower):
        # The match is longer than its base only because letters repeat
        # ("cook" for "cok", "anall" in "anally"). Ordinary double letters never
        # manufacture a profanity: if the literal base is not inside the match the
        # word is simply a different word; otherwise judge the literal occurrence
        # as if the repetition were not there ("shitt|ier" -> "shit|ier").
        if _STRETCHED_LETTERS.search(match_lower):
            return False
        # A rarely doubled letter is deliberate when each side of the match is
        # the word's edge or another profanity ("fuuckin", "shiits",
        # "biitch|fuck"); inside a longer word it is real spelling ("va|cuum",
        # "an|giit|is").
        left_edge = not before or left_profane
        right_edge = not after or after in _INFLECTION_SUFFIXES or right_profane
        if left_edge and right_edge and any(
            double.group(0) not in base_lower for double in _RARELY_DOUBLED.finditer(match_lower)
        ):
            return False
        if base_lower not in match_lower:
            return True
        # A repeat at the end may be the ending's first letter ("nazi|ing"
        # matched as "nazii" + "ng").
        if match_lower.startswith(base_lower):
            endings.append((base_lower, match_lower[len(base_lower) :] + after))
        match_lower = base_lower

    if (not before and not after) or len(match_lower) > len(base_lower):
        return False
    if not before and any(_is_inflection(stem, ending, base_lower, compound_parts) for stem, ending in endings):
        return False

    return not (left_profane or right_profane)


def _ends_with_profanity(text: str, compound_parts: Iterable[str]) -> bool:
    return any(text[-size:] in compound_parts for size in range(3, len(text) + 1))


def _starts_with_profanity(text: str, compound_parts: Iterable[str]) -> bool:
    return any(text[:size] in compound_parts for size in range(3, len(text) + 1))


def _score(matches: list[Match], text: str) -> int:
    if not matches:
        return 0
    total_words = max(1, len(re.findall(r"\S+", text.strip())))
    raw = sum(match.severity.weight for match in matches)
    density = len(matches) / total_words
    return min(100, int(raw * (1 + density)))


def _build_severity_map(data: Mapping[str, object]) -> dict[str, Severity]:
    severity_map: dict[str, Severity] = {}
    for level, words in dict(data.get("severity", {})).items():
        severity = _coerce_severity(level) or Severity.HIGH
        for word in words:
            severity_map[str(word).lower()] = severity
    for word in data.get("profanities", []):
        severity_map.setdefault(str(word).lower(), Severity.HIGH)
    return severity_map


def _coerce_languages(languages: Union[str, Iterable[str]]) -> list[str]:
    items = [languages] if isinstance(languages, str) else list(languages)
    available = _available_languages()
    selected: list[str] = []
    unknown: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f"language names must be strings, not {type(item).__name__}")
        name = item.strip().lower()
        if name not in available:
            unknown.append(item)
        elif name not in selected:
            selected.append(name)
    if unknown:
        raise ValueError(
            "Unknown language(s): "
            + ", ".join(repr(item) for item in unknown)
            + ". Available languages: "
            + ", ".join(available)
        )
    if not selected:
        raise ValueError("At least one language is required. Available languages: " + ", ".join(available))
    return selected


def _coerce_words(words: Iterable[str], option: str) -> frozenset[str]:
    # A bare string is one word, not an iterable of letters. Blank entries are
    # ignored: an empty pattern would match everywhere without consuming text.
    items = [words] if isinstance(words, str) else words
    cleaned: set[str] = set()
    for word in items:
        if not isinstance(word, str):
            raise TypeError(f"{option} entries must be strings, not {type(word).__name__}")
        # Whitespace inside an entry is collapsed like the checked text is.
        word = " ".join(word.split()).lower()
        if word:
            cleaned.add(word)
    return frozenset(cleaned)


def _coerce_driver(driver: str) -> str:
    if driver not in DRIVERS:
        raise ValueError(f"Unknown driver {driver!r}. Available drivers: " + ", ".join(DRIVERS))
    return driver


def _coerce_severity(value: Optional[Union[Severity, str]]) -> Optional[Severity]:
    if value is None or isinstance(value, Severity):
        return value
    return Severity(value.lower())


def _is_invisible(character: str) -> bool:
    # Format characters (zero-width space/joiner, bidi marks, tag characters, ...)
    # and variation selectors render as nothing but can split a word.
    return (
        unicodedata.category(character) == "Cf"
        or "\ufe00" <= character <= "\ufe0f"
        or "\U000e0100" <= character <= "\U000e01ef"
    )


def _strip_invisible_with_mapping(text: str) -> tuple[str, list[tuple[int, int]]]:
    kept: list[str] = []
    span_map: list[tuple[int, int]] = []
    for index, character in enumerate(text):
        if not _is_invisible(character):
            kept.append(character)
            span_map.append((index, index + 1))
    return "".join(kept), span_map


def _collapse_whitespace_with_mapping(
    text: str,
    span_map: list[tuple[int, int]],
) -> tuple[str, list[tuple[int, int]]]:
    return _replace_with_mapping(text, span_map, r"\s+", lambda _: " ")


def _replace_with_mapping(
    text: str,
    span_map: list[tuple[int, int]],
    pattern: str,
    replacement: Callable[[re.Match[str]], str],
) -> tuple[str, list[tuple[int, int]]]:
    pieces: list[str] = []
    mapped: list[tuple[int, int]] = []
    cursor = 0

    for match in re.finditer(pattern, text, flags=re.I):
        start, end = match.span()
        pieces.append(text[cursor:start])
        mapped.extend(span_map[cursor:start])

        replacement_text = replacement(match)
        original_start, original_end = _original_span(span_map, start, end)
        pieces.append(replacement_text)
        mapped.extend((original_start, original_end) for _ in replacement_text)
        cursor = end

    pieces.append(text[cursor:])
    mapped.extend(span_map[cursor:])
    return "".join(pieces), mapped


def _shorten_runs_with_mapping(
    text: str,
    span_map: list[tuple[int, int]],
    pattern: str,
    keep: int,
) -> tuple[str, list[tuple[int, int]]]:
    pieces: list[str] = []
    mapped: list[tuple[int, int]] = []
    cursor = 0
    for match in re.finditer(pattern, text):
        start, end = match.span()
        pieces.append(text[cursor : start + keep])
        mapped.extend(span_map[cursor : start + keep - 1])
        mapped.append((span_map[start + keep - 1][0], span_map[end - 1][1]))
        cursor = end
    pieces.append(text[cursor:])
    mapped.extend(span_map[cursor:])
    return "".join(pieces), mapped


def _original_span(span_map: list[tuple[int, int]], start: int, end: int) -> tuple[int, int]:
    spans = span_map[start:end]
    return min(span[0] for span in spans), max(span[1] for span in spans)


@lru_cache(maxsize=None)
def _available_languages() -> tuple[str, ...]:
    root = resources.files(__package__).joinpath("data/languages")
    return tuple(
        sorted(
            path.name.removesuffix(".json")
            for path in root.iterdir()
            if path.name.endswith(".json")
        )
    )


def _load_language(language: str) -> dict[str, object]:
    return _load_json(f"languages/{language}.json")


def _load_json(path: str) -> dict[str, object]:
    target = resources.files(__package__).joinpath("data", path)
    return json.loads(target.read_text(encoding="utf-8"))


def _translate_with_mapping(
    text: str,
    span_map: list[tuple[int, int]],
    mapping: Mapping[str, str],
) -> tuple[str, list[tuple[int, int]]]:
    pieces: list[str] = []
    mapped: list[tuple[int, int]] = []
    for index, character in enumerate(text):
        replacement = mapping.get(character, character)
        pieces.append(replacement)
        mapped.extend(span_map[index] for _ in replacement)
    return "".join(pieces), mapped


def _case_like(source: str, replacement: str) -> str:
    if source.isupper():
        return replacement.upper()
    if source[0].isupper():
        return replacement.capitalize()
    return replacement


_SPANISH_MAP = {
    "á": "a", "Á": "A", "é": "e", "É": "E", "í": "i", "Í": "I", "ó": "o", "Ó": "O",
    "ú": "u", "Ú": "U", "ü": "u", "Ü": "U", "ñ": "n", "Ñ": "N",
}

_GERMAN_MAP = {
    "ä": "ae", "Ä": "AE", "ö": "oe", "Ö": "OE", "ü": "ue", "Ü": "UE", "ß": "ss",
}

_FRENCH_MAP = {
    "à": "a", "â": "a", "ä": "a", "á": "a", "è": "e", "é": "e", "ê": "e", "ë": "e",
    "ì": "i", "í": "i", "î": "i", "ï": "i", "ò": "o", "ó": "o", "ô": "o", "ö": "o",
    "ù": "u", "ú": "u", "û": "u", "ü": "u", "ý": "y", "ÿ": "y", "À": "A", "Â": "A",
    "Ä": "A", "Á": "A", "È": "E", "É": "E", "Ê": "E", "Ë": "E", "Ì": "I", "Í": "I",
    "Î": "I", "Ï": "I", "Ò": "O", "Ó": "O", "Ô": "O", "Ö": "O", "Ù": "U", "Ú": "U",
    "Û": "U", "Ü": "U", "Ý": "Y", "Ÿ": "Y", "ç": "c", "Ç": "C", "œ": "oe", "Œ": "OE",
    "æ": "ae", "Æ": "AE",
}
