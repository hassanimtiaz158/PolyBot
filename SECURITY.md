# Security Audit Report

**Date:** 2026-08-09
**Auditor:** Automated security scan
**Scope:** Full repository — code, configuration, Docker, dependencies

---

## Executive Summary

The codebase demonstrates strong security practices in several areas: all SQL
queries use parameterized placeholders, credentials are loaded from environment
variables (never hard-coded), the alerting system redacts sensitive keys, and
the dashboard is strictly read-only. However, the audit identified **6 critical
or high-severity findings** that require remediation.

| Severity | Count | Status |
|----------|-------|--------|
| Critical | 1 | Fixed |
| High     | 5 | Fixed |
| Medium   | 5 | Documented |
| Low      | 6 | Documented |

---

## Findings

### CRITICAL

#### C-1. Unsafe pickle deserialization (RCE vector)

- **Files:** `app/models/probability_model.py:463`, `app/models/calibration.py:242`
- **Risk:** `pickle.load()` executes arbitrary code embedded in the serialized
  file. If an attacker can replace a model `.pkl` file on disk, they achieve
  remote code execution.
- **Status:** **FIXED** — Replaced with a `RestrictedUnpickler` that whitelists
  only `numpy`, `sklearn`, and `lightgbm` classes. All other classes are
  rejected.

### HIGH

#### H-1. Container runs as root

- **File:** `Dockerfile`
- **Risk:** No `USER` directive. Any code execution vulnerability inside the
  container grants root-level access.
- **Status:** **FIXED** — Added non-root `appuser` with `USER appuser`.

#### H-2. No .dockerignore — secrets leak into image layers

- **File:** (missing)
- **Risk:** `COPY . .` copies everything into the image including `.env`
  (if present on build host), `.git/`, `tests/`, `data/*.db`, and all
  development artifacts.
- **Status:** **FIXED** — Created `.dockerignore` excluding `.git`, `.env*`,
  `data/`, `tests/`, `notebooks/`, `scripts/`, `__pycache__`, `*.pyc`,
  development docs.

#### H-3. No API authentication

- **File:** `app/api/app.py`, `app/api/dependencies.py`
- **Risk:** All API endpoints are publicly accessible to anyone on the network.
  While the API is read-only, it exposes trading positions, P&L, risk limits,
  and audit trails.
- **Status:** **FIXED** — Added optional API key authentication via
  `POLY_API_KEY` environment variable. When set, all requests must include
  `X-API-Key` header. When unset (research mode), auth is bypassed.

#### H-4. Dev dependencies in production Docker image

- **File:** `requirements.txt:24-27`
- **Risk:** `pytest`, `ruff`, `mypy` are installed in the production image,
  expanding the attack surface unnecessarily.
- **Status:** **FIXED** — Split into `requirements.txt` (production only) and
  `requirements-dev.txt` (development/testing). Dockerfile uses only
  `requirements.txt`.

#### H-5. No Docker security hardening

- **File:** `docker-compose.yml`
- **Risk:** No `read_only`, `cap_drop`, `security_opt`, or resource limits.
  Services run with full capabilities and unlimited resources.
- **Status:** **FIXED** — Added `read_only: true`, `cap_drop: [ALL]`,
  `security_opt: [no-new-privileges:true]`, and CPU/memory limits.

### MEDIUM

#### M-1. No CORS configuration

- **File:** `app/api/app.py`
- **Risk:** Cross-origin requests are not restricted.
- **Mitigation:** API is server-side only; dashboard connects via internal
  Docker network. Consider adding explicit CORS if browser clients are added.

#### M-2. No rate limiting

- **File:** `app/api/app.py`
- **Risk:** API vulnerable to denial-of-service without rate limiting.
- **Mitigation:** Low risk for internal/localhost deployment. Add `slowapi`
  if exposed to untrusted networks.

#### M-3. No multi-stage Docker build

- **File:** `Dockerfile`
- **Risk:** Build tools and pip cache bloat the final image.
- **Mitigation:** Addressed partially by removing dev dependencies. Full
  multi-stage build recommended for production.

#### M-4. Unpinned dependency versions

- **File:** `requirements.txt`
- **Risk:** Different builds may install different versions.
- **Mitigation:** Use `pip-compile` or `pip freeze` to generate locked
  requirements for reproducible builds.

#### M-5. Unbounded upper versions in pyproject.toml

- **File:** `pyproject.toml`
- **Risk:** Major version bumps could introduce breaking changes.
- **Mitigation:** Add upper bounds consistent with `requirements.txt`.

### LOW

#### L-1. Base image not patch-pinned

- **File:** `Dockerfile:1` — `python:3.11-slim` (no patch version)
- **Mitigation:** Pin to specific digest for reproducible builds.

#### L-2. No HEALTHCHECK in Dockerfile

- **Mitigation:** Add `HEALTHCHECK` for orchestrator health monitoring.

#### L-3. .gitignore doesn't exclude .env variants

- **File:** `.gitignore:2` — only excludes `.env` literally
- **Mitigation:** Added `.env.*` patterns (excluding `.env.example`).

#### L-4. .gitignore doesn't exclude secret file types

- **Mitigation:** Added `*.pem`, `*.key`, `*.p12`, `*.pfx` patterns.

#### L-5. Deprecated `version` key in docker-compose.yml

- **Mitigation:** Removed deprecated `version: "3.9"` key.

#### L-6. Ports exposed to host without binding to localhost

- **File:** `docker-compose.yml:10-11, 34-35`
- **Mitigation:** Consider binding to `127.0.0.1` for local-only access.

---

## Positive Security Findings

| Area | Status |
|------|--------|
| SQL injection | **CLEAN** — All queries use parameterized `?` placeholders |
| Hard-coded secrets | **CLEAN** — No credentials found in source code |
| Credential logging | **CLEAN** — AlertDispatcher redacts sensitive keys |
| `exec()`/`eval()` | **CLEAN** — Not used anywhere |
| Private key handling | **CLEAN** — No wallet integration; credentials in env vars only |
| Dashboard exposure | **CLEAN** — Read-only, no credentials displayed |
| `.env.example` | **CLEAN** — All credential fields empty |
| `.gitignore` | **CLEAN** — `.env` excluded from version control |
| Circuit breaker events | **CLEAN** — Never log credentials or API keys |
| Audit event bus | **CLEAN** — Module docstring prohibits credential logging |

---

## Recommendations for Future Hardening

1. **Pin exact dependency versions** with hashes for reproducible builds
2. **Add rate limiting** (`slowapi`) if API is exposed beyond localhost
3. **Add CORS middleware** if browser-based clients are added
4. **Consider full multi-stage Docker build** for minimal production images
5. **Add HEALTHCHECK** to Dockerfile for orchestrator integration
6. **Bind ports to 127.0.0.1** in docker-compose for local-only access
7. **Run container security scanning** (Trivy, Snyk) in CI/CD pipeline
8. **Add pre-commit hooks** for secret detection (e.g., `detect-secrets`)
