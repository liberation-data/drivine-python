# Releasing

drivine-python, drivine4j and drivine are released the same way, from a tag, by CI. Each repository has this file,
differing only in its publish step.

## The process

1. **Release PR.** A PR bumps the version (build.gradle / package.json / pyproject.toml) and the changelog, if the
   repo has one. It is reviewed and passes CI like any other PR.
2. **Tag the merge commit.** Merging the release PR is the release point. The maintainer then tags the merge commit
   on the default branch `vX.Y.Z` and pushes the tag.
3. **CI publishes.** The tag push runs `.github/workflows/release.yml`, which checks out the tag, runs the full gate,
   builds, checks, and publishes. Nothing is built from a laptop.
4. **Credentials.** Repository secrets or trusted publishing (OIDC), held in a GitHub environment named `release`.

For step 2:

```
git switch main && git pull
git tag vX.Y.Z
git push origin vX.Y.Z
```

## What release.yml does

- **`check`** works out the version: the tag without its `v` on a tag push, or the build file's version otherwise.
  On a tag push it fails, saying why, unless the tag is `vX.Y.Z` (or `vX.Y.Z-suffix`), its version equals the build
  file's, and the tagged commit is on the default branch.
- **`gate`** runs the repository's full CI, `ci.yml`, unchanged.
- **`build`** builds the release artifacts from the checked-out ref, checks them, runs a smoke test, and uploads them
  as the run's artifact.
- **`publish`** runs on a tag push alone, in the `release` environment. It publishes the exact files `build` made.
- **`github-release`** then creates the GitHub release for the tag, with generated notes.

A **dry run** is everything but `publish` and `github-release`. Run it from the Actions tab (Release, Run workflow,
on any branch or tag), and it runs by itself on a pull request that changes the release machinery. A tag push is the
only thing that publishes.

## drivine-python: PyPI

- **Version:** `version` in `pyproject.toml`. The tag must be `v` and that version, character for character: `v0.3.0`
  for `0.3.0`. There is no `__version__` to keep in step.
- **Build:** `uv build --no-sources`, which writes the sdist and the wheel built from it to `dist/` (hatchling).
- **Checks:** `twine check --strict` on both, then `scripts/smoke_wheel.py`, which installs the wheel into a clean
  virtual environment on Python 3.11 and 3.14, imports `drivine`, and runs `python -m drivine.stubs` on a small
  project's models.
- **Publish:** `pypa/gh-action-pypi-publish` uploads the built `dist/` to [PyPI](https://pypi.org/project/drivine/)
  with trusted publishing (OIDC). No token is held anywhere.
- **Dry run:** leaves the `dist` artifact on the run: `drivine-X.Y.Z.tar.gz` and `drivine-X.Y.Z-py3-none-any.whl`.

The same checks, locally, publishing nothing:

```
uv build --no-sources
uvx twine check --strict dist/*
python scripts/smoke_wheel.py dist/*.whl
```

### Settings, made once

- **GitHub:** Settings, Environments, a `release` environment. It can carry required reviewers, and a deployment tag
  rule `v*`.
- **PyPI:** the `drivine` project's Publishing settings, a GitHub trusted publisher with owner `liberation-data`,
  repository `drivine-python`, workflow `release.yml`, and environment `release`.
