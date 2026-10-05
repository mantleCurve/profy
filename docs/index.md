# Profy Documentation

Profy is a Python fork of [Blaspsoft/blasp](https://github.com/Blaspsoft/blasp). It keeps Blasp's core profanity filtering ideas and bundled language data while exposing a small Python API.

## API

Use `filter_text()` when you want the full `ShieldResult`, `clean_text()` when you only need sanitized text, and `check_text()` when you only need a boolean.

```python
from profy import filter_text

result = filter_text("f-u-c-k", mask="*")
assert result.clean == "*******"
```

## Matching Behavior

The detector is regex-based and uses Blasp-style generated expressions for each dictionary word. It supports:

- case-insensitive matching
- substitution characters such as `@`, `$`, `!`, `1`, `*`, and accented variants
- separators between letters
- repeated substitution characters (ordinary double letters such as `cook` never create a match on their own)
- invisible Unicode characters ignored for matching only; the original text is returned unchanged outside matched spans
- false-positive guards for known words, UUIDs, long hex tokens, and many embedded clean words

Each generated expression assigns every input character to exactly one letter or separator without re-splitting, so matching time stays linear even for adversarial input such as long runs of `*`.

Pass `driver="pattern"` for Blasp's literal driver instead: exact, whole-word, case-insensitive matches with no obfuscation handling.

## Languages

Bundled dictionaries: English, Spanish, German, and French.

```python
from profy import filter_text

result = filter_text("maldición", languages="spanish")
```

Unknown language names raise `ValueError` listing the available languages.

For multi-language checks:

```python
result = filter_text("text", languages=["english", "spanish"])
result = filter_text("text", all_languages=True)
```

## Release Checklist

1. Update `profy/__init__.py` and `pyproject.toml` with the new version.
2. Update `CHANGELOG.md`.
3. Install development dependencies with `python -m pip install -e ".[dev]"`.
4. Run `python -m pytest --cov` (100% line and branch coverage is required).
5. Build with `python -m build`.
6. Create and push a `v*` version tag.
