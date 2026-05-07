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
- repeated substitution characters
- invisible Unicode format character removal
- false-positive guards for known words, UUIDs, long hex tokens, and many embedded clean words

## Languages

Bundled dictionaries: English, Spanish, German, and French.

```python
from profy import filter_text

result = filter_text("maldición", languages="spanish")
```

For multi-language checks:

```python
result = filter_text("text", languages=["english", "spanish"])
result = filter_text("text", all_languages=True)
```

## Release Checklist

1. Update `profy/__init__.py` and `pyproject.toml` with the new version.
2. Update `CHANGELOG.md`.
3. Install development dependencies with `python -m pip install -e ".[dev]"`.
4. Run `python -m pytest`.
5. Build with `python -m build`.
6. Create and push a `v*` version tag.
