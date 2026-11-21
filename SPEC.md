# SPEC — Direct download of `Response Repository Matrix.xlsx`

- **Status:** revised 2026-11-21 — see §11 (deployed-worker findings and revised direction). Phase 1 (the ingress-served `api` module + `download_url`/`download_url_detail` outputs) is implemented on this branch; the prioritized mechanism for R1 is now the Files API (§11).
- **Branch:** `directDownload`
- **Scope:** let an authenticated user download the current test-log workbook directly from the deployed app (`GET /download`), without going through a chat agent — and let the chat surface that link after each update and on request (R7).

## 1. Requirements

- **R1.** A user can download `Response Repository Matrix.xlsx` directly, without the chat intermediary.
- **R2.** The download path is an **authenticated API module** behind the platform's ingress (the "proposal 2" option). Unauthenticated or unauthorized callers get no file.
- **R3.** The GitHub credential (`githubtoken` secret, bound as `GITHUB_TOKEN`) stays server-side: it is used only inside the deployment and never appears in a response, a log line, or client-visible output. The repository is private.
- **R4.** Expected load is small: at most 2–3 concurrent users, at most 10 file updates per user per day (≈90 GitHub calls/day). Neither the platform rate limit nor GitHub's quota may become a bottleneck.
- **R5.** Existing behaviour must keep working unchanged: `update-github-log` and the chat-based `download-testlog` artifact path.
- **R6.** The worker stays a pure workflow worker: no HTTP listener inside the worker process.
- **R7.** The chat also surfaces the direct-download link: after each successful `update-github-log` run, and whenever the user asks for the workbook, the chat reply includes the `<app-url>/download` link (a button where the UI supports it), while still publishing the artifact as a fallback.

## 2. Current state

- The app (`workflow/mistral.json`) declares a single module, `worker`, with `"routes": []` and `"port": null`; its entrypoint only runs `workflows_client.run_worker(...)` (`workflow/apps/worker/src/worker/entrypoints/worker.py:47`). Nothing is reachable over HTTP today.
- The only download path is `download-testlog` (`workflow/apps/worker/src/worker/workflows/download_testlog.py`): a chat agent starts the execution, the workflow returns the workbook as `base64_content` in its result, and the chat publishes it as a downloadable artifact server-side.
- GitHub access happens only inside activities, under the shared rate limit `GITHUB_API_RATE_LIMIT` = 30 executions/second, key `github-api`, shared by `github-read-testlog`, `github-commit-testlog`, `github-verify-testlog` (`workflow/apps/worker/src/worker/workflows/update_github_log.py:62-65`, applied at lines 415, 481, 584).
- Writes are guarded by a blob-sha optimistic lock: a conflicting PUT gets 409 → non-retryable `github_write_conflict_file_changed_since_read`.

## 3. What needs to change

1. **New API module in `workflow/mistral.json`**, next to `worker`:
   - name `api`, path `apps/api`, `serve: api`, a `port`, and `routes` exposing `GET /download`;
   - build via a new `deploy/docker/Dockerfile.api`;
   - same environment as the worker (`GITHUB_LOG_*` from `workflow/.env.example`, `GITHUB_TOKEN` bound from the `githubtoken` secret).
   - The `worker` module is **not** modified (`routes: []`, `port: null` stay).
2. **New app `workflow/apps/api`**: a small ASGI service (uvicorn is the existing house pattern — see `workflow/packages/py/utils/src/utils/logging.py:44-52`) with one route, `GET /download`, which:
   1. **Authenticates the caller** using the per-request caller credentials the platform installs (`caller_credentials()` / `install_caller_credentials()` in `workflow/packages/py/utils/src/utils/mistral.py:41-66`). No caller credential → `401`. Caller not authorized → `403`. Authorization = the caller is allowed to execute `download-testlog` on this deployment (same notion of "who may fetch the log" the chat path uses); optionally narrowed by an allowlist.
   2. **Fetches the workbook** by starting a `download-testlog` workflow execution and awaiting its result — this reuses the exact read path (`read_testlog` activity), keeps the GitHub read under `GITHUB_API_RATE_LIMIT`, and keeps one code path that owns "how the workbook is read". (Fallback if executions prove awkward: call the GitHub contents API directly with `GITHUB_TOKEN`, like `read_testlog` does, plus an in-handler throttle and a blob-sha-keyed cache.)
   3. **Responds as a file download**: `200`, `Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, `Content-Disposition: attachment; filename="<basename of GITHUB_LOG_PATH>"`, body = the decoded bytes. Writes an audit log line: caller, blob sha, size, timestamp.
3. **Expose the download URL in the worker outputs.** New env var `APP_DOWNLOAD_URL` (declared in `.env.example`, bound at deploy time to the deployed API module's URL — the `update-github-log-api` deployment URL printed by `mistral apps deploy` and shown in the Console). It is read **inside the `read_testlog` activity**, like the file name today (determinism rule: no env reads in workflow bodies), added as `download_url` to `SheetState`, and surfaced on both `DownloadTestLogOutput` and `UpdateGitHubLogOutput`. Empty when unset → the chat keeps today's artifact-only behavior.
4. **Chat agent instructions / UI (R7).** After a successful `update-github-log`, the chat reply includes the `download_url` link; when the user asks for the workbook, the agent runs `download-testlog` and replies with the same link. Where the conversational UI supports it, render a `ButtonLink(href=download_url)` (`workflow/.agents/skills/capability-workflows/references/guides/assist-workflows-output.mdx`, component table) instead of a bare URL. Artifact publication stays as the fallback.
5. **Tests** in `workflow/apps/api/tests/`, same style as `workflow/apps/worker/tests/`: mocked GitHub API / mocked workflow execution plus a fake caller-credential provider. Cases: 401 without credentials, 403 for an unauthorized caller, 200 with correct headers/bytes/audit for an authorized caller, and a test asserting no token material appears in any response or log. Worker tests extended for `download_url`: present and token-free when `APP_DOWNLOAD_URL` is set, empty when unset, in both `download-testlog` and `update-github-log` outputs.
6. **README** (`README.md`): a short section documenting the endpoint, the auth model, the user flow (browser URL and `curl`), the deployed module URL (`update-github-log-api`, from the Console / deploy output), and that the chat shows the link after each update and on request.
7. **`.env.example`**: document `APP_DOWNLOAD_URL` alongside the existing `GITHUB_LOG_*` and `GITHUB_TOKEN`.

## 4. Why

- **UX:** today the only way to get the file is to open a chat, run `download-testlog`, and wait for the chat to publish an artifact. A direct, authenticated download is one request.
- **Security:** the repo is private and the only credential is the worker's service token. Any direct endpoint must therefore authenticate callers itself; an open or link-based endpoint would hand the private workbook to anyone. The platform's caller credentials provide per-request identity without inventing a new shared secret, and the token never leaves the deployment.
- **Consistency:** "who can start a `download-testlog` execution" is already the authorization boundary of the chat path; the API module applies the same check at the ingress.
- **Load:** at ≤ ~90 GitHub calls/day the existing 30/s activity rate limit and GitHub's 5,000 req/h quota are irrelevant; the rate limit stays as protection against bursts, not as a bottleneck (see R4).
- **Discoverability (R7):** users learn the `<app-url>/download` link from the chat itself — after each update or when they ask — and from the README, instead of having to track down the deployment URL.
- **No base64 on the user-facing hop:** `GET /download` returns the raw workbook bytes as the HTTP response body, so the user never has to decode base64 out of a JSON workflow result (unlike the chat-artifact path today). Base64 remains only where it is inherent: GitHub's contents API returns base64 inline (≤ 1 MB), and when the handler fetches via a `download-testlog` execution the bytes still ride as `base64_content` inside the workflow result — decoded once server-side before streaming.

## 5. Expected behaviour

### User point of view

- A signed-in, authorized user opens `<app-url>/download` in a browser, or runs
  `curl -H "Authorization: Bearer <their workspace token>" <app-url>/download -o "Response Repository Matrix.xlsx"`.
- The browser/curl receives the current workbook as a file download — no chat, no GitHub access needed by the user.
- Not signed in → `401`, no body. Signed in but not authorized → `403`, no body.
- Every successful download is audit-logged (caller, blob sha, size, timestamp).
- In chat (R7): after each successful `update-github-log`, the reply shows a clickable link to `<app-url>/download`; when the user asks for the workbook, the chat replies with the same link and still offers the artifact.

### API contract

| Case | Status | Body |
|---|---|---|
| No caller credential | 401 | empty |
| Caller not authorized | 403 | empty |
| Authorized | 200 | xlsx bytes; `Content-Type` = xlsx MIME; `Content-Disposition: attachment; filename="Response Repository Matrix.xlsx"` |
| GitHub read fails transiently | 503 (after the activity's own retries) | empty; error logged |
| File too large / unexpected GitHub response | 502 | empty; `github_log_file_too_large_or_unexpected_response` semantics |

### Concurrency

Downloads are read-only and idempotent. Concurrent *updates* remain guarded by the blob-sha optimistic lock: the loser of a race gets the existing non-retryable `github_write_conflict_file_changed_since_read` error and simply re-runs; with 2–3 users this stays rare.

## 6. Constraints

- **C1 — Authentication only via platform ingress.** The endpoint is reachable only through the platform's authenticated ingress. No public ingress, tunnels, relays, reverse proxies, broad listeners, or "anyone with the link" access; no `0.0.0.0` binding outside the platform-managed container.
- **C2 — Secret hygiene.** `GITHUB_TOKEN` is read only inside the deployment (activity or handler), `GITHUB_API_BASE_URL` must stay https (existing rule in `_github_config`), and no secret may appear in responses, logs, or errors.
- **C3 — Worker untouched.** The worker module keeps `routes: []` / `port: null`; no HTTP server is added to the worker process (R6).
- **C4 — Existing design rules.** Side effects and environment reads stay inside activities; repo/path/branch/sheet stay `GITHUB_LOG_*` configuration, not workflow input; columns keep being matched by header name.
- **C5 — Rate limiting.** GitHub reads go through activities under the shared `GITHUB_API_RATE_LIMIT` (30/s, key `github-api`, shared across all workers in the task workspace). Expected load (R4) is ~0.001% of that. If the handler ever calls GitHub directly instead, it needs its own throttle.
- **C6 — Payload size.** The platform enforces a 2 MB payload limit on workflow/activity payloads; the workbook currently travels as base64 (~30 KB file → ~40 KB string) and GitHub's contents API inlines base64 only up to 1 MB. Both are far away today. If the workbook grows, either keep the result under the limits or switch the read to `OffloadableField` blob storage (see `workflow/.agents/skills/capability-workflows/references/guides/handling-large-data.mdx`) / stream `download_url` in the handler.
- **C7 — Dependencies.** No new dependencies beyond what the app already uses (httpx, openpyxl, pydantic) and what the platform provides.
- **C8 — Backward compatibility.** `update-github-log` and `download-testlog` behave exactly as before; the chat artifact path remains available (R5).
- **C9 — Style.** Match `apps/worker`: pydantic input/output models, the `WorkflowError` taxonomy, tests against a mocked GitHub API, semgrep lint per `apps/*/project.json`.
- **C10 — The link is not a capability.** `download_url` carries no token or secret; access stays gated by caller authentication at the platform ingress, so a leaked link alone yields 401. Like the file name, the URL is worker configuration read inside an activity, never workflow input.

## 7. Acceptance criteria

- **AC1.** `workflow/mistral.json` declares the `api` module (route `GET /download`, port, dockerfile) and the `worker` module still has `routes: []`, `port: null`.
- **AC2.** `GET /download` without caller credentials → 401, no file bytes.
- **AC3.** `GET /download` with an authenticated but unauthorized caller → 403, no file bytes.
- **AC4.** `GET /download` with an authorized caller → 200, xlsx MIME type, `Content-Disposition: attachment` with the configured file name, and a body that decodes to a valid workbook identical to the blob served by the mocked GitHub API (blob sha matches).
- **AC5.** No `GITHUB_TOKEN`/secret material in any response, error, or log line (tested).
- **AC6.** An audit log entry exists per successful download with caller, blob sha, size, timestamp.
- **AC7.** Tests pass: `cd workflow && bash tools/uv.sh run --all-packages pytest apps/api apps/worker`; lint passes for the new module.
- **AC8.** `README.md` documents the endpoint, auth model, and user flow.
- **AC9.** The diff contains no public-exposure mechanism (no tunnel/proxy config, no listener bound to all interfaces, no unsigned-URL access) — C1 holds by construction.
- **AC10.** Existing worker tests are unchanged and green (C8).
- **AC11.** With `APP_DOWNLOAD_URL` set, `download-testlog` and `update-github-log` outputs contain `download_url` equal to it; unset → the field is empty. The value never contains `GITHUB_TOKEN` or any secret material (tested) — C10.
- **AC12.** The chat reply after an `update-github-log` run and on a download request includes the link (agent instructions / `ButtonLink`); the README documents the chat link and the deployed module URL (extends AC8).

## 8. Out of scope

- A direct **upload/update** endpoint — updates stay on `update-github-log` via the chat.
- Changes to the workbook structure or to the update workflows.
- Using GitHub access as the distribution mechanism (already possible for users with repo access; not the point of R1).

## 9. Alternatives considered

- **Status quo (chat artifact):** works today, but requires a chat session and execution rights; rejected as the only path per R1.
- **Platform HTTP execution routes (`fastapi-workflows-auth` overlay):** the platform can expose workflow executions over authenticated HTTP; considered, but the result would be JSON with base64 inside rather than a browser file download, so a thin API module that returns proper `Content-Disposition` is preferred for R1.
- **GitHub access for users:** simplest, but does not satisfy R1 (no GitHub access needed) and the repo is private.

## 10. Decisions taken (flag if you disagree)

- `SPEC.md` lives at the repository root.
- Route path `GET /download`; module name `api`; app directory `workflow/apps/api`; image `deploy/docker/Dockerfile.api`.
- Authorization = platform-authenticated caller allowed to execute `download-testlog` (optionally narrowed by an allowlist later).
- Fetch path = start a `download-testlog` execution and await its result (keeps the rate-limited read path); direct GitHub GET kept as documented fallback under C5/C6.
- Chat link (R7): new config var `APP_DOWNLOAD_URL` bound at deploy time to the `update-github-log-api` module URL; shown as a `ButtonLink` where the UI supports it, as a raw link otherwise; the artifact stays as fallback.
- 2026-11-21: direct download without the chat is prioritized via the **Mistral Files API** (`file_id` in the workflow result, authenticated download via `GET /v1/files/{file_id}/content`) — see §11. Signed URLs are not the primary mechanism (unauthenticated for their TTL, 1–168 h, conflicting with R2/C1/C10); object storage via bound S3/GCS/Azure secrets is rejected as primary (new long-lived credential + bucket lifecycle ops, no security gain). The inline base64 result stays unchanged as the chat artifact fallback.
- 2026-11-21: the `api` module's URL cannot be auto-discovered from `/v1/workflows/deployments` (it lists only managed worker deployments — see §11); bind `APP_DOWNLOAD_URL` at deploy time to its URL. Auto-discovery remains best-effort only.
- 2026-11-21: both workflow outputs also carry `download_url_detail`, a secret-free explanation of how `download_url` was resolved (source + steps/outcome), for diagnosability.

## 11. Findings from the deployed worker and revised direction (2026-11-21)

Live observation (worker `update-github-log-worker` at `b10547f`), returned as `download_url_detail` in a `download-testlog` result:

```
unresolved: GET /v1/workflows/deployments/app-workspace-api -> HTTP 404; GET /v1/workflows/deployments -> 1 deployment(s) [update-github-log-worker]; none named *-api with an https endpoint
```

Conclusions:

- The worker's service-account token can read the deployments API and `WORKFLOWS_BASE_URL` is https, so the resolution pipeline itself works; the failure is purely "no such entry".
- `APP_NAME` is not bound on the worker deployment, so the name probe used the package default `app-workspace` (`app-workspace-api` → 404).
- `/v1/workflows/deployments` lists only managed **worker** deployments (`mistral apps deploy` creates exactly one, `<app>-worker`). The `api` module (`serve: "server"`) is deployed as an app/space module (`/v1/spaces/{id}/modules/api/deploy`) and **never appears in that API** — there is no list-spaces endpoint, and the worker does not know its space id. Auto-discovery of the api module URL from the deployments API therefore cannot work; the URL must be bound via `APP_DOWNLOAD_URL` (from the Console / deploy output).
- A managed worker container exposes no shell, exec, or filesystem, and shares no volume with the caller: a file produced inside a workflow run is reachable only if the worker returns it inline or pushes it out itself.

Revised priority for R1 (direct download without the chat intermediary):

1. **Mistral Files API (chosen).** A new `publish-testlog-file` activity uploads the workbook bytes with the worker's SA-token client (`visibility="workspace"`; 512 MB per-file cap ≫ the ~30 KB workbook; `expiry` on upload plus `files.list`/`files.delete` for cleanup; cache by blob sha so only changed files are re-uploaded). The activity runs inside the workflow read path (determinism rule; not under `GITHUB_API_RATE_LIMIT` — it is a Mistral API call). Both `download-testlog` and `update-github-log` outputs gain `file_id`. Callers download with their own bearer token: `curl -H "Authorization: Bearer <their token>" "$WORKFLOWS_BASE_URL/v1/files/<file_id>/content" -o "Response Repository Matrix.xlsx"`, or `client.files.download(file_id)` — one authenticated call, no chat, no GitHub access. `file_id` alone is not a capability: `/v1/files/{file_id}/content` requires auth. Verify the accepted `purpose` value against the live API first (SDK literals are `fine-tune`/`batch`/`ocr`/`agentic_safety`; `ocr` is the usual arbitrary-document purpose).
2. **Signed URLs — optional convenience only, never primary.** `client.files.get_signed_url(file_id, expiry=…)` yields an unauthenticated URL valid 1–168 h (default 24 h); that is "anyone with the link" access and conflicts with R2/C1/C10 for a private workbook. If a one-click browser link is wanted, the chat should request a signed URL on demand with the minimum TTL; the worker must not bake a long-lived signed URL into the result.
3. **Object storage (S3/GCS/Azure via deployment secrets)** — rejected as primary: a new long-lived credential to bind/rotate, bucket retention and CORS ops, and no security gain over option 1.
4. **Inline base64** — stays exactly as it is (chat artifact fallback); not being improved.

The ingress-served `api` module implemented on this branch remains the best UX when it is deployed and `APP_DOWNLOAD_URL` is bound to its URL (single authenticated `GET /download`, proper `Content-Disposition`, audit log, no storage at all); it needs no deployments-API discovery for access, only for the optional URL auto-discovery. `download_url`/`download_url_detail` already report how the URL was resolved (`override` / `deployment-name` / `deployments-list` / `unresolved` plus the steps), which is how the findings above were obtained.
