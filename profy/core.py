from __future__ import annotations

from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from itertools import chain, groupby
import json
from importlib import resources
from math import lcm
import re
from types import MappingProxyType
import unicodedata
from typing import Callable, Container, Iterable, Mapping, NamedTuple, Optional, Union


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
        self.driver = _coerce_driver(driver)
        # Entries are normalized like the text the driver matches: the regex
        # driver ignores invisible characters, the literal driver does not.
        self.allow = set(_coerce_words(allow, "allow", strip_invisible=self.driver == "regex"))
        self.block = set(_coerce_words(block, "block", strip_invisible=self.driver == "regex"))
        self.mask = mask
        self.minimum_severity = _coerce_severity(minimum_severity)
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
        # Runs of interchangeable characters longer than any dictionary word
        # needs ("fuuuuuuuuu...ck", "fuUuUuU...ck", "*" * 1000) are shortened for
        # matching only; masks still cover the whole original run.
        normalized, normalized_map = self.dictionary.runs.shorten(normalized, normalized_map)
        immutable_normalized = normalized
        expressions = self.dictionary.sorted_expressions
        dictionary = self.dictionary
        window = dictionary.context_window

        # Letters of the unmasked text, for the Scunthorpe guard.
        letters = _RunIndex(normalized, r"[a-zA-Z]+")

        # An explicit block entry is judged on the visible input (``text``: the
        # original without invisible characters, before normalization and run
        # shortening). Built on first use.
        visible: list[int] = []
        alphanumeric: list[_RunIndex] = []
        token_roots: dict[tuple[int, int], tuple[str, int]] = {}

        def is_exact_block(start: int, end: int, base: str) -> bool:
            """Whether a match of the explicit block entry ``base`` is that
            entry as typed (case-folded, invisible characters and whitespace
            runs ignored) and not part of a longer alphanumeric token, or lies
            in a token that is nothing but the entry repeated ("1212|1212").
            Such a match skips every heuristic guard."""
            if not visible:
                visible.extend(span[0] for span in text_map)
                alphanumeric.append(_RunIndex(text, r"[^\W_]+"))
            # Spans grow left to right, so the match's ends give its extent.
            first, last = bisect_left(visible, working_map[start][0]), bisect_left(visible, working_map[end - 1][1])
            occurrence = text[first:last].lower()
            if occurrence != base and " " in base:
                occurrence = " ".join(occurrence.split())
            if occurrence == base and not (
                first and _ALPHANUMERIC.match(text, first - 1) and _ALPHANUMERIC.match(text, first)
            ) and not (last < len(text) and _ALPHANUMERIC.match(text, last - 1) and _ALPHANUMERIC.match(text, last)):
                return True
            token = alphanumeric[0].around(first, last)
            if token not in token_roots:
                folded = text[token[0] : token[1]].lower()
                token_roots[token] = (folded, _root_length(folded))
            return _repeats(*token_roots[token], base)

        matches: list[Match] = []
        # Matches withdrawn for a longer one that contains them (see replace).
        withdrawn: set[int] = set()
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
            pending: list[tuple[int, int, str, tuple[tuple[bool, bool], ...]]] = []
            # Per accepted match: severity, whether it is an explicit block
            # entry's own text, and its reported Match; and per position of
            # ``working``, which accepted match (1-based) holds it.
            records: list[tuple[Severity, bool, Optional[Match]]] = []
            owners = array("i")
            # Per position, the longest pending candidate covering it (capped at
            # 255), and the shorter candidates waiting for those to be resolved.
            pending_lengths = bytearray()
            deferred: list[tuple[int, int, str]] = []

            def masked(index: int) -> bool:
                return 0 <= index < len(working) and (working[index] == "\x01" or bool(taken[index]))

            def is_false_positive(start: int, end: int) -> bool:
                word_start, word_end = words.around(start, end)
                return (
                    word_end - word_start <= dictionary.longest_false_positive
                    and working[word_start:word_end].lower() in dictionary.false_positives
                )

            def accept(start: int, end: int, base: str, exact: bool = False) -> None:
                taken[start:end] = b"\x01" * (end - start)
                if not owners:
                    owners.frombytes(bytes(owners.itemsize * len(working)))
                owners[start:end] = array("i", [len(accepted) + 1]) * (end - start)
                accepted.append((start, end))

                severity = dictionary.severity_for(base)
                records.append((severity, exact, None))
                if self.minimum_severity is not None and not severity.is_at_least(self.minimum_severity):
                    return
                original_start, original_end = _original_span(working_map, start, end)
                # Variation selectors and tag characters belong to the character
                # before them ("\u2764\ufe0f"), so they are masked along with it.
                while original_end < len(original) and _modifies_previous(original[original_end]):
                    original_end += 1
                if claimed.find(1, original_start, original_end) != -1:
                    return
                claimed[original_start:original_end] = b"\x01" * (original_end - original_start)
                match = Match(
                    text=original[original_start:original_end],
                    base=base,
                    severity=severity,
                    position=original_start,
                    length=original_end - original_start,
                    language=",".join(self.languages),
                )
                matches.append(match)
                records[-1] = (severity, exact, match)

            def contained(start: int, end: int) -> Optional[list[int]]:
                """This pass's accepted matches overlapping [start, end) when all
                lie strictly inside it and none is an explicit block entry's
                own text, else None."""
                inside: list[int] = []
                position = taken.find(1, start, end)
                while position != -1:
                    index = owners[position] - 1
                    first, last = accepted[index]
                    if first < start or last > end or (first, last) == (start, end) or records[index][1]:
                        return None
                    inside.append(index)
                    position = taken.find(1, last, end)
                return inside

            def replace(inside: list[int], base: str, exact: bool = False) -> bool:
                """Withdraw the matches ``inside`` for one that contains them
                ("w|hooore" for "whooore"), unless one of them is more severe
                (an explicit block entry's own text replaces them anyway)."""
                severity = dictionary.severity_for(base)
                if not exact and any(records[index][0].weight > severity.weight for index in inside):
                    return False
                for index in inside:
                    first, last = accepted[index]
                    taken[first:last] = bytes(last - first)
                    accepted[index] = (first, first)
                    match = records[index][2]
                    if match is not None:
                        claimed[match.position : match.position + match.length] = bytes(match.length)
                        withdrawn.add(id(match))
                return True

            def follow_from(new_end: int, old_end: int) -> None:
                # A shortened candidate frees letters finditer() has already
                # passed; the current expression's next match may start in them
                # ("shit-ass|,shit-ass" after "shit-ass,s"). At most as many
                # starts as the longest entry has letters are tried.
                for position in range(new_end, min(old_end, new_end + dictionary.longest_word)):
                    follow = expression.match(working, position)
                    if follow is not None and follow.end() > position:
                        stack.append((position, follow.end(), follow.group(0)))
                        return

            for number, (base, expression) in enumerate(expressions):
                if finder.mode == "full":
                    candidates = [(found.start(), found.end(), found.group(0)) for found in expression.finditer(working)]
                else:
                    candidates = finder.candidates(expression, previous.get(number))
                previous[number] = candidates
                for candidate in candidates:
                    # A candidate may give back its end to the next occurrence
                    # (see _end_before_profanity), which is then judged next.
                    stack = [candidate]
                    while stack:
                        start, end, matched_text = stack.pop()
                        # An explicit block entry's own text is masked whatever the
                        # heuristics below would say about it.
                        exact = base in dictionary.blocked and is_exact_block(start, end, base)
                        candidate_text = matched_text
                        if not exact and _HEX_RUN.fullmatch(matched_text):
                            # Inside one hex-like token every shorter reading lies in
                            # it too: the hex/UUID guard decides before anything else.
                            token = hex_runs.around(start, end)
                            if token not in hex_verdicts:
                                hex_verdicts[token] = _is_hex_token(working[token[0] : token[1]])
                            if hex_verdicts[token]:
                                continue

                        if exact:
                            pass
                        elif _is_spanning_word_boundary(
                            matched_text, working, start, dictionary.compound_parts, window, expression, dictionary.longest_match
                        ):
                            # Rejected for running into the next word ("hell, Ll|oyd",
                            # "hell - Ll|oyd"); the same profanity may still match
                            # before the gap between the words.
                            retry = _retry_before_word_gap(
                                expression, working, start, matched_text, dictionary.compound_parts, window
                            )
                            if retry is None:
                                continue
                            follow_from(start + retry.end(), end)
                            end = start + retry.end()
                            matched_text = retry.group(0)
                        else:
                            # Through symbols into the first letters of a joined word
                            # ("hell-Ll|oyd"): the profanity before the symbols is
                            # retried alone, and when only another entry spells the
                            # word before them ("shit-t|om" read as "shitt"), that
                            # entry gets it.
                            cut = _joined_word_cut(
                                expression,
                                matched_text,
                                working,
                                start,
                                end,
                                dictionary.words,
                                dictionary.longest_word,
                                dictionary.compound_parts,
                                dictionary.joined,
                            )
                            if cut == _DISCARD:
                                continue
                            if cut is not None:
                                retry = expression.match(working[start : start + cut])
                                if retry is not None and not _is_spanning_word_boundary(
                                    retry.group(0), working, start, dictionary.compound_parts, window
                                ):
                                    follow_from(start + retry.end(), end)
                                    end = start + retry.end()
                                    matched_text = retry.group(0)
                                elif matched_text[:cut].lower() in dictionary.words or any(
                                    prefix.fullmatch(matched_text[:cut]) for prefix in dictionary.prefix_expressions(base)
                                ):
                                    # Another entry spells the word before the symbols
                                    # ("shit-t|om" read as "shitt", "sluut,s|luut" as
                                    # "sluts"): that entry gets it.
                                    continue

                        if not exact and end < len(working) and not working[end].isspace():
                            # A stretched run at the end may hold the first letter of a
                            # profanity that follows ("twatt|wat"): ending before it
                            # leaves that occurrence whole.
                            info = dictionary.info(base, expression)
                            given_back = _end_before_profanity(
                                expression,
                                working,
                                start,
                                end,
                                matched_text,
                                dictionary.compound_parts,
                                window,
                                dictionary.longest_match,
                                info.first,
                                info.takeovers,
                                info.wildcards,
                            )
                            if given_back is not None:
                                shorter, follow, entry = given_back
                                end = start + shorter.end()
                                matched_text = shorter.group(0)
                                if entry is not None:
                                    # The rest of this candidate is judged as that entry.
                                    base, expression = entry
                                if follow is not None and follow.end():
                                    stack.append((end, end + follow.end(), follow.group(0)))
                            elif (
                                working[end].lower() not in info.wildcards
                                and info.first.match(working, end)
                                and not expression.match(working, end)
                            ):
                                # The next occurrence may start where the start guard
                                # refuses it, when this match's last letter can also
                                # be its first ("clos|clos" in French, where "c"
                                # stands for "s" too): finditer() would skip it.
                                follow = expression.match(working[end : end + min(dictionary.longest_match, 4 * (end - start) + 16)])
                                if follow is not None and follow.end() and not _SPACE.search(follow.group(0)):
                                    stack.append((end, end + follow.end(), follow.group(0)))

                        # A match that only exists by leaving the vowel out must spell
                        # every consonant ("fck", "f--ck", "$ht"); a character any of
                        # the word's letters accepts spells none ("*Ll|oyd" as "hll").
                        elided = dictionary.info(base, expression).elision
                        if (
                            not exact
                            and elided is not None
                            and not _spells(matched_text, *elided)
                            and not dictionary.unelided(base, expression).fullmatch(matched_text)
                        ):
                            continue

                        # A zero-length match can never be masked and would keep the
                        # scan loop alive forever, so it is never accepted. Masked
                        # characters (\x01) and this pass's matches are never reused.
                        if start == end or "\x01" in matched_text:
                            continue
                        # A candidate may only take letters this pass already
                        # matched by containing those matches entirely.
                        inside = contained(start, end) if taken.find(1, start, end) != -1 else []
                        if inside is None:
                            continue
                        if inside and not exact and start and _ALPHANUMERIC.match(working, start - 1):
                            # Starting inside a word it would also cut off the match
                            # its first letters belong to ("a|sscck" for "ass|cck").
                            continue
                        if exact:
                            replace(inside, base, exact=True)
                            keep_scanning = True
                            accept(start, end, base, exact=True)
                            continue
                        if matched_text != candidate_text:
                            # Shortened above, it may now lie in a hex-like token.
                            token = hex_runs.around(start, end)
                            if token not in hex_verdicts:
                                hex_verdicts[token] = _is_hex_token(working[token[0] : token[1]])
                            if hex_verdicts[token]:
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
                            needs = _chain_needs(guard, touches_before, touches_after)
                            if (
                                needs
                                and dictionary.info(base, expression).elision is not None
                                and not dictionary.unelided(base, expression).fullmatch(matched_text)
                            ):
                                # Without its vowel, a candidate inside a longer word is
                                # just letters of it ("ngr" in "sangreeroot"): it only
                                # counts with a word edge or a match on both sides
                                # ("sht|dck").
                                needs = ((bool(guard[1]) and not touches_before, bool(guard[2]) and not touches_after),)
                            if needs and not inside and not is_false_positive(start, end):
                                pending.append((start, end, base, needs))
                                if not pending_lengths:
                                    pending_lengths.extend(bytes(len(working)))
                                length = min(end - start, 255)
                                pending_lengths[start:end] = bytes(max(item, length) for item in pending_lengths[start:end])
                        elif not is_false_positive(start, end):
                            if pending_lengths and max(pending_lengths[start:end]) > end - start:
                                # A longer candidate waiting for its chain gets these
                                # letters first ("baastard|baastard", not "baas|tard").
                                deferred.append((start, end, base))
                            elif replace(inside, base):
                                keep_scanning = True
                                accept(start, end, base)

            # Accept the chains of protected candidates whose missing neighbours
            # are each other ("biitch|biitch", "biitch|biitch|fuck").
            for start, end, base in _resolve_chains(pending, masked):
                keep_scanning = True
                accept(start, end, base)
            for start, end, base in deferred:
                if taken.find(1, start, end) == -1:
                    keep_scanning = True
                    accept(start, end, base)

            accepted = [span for span in accepted if span[0] < span[1]]
            working = _mask_spans(working, accepted)
            changed = sorted(accepted)

        matches = [match for match in matches if id(match) not in withdrawn]
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
        bundled: Iterable[str] = (),
    ) -> None:
        # Instances are cached and shared between filters, so expose read-only views.
        self.profanities = tuple(profanities)
        self.false_positives = frozenset(false_positives)
        self.severity_map = MappingProxyType(dict(severity_map))
        self.languages = list(languages)
        # Parts that make a word a compound of profanities ("hell|fuck"), and how
        # far around a match the substring guard needs to look.
        self.words = frozenset(word.lower() for word in profanities)
        self.longest_word = max([0] + [len(word) for word in self.words])
        # The most characters one match can span: every letter a full run plus
        # a separator gap.
        self.longest_match = (self.longest_word + 1) * (_RUN_LENGTH_LIMIT + _SEPARATOR_LIMIT)
        self.compound_parts = frozenset(word for word in self.words if len(word) >= _COMPOUND_PART_MINIMUM)
        self.context_window = max([len(part) for part in self.compound_parts] + [_LONGEST_SUFFIX + 1])
        self.longest_false_positive = max([0] + [len(word) for word in self.false_positives])
        # Stop classes of the generated expressions (see _compile_profanity),
        # keyed by id(): hashing a compiled pattern re-hashes its whole program.
        # The expressions live as long as this dictionary, so ids stay unique.
        self.stops: dict[int, re.Pattern[str]] = {}
        variants = []
        if driver == "pattern":
            self.expressions = _generate_literal_expressions(profanities)
        else:
            self.expressions, stops = _generate_reaching_expressions(profanities, separators, substitutions)
            self.stops = {id(expression): stop for expression, stop in stops.items()}
            # Runs keep at least what the language's own words need; block-list
            # words only lengthen the runs of their own characters (a block word
            # "x" * 300 must not keep "u" runs too long to match "fuu...ck").
            ordered = _ordered_substitutions(substitutions)
            # A word spelled with a multi-letter key ("sch", "ck", "ll") also
            # gets an expression with a token per letter, so each letter of the
            # key can be obfuscated or stretched on its own ("5chwuler",
            # "schhmaehlich"); the key's own options only cover it whole.
            # Keys the language's normalization rewrites in the text (see
            # _NORMALIZED_KEYS) stay whole or get an optional letter.
            rewritten = _NORMALIZED_KEYS.get(languages[0], {}) if len(languages) == 1 else {}
            single = tuple(item for item in ordered if len(item[0]) == 1 or item[0] in rewritten and rewritten[item[0]] is None)
            self._orderings = {id(expression): ordered for expression in self.expressions.values()}
            self._optionals: dict[int, tuple[int, ...]] = {}
            variants: list[tuple[str, re.Pattern[str]]] = []
            variant_tokens: list[tuple[str, tuple[_Token, ...]]] = []
            if single != ordered:
                for word in profanities:
                    lowered = word.lower()
                    optional = tuple(
                        index + offset
                        for key, offset in rewritten.items()
                        if offset is not None
                        for index in range(len(lowered))
                        if lowered.startswith(key, index)
                    )
                    tokens = _tokenize(word, single, True, optional)
                    if tokens != _tokenize(word, ordered):
                        expression, stop = _compile_profanity(word, single, tuple(separators), True, optional)
                        variants.append((word, expression))
                        variant_tokens.append((word, tokens))
                        self.stops[id(expression)] = stop
                        self._orderings[id(expression)] = single
                        self._optionals[id(expression)] = optional
            self._ordered = ordered
            self._separators = tuple(separators)
            self._prefixes: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {}
            self._expressions_of: dict[str, tuple[re.Pattern[str], ...]] = {}
            # Keyed by id() like the stop classes (the expressions live as long).
            self._infos: dict[int, _ExpressionInfo] = {}
            for word, expression in list(self.expressions.items()) + variants:
                self._expressions_of[word] = self._expressions_of.get(word, ()) + (expression,)
            self._entries_by_lower: dict[str, list[str]] = {}
            for word in profanities:
                self._entries_by_lower.setdefault(word.lower(), []).append(word)
            # Separators no letter stands for ("-", ",", "_" but not "*", "!"),
            # and the reversed tail of a joined match (see _joined_word_cut).
            substitutes = {
                character.lower()
                for key, options in ordered
                for text in (key, *options)
                for character in (text[1:] if re.fullmatch(r"\\.", text) else text)
            }
            pure = [character for character in separators if character.lower() not in substitutes] + ["_"]
            members = "".join(_class_members(pure))
            self.joined = (
                re.compile("[" + members + "]"),
                re.compile(r"([^\s" + members + r"]+)([" + members + r"]+)"),
                re.compile(r"[^\s" + members + r"]+"),
            )
            self.runs = _RunShortener(
                [(word, _tokenize(word, ordered)) for word in profanities] + variant_tokens,
                separators,
                _longest_needed_run(bundled, substitutions),
            )
        # Longest first; among equally long words an explicitly blocked one goes
        # first, so it always matches its own text ("*6zy" before "fagz").
        blocked = frozenset(blocked)
        self.blocked = blocked
        # A word's per-letter variant goes before its expression with
        # multi-letter keys: it reads every spelling the other reads in the
        # normalized text, and more ("5cheiss" whole, not "5|cheiss").
        variant_ids = {id(expression) for _, expression in variants}
        self.sorted_expressions = tuple(
            sorted(
                list(self.expressions.items()) + (variants if driver != "pattern" else []),
                key=lambda item: (len(item[0]), item[0] in blocked, id(item[1]) in variant_ids),
                reverse=True,
            )
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
        bundled = [word for word in profanities if word.lower() not in allow]
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
            bundled=bundled,
        )

    def info(self, word: str, expression: re.Pattern[str]) -> "_ExpressionInfo":
        """What the scan needs to know about ``expression`` (one of ``word``'s),
        computed once: see _ExpressionInfo."""
        info = self._infos.get(id(expression))
        if info is None:
            tokens = _tokenize(word, self._ordering(expression), True, self._optionals.get(id(expression), ()))
            if not tokens:
                # Not a generated expression: any character may start it.
                first = re.compile(r"[\s\S]")
            elif tokens[0].literal is not None:
                first = re.compile(re.escape(tokens[0].literal), re.IGNORECASE | re.UNICODE)
            else:
                first = _class_pattern(tokens[0].chars | {option[0] for option in tokens[0].multi})
            classes = [frozenset(character.lower() for character in token.chars) for token in tokens if token.literal is None]
            elision = None
            if _is_elidable(word):
                # Positions besides the vowel ("ck" in German "sack" is one).
                elision = (sum(1 for token in tokens if not token.optional), _spelling_characters(tokens))
            takeovers = tuple((word, other) for other in self.expressions_of(word) if other is not expression)
            info = _ExpressionInfo(
                first,
                frozenset.intersection(*classes) if classes else frozenset(),
                elision,
                takeovers + self.prefix_entries(word),
            )
            self._infos[id(expression)] = info
        return info

    def _ordering(self, expression: re.Pattern[str]) -> tuple[tuple[str, tuple[str, ...]], ...]:
        # The keys an expression was built with (custom ones: the dictionary's).
        return self._orderings.get(id(expression), self._ordered)

    def prefix_entries(self, word: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
        """The entries ``word`` starts with ("slut" for "sluts"), longest
        first, with their expressions; cached."""
        prefixes = self._prefixes.get(word)
        if prefixes is None:
            lowered = word.lower()
            prefixes = tuple(
                (entry, self.expressions[entry])
                for size in range(len(word) - 1, 2, -1)
                for entry in self._entries_by_lower.get(lowered[:size], ())
            )
            self._prefixes[word] = prefixes
        return prefixes

    def expressions_of(self, word: str) -> tuple[re.Pattern[str], ...]:
        """Every expression of ``word`` (see the per-letter variants)."""
        return self._expressions_of.get(word, ())

    def prefix_expressions(self, word: str) -> tuple[re.Pattern[str], ...]:
        """The expressions of ``prefix_entries``."""
        return tuple(expression for _, expression in self.prefix_entries(word))

    def unelided(self, word: str, expression: re.Pattern[str]) -> re.Pattern[str]:
        """``expression`` (one of the elidable ``word``'s) with the vowel
        required."""
        optional = self._optionals.get(id(expression), ())
        return _compile_profanity(word, self._ordering(expression), self._separators, False, optional)[0]

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
                # A whole run, so a stretched "rrr" is no ordinary double "rr".
                r"r{2,}",
                lambda match: _case_like(match.group(0), "r"),
            )
        elif self.languages == ["german"]:
            normalized, span_map = _translate_with_mapping(normalized, span_map, _GERMAN_MAP)
            normalized, span_map = _replace_with_mapping(
                normalized,
                span_map,
                # Stretched too ("sschhh"), so a doubled letter is no new word.
                r"s+c+h+",
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


# Runs short enough that no shortening can apply (every run keeps at least 3).
_LONG_RUN = re.compile(r"(.)\1{3,}", re.DOTALL)
_REPEATED = re.compile(r"(.)\1+", re.DOTALL)
_WORD_CHARACTER = re.compile(r"\w")


class _ExpressionInfo(NamedTuple):
    """Per expression: the characters it can start with; those every letter
    of it accepts ("*"); for a word whose vowel may be left out, how many
    positions besides the vowel it has and the characters that spell one of
    its letters (else None); and the expressions that may take over a
    given-back match (the word's other spellings, then the entries it starts
    with)."""

    first: re.Pattern[str]
    wildcards: frozenset
    elision: Optional[tuple[int, frozenset]]
    takeovers: tuple[tuple[str, re.Pattern[str]], ...]


class _RunShortener:
    """Shortens long runs of interchangeable characters, for matching only.

    Letter runs inside an expression are bounded (see ``_RUN_LENGTH_LIMIT``), so
    a run longer than any word needs ("fuuu...ck", "fuUuU...ck", "*" * 1000) is
    cut down before matching. The cut removes characters from inside the run
    and the kept character right after the cut stands for all of them, so a
    mask over it covers the whole original run and every original character
    still maps to exactly one kept one.

    Two characters are interchangeable when no generated expression can tell
    them apart: every letter class, separator set and literal the expressions
    are built from contains both or neither (case-insensitively, as the
    expressions match), so "u", "U" and "ü" form one run in English. The
    characters of a literal, and those a multi-character option repeats (the
    "s" of "ss"), only run with their own case variants, and the first and
    last character of a run are always kept, so an option reaching into the
    run from outside ("ue" after "...u") reads the same text.

    Only runs of characters a letter class accepts are shortened: nothing else
    repeats inside an expression (separator gaps hold at most
    ``_SEPARATOR_LIMIT`` characters, literals one each), so other runs ("-" * 8,
    emoji) are matched as they are. A run keeps at least ``floor`` characters
    (``_HEX_TOKEN_MINIMUM`` for hexadecimal digits) and as many as any word or
    repeated option needs in a row (see ``_longest_needed_run``). For
    characters that also occur as literals the kept length is congruent to
    the run length modulo every literal run of them ("00" in "b00bs", a block
    word "**") and the cut starts a multiple of it into the run, so complete
    occurrences stay complete.
    """

    def __init__(self, tokenized: list[tuple[str, tuple[_Token, ...]]], separators: Iterable[str], floor: int) -> None:
        classes: set[frozenset] = set()
        literals: set[str] = set()
        options: set[str] = set()
        for token in set(chain.from_iterable(tokens for _, tokens in tokenized)):
            if token.literal is not None:
                literals.add(token.literal)
                continue
            # What the token accepts, and what the letter before it sees as its
            # possible start (see _follow_chars).
            classes.add(token.chars)
            classes.add(token.chars | {option[0] for option in token.multi})
            options.update(token.multi)
        classes.discard(frozenset())
        letters = frozenset().union(*classes)
        sets = list(classes) + [frozenset(separators), frozenset("."), *(frozenset(item) for item in literals)]
        sets.append(frozenset().union(letters, *sets, *options))
        self._floor = floor
        self._letters = _class_pattern(letters)
        self._readable = _class_pattern(sets[-1])
        self._sets = [_class_pattern(chars) for chars in sets if chars]
        self._by_signature: dict[int, str] = {}
        self._representatives: dict[str, str] = {}
        # A multi-character option reading two characters of one class tells
        # them apart (German substitutes "c" and "k" for each other, but the
        # option "ck" does not match "kc"): those only run with their own case
        # variants.
        repeated = set()
        for option in options:
            keys = [self._representative(character) for character in option]
            repeated.update(character for character, key in zip(option, keys) if keys.count(key) > 1)
        if repeated:
            self._sets.extend(_class_pattern(character) for character in sorted(repeated))
            self._by_signature.clear()
            self._representatives.clear()

        # Word characters by class; one no expression reads (a key none of its
        # own options spells) stays itself.
        table = {
            ord(character): self._representative(character) or character
            for character in set("".join(word for word, _ in tokenized))
        }
        needed: dict[str, int] = {}
        periods: dict[str, int] = {}
        for word, tokens in tokenized:
            repeats = list(_REPEATED.finditer(word.translate(table)))
            for repeat in repeats:
                needed[repeat.group(1)] = max(needed.get(repeat.group(1), 0), len(repeat.group(0)))
            if repeats:
                literal_keys = (table[ord(token.literal)] if token.literal is not None else None for token in tokens)
                for key, group in groupby(literal_keys):
                    if key is not None:
                        periods[key] = lcm(periods.get(key, 1), sum(1 for _ in group))
        for option in options:
            for repeat in _REPEATED.finditer(option.translate(table)):
                needed[repeat.group(1)] = max(needed.get(repeat.group(1), 0), 2 * len(repeat.group(0)))
        self._needed = needed
        self._periods = periods
        # Representative -> (characters to keep, period), or None when no letter
        # class accepts the characters.
        self._limits: dict[str, Optional[tuple[int, int]]] = {}

    def _representative(self, character: str) -> Optional[str]:
        """The first seen character interchangeable with ``character``, or None
        when no expression reads it. Only readable characters are cached, so
        the cache is bounded by the dictionary, not by the input."""
        representative = self._representatives.get(character)
        if representative is None:
            if not self._readable.fullmatch(character):
                return None
            signature = sum(1 << index for index, chars in enumerate(self._sets) if chars.fullmatch(character))
            if _WORD_CHARACTER.match(character):
                signature |= 1 << len(self._sets)
            representative = self._by_signature.setdefault(signature, character)
            self._representatives[character] = representative
        return representative

    def _limits_for(self, representative: str) -> Optional[tuple[int, int]]:
        if representative not in self._limits:
            limits = None
            if self._letters.fullmatch(representative):
                period = self._periods.get(representative, 1)
                limits = (max(self._floor, self._needed.get(representative, 0), period), period)
            self._limits[representative] = limits
        return self._limits[representative]

    def shorten(self, text: str, span_map: list[tuple[int, int]]) -> tuple[str, list[tuple[int, int]]]:
        # Runs are found on a copy where interchangeable characters are equal.
        table: dict[int, str] = {}
        for character in set(text):
            representative = self._representative(character)
            if representative is not None and self._limits_for(representative) is not None:
                table[ord(character)] = representative
        canonical = text.translate(table)
        pieces: list[str] = []
        mapped: list[tuple[int, int]] = []
        cursor = 0
        for run in _LONG_RUN.finditer(canonical):
            limits = self._limits.get(run.group(1))
            if limits is None:
                continue
            keep, period = limits
            start, end = run.span()
            if _HEX_CHARACTER.match(text, start):
                # The hex/UUID guard must still see a long identifier as long.
                keep = max(keep, _HEX_TOKEN_MINIMUM)
            # The smallest length >= keep that is congruent to the run's length
            # modulo the period.
            keep += (end - start - keep) % period
            if end - start <= keep:
                continue
            removed = end - start - keep
            # The kept character at ``cut`` is the one after the removed ones.
            cut = start + period * (keep // period - 1)
            pieces.append(text[cursor:cut])
            mapped.extend(span_map[cursor:cut])
            pieces.append(text[cut + removed])
            mapped.append(_original_span(span_map, cut, cut + removed + 1))
            cursor = cut + removed + 1
        pieces.append(text[cursor:])
        mapped.extend(span_map[cursor:])
        return "".join(pieces), mapped


def _class_pattern(chars: Iterable[str]) -> re.Pattern[str]:
    # An empty class (a dictionary without letters, e.g. only block=["--"])
    # matches nothing.
    members = "".join(_class_members(chars))
    return re.compile("[" + members + "]" if members else "(?!)", re.IGNORECASE | re.UNICODE)


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
    ordered = _ordered_substitutions(substitutions)
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
    elide: bool = True,
    optional: tuple[int, ...] = (),
) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """The expression for one profanity and its *stop* class (``elide`` and
    ``optional``: see ``_tokenize``).

    The stop class matches every character that no part of the expression can
    consume or test positively: all token classes, multi-character options and
    literals, every separator and whitespace are excluded from it. Each
    construct reads forward only through characters it accepts, and the only
    look-behind (the start guard) reads one character before the match, so an
    attempt starting at ``p`` reads nothing outside ``[p - 1, s]``, where ``s``
    is the first stop character at or after ``p``. The confirming passes rely
    on this to rescan only where masking changed the text.
    """
    tokens = _tokenize(profanity, ordered, elide, optional)
    readable: set[str] = set(separators)
    for token in tokens:
        readable.update(token.chars)
        readable.update(character for option in token.multi for character in option)
        if token.literal is not None:
            readable.add(token.literal)
    stop = re.compile(r"[^\s" + "".join(_class_members(readable)) + "]", re.IGNORECASE | re.UNICODE)
    return re.compile(_tokens_expression(list(tokens), separators), re.IGNORECASE | re.UNICODE), stop


def _is_elidable(profanity: str) -> bool:
    """Whether the single interior vowel of a word may be left out ("fck")."""
    lowered = profanity.lower()
    vowel_positions = [index for index, character in enumerate(lowered) if character in _VOWEL_KEYS]
    consonant_letters = sum(1 for character in lowered if character.isalpha() and character not in _VOWEL_KEYS)
    return (
        len(vowel_positions) == 1
        and 0 < vowel_positions[0] < len(lowered) - 1
        and consonant_letters >= _MIN_CONSONANTS_FOR_ELISION
    )


def _ordered_substitutions(substitutions: Mapping[str, list[str]]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Substitution options by key (slashes stripped), longest key first."""
    options_by_key = {
        character.strip("/"): tuple(options)
        for character, options in substitutions.items()
        if character.strip("/")
    }
    return tuple(sorted(options_by_key.items(), key=lambda item: len(item[0]), reverse=True))


@lru_cache(maxsize=16384)
def _tokenize(
    profanity: str,
    ordered: tuple[tuple[str, tuple[str, ...]], ...],
    elide: bool = True,
    optional: tuple[int, ...] = (),
) -> tuple[_Token, ...]:
    """The positions of one profanity: substitution sets, or literal characters.
    With ``elide`` the vowel of an elidable word is optional ("fck"), as is the
    token starting at each character index in ``optional``."""
    elidable = elide and _is_elidable(profanity)
    tokens: list[_Token] = []
    i = 0
    while i < len(profanity):
        for key, options in ordered:
            if profanity.startswith(key, i):
                tokens.append(_substitution_token(options, (elidable and key in _VOWEL_KEYS) or i in optional))
                i += len(key)
                break
        else:
            tokens.append(_Token(frozenset(), (), literal=profanity[i]))
            i += 1
    return tuple(tokens)


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


# Profanities this long or longer make a compound with what they abut.
_COMPOUND_PART_MINIMUM = 3


def _chain_needs(guard: tuple, touches_before: bool, touches_after: bool) -> tuple[tuple[bool, bool], ...]:
    """Every way a protected candidate can count as part of a compound: the
    side(s) (before, after) that would still have to touch another match, the
    minimal alternatives only; empty if touching cannot lift the protection.

    All alternatives are kept: "bitchh|biitch" can only lean on its right
    neighbour, although touching on the left would lift it too.
    """
    needs: list[tuple[bool, bool]] = []
    for need_before, need_after in ((True, False), (False, True), (True, True)):
        if need_before and need_after and needs:
            break  # touching on one side suffices; both is no further way
        if not _is_pure_alpha_substring(
            *guard,
            touches_before=touches_before or need_before,
            touches_after=touches_after or need_after,
        ):
            needs.append((need_before and not touches_before, need_after and not touches_after))
    return tuple(needs)


def _resolve_chains(
    pending: list[tuple[int, int, str, tuple[tuple[bool, bool], ...]]],
    masked: Callable[[int], bool],
) -> list[tuple[int, int, str]]:
    """The greatest set of pending candidates, without overlaps, in which each
    candidate has one alternative (see ``_chain_needs``) whose needed sides all
    touch a match or another candidate of the set. Like a compound part, a
    supporting candidate must be a word of three or more letters: "su" read as
    "zu" next to "spek" read as "speck" makes no compound in "xsuspekty".

    Linear-time support counting: a candidate none of whose alternatives is
    supported any more is removed, and its neighbours lose its support in turn.
    """
    alive = [not any(masked(index) for index in range(start, end)) for start, end, _, _ in pending]

    def settle() -> None:
        ending: dict[int, list[int]] = {}
        starting: dict[int, list[int]] = {}
        for index, (start, end, base, _) in enumerate(pending):
            if alive[index] and len(base) >= _COMPOUND_PART_MINIMUM:
                ending.setdefault(end, []).append(index)
                starting.setdefault(start, []).append(index)
        # Live neighbours on each side that can support a chain.
        before = [len(ending.get(start, ())) for start, _, _, _ in pending]
        after = [len(starting.get(end, ())) for _, end, _, _ in pending]

        def supported(index: int) -> bool:
            start, end, _, needs = pending[index]
            left = before[index] > 0 or masked(start - 1)
            right = after[index] > 0 or masked(end)
            return any((left or not need_before) and (right or not need_after) for need_before, need_after in needs)

        queue = [index for index in range(len(pending)) if alive[index] and not supported(index)]
        while queue:
            index = queue.pop()
            if not alive[index]:
                continue
            alive[index] = False
            start, end = pending[index][0], pending[index][1]
            for neighbour in ending.get(start, ()):
                after[neighbour] -= 1
                if alive[neighbour] and not supported(neighbour):
                    queue.append(neighbour)
            for neighbour in starting.get(end, ()):
                before[neighbour] -= 1
                if alive[neighbour] and not supported(neighbour):
                    queue.append(neighbour)

    settle()
    # Overlapping survivors (different words over the same letters): keep the
    # first (longer words come first), then settle the supports again.
    claimed: set[int] = set()
    for index, (start, end, _, _) in enumerate(pending):
        if alive[index]:
            if claimed.intersection(range(start, end)):
                alive[index] = False
            else:
                claimed.update(range(start, end))
    settle()
    return [(start, end, base) for index, (start, end, base, _) in enumerate(pending) if alive[index]]


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


# Fewest hexadecimal characters that make an identifier (see _is_hex_token).
_HEX_TOKEN_MINIMUM = 8
_HEX_CHARACTER = re.compile(r"[0-9a-fA-F]")
_HEX_RUN = re.compile(r"[0-9a-fA-F-]+")


def _root_length(text: str) -> int:
    """Length of the shortest string ``text`` is a repetition of ("1212" -> 2),
    via the KMP prefix function (linear on every Python version)."""
    if not text:
        return 0
    border = [0] * len(text)
    length = 0
    for index in range(1, len(text)):
        while length and text[index] != text[length]:
            length = border[length - 1]
        if text[index] == text[length]:
            length += 1
        border[index] = length
    period = len(text) - border[-1]
    return period if len(text) % period == 0 else len(text)


def _repeats(folded: str, root: int, unit: str) -> bool:
    """Whether ``folded`` (case-folded, with its root length) is ``unit`` once
    or more, case-insensitively, in time linear in ``unit``: the unit must be a
    whole number of roots, fit a whole number of times and start the text."""
    unit = unit.lower()
    return bool(unit) and not len(unit) % root and not len(folded) % len(unit) and folded.startswith(unit)


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
        len(stripped) >= _HEX_TOKEN_MINIMUM
        and bool(re.fullmatch(r"[0-9a-fA-F]+", stripped))
        and bool(re.search(r"\d", stripped))
    )


def _is_spanning_word_boundary(
    matched: str,
    full_text: str,
    start: int,
    compound_parts: Container[str] = frozenset(),
    window: int = 0,
    expression: Optional[re.Pattern[str]] = None,
    reach: int = 0,
) -> bool:
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
    # A word glued to another profanity, or to the same one stretched, is a
    # compound, not a boundary to stop at ("beef curtains|beef curtains",
    # "fuck|curry muncher", "goo giirl|goo giirl").
    # Glued on both sides, both neighbours must be profanities: otherwise the
    # phrase reads across the insides of two words ("doggy|style doggy|style").
    glued_before = embedded_start and _ends_with_profanity(full_text[max(0, start - window) : start].lower(), compound_parts)
    glued_after = embedded_end and (
        _starts_with_profanity(full_text[end : end + window].lower(), compound_parts)
        or (expression is not None and expression.match(full_text[end : end + reach]))
    )
    if embedded_start and embedded_end:
        if glued_before and glued_after:
            embedded_start = embedded_end = False
    else:
        embedded_start = embedded_start and not glued_before
        embedded_end = embedded_end and not glued_after

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


# How many letters of a stretched final run a match gives back to a profanity
# that follows it.
_GIVE_BACK_LIMIT = 3


def _end_before_profanity(
    expression: re.Pattern[str],
    text: str,
    start: int,
    end: int,
    matched: str,
    compound_parts: Container[str],
    window: int,
    reach: int,
    first: re.Pattern[str],
    prefixes: Iterable[tuple[str, re.Pattern[str]]] = (),
    wildcards: Container[str] = frozenset(),
) -> Optional[tuple[re.Match[str], Optional[re.Match[str]], Optional[tuple[str, re.Pattern[str]]]]]:
    """The same match ending a few characters earlier, when more than
    whitespace follows it and what it gave back starts another match of the same
    expression ("twaatt|waat", "¢yberfuc¢|yberfuc"; ``first`` matches the
    characters such a match can start with), or, within a final run of one
    letter, a literal profanity that runs to the end of the word or to an
    inflection ("shitt|its" -> "shit|tits"; not "gitt|ite", where the double
    is the word's own). An entry the matched one starts with may take its place
    when it both fits the shorter text and follows ("5h1t5|h1t" read as
    "shits" is "shit|shit", "®ape®|ape" is "rape|rape"), as may another
    expression of the same word ("5cheiss5|cheiss" spelled per letter).
    Returns the shorter match, the following one (relative to where it
    starts) when it is one, and the entry and expression taking over (or
    None), or None.

    A character every letter of the word accepts ("*", in ``wildcards``)
    spells nothing and is never given back, and a following match is a copy
    glued to this one, so it holds no whitespace. Following matches are looked for
    within a few times the match's own length, which a stretched copy of the
    same word fits in.

    The following match starts inside the run, where the expression's start
    guard would refuse it, so it is matched on the text from there on, cut to
    ``reach`` (the longest a match can be)."""
    if not matched or end >= len(text) or text[end].isspace():
        return None
    if text[end - 1].lower() in wildcards:
        return None
    reach = min(reach, 4 * len(matched) + 16)
    # Nothing to give back when the next occurrence already starts where the
    # match ends ("cordes|cordes").
    if expression.match(text[end : end + reach]):
        return None
    prefixes = [(word, prefix) for word, prefix in prefixes if not prefix.match(text[end : end + reach])]
    last = matched[-1].lower()
    run = 1
    while run < len(matched) and matched[-run - 1].lower() == last:
        run += 1
    for given in range(1, min(len(matched) - 1, _GIVE_BACK_LIMIT) + 1):
        if text[end - given].lower() in wildcards:
            break
        follow = expression.match(text[end - given : end - given + reach]) if first.match(text, end - given) else None
        if follow is not None and _SPACE.search(follow.group(0)):
            follow = None
        if follow is not None or (given < run and _ends_word_as_profanity(text, end - given, window, compound_parts)):
            shorter = expression.fullmatch(matched[:-given])
            if shorter is not None:
                return shorter, follow, None
        for word, prefix in prefixes:
            shorter = prefix.fullmatch(matched[:-given])
            if shorter is not None:
                follow = prefix.match(text[end - given : end - given + reach])
                if follow is not None and not _SPACE.search(follow.group(0)):
                    return shorter, follow, (word, prefix)
    return None


def _ends_word_as_profanity(text: str, position: int, window: int, compound_parts: Container[str]) -> bool:
    """Whether the letters from ``position`` to the end of their word are a
    profanity, possibly with an inflection ("tits" in "shit|tits"), and not
    just begin with one ("tit" in "git|tite")."""
    rest = _PLAIN_HEAD.match(text, position, position + window + _LONGEST_SUFFIX + 1)
    if rest is None or (rest.end() < len(text) and _ALPHANUMERIC.match(text, rest.end())):
        return False
    word = rest.group(0).lower()
    return any(
        word[:size] in compound_parts and (size == len(word) or word[size:] in _INFLECTION_SUFFIXES)
        for size in range(_COMPOUND_PART_MINIMUM, len(word) + 1)
    )


def _joined_word_cut(
    expression: re.Pattern[str],
    matched: str,
    text: str,
    start: int,
    end: int,
    words: Container[str],
    longest: int,
    compound_parts: Iterable[str],
    joined: tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]],
) -> Optional[int]:
    """Where the plain word ends in a match that runs from it through
    separators into the first letters of the next word ("hell|-Ll|oyd",
    "ass|-S|asha", "\\ick|,\\|ick" with a substitute for its first letter),
    ``_DISCARD`` when the match also starts inside the word before it
    ("ha|ji-had|ji", "tu|sh-t|ush"): it only joins the ends of two words, or
    None.

    A separator here is a character no letter stands for. ``joined`` holds the
    patterns for one, for the last run of them with what follows on the
    reversed match, and for a word (anything else but whitespace). The word
    before them has no separator, or is a dictionary word with its own
    ("shit-brain|-s|hit"). Symbols inside the obfuscated word itself
    ("fu-ck|head", "a*s*s|wad", "f-u-c-k|you") are no word boundary, and
    neither is an inflection after the match ("fu-ck|ing") nor a separator
    inside one dictionary word, often with its letter repeated around it
    ("cock*k|blocker", "ra|pis(s|t"). Symbols that stand for letters are no
    separator either: the profanity must match without them ("lust**g|e" is
    "lusting", "hell-Ll" still "hellLl"). Nor is the separator a boundary when
    another profanity continues the word after the match ("ass-hole|fuck"):
    cutting there would leave that profanity inside an ordinary-looking word,
    where the substring guard protects it.
    """
    if end >= len(text) or not _LETTER.match(text, end):
        return None
    # The letters after the last run of symbols, found on the reversed match
    # so the search stays linear.
    separators, joined_tail, word_head = joined
    backwards = matched[::-1]
    # Letters or digits after any symbols, from a plain word; or anything
    # after separators no letter stands for, from a word without them.
    tail = _JOINED_TAIL.match(backwards)
    if tail is not None and tail.end() < len(matched) and _PLAIN.fullmatch(matched[: len(matched) - tail.end()]):
        word_head = _PLAIN_HEAD
    else:
        tail = joined_tail.match(backwards)
        if tail is None or tail.end() == len(matched):
            return None
    cut = len(matched) - tail.end()
    plain, letters = matched[:cut], matched[len(matched) - tail.end(1) :]
    if (
        not (not separators.search(plain) or plain.lower() in words)
        or _WORD_HEAD.match(text, end).group(0).lower() in _INFLECTION_SUFFIXES
        or not expression.fullmatch(plain + letters)
    ):
        return None
    # The words on both sides, as far as a dictionary word can reach.
    before = _PLAIN_TAIL.search(text, max(0, start + cut - longest), start + cut)
    after = word_head.match(text, end - len(letters), end - len(letters) + longest + 1)
    left, right = (before.group(0).lower() if before else ""), after.group(0).lower()
    if left.endswith(plain.lower()) and (left + right in words or (left[-1:] == right[:1] and left + right[1:] in words)):
        return None
    if start and _ALPHANUMERIC.match(text, start - 1) and _ALPHANUMERIC.match(matched):
        return _DISCARD
    if _starts_with_profanity(right[len(letters) :], compound_parts):
        return None
    return cut


def _spelling_characters(tokens: Iterable[_Token]) -> frozenset:
    """Case-folded characters that stand for some letter of a word but not for
    all of them ("$" in "shit", not "*")."""
    classes = [frozenset(character.lower() for character in token.chars) for token in tokens if token.chars]
    return frozenset().union(*classes) - frozenset.intersection(*classes) if classes else frozenset()


def _spells(text: str, consonants: int, spelling: frozenset) -> bool:
    """Whether ``text`` has at least ``consonants`` letters or spelling characters."""
    if len(text) - len(_LETTER.sub("", text)) >= consonants:
        return True
    count = 0
    for character in text:
        if character.isalpha() or character.lower() in spelling:
            count += 1
            if count == consonants:
                return True
    return False


def _retry_before_word_gap(
    expression: re.Pattern[str],
    text: str,
    start: int,
    matched: str,
    compound_parts: Container[str] = frozenset(),
    window: int = 0,
) -> Optional[re.Match[str]]:
    """The longest match of ``expression`` at ``start`` that stops before one of
    the gaps between words in ``matched`` and does not span a word boundary
    itself, or None. Like every re-match of a candidate's own span it runs on
    the candidate's text (positions relative to ``start``): the candidate's
    start already passed the start guard, and one that follows a given-back
    run (see ``_end_before_profanity``) starts inside that run."""
    # Each maximal run of non-word characters is scanned once; a gap is one
    # that holds whitespace.
    gaps = [gap for gap in _NON_WORD_RUN.finditer(matched) if _SPACE.search(gap.group(0))]
    for gap in reversed(gaps):
        # Before the gap's symbols first ("hell|! Ll"), then at each space in
        # it, last first ("fu**| - k").
        spaces = reversed([gap.start() + space.start() for space in _SPACES.finditer(gap.group(0))])
        for cut in dict.fromkeys([gap.start(), *spaces]):
            retry = expression.match(matched[:cut])
            if retry is not None and not _is_spanning_word_boundary(retry.group(0), text, start, compound_parts, window):
                return retry
    return None


_PHRASE_BREAK = re.compile(r"[,.;:?!]\s|\s[,.;:?!]")
# A gap between words is a maximal run of non-word characters holding
# whitespace (", ", " - ", " / ").
_NON_WORD_RUN = re.compile(r"\W+")
_LETTER = re.compile(r"[^\W\d_]")
_ALPHANUMERIC = re.compile(r"[^\W_]")
# Returned by _joined_word_cut for a match that only joins two words' ends.
_DISCARD = -1
_PLAIN_TAIL = re.compile(r"[^\W_]+\Z")
_PLAIN_HEAD = re.compile(r"[^\W_]+")
# On a reversed match: letters or digits, then symbols (or "_") before them
# ("hell-Ll", "sh1t-t", "hell]Ll", "5hit-5" read backwards).
_JOINED_TAIL = re.compile(r"([^\W_]+)((?:[^\w\s]|_)+)")
_PLAIN = re.compile(r"[^\W_]+")
_SPACE = re.compile(r"\s")
_SPACES = re.compile(r"\s+")
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


def _coerce_words(words: Iterable[str], option: str, *, strip_invisible: bool = False) -> frozenset[str]:
    # A bare string is one word, not an iterable of letters. Blank entries are
    # ignored: an empty pattern would match everywhere without consuming text.
    items = [words] if isinstance(words, str) else words
    cleaned: set[str] = set()
    for word in items:
        if not isinstance(word, str):
            raise TypeError(f"{option} entries must be strings, not {type(word).__name__}")
        if strip_invisible:
            # The checked text loses its invisible characters before matching,
            # so an entry keeping them ("\u2764\ufe0f") could never match.
            word = "".join(character for character in word if not _is_invisible(character))
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


def _modifies_previous(character: str) -> bool:
    # Invisible characters that extend the character before them into one
    # grapheme (UAX #29): variation selectors and emoji tag characters.
    return (
        "\ufe00" <= character <= "\ufe0f"
        or "\U000e0100" <= character <= "\U000e01ef"
        or "\U000e0020" <= character <= "\U000e007f"
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


# How single-language normalization rewrites multi-letter keys in the text
# (see normalize_with_mapping), for the per-letter variant expressions: German
# "sch" becomes "sh", so its "c" (at offset 1) is optional ("sh" and "5ch" both
# match); Spanish "rr" becomes "r" everywhere, so it stays whole (None), while
# "ll" only becomes "y" at the start of a word and is spelled per letter too.
_NORMALIZED_KEYS: dict[str, dict[str, Optional[int]]] = {"german": {"sch": 1}, "spanish": {"rr": None}}

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
