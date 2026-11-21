# IMPLEMENTATION PLAN — Getting `Response Repository Matrix.xlsx` out of the worker

- **Implements:** `SPEC.md` (same branch, `directDownload`)
- **Branch:** `directDownload`
- **Status:** the **Files API approach** (§3) is **implemented on this branch**. The two supported approaches are inline (works today, untouched) and the Files API (`SPEC.md` §3). The phase-1 `api` module (`d31798a`…`b10547f`) was **removed** after the live findings in `SPEC.md` §11.
- **Approach in one line:** a new `publish-testlog-file` activity uploads the workbook to the Mistral Files API with the worker's service-account token and returns the `file_id` on both workflow outputs; callers download with one authenticated call (`client.files.download(file_id)` / `GET /v1/files/<file_id>/content` with their own bearer token). The inline `base64_content` artifact path stays exactly as it is.

## 1. Current state

- `workflow/mistral.json` declares only the `worker` module (`routes: []`, `port: null`); nothing is reachable over HTTP and that stays (R6/C1/C3).
- `download-testlog` returns the workbook inline as `base64_content` (+ file name, blob sha, size, test IDs) through the `read_testlog` activity; the chat publishes it as an artifact. This is approach 1 and is not modified.
- GitHub reads/writes happen in activities under `GITHUB_API_RATE_LIMIT` (30/s, key `github-api`); writes use the blob-sha optimistic lock (409 → `github_write_conflict_file_changed_since_read`).
- Live finding (`SPEC.md` §11): the managed worker container exposes no shell/exec/filesystem and `/v1/workflows/deployments` contains only `update-github-log-worker` — so there is no download path into the container and no discoverable api-module URL. Files are only reachable if the worker returns them inline or pushes them out.

## 2. What was removed in this revision (phase 1, reverted)

`git rm` of `workflow/apps/api/`, `workflow/deploy/docker/Dockerfile.api`, `workflow/apps/worker/tests/test_resolve_download_url.py`; the `api` module entry removed from `workflow/mistral.json`; the worker files (`update_github_log.py`, `download_testlog.py`, both workflow test files) reverted to `origin/main` (the `resolve-download-url` activity, `download_url`/`download_url_detail` fields and tests were the only branch changes there); `APP_DOWNLOAD_URL`/`APP_DOWNLOAD_ALLOWLIST` removed from `workflow/.env.example`; the `GET /download` section removed from `README.md`. The root `pyproject.toml` keeps the shared pytest asyncio options (generic test infra, not direct-download code).

## 3. File-by-file changes (Files API approach — implemented)

| File | Change |
|---|---|
| `workflow/apps/worker/src/worker/workflows/update_github_log.py` | New `publish-testlog-file` activity next to the GitHub activities: builds the Files API client from `FILES_API_KEY` when bound (`utils.mistral.mistral_client_with_api_key`, new helper), falling back to the app credential — the worker SA token is scoped to the Workflows API and `/v1/files` refuses it (401/403, verified live 2026-11-21). Uploads via `client.files.upload_async(file={"file_name", "content", "content_type"}, purpose="ocr", visibility="workspace")` — exactly the curl-proven shape, no `expiry` — caches `blob_sha -> file_id` in-process so an unchanged file is not re-uploaded, deletes the superseded upload (`client.files.delete_async`) when the blob changes, and logs the error class + HTTP status on failure (never the key). Returns the `file_id` (or `""` on any failure — never raises into the workflow). Mistral API call, so **not** under `GITHUB_API_RATE_LIMIT`. |
| `workflow/packages/py/utils/src/utils/mistral.py` | New `mistral_client_with_api_key(api_key)` helper: a client spending an explicit workspace key against `MISTRAL_BASE_URL`, for calls the app's own credential cannot make. Covered in `packages/py/utils/tests/test_mistral.py`. |
| `workflow/.env.example` | Declare `FILES_API_KEY=` (name only). |
| same file | `SheetState` gains `file_id: str = ""`; `read_testlog` fills it by calling the activity (activity-in-activity runs inline); `UpdateGitHubLogOutput` gains `file_id: str = ""`, set on both the dry-run and the committed return paths. |
| `workflow/apps/worker/src/worker/workflows/download_testlog.py` | `DownloadTestLogOutput` gains `file_id: str = ""`, set from `state.file_id`. |
| `workflow/apps/worker/tests/test_update_github_log.py`, `test_download_testlog.py` | New cases with a mocked Files API through a client seam: `file_id` present and token-free in both outputs on success; empty when the upload fails; no second upload when the blob sha is unchanged; no token material in outputs. Existing tests unchanged. |
| `README.md` | The "Getting the workbook out" section already documents both approaches; adjust the exact output field names/commands once implemented. |

No changes to `mistral.json`, no new module, no new env vars, no Dockerfile changes.

## 4. Technical approach

- **Upload inside an activity** (determinism rule C4): `publish-testlog-file` runs in the worker process with the SA token; the workflow only passes the bytes/blob sha through `SheetState`. No env reads in workflow bodies.
- **Client:** `FILES_API_KEY` when bound (workspace key, read inside the activity; new `mistral_client_with_api_key` helper in `utils.mistral`), else the app credential. Both target `env.mistral_base_url` / the public API — the Files API lives on the main Mistral API host, not `WORKFLOWS_BASE_URL`. Tests monkeypatch the `_files_client` seam, so no network.
- **Dedup/cleanup:** in-process `dict[blob_sha, file_id]`; on a new blob sha, upload then delete the previous file id (best-effort, logged on failure) and always set an `expiry` on upload so orphaned files self-delete.
- **Failure semantics:** any upload error → `file_id = ""` and a warning log line (never the token); the inline artifact path keeps working (R5/C8).
- **Security:** `file_id` is not a capability — download requires the caller's own token at `/v1/files/{id}/content` (C10/R2). No signed URLs. `GITHUB_TOKEN` untouched (C2).

## 5. Order of work (done on this branch)

1. `publish-testlog-file` activity + `file_id` on `SheetState`/both outputs + tests — done (direct activity tests plus workflow-level cases; `verify_testlog` also publishes the post-commit blob so an update's `file_id` is current).
2. README field-name/command check — done.
3. Deploy (`mistral apps deploy` from `workflow/`) and bind `FILES_API_KEY` on `update-github-log-worker` from a workspace secret (`PATCH /v1/workflows/deployments/update-github-log-worker`, same pattern as the `GITHUB_TOKEN` binding in the managed-deployments guide). Then verify end-to-end: run `download-testlog`, take `file_id`, download with a workspace token, compare sha; confirm 401 without a token; check the worker log shows `publish_testlog_file: uploaded` (and `error=… status=…` on failure) — remaining manual step.

## 6. Build, deploy, verify

- Tests: `cd workflow && bash tools/uv.sh run --all-packages pytest apps/worker` (AC6). Note: in this sandbox the Temporal-based workflow tests hang due to pre-existing SDK drift (no committed `uv.lock`; `mistralai-workflows` 3.20.0 routes rate-limited activities to a dedicated queue `create_test_worker` does not serve — affects `origin/main` identically). Verify new workflow-level assertions with a harness that also serves that queue, as done for phase 1.
- Lint: semgrep per the NX `project.json` targets (`apps/worker`).
- Manual: as in §5.3; plus `client.files.list` to confirm cleanup of superseded uploads.

## 7. Rollout and rollback

- Fully additive: new optional `file_id` output field only; inline behavior unchanged (C8).
- Rollback: revert the branch commit(s), or delete the uploaded files; the worker change is backward compatible on its own.
- Size guard (C6): inline base64 ≤ 1 MB (GitHub) / 2 MB (payload); Files API cap 512 MB. Current file ~30 KB.

## 8. Mapping to SPEC acceptance criteria

AC1 → §3 `file_id` on both outputs; AC2/AC3/AC4/AC5 → §3/§4 activity + tests; AC6 → §6 commands; AC7 → §3 README; AC8 → existing worker tests unchanged apart from the new `file_id` cases; AC9 → no `mistral.json`/listener changes in the diff (§2); AC10 → Studio-side skill (out of repo).
