from __future__ import annotations

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
        # ("fuuuuuuuuu...ck", "*" * 1000) is shortened for matching only; the kept
        # characters map back to the whole original run, so masks still cover it.
        normalized, normalized_map = _replace_with_mapping(
            normalized,
            normalized_map,
            self.dictionary.long_run_pattern,
            lambda match: match.group(0)[: self.dictionary.longest_run],
        )
        immutable_normalized = normalized
        expressions = self.dictionary.sorted_expressions

        matches: list[Match] = []
        masked_ranges: list[tuple[int, int]] = []
        working = normalized
        working_map = normalized_map
        keep_scanning = True

        while keep_scanning:
            keep_scanning = False
            working, working_map = _collapse_whitespace_with_mapping(working, working_map)

            for base, expression in expressions:
                for found in list(expression.finditer(working)):
                    start, end = found.span()
                    matched_text = found.group(0)
                    length = end - start

                    # A zero-length match can never be masked and would keep the
                    # scan loop alive forever, so it is never accepted.
                    if not length:
                        continue
                    if any(
                        start < masked_end and end > masked_start
                        for masked_start, masked_end in masked_ranges
                    ):
                        continue
                    if _is_spanning_word_boundary(matched_text, working, start):
                        continue
                    if _is_inside_hex_token(working, start, length):
                        continue

                    full_word = _full_word_context(working, start, length)
                    # For the Scunthorpe-style substring guard we use the
                    # surrounding *alphabetic run* rather than the full \w-context.
                    # A trailing/leading digit (e.g. "hello9") must not strip the
                    # protection that keeps a real word like "hello" from being
                    # masked just because it contains the profanity "hell".
                    original_full_word = _alpha_word_context(immutable_normalized, start, length)
                    if _is_pure_alpha_substring(
                        matched_text,
                        original_full_word,
                        base,
                        self.dictionary.profanities,
                    ):
                        continue
                    if full_word.lower() in self.dictionary.false_positives:
                        continue

                    keep_scanning = True
                    working = working[:start] + ("\x01" * length) + working[end:]
                    working_map = working_map[:start] + working_map[start:end] + working_map[end:]
                    masked_ranges.append((start, end))

                    severity = self.dictionary.severity_for(base)
                    if (
                        self.minimum_severity is not None
                        and not severity.is_at_least(self.minimum_severity)
                    ):
                        continue

                    original_start, original_end = _original_span(working_map, start, end)
                    original_length = original_end - original_start
                    matches.append(
                        Match(
                            text=original[original_start:original_end],
                            base=base,
                            severity=severity,
                            position=original_start,
                            length=original_length,
                            language=",".join(self.languages),
                        )
                    )

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
        clean = text
        for match in sorted(matches, key=lambda item: item.position, reverse=True):
            clean = (
                clean[: match.position]
                + self._mask(match.text, match.length)
                + clean[match.position + match.length :]
            )
        return clean

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
    ) -> None:
        # Instances are cached and shared between filters, so expose read-only views.
        self.profanities = tuple(profanities)
        self.false_positives = frozenset(false_positives)
        self.severity_map = MappingProxyType(dict(severity_map))
        self.languages = list(languages)
        self.longest_run = _longest_needed_run(profanities, substitutions)
        self.long_run_pattern = rf"(.)\1{{{self.longest_run},}}"
        if driver == "pattern":
            self.expressions = _generate_literal_expressions(profanities)
        else:
            self.expressions = _generate_expressions(profanities, separators, substitutions)
        self.sorted_expressions = tuple(
            sorted(self.expressions.items(), key=lambda item: len(item[0]), reverse=True)
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
    options_by_key = {
        character.strip("/"): tuple(options)
        for character, options in substitutions.items()
        if character.strip("/")
    }
    ordered = tuple(sorted(options_by_key.items(), key=lambda item: len(item[0]), reverse=True))
    return {
        profanity: _compile_profanity(profanity, ordered, tuple(separators))
        for profanity in profanities
    }


# Compiled expressions are shared between dictionaries, so a filter that only
# differs by its allow/block list reuses everything except the changed words.
@lru_cache(maxsize=16384)
def _compile_profanity(
    profanity: str,
    ordered: tuple[tuple[str, tuple[str, ...]], ...],
    separators: tuple[str, ...],
) -> re.Pattern[str]:
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
    return re.compile(_tokens_expression(tokens, separators), re.IGNORECASE | re.UNICODE)


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
        nonlocal groups
        groups += 1
        return f"(?=({body}))(?:\\{groups})"

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
            if index == 0 and not token.multi:
                # The very first character stays a plain class so the regex
                # engine can skip ahead to candidate positions.
                run = _options_expression(token.chars, token.multi) + _start_guard(token, own, follow_chars)
                if repeats == own_run:
                    run += atomic(f"{own}*")
                elif repeats:
                    run += atomic(f"(?:{repeats})?")
            elif not own:
                # Every repeat could equally start the next letter ("ss" in "ass"),
                # so this run keeps one character and leaves the rest to the next.
                run = atomic(shared) if token.multi else shared
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


def _gap_expression(separators: Iterable[str], follow_chars: frozenset) -> str:
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
    return "(?:" + "|".join(options) + f"){{0,{_SEPARATOR_LIMIT}}}"


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
    return {
        profanity: re.compile(r"\b" + re.escape(profanity.lower()) + r"\b", re.IGNORECASE | re.UNICODE)
        for profanity in profanities
    }


def _is_inside_hex_token(text: str, start: int, length: int) -> bool:
    end = start + length
    token_start = start
    while token_start > 0 and re.match(r"[0-9a-fA-F-]", text[token_start - 1]):
        token_start -= 1
    token_end = end
    while token_end < len(text) and re.match(r"[0-9a-fA-F-]", text[token_end]):
        token_end += 1
    token = text[token_start:token_end].strip("-")
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
    if embedded_start and not embedded_end:
        return not _has_letter_and_nonletter(" ".join(parts[1:]))
    if not embedded_start and embedded_end:
        return not _has_letter_and_nonletter(" ".join(parts[:-1]))
    return False


def _has_letter_and_nonletter(text: str) -> bool:
    return bool(re.search(r"[a-z]", text, re.I)) and bool(re.search(r"[^a-z\s]", text, re.I))


def _full_word_context(text: str, start: int, length: int) -> str:
    left = start
    right = start + length
    while left > 0 and re.match(r"\w", text[left - 1], re.UNICODE):
        left -= 1
    while right < len(text) and re.match(r"\w", text[right], re.UNICODE):
        right += 1
    return text[left:right]


def _alpha_word_context(text: str, start: int, length: int) -> str:
    """Return the contiguous ASCII-letter run surrounding ``[start, start+length)``.

    Unlike :func:`_full_word_context` (which expands across any ``\\w`` character,
    digits and underscores included), this expansion stops at the first
    non-letter. It is used by the substring-protection guard so that a legitimate
    word ("hello") embedded next to digits ("hello9") is still recognised as the
    real word, and therefore the profanity it happens to contain ("hell") is not
    masked. Matching ``[a-zA-Z]`` here mirrors the alphabetic check inside
    :func:`_is_pure_alpha_substring`.
    """
    left = start
    right = start + length
    while left > 0 and re.match(r"[a-zA-Z]", text[left - 1]):
        left -= 1
    while right < len(text) and re.match(r"[a-zA-Z]", text[right]):
        right += 1
    return text[left:right]


# Three identical letters in a row never occur in ordinary English spelling, so
# they mark a deliberately stretched word ("fuuuck", "shiiiit").
_STRETCHED_LETTERS = re.compile(r"(.)\1\1")


def _squeeze(text: str) -> str:
    return re.sub(r"(.)\1+", r"\1", text)


def _is_pure_alpha_substring(
    matched: str,
    full_word: str,
    base: str,
    profanities: Iterable[str],
) -> bool:
    if not re.fullmatch(r"[a-zA-Z]+", matched):
        return False
    if not re.fullmatch(r"[a-zA-Z]+", full_word):
        return False

    match_lower = matched.lower()
    word_lower = full_word.lower()
    base_lower = base.lower()
    if len(match_lower) > len(base_lower) and _squeeze(match_lower) == _squeeze(base_lower):
        # The match is longer than its base only because letters repeat
        # ("cook" for "cok", "anall" in "anally"). Ordinary double letters never
        # manufacture a profanity: if the literal base is not inside the match the
        # word is simply a different word; otherwise judge the literal occurrence
        # as if the repetition were not there.
        if _STRETCHED_LETTERS.search(match_lower):
            return False
        if base_lower not in match_lower:
            return True
        at = word_lower.find(match_lower)
        word_lower = word_lower[:at] + base_lower + word_lower[at + len(match_lower) :]
        match_lower = base_lower

    if len(word_lower) <= len(match_lower) or len(match_lower) > len(base_lower):
        return False

    for suffix in ("s", "es", "ed", "er", "ers", "est", "ing", "ings", "ly", "y"):
        if word_lower == match_lower + suffix:
            return False

    pos = word_lower.find(match_lower)
    if pos >= 0:
        remainder = word_lower[:pos] + word_lower[pos + len(match_lower) :]
        for profanity in profanities:
            if len(profanity) >= 3 and profanity.lower() in remainder:
                return False
    return True


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
        word = word.strip().lower()
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
