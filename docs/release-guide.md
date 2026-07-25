# Release guide (alpha)

This document covers **TestPyPI** and future **production PyPI** releases for
`recollect-lines`. Normal CI builds and validates distributions but **never
publishes**. Publishing is manual, opt-in, and environment-gated.

**Current distribution:** `0.1.0a1` is published on **TestPyPI only**;
production PyPI has no release. `0.1.0a2` is the current **durable-only
release candidate** — packaged in this repository but not yet published
anywhere. Both are **alpha / pre-production**, not production-certified or
stable. Install only when you accept that risk.

Publishing `0.1.0a2` to TestPyPI is a **separate, manually authorized action**
(workflow_dispatch with an explicit confirmation, see §3) — merging packaging
changes does not publish anything. The RFC-004 legacy-reader sunset
observation window (see `docs/design/RFC-004.md`, P3.3 retirement criteria)
begins only after a durable-only build is actually **deployed to a given
broker home**, not at merge or at publish time; each supported broker home
starts its own window when that home is upgraded.

## Version immutability

PyPI and TestPyPI treat each uploaded version as **immutable**. You cannot
replace or overwrite an existing version. To withdraw a bad release, **yank**
(or deprecate) the version on the index; do not attempt to re-upload the same
version number.

## 1. GitHub environment: `testpypi`

Create a protected GitHub environment named **`testpypi`** on the upstream
repository (`Umi4Life/recollect-lines`):

1. **Settings → Environments → New environment** → name it `testpypi`.
2. Enable **Required reviewers** (recommended) so TestPyPI uploads need
   explicit approval.
3. Restrict deployment branches if your process requires it (e.g. `master`
   only).

The workflow `.github/workflows/publish-testpypi.yml` targets this environment.
No publish step runs without passing the confirmation gate below.

## 2. TestPyPI trusted publisher (OIDC)

Configure **trusted publishing** on TestPyPI (no API tokens in the repo):

1. Sign in at [test.pypi.org](https://test.pypi.org/).
2. **Account settings → Publishing** → add a pending publisher (or edit the
   project after the first upload).
3. Set:
   - **PyPI project name:** `recollect-lines`
   - **Owner:** `Umi4Life`
   - **Repository:** `recollect-lines`
   - **Workflow name:** `publish-testpypi.yml`
   - **Environment name:** `testpypi`
4. Save. The workflow uses `id-token: write` and
   `pypa/gh-action-pypi-publish` with
   `repository-url: https://test.pypi.org/legacy/` only.

Claim the project name on TestPyPI with the first successful upload if it is
still unclaimed.

## 3. Manual TestPyPI publish (workflow dispatch)

Publishing is **workflow_dispatch only** and **defaults to no publish**.

1. Merge packaging changes and ensure CI packaging validation is green.
2. **Actions → Publish to TestPyPI → Run workflow**.
3. Set **`confirm_publish`** to exactly: `publish-testpypi`
   (any other value fails closed).
4. Approve the `testpypi` environment deployment if required.
5. The workflow builds artifacts, uploads them as a CI artifact, downloads
   that same artifact in the publish job, and uploads to TestPyPI only.

There is **no** production PyPI URL, tag trigger, or automatic release in this
workflow.

## 4. Smoke install from TestPyPI

After a TestPyPI upload, verify in a **clean** virtual environment. PyYAML is
not on TestPyPI by default, so use the main index for dependencies:

```bash
python3 -m venv /tmp/recollect-testpypi
source /tmp/recollect-testpypi/bin/activate
pip install --upgrade pip
pip install recollect-lines==0.1.0a2 \
  --extra-index-url https://test.pypi.org/simple/
recollect-lines --help
recollect-mcp --help
recollect-lines --home /tmp/recollect-smoke doctor --json
```

Or pin with hashes after you record them from a trusted build.

## 5. Production PyPI (separate authorization)

Production release is **out of scope** for the TestPyPI workflow. When ready
for a non-alpha stable line:

1. Create a **separate** GitHub environment (e.g. `pypi`) with stricter
   reviewers and branch protection.
2. Register a **production** trusted publisher on [pypi.org](https://pypi.org/)
   pointing at a **dedicated** production workflow (not added until explicitly
   authorized).
3. Bump version in `pyproject.toml` (never re-use a published version).
4. Run full local validation (`python -m build`, `twine check`, dist artifact
   acceptance).
5. Dispatch the production workflow only after TestPyPI smoke passes.

Do not store PyPI API tokens in the repository or workflow secrets for trusted
publishing.

## 6. Rollback reality

You cannot delete a version and upload again under the same number. Options:

- **Yank** the version on TestPyPI or PyPI (hides from default installs;
  existing pins may still resolve).
- Ship a **new** patch/pre-release version with the fix.
- Document the yanked version in release notes.

## Local validation (maintainers)

Before any publish dispatch:

```bash
git diff --check
python -m compileall -q src tests scripts
python -m pytest -q
python -m pip install build twine
python -m build
python -m twine check dist/*
python3 scripts/dist_artifact_acceptance.py
```

Optional: `python3 scripts/mcp_acceptance.py` and
`PYTHONPATH=src python3 scripts/side_agent_fixture_acceptance.py` after install
from `dist/` if you need broader acceptance coverage.
