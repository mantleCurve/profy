"""Profy: a Python profanity filter forked from Blaspsoft/blasp."""

from .core import (
    Match,
    ProfanityFilter,
    Severity,
    ShieldResult,
    check_text,
    clean_text,
    filter_text,
)

__all__ = [
    "Match",
    "ProfanityFilter",
    "Severity",
    "ShieldResult",
    "check_text",
    "clean_text",
    "filter_text",
]

__version__ = "0.1.2"
