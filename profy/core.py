from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from importlib import resources
import re
import unicodedata
from typing import Callable, Iterable, Mapping, Optional, Union


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
    ) -> None:
        self.languages = _available_languages() if all_languages else _coerce_languages(languages)
        self.allow = {word.lower() for word in allow}
        self.block = {word.lower() for word in block}
        self.mask = mask
        self.minimum_severity = _coerce_severity(minimum_severity)
        self.dictionary = _Dictionary.for_languages(
            self.languages,
            allow=self.allow,
            block=self.block,
        )

    def check(self, text: Optional[str]) -> ShieldResult:
        text = _strip_format_characters(text or "")
        if not text:
            return ShieldResult(text, text, (), 0)

        normalized, normalized_map = self.dictionary.normalize_with_mapping(text)
        normalized, normalized_map = _collapse_whitespace_with_mapping(normalized, normalized_map)
        immutable_normalized = normalized
        expressions = sorted(
            self.dictionary.expressions.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        )

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
                    original_full_word = _full_word_context(immutable_normalized, start, length)
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
                            text=text[original_start:original_end],
                            base=base,
                            severity=severity,
                            position=original_start,
                            length=original_length,
                            language=",".join(self.languages),
                        )
                    )

        clean = text
        for match in sorted(matches, key=lambda item: item.position, reverse=True):
            clean = (
                clean[: match.position]
                + self._mask(match.text, match.length)
                + clean[match.position + match.length :]
            )

        return ShieldResult(text, clean, tuple(matches), _score(matches, text))

    def clean(self, text: Optional[str]) -> str:
        return self.check(text).clean

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
) -> ShieldResult:
    return ProfanityFilter(
        languages=languages,
        all_languages=all_languages,
        allow=allow,
        block=block,
        mask=mask,
        minimum_severity=minimum_severity,
    ).check(text)


def check_text(text: Optional[str], **options: object) -> bool:
    return filter_text(text, **options).is_offensive


def clean_text(text: Optional[str], **options: object) -> str:
    return filter_text(text, **options).clean


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
    ) -> None:
        self.profanities = profanities
        self.false_positives = false_positives
        self.severity_map = severity_map
        self.languages = languages
        self.expressions = _generate_expressions(profanities, separators, substitutions)

    @classmethod
    def for_languages(
        cls,
        languages: list[str],
        *,
        allow: set[str],
        block: set[str],
    ) -> "_Dictionary":
        global_data = _load_json("global.json")
        profanities: list[str] = []
        false_positives = {item.lower() for item in global_data.get("false_positives", [])}
        severity_map: dict[str, Severity] = {}
        substitutions = dict(global_data.get("substitutions", {}))

        for language in languages:
            data = _load_language(language)
            profanities.extend(data.get("profanities", []))
            false_positives.update(item.lower() for item in data.get("false_positives", []))
            severity_map.update(_build_severity_map(data))

            for key, values in data.get("substitutions", {}).items():
                existing = substitutions.setdefault(key, [])
                for value in values:
                    if value not in existing:
                        existing.append(value)

        for word in block:
            if word not in profanities:
                profanities.append(word)
            severity_map[word.lower()] = Severity.HIGH

        profanities = list(dict.fromkeys(word for word in profanities if word.lower() not in allow))
        return cls(
            profanities=profanities,
            false_positives=false_positives,
            severity_map=severity_map,
            substitutions=substitutions,
            separators=global_data.get("separators", []),
            languages=languages,
        )

    def severity_for(self, word: str) -> Severity:
        return self.severity_map.get(word.lower(), Severity.HIGH)

    def normalize(self, text: str) -> str:
        normalized, _ = self.normalize_with_mapping(text)
        return normalized

    def normalize_with_mapping(self, text: str) -> tuple[str, list[tuple[int, int]]]:
        normalized = text
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


def _generate_expressions(
    profanities: list[str],
    separators: list[str],
    substitutions: Mapping[str, list[str]],
) -> dict[str, re.Pattern[str]]:
    separator_expression = _separator_expression(separators)
    substitution_expressions: dict[str, str] = {}
    for character, options in substitutions.items():
        plain = character.strip("/")
        has_multi = any(len(option) > 1 and not re.fullmatch(r"\\.", option) for option in options)
        if has_multi:
            parts = [
                option if re.fullmatch(r"\\.", option) else re.escape(option)
                for option in options
            ]
            substitution_expressions[plain] = "(?:" + "|".join(parts) + ")+" + "{!!}"
        else:
            substitution_expressions[plain] = _escaped_expression(options, quantifier="+") + "{!!}"

    ordered_substitutions = sorted(
        substitution_expressions.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    expressions: dict[str, re.Pattern[str]] = {}
    for profanity in profanities:
        pieces: list[str] = []
        i = 0
        while i < len(profanity):
            for key, replacement in ordered_substitutions:
                if profanity.startswith(key, i):
                    pieces.append(replacement)
                    i += len(key)
                    break
            else:
                pieces.append(re.escape(profanity[i]))
                i += 1
        pattern = "".join(pieces).replace("{!!}", separator_expression)
        expressions[profanity] = re.compile(pattern, re.IGNORECASE | re.UNICODE)
    return expressions


def _separator_expression(separators: list[str]) -> str:
    normal = [separator for separator in separators if separator != "."]
    return "(?:" + _escaped_expression(normal, escaped=[r"\s"], quantifier="") + r"|\.(?=\w)){0,3}?"


def _escaped_expression(
    characters: Iterable[str],
    *,
    escaped: Iterable[str] = (),
    quantifier: str = "*?",
) -> str:
    parts = list(escaped)
    parts.extend(re.escape(character) for character in characters)
    return "[" + "".join(parts) + "]" + quantifier


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
    if len(parts) <= 1:
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
    if len(full_word) <= len(matched) or len(matched) > len(base):
        return False

    match_lower = matched.lower()
    word_lower = full_word.lower()
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
    if isinstance(languages, str):
        return [languages]
    return list(languages)


def _coerce_severity(value: Optional[Union[Severity, str]]) -> Optional[Severity]:
    if value is None or isinstance(value, Severity):
        return value
    return Severity(value.lower())


def _strip_format_characters(text: str) -> str:
    return "".join(character for character in text if unicodedata.category(character) != "Cf")


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
    if start >= end or not span_map:
        return start, end
    spans = span_map[start:end]
    return min(span[0] for span in spans), max(span[1] for span in spans)


def _available_languages() -> list[str]:
    root = resources.files(__package__).joinpath("data/languages")
    return sorted(
        path.name.removesuffix(".json")
        for path in root.iterdir()
        if path.name.endswith(".json")
    )


def _load_language(language: str) -> dict[str, object]:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", language):
        return {"profanities": [], "false_positives": []}
    return _load_json(f"languages/{language}.json")


def _load_json(path: str) -> dict[str, object]:
    target = resources.files(__package__).joinpath("data", path)
    if not target.is_file():
        return {"profanities": [], "false_positives": []}
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
