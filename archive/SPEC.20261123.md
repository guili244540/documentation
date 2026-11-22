# SPEC — Getting `Response Repository Matrix.xlsx` out of the worker

- **Status:** revised and implemented 2026-11-21 (twice) — exactly two approaches are supported (see §3): **inline** (works today, unchanged) and the **Mistral Files API** (`file_id`, `signed_url`, and the 5-minute retention purge all implemented on this branch). An ingress-served `api` module was implemented on this branch earlier and has been removed again after live findings (§11).
- **Branch:** `directDownload`
- **Scope:** let an authenticated user download the current test-log workbook without going through a chat agent, and without anyone needing GitHub access of their own — while the existing chat artifact path keeps working unchanged.

## 1. Requirements

- **R1.** A user can download `Response Repository Matrix.xlsx` directly, without the chat intermediary.
- **R2.** Every download path is authenticated: the caller presents their own workspace token and gets nothing without it, and `file_id` alone is not a capability. The single exception is a **short-lived signed URL** minted by the worker (§3): link-based access is accepted only because its effective lifetime is bounded to ≤ 10 min by the retention purge (R8) — no long-lived links, and no token is ever embedded in a URL.
- **R3.** The GitHub credential (`githubtoken` secret, bound as `GITHUB_TOKEN`) stays server-side: it is used only inside the deployment and never appears in a response, a log line, or client-visible output. The repository is private.
- **R4.** Expected load is small: at most 2–3 concurrent users, at most 10 file updates per user per day (≈90 GitHub calls/day). Neither the platform rate limit nor GitHub's quota may become a bottleneck.
- **R5.** Existing behaviour must keep working unchanged: `update-github-log` and the chat-based `download-testlog` inline-artifact path.
- **R6.** The worker stays a pure workflow worker: no HTTP listener inside the worker process, no new app module.
- **R7.** The chat surfaces the download references too: after each successful `update-github-log` run, and whenever the user asks for the workbook, the chat reply shows the clickable **signed URL** (when present) and the Files API reference (`file_id`) from the result, while still publishing the inline artifact as the fallback.
- **R8.** Retention: the worker tracks every published `file_id` with its creation time in a **process-local** registry and purges the Mistral Files API objects older than 5 minutes via a cron job running every 5 minutes, so a published file lives **at most ~10 minutes** (guaranteed ≥ 5 min; deleted at the next cron tick). Only files the worker itself uploaded — ids in its own registry — are ever deleted; never other files in the workspace (C11). Accepted trade-off: the registry dies with the worker process, so after a restart the previous upload is orphaned until manual deletion — no state file is kept in the GitHub repo for this.

## 2. Current state

- The app (`workflow/mistral.json`) declares a single module, `worker`, with `"routes": []` and `"port": null`; its entrypoint only runs `workflows_client.run_worker(...)` (`workflow/apps/worker/src/worker/entrypoints/worker.py:47`). Nothing is reachable over HTTP, and that stays.
- The only download path today is `download-testlog` (`workflow/apps/worker/src/worker/workflows/download_testlog.py`): a chat agent starts the execution, the workflow returns the workbook as `base64_content` in its result, and the chat publishes it as a downloadable artifact server-side. This is **approach 1 (inline)** and stays exactly as it is.
- GitHub access happens only inside activities, under the shared rate limit `GITHUB_API_RATE_LIMIT` = 30 executions/second, key `github-api`, shared by the `github-read-testlog`, `github-commit-testlog`, and `github-verify-testlog` activities in `workflow/apps/worker/src/worker/workflows/update_github_log.py`.
- Writes are guarded by a blob-sha optimistic lock: a conflicting PUT gets 409 → non-retryable `github_write_conflict_file_changed_since_read`.
- A managed worker deployment runs in Mistral's cloud with no shell, exec, or shared filesystem exposed to the caller (§11): a file produced in a workflow run is reachable only if the worker returns it inline or pushes it out itself.

## 3. The two supported approaches

1. **Inline (approach 1 — works today, unchanged).** `download-testlog` (and `update-github-log`) return the workbook as `base64_content` in the workflow result; the chat decodes it and publishes it as a downloadable artifact. Base64 remains only where it is inherent: GitHub's contents API returns base64 inline (≤ 1 MB), and the result rides through the 2 MB workflow payload cap.
2. **Mistral Files API (approach 2 — the direct path for R1).** A new `publish-testlog-file` activity uploads the current workbook to the Files API and the workflows return the `file_id` **and a `signed_url`** minted at upload time. Anyone with a workspace token then downloads the file with one authenticated call — no chat, no GitHub access:
   - SDK: `client.files.download(file_id)` (hits `GET /v1/files/{file_id}/content`, requires the caller's bearer token);
   - or raw HTTP with the caller's token in the `Authorization` header, saving the body as `Response Repository Matrix.xlsx`;
   - or, in chat, by clicking the `signed_url`: a pre-signed link whose signature expires (shortest TTL the API accepts; SDK range 1–168 h → 1 h), while the underlying file is purged after ≤ 10 min (R8) — so the *effective* link lifetime is ≤ 10 min and a purged file answers 404 even with a still-valid signature.

### What changed (approach 2, implemented on this branch)

1. **`workflow/apps/worker/src/worker/workflows/update_github_log.py`** — new activity `publish-testlog-file` (details in `IMPLEMENTATION_PLAN.md` §3): uploads the workbook bytes via `client.files.upload_async` with `purpose="ocr"` and `visibility="workspace"` — the exact shape proven live with curl on 2026-11-21 — authenticated by `FILES_API_KEY`, because the worker's service-account token is scoped to the Workflows API and `/v1/files` refuses it with 401/403. Caches by blob sha so an unchanged file is not re-uploaded, and deletes the superseded upload when the blob changes. Runs inside the activity only (determinism rule: no env reads or side effects in workflow bodies); it is a Mistral API call, so it stays out of `GITHUB_API_RATE_LIMIT`.
2. **`SheetState` gains `file_id: str = ""`**, filled by `read_testlog`; both `DownloadTestLogOutput` and `UpdateGitHubLogOutput` gain `file_id: str = ""` (empty when the upload fails → callers fall back to the inline artifact). No new env vars, no `mistral.json` change, no new module.
3. **Tests** in `workflow/apps/worker/tests/`, same style as the existing ones (mocked HTTP through a client seam): `file_id` present and token-free in both workflows' outputs when the upload succeeds; empty on upload failure; no re-upload when the blob sha is unchanged; no token material in outputs or logs.
4. **`workflow/.env.example`** — declare `FILES_API_KEY=` (name only); at deploy time bind it from a workspace secret holding an API key with Files access (`PATCH /v1/workflows/deployments/update-github-log-worker`, same pattern as `GITHUB_TOKEN`).
5. **README** (`README.md`): document both approaches — the inline artifact (today) and the Files API reference (how to download with your own token), plus the `FILES_API_KEY` deploy binding.
6. **Chat agent instructions (R7, Studio side, out of this repo):** after a successful `update-github-log` and on a download request, show the `signed_url` as a clickable link and mention the `file_id` from the result; the artifact stays as fallback.
7. **Implemented in this revision:** `publish-testlog-file` additionally mints a signed URL (`files.get_signed_url_async`, shortest accepted TTL = 1 h) returned as `signed_url` next to `file_id` on both outputs, and the cron-driven `purge-testlog-files` workflow deletes the worker-registered Files API objects older than 5 min (R8) — never other files — so a published file lives at most ~10 min; the `*/5 * * * *` schedule is created through the Workflows schedules API at deploy time (the worker itself registers no schedules; `@workflow.define(schedules=...)` is deprecated). Details in `IMPLEMENTATION_PLAN.md` §3/§4.

## 4. Why

- **UX:** today the only way to get the file is to open a chat, run `download-testlog`, and wait for an artifact. With approach 2, a signed-in user downloads the current workbook with a single authenticated API call.
- **Security:** the repo is private and the only credential is the worker's service token. The Files API enforces the same boundary as executing `download-testlog`: a valid workspace token is required to download by `file_id`, and `file_id` alone grants nothing. The signed URL is the one link-based exception, bounded twice — its own signature TTL (shortest the API accepts) and the retention purge (R8) — so the effective link lifetime is ≤ 10 min and a leaked link dies with the file. No token is ever embedded in a URL. The GitHub token never leaves the deployment.
- **No new exposure:** approach 2 adds no endpoint, no listener, no tunnel — nothing new is reachable from outside (C1). The earlier `api`-module idea failed exactly here in practice (§11). The signed URL points at the existing Files API, not at anything the worker serves.
- **Load:** ≈90 uploads/day worst case is nothing for the Files API, and the blob-sha cache cuts it to one upload per actual change; the retention purge (R8) bounds how long each upload lingers (≤ 10 min). GitHub reads stay under the existing 30/s activity rate limit; GitHub's 5,000 req/h quota is irrelevant at this volume (R4).
- **Size headroom:** the Files API accepts files up to 512 MB, far beyond the ~30 KB workbook, so growth past the 1 MB inline-base64 / 2 MB payload limits degrades gracefully to approach 2 instead of breaking approach 1 (C6).

## 5. Expected behaviour

### User point of view

- Approach 1 (unchanged): in chat, ask for the workbook → `download-testlog` runs → the chat publishes the artifact.
- Approach 2: take the `file_id` from a `download-testlog` / `update-github-log` result and download with your own token: `client.files.download(file_id)`, or `GET /v1/files/<file_id>/content` with `Authorization: Bearer <your workspace token>`, saving the body as the xlsx. No chat, no GitHub access needed.
- In chat, the `signed_url` from the result is a clickable link to the same bytes, valid until its signature expires and, in any case, only while the file is still published (≤ 10 min, R8 — afterwards the link answers 404).
- No token → `401`. Unknown/expired/purged `file_id` → `404`. The bytes are the exact blob the worker read (blob sha matches the GitHub blob).

### API contract (approach 2, all on the existing Mistral Files API — nothing new is served)

| Case | Status | Body |
|---|---|---|
| No/invalid caller token | 401 | Files API error JSON |
| Valid token, known `file_id` | 200 | xlsx bytes |
| Valid token, unknown/expired/purged `file_id` | 404 | Files API error JSON |
| Valid signed URL, file still published | 200 | xlsx bytes |
| Valid signed URL, signature expired or file purged (R8) | 404 | Files API error JSON |
| File above the 512 MB per-file cap | 413 at upload time | — (upload fails; `file_id`/`signed_url` empty; inline path unaffected) |

### Concurrency

Downloads are read-only and idempotent. Concurrent *updates* remain guarded by the blob-sha optimistic lock: the loser of a race gets the existing non-retryable `github_write_conflict_file_changed_since_read` error and simply re-runs; with 2–3 users this stays rare. Uploads are keyed by blob sha, so a re-read of an unchanged file does not create a new Files API object, and every upload is purged after at most ~10 min (R8).

## 6. Constraints

- **C1 — No new exposure.** No public ingress, tunnels, relays, reverse proxies, or broad listeners; no HTTP server module is added; the worker keeps `routes: []` / `port: null`. The only link-based access is the short-lived signed URL of §3/R2, bounded to ≤ 10 min by R8; no token-bearing URLs ever.
- **C2 — Secret hygiene.** `GITHUB_TOKEN` is read only inside activities, `GITHUB_API_BASE_URL` must stay https, and no secret may appear in outputs, logs, or errors. `FILES_API_KEY` is likewise read only inside the `publish-testlog-file` activity and only ever placed in the client's credential; the worker's SA token is never used for the Files API.
- **C3 — Worker untouched structurally.** No HTTP listener in the worker process (R6); no new app module in `mistral.json`.
- **C4 — Determinism.** Side effects and environment reads stay inside activities; repo/path/branch/sheet stay `GITHUB_LOG_*` configuration, not workflow input; columns keep being matched by header name.
- **C5 — Rate limiting.** GitHub reads go through activities under the shared `GITHUB_API_RATE_LIMIT`. Files API calls are Mistral API calls: keep them out of that limiter, and rely on the blob-sha cache to keep volume at ~1 upload per file change.
- **C6 — Payload size.** Inline base64 is bounded by GitHub's 1 MB inline limit and the 2 MB workflow payload cap (current file ~30 KB). The Files API cap is 512 MB per file, so approach 2 has ample headroom; if the workbook ever outgrows the inline limits, approach 1 degrades (clear error) while approach 2 keeps working.
- **C7 — Dependencies.** No new dependencies: the `mistralai` SDK (already a worker dependency) provides `files.upload/download/list/delete`.
- **C8 — Backward compatibility.** `update-github-log` and `download-testlog` behave exactly as before; the inline artifact path remains available (R5).
- **C9 — Style.** Match `apps/worker`: pydantic models, the `WorkflowError` taxonomy, tests against mocked HTTP, semgrep lint per `apps/*/project.json`.
- **C10 — The reference is not a capability.** `file_id` carries no token or secret; access stays gated by the caller's token at the Files API, so a leaked `file_id` alone yields 401. Signed URLs (`files.get_signed_url`, SDK TTL range 1–168 h) are used **only** as a short-lived convenience link minted with the shortest accepted TTL (1 h) at publish time; the 5-min retention purge (C11/R8) bounds the effective access window to ≤ 10 min, after which the link answers 404 even if the signature is still valid. No token is ever placed in a URL.
- **C11 — Retention purge, worker-owned files only.** The worker records every `file_id` it uploads, with its creation time, in a **process-local** registry (in-memory `dict`, like the existing blob-sha cache). A cron-scheduled workflow runs every 5 minutes (`*/5 * * * *`) on the same deployment and deletes via `client.files.delete_async` exactly the registered entries older than 5 minutes — best-effort, failures logged (error class + HTTP status, never the key). **Only registered ids are ever deleted — `files.list` is never used to select deletions**, because a name filter cannot prove ownership (a foreign file with the same name would match). A published file therefore lives at most ~10 min. Accepted limitation (owner decision 2026-11-21): the registry is lost on worker restart, so the previous upload is then orphaned until manual deletion; no registry file is kept in the GitHub repo. Purge calls are Mistral API calls and stay outside `GITHUB_API_RATE_LIMIT`; no GitHub calls are added.

## 7. Acceptance criteria

- **AC1.** Both `download-testlog` and `update-github-log` outputs carry `file_id` (empty string when the upload fails), in addition to the unchanged inline fields.
- **AC2.** With a valid workspace token, `GET /v1/files/<file_id>/content` (or `client.files.download`) returns bytes identical to the blob the worker read (blob sha matches).
- **AC3.** Without a valid token, the download is `401`; a leaked `file_id` alone grants nothing.
- **AC4.** No `GITHUB_TOKEN`/SA-token material in any output, error, or log line (tested).
- **AC5.** An unchanged blob sha does not trigger a new upload (cache); a changed blob uploads once and the superseded file is deleted (or expired).
- **AC6.** Tests pass: `cd workflow && bash tools/uv.sh run --all-packages pytest apps/worker`; semgrep lint passes.
- **AC7.** `README.md` documents both approaches and the caller-side download command.
- **AC8.** Existing worker tests are unchanged apart from the new `file_id` cases and stay green (C8).
- **AC9.** `mistral.json` still declares only the `worker` module; no HTTP listener exists anywhere in the diff (C1/C3).
- **AC10.** The chat skill (Studio side) shows the `signed_url` as a clickable link and mentions the `file_id` after updates and on request; the artifact remains the fallback (R7).
- **AC11.** Both outputs also carry `signed_url` (empty when minting fails); it is minted with the shortest TTL the API accepts, and the underlying file is guaranteed present for ≥ 5 min (R8), so the effective link lifetime is ≤ 10 min.
- **AC12.** The cron purge deletes only registered (worker-uploaded) Files API objects older than 5 min: a file published at time *t* is gone by the first cron tick after *t* + 5 min (≤ ~10 min; tested with a fake clock, verified live after deploy); a foreign file sharing the workbook's name is never deleted (tested); with an empty registry (post-restart) the purge deletes nothing — the orphan is left for manual deletion (accepted).

## 8. Out of scope

- An HTTP `api` module / `GET /download` endpoint — tried and dropped (§9, §11).
- Long-lived signed URLs (> the shortest accepted TTL) as the access mechanism; token-bearing URLs of any kind; external object storage (S3/GCS/Azure) with bound credentials.
- Any change to the inline base64 path, the workbook structure, or the update workflows.
- A direct **upload/update** endpoint — updates stay on `update-github-log` via the chat.
- Using GitHub access as the distribution mechanism (possible for users with repo access; the repo is private and this does not satisfy R1).

## 9. Alternatives considered

- **Ingress-served `api` module (`GET /download` behind the platform ingress)** — implemented on this branch (`d31798a`…`b10547f`), then removed after live findings (§11): the module is not a workflows deployment, its URL is not discoverable by the worker (`/v1/workflows/deployments` lists only `update-github-log-worker`), and a managed worker container exposes nothing to download from directly. Dropped; the code removal is part of this revision.
- **Signed URLs (`files.get_signed_url`)** — link-based access for the signature TTL (SDK range 1–168 h, default 24 h). Accepted **only** as a short-lived convenience link: minted with the shortest accepted TTL (1 h) and, decisively, the file itself is purged after ≤ 10 min (R8/C11), so the effective exposure is ≤ 10 min and revocation is just "delete the file". Rejected as the *primary* access mechanism — `file_id` + caller token remains the durable path; long-lived signed URLs stay out of scope (§8).
- **External object storage** — needs a bound S3/GCS/Azure credential, bucket retention/CORS ops, and still ends in a pre-signed URL for browser access; no security gain over the Files API. Rejected.
- **Status quo (inline only)** — works today, but requires a chat session; rejected as the only path per R1. Kept as approach 1 / fallback.

## 10. Decisions taken (flag if you disagree)

- Exactly two approaches: **inline** (unchanged) and **Mistral Files API** (`file_id` + authenticated download). Owner decision 2026-11-21.
- The `api`-module code from phase 1 is removed in this revision; `SPEC.md` lives at the repository root.
- Direct download = the worker pushes the file out (Files API) and returns the reference; there is no download path into a managed-deployment container.
- `file_id` is returned on both workflow outputs, empty on failure; callers authenticate at the Files API with their own token.
- Signed URLs are allowed **only** as a short-lived convenience link returned next to `file_id` (owner decision 2026-11-21, revising the earlier C10 rejection): minted in `publish-testlog-file` via `files.get_signed_url` with the shortest TTL the API accepts (SDK range 1–168 h → 1 h), exposed as `signed_url` on both outputs and shown as a clickable link in chat (R7). Effective access is bounded to ≤ 10 min by the retention purge below, so a leaked link dies with the file; the durable access path remains `file_id` + the caller's own token.
- Retention: the worker keeps a **process-local** registry of every published `file_id` with its creation time, and a cron workflow (`purge-testlog-files`, `*/5 * * * *`, same deployment) deletes registered files older than 5 min every 5 min → max file lifetime ~10 min. Deletion is restricted to registered ids — `files.list` is never used to pick deletions, since a name match cannot prove the worker uploaded the file. Owner decision 2026-11-21: **no registry file in the GitHub repo** — after a worker restart the previous upload is orphaned until manual deletion (accepted; few files, easily deleted by hand). The registry also reconciles the blob-sha cache, and every cache hit is guarded by a `files.retrieve` existence check (404 → evict + re-upload; the signed URL is re-minted on each hit), so a cached `file_id` whose file was deleted — by the purge or manually — is re-uploaded instead of returned stale. No `expiry` on upload — its accepted range is unverified live; the purge is the enforcement mechanism while the process lives.
- No object storage, no new module. One new env var: `FILES_API_KEY` (a workspace API key bound as a deployment secret), because the worker's SA token cannot call `/v1/files` (verified live 2026-11-21: upload/download with a user key work; the SA token gets 401/403 there). Upload params match the proven curl exactly: `purpose="ocr"`, `visibility="workspace"`, no `expiry`. Failure logging carries the error class and HTTP status, never the key.

## 11. Findings from the deployed worker (2026-11-21)

Live observation (worker `update-github-log-worker` at `b10547f`), returned as `download_url_detail` in a `download-testlog` result:

```
unresolved: GET /v1/workflows/deployments/app-workspace-api -> HTTP 404; GET /v1/workflows/deployments -> 1 deployment(s) [update-github-log-worker]; none named *-api with an https endpoint
```

Conclusions:

- The worker's service-account token can read the deployments API and `WORKFLOWS_BASE_URL` is https, so API access from the worker works; the failure was purely "no such entry".
- `APP_NAME` is not bound on the worker deployment, so the name probe used the package default `app-workspace`.
- `/v1/workflows/deployments` lists only managed **worker** deployments (`mistral apps deploy` creates exactly one, `<app>-worker`). The `api` module (`serve: "server"`) is deployed as an app/space module (`/v1/spaces/{id}/modules/api/deploy`) and never appears in that API — there is no list-spaces endpoint, and the worker does not know its space id. Auto-discovery of an api module URL from the deployments API therefore cannot work.
- A managed worker container exposes no shell, exec, or filesystem, and shares no volume with the caller: a file produced inside a workflow run is reachable only if the worker returns it inline or pushes it out itself. Hence approach 2 (Files API) for R1.
