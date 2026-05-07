# Releases

Profy follows semantic versioning with PEP 440 pre-release suffixes for alpha builds, for example `0.1.0a1`.

## Publishing

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine upload dist/*
```

Create a matching Git tag:

```bash
git tag v0.1.0a1
git push origin v0.1.0a1
```

Pushing that tag runs the `Release` GitHub Actions workflow, which runs tests, builds the source distribution and wheel, uploads them as workflow artifacts, and attaches them to a GitHub Release.

The same workflow publishes to PyPI using Trusted Publishing. Configure PyPI with:

- Project name: `profy-filter`
- Owner: `mantleCurve`
- Repository: `profy`
- Workflow: `release.yml`
- Environment: `pypi`

GitHub Releases should include:

- version number
- installation command
- upstream Blasp commit, if synced
- highlights
- any compatibility or behavior notes
