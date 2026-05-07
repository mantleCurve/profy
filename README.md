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
python -m pytest
```

## Reuse a Filter

Create a `ProfanityFilter` when you need to process many strings with the same options.

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

- `languages`: a language name or iterable. Bundled data includes `english`, `spanish`, `german`, and `french`.
- `all_languages`: use every bundled language dictionary.
- `allow`: words that should never be flagged.
- `block`: extra words that should always be flagged.
- `mask`: a replacement character or callback `(word, length) -> str`.
- `minimum_severity`: `mild`, `moderate`, `high`, or `extreme`.

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

Use `scripts/sync_from_blasp.py` to fetch the latest Blasp PHP repository, export updated dictionary/config data into Profy's Python package, and generate an implementation-change report for PHP files that need manual port review.

```bash
python scripts/sync_from_blasp.py
```

## Release

Alpha releases use PEP 440 versions such as `0.1.0a1` and Git tags such as `v0.1.0a1`. The GitHub Actions release workflow runs tests, builds the source distribution and wheel, uploads them as artifacts, and attaches them to the GitHub Release.

## Attribution

Profy is a Python fork of [Blaspsoft/blasp](https://github.com/Blaspsoft/blasp), originally authored by Michael Deeming and released under the MIT license.

## License

MIT. See [LICENSE.md](LICENSE.md).
