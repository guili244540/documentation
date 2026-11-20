# IMPLEMENTATION PLAN — Direct download of `Response Repository Matrix.xlsx`

- **Implements:** `SPEC.md` (same branch, `directDownload`)
- **Branch:** `directDownload`
- **Approach in one line:** add an authenticated `api` module serving `GET /download` (raw bytes, caller auth at the platform ingress, fetch via a `download-testlog` execution), and plumb the download URL into the worker workflow outputs (`download_url`) so the chat skill can show the link (R7). The worker module itself is not turned into an HTTP server.

## 1. File-by-file changes

### 1.1 Worker — `download_url` plumbing (R7)

| File | Change |
|---|---|
| `workflow/apps/worker/src/worker/workflows/update_github_log.py` | `_github_config()` also reads `APP_DOWNLOAD_URL` (default `""`). `SheetState` gains `download_url: str = ""`; `read_testlog` fills it from config (env read stays inside the activity — determinism rule). `UpdateGitHubLogOutput` gains `download_url: str = ""`, set in `UpdateGitHubLog.run()` from `state.download_url` (both the dry-run and the committed return paths). |
| `workflow/apps/worker/src/worker/workflows/download_testlog.py` | `DownloadTestLogOutput` gains `download_url: str = ""`, set from `state.download_url`. |
| `workflow/apps/worker/tests/test_download_testlog.py`, `test_update_github_log.py` | New cases: with `APP_DOWNLOAD_URL` set, both outputs carry it verbatim and it contains no token material; unset → empty string. |
| `workflow/.env.example` | Add the name-only line `APP_DOWNLOAD_URL=` next to the `GITHUB_LOG_*` block, with a comment that it is bound at deploy time to the `update-github-log-api` module URL. Same declaration style as the existing `GITHUB_LOG_*` vars (the worker reads them via `os.environ`; no new env-package field needed). |

### 1.2 New `api` module — the endpoint (R1/R2)

| File | Change |
|---|---|
| `workflow/mistral.json` | Add a second module next to `worker`: name `api`, path `apps/api`, `serve: "api"`, a `port`, `routes` exposing `GET /download`, `build.dockerfile: "deploy/docker/Dockerfile.api"`, same `platform`, plus a `dev.startCommand`. The `worker` module entry is **not** modified (`routes: []`, `port: null` stay). |
| `workflow/apps/api/pyproject.toml` | New package modeled on `apps/worker/pyproject.toml`: `[tool.uv.build-backend] module-name = "api"`; deps `utils`, `env`, `mistralai`, `mistralai-workflows[mistralai]`, `pydantic`, `structlog` (drop `openpyxl` — the api app never parses the workbook). Dev group: `pytest`, `pytest-asyncio`; same `[tool.pytest.ini_options]`. Root `workflow/pyproject.toml` needs **no** edit: workspace members are the glob `apps/*` (only `apps/web` is excluded), so `apps/api` is picked up automatically. |
| `workflow/apps/api/project.json` | NX project mirroring `apps/worker/project.json`, with a `semgrep` target running the same rules over `apps/api/src`. |
| `workflow/apps/api/src/api/__init__.py`, `.../entrypoints/` | ASGI app factory `create_app()` with one route, `GET /download` (see §2). Entrypoint module run by the deployed image / `mistral apps dev --filter api`. |
| `workflow/apps/api/tests/` | `conftest.py` reusing the SDK fixtures (`temporal_env`, etc., as in `apps/worker/tests/conftest.py`) plus a fake caller-credential provider installed via `install_caller_credentials()`; tests for 401 / 403 / 200 / headers / bytes / audit / no-secret-leak (AC2–AC6, AC11). |
| `workflow/deploy/docker/Dockerfile.api` | Mirror the build/runtime stages of `Dockerfile.worker`; final stage runs the api module entrypoint. No worker health-server env (that belongs to the SDK worker). |

### 1.3 Docs

| File | Change |
|---|---|
| `README.md` | New short section: the endpoint, the auth model, the user flow (browser URL, `curl -H "Authorization: Bearer …"`), the deployed module URL (`update-github-log-api`), and that the chat shows the link after each update / on request (R7). |

### 1.4 Out of this repo (manual, Studio side)

- Update the chat agent's **skill/prompt**: put the deployed `…/download` URL in it and instruct: on download request or after a successful `update-github-log`, emit the link/button **first** (from the configured URL, not by waiting for the workflow result), artifact as fallback. This is skill-level, not code — see SPEC.md §10.

## 2. Technical approach

**Route handler `GET /download`** (`apps/api`):

1. **Auth.** The platform installs per-request caller credentials; the handler calls `caller_credentials()` (`workflow/packages/py/utils/src/utils/mistral.py:41-66`). Provider absent / raises → **401**. Then the authorization check: caller must be allowed to execute `download-testlog` — implemented as an optional allowlist env `APP_DOWNLOAD_ALLOWLIST` (comma-separated caller ids); unset → any authenticated workspace caller passes. Not allowed → **403**. No credential is ever put in a URL or response (C2/C10).
2. **Fetch.** Start a `download-testlog` execution with the same client the platform docs use (`mistralai_capabilities.workflows.client.get_client()`, `deployment_name` from `env.workflows.env.deployment_name`, i.e. `update-github-log-worker`) and await `handle.result()`. This reuses `read_testlog`, so the GitHub read stays under `GITHUB_API_RATE_LIMIT` (C5) and there is a single read code path. Decode `base64_content` **once**, server-side, here.
3. **Respond.** `200` with body = raw xlsx bytes, `Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, `Content-Disposition: attachment; filename="<result.file_name>"` (the activity already derives the file name from `GITHUB_LOG_PATH`). **Audit-log** (structlog) caller + `blob_sha` + `size_bytes` + timestamp. Errors map to the SPEC's contract table (401/403/200/503/502).

**Worker outputs (R7):** `APP_DOWNLOAD_URL` is read inside `read_testlog` (activity) and returned as `download_url` on `SheetState` → both workflow outputs. Empty when unset → chat falls back to artifact-only.

**Determinism rules preserved:** no env reads or side effects in workflow bodies; the api handler is not workflow code, so reading env there is allowed, but the URL still travels through the activity so the outputs stay self-describing.

## 3. Order of work

1. Worker `download_url` plumbing + tests (no deploy needed; pure additive field).
2. `api` module: `mistral.json` entry, `apps/api` package + `project.json`, `Dockerfile.api`, tests.
3. `.env.example` + `README.md`.
4. Deploy (see §4). Note the bootstrap order: the concrete URL only exists after the `api` module is deployed once, so `APP_DOWNLOAD_URL` is bound in a **second** deploy pass (see §4).

## 4. Build, deploy, verify

- Tests: `cd workflow && bash tools/uv.sh run --all-packages pytest apps/api apps/worker` (AC7).
- Lint: semgrep per the NX `project.json` targets (`apps/worker` and `apps/api`).
- Local smoke: `mistral apps dev --filter api` (and `--filter worker`) from `workflow/`; `curl` the dev URL without/with a caller credential → expect 401 then 200.
- Deploy: push, then `mistral apps deploy` from `workflow/` (builds from the pushed commit; the worker deploys as `update-github-log-worker`, the api module as `update-github-log-api`). Bind `GITHUB_TOKEN` from the workspace secret `githubtoken` on the worker deployment via `PATCH /v1/workflows/deployments/update-github-log-worker` (pattern in `.agents/skills/capability-workflows/references/guides/managed-deployments.mdx`); the api module needs `GITHUB_LOG_*` only if it reads GitHub directly (it does not in the preferred path) — it needs `APP_DOWNLOAD_ALLOWLIST` if used.
- Read the `update-github-log-api` URL from the deploy output / Console, then bind `APP_DOWNLOAD_URL=<that url>/download` on the **worker** deployment and re-deploy the worker so `download_url` appears in workflow outputs.
- Manual checks: `curl` the endpoint without a token → 401; with an unauthorized caller → 403; with an authorized caller → 200, correct headers, bytes open as a valid workbook; audit log line present; Studio skill shows the link after an update.
- Update the chat skill prompt with the URL (out of repo).

## 5. Rollout and rollback

- Fully additive: existing `update-github-log` / `download-testlog` behavior unchanged (new optional output field only); the chat artifact path remains as fallback.
- Rollback: revert this branch's commits, or remove the `api` module from `mistral.json` and redeploy; the worker change is backward compatible on its own.
- Size guard (C6): contents API inlines base64 ≤ 1 MB and workflow payloads cap at 2 MB; current file ~30 KB. If the workbook grows past this, switch the read to `OffloadableField` blob storage or stream `download_url` (SPEC.md C6).

## 6. Mapping to SPEC acceptance criteria

AC1 → §1.2 `mistral.json`; AC2/AC3/AC4/AC5/AC6 → §2 handler + api tests; AC7 → §4 test/lint commands; AC8/AC12 → §1.3 README + §1.4 skill; AC9 → C1 (no exposure mechanisms anywhere in the diff); AC10 → worker tests unchanged apart from the new `download_url` cases; AC11 → §1.1.
