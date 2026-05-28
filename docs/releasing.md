# Releasing

This document describes how to cut a new release of codex-imagen.

The canonical copy is in `docs/releasing.md`; `RELEASING.md` at the repo root
also exists for projects that expect it at the top level.

---

## One-time setup

1. Create a PyPI account at https://pypi.org and verify your email.
2. Generate an API token at https://pypi.org/manage/account/token/ — for the
   very first release, scope it to **Entire account** (the project `codex-imagen`
   doesn't exist on PyPI yet, so project-scoped tokens aren't available). After
   v0.1.0 ships you can replace it with a project-scoped token.
3. Add the token to GitHub: **Settings → Secrets and variables → Actions → New
   repository secret**
   - Name: `PYPI_API_TOKEN`
   - Value: `pypi-...` (the full token string from step 2)

## Each release

1. Bump `version` in `pyproject.toml` (e.g. `0.1.0` → `0.1.1`).
2. Update `__version__` in `src/codex_imagen/__init__.py` to match.
3. Update CHANGELOG if you keep one.
4. Commit and push to main:
   ```bash
   git add pyproject.toml src/codex_imagen/__init__.py
   git commit -m "Bump version to 0.1.1"
   git push origin main
   ```
5. Tag and push the tag:
   ```bash
   git tag v0.1.1
   git push --tags
   ```
6. GitHub Actions picks up the `v*.*.*` tag, builds the wheel + sdist, runs
   `twine check`, and uploads to PyPI automatically. Watch progress at
   https://github.com/VelmoAI/codex-imagen/actions.

## First release ever

After the first successful publish:
- Optionally re-scope the PyPI token to project `codex-imagen` only (more
  secure than an account-wide token).
- Verify the package page looks right: https://pypi.org/project/codex-imagen/
- Test a clean install: `pip install codex-imagen` in a fresh venv.
