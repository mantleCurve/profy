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

Pushing that tag runs the `Release` GitHub Actions workflow. It fails unless the tag (without the leading `v`), the `pyproject.toml` version and `profy.__version__` agree; then it runs the tests, builds the source distribution and wheel, smoke-tests the wheel in a clean virtualenv, uploads them as workflow artifacts, and attaches them to a GitHub Release. The release is marked as a pre-release only when the version is a PEP 440 pre-release (`a`, `b`, `rc` or `dev`).

The same workflow publishes to PyPI using Trusted Publishing. Publishing an already-existing version fails instead of being skipped. Running the workflow manually (`workflow_dispatch`) on a branch is a dry run: it tests, builds and smoke-tests the distributions but creates no GitHub Release and publishes nothing. Configure PyPI with:

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
