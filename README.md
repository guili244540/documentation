# documentation
A place to store our documentation.

## Contents

- `Response Repository Matrix.xlsx` — the test log: one row per tested question/answer, with quality rating, error category, correction level and the final human-edited answer.
- `TestLog.xlsx` — an earlier log workbook.
- `workflow/` — the Mistral Workflows app (`update-github-log`) that maintains the matrix from a chat.

## The workflow app (`workflow/`)

**Goal.** Let a chat agent record each new prompt/answer test in `Response Repository Matrix.xlsx` in this repository — and hand out the current workbook as a download — without anyone needing GitHub access of their own. The credential lives in the workspace secret `githubtoken`, bound as `GITHUB_TOKEN` at deploy time, never in the conversation or the code.

**How it works.** `workflow/apps/worker` is a Python worker serving three workflows:

- `update-github-log` — appends a new row to the `Test Log` sheet, or updates an existing `Test ID` row in place (`update_existing`); `dry_run` plans the change without committing. Run shape: `read_testlog` (GET the workbook) → dry-run gate → `commit_testlog` (edit with openpyxl, PUT using the blob sha as an optimistic lock) → `verify_testlog` (GET the file back and confirm the row landed).
- `download-testlog` — read-only sibling that returns the current workbook as base64 plus file name, blob sha, size and the existing/next Test IDs, through the same `read_testlog` activity, so the chat can publish it as a downloadable artifact.
- `purge-testlog-files` — retention cron (runs every 5 min on a schedule created through the Workflows schedules API at deploy time, not via `@workflow.define`): deletes the worker's own Files API uploads older than 5 min, so a published workbook lives at most ~10 min.

**Design rules.**

- Workflow code stays deterministic: every side effect and every environment read happens inside an activity, never in the workflow body (the file name comes from the activity, not from `os.environ` in the workflow).
- Repo, path, branch, sheet and header row are worker configuration (`GITHUB_LOG_*` variables, declared in `workflow/.env.example`), not workflow input.
- Columns are matched by header name and categorical fields are validated against the `Lists` sheet, so extra or reordered columns do not break the append.

**Layout and tests.** Workflow definitions and activities live in `workflow/apps/worker/src/worker/workflows/`; tests run against a mocked GitHub API in `workflow/apps/worker/tests/` (`cd workflow && bash tools/uv.sh run --all-packages pytest apps/worker`). The worker image is built from `workflow/deploy/docker/Dockerfile.worker`; app wiring is in `workflow/mistral.json`.

**Getting the workbook out — the two supported approaches** (see `SPEC.md`; an ingress-served `api` module was tried on this branch and dropped):

1. **Inline.** `download-testlog` returns the workbook as `base64_content` in the workflow result; the chat decodes it and publishes it as a downloadable artifact. This stays as-is.
2. **Mistral Files API.** The worker uploads the workbook to the Files API in the `publish-testlog-file` activity (run by `read_testlog`, and by `verify_testlog` after a commit so an update's output points at the post-commit file) and both `download-testlog` and `update-github-log` return the `file_id` **and a `signed_url`** in their result. Anyone with a workspace token downloads it with one authenticated call against `/v1/files/<file_id>/content` (SDK: `client.files.download(file_id)`), with their bearer token in the `Authorization` header — no chat, no GitHub access; or, in chat, clicks the `signed_url`, a pre-signed link minted with the shortest TTL the API accepts (1 h) whose effective lifetime is bounded to ≤ 10 min by the retention purge below. Uploads are cached by blob sha (an unchanged file is not re-uploaded) and superseded uploads are deleted; `file_id` alone is not a capability and no token is ever placed in a URL (see `SPEC.md` §11). Every upload is registered in a process-local registry, and the `purge-testlog-files` cron deletes registered uploads older than 5 min — only worker-registered ids are ever deleted (`files.list` is never used to pick deletions), so a published file lives at most ~10 min; after a worker restart the registry is empty and the previous upload is orphaned until manual deletion. On upload failure `file_id`/`signed_url` are empty and the inline artifact path still works. Deploy note: the worker's service-account token is scoped to the Workflows API and is refused by `/v1/files`, so bind a workspace API key with Files access as a secret and set `FILES_API_KEY` on the worker deployment (declared in `workflow/.env.example`); register the purge schedule once via the schedules API (the app's `schedules` command), e.g. `client.workflows.schedules.schedule_workflow_async(workflow_identifier="purge-testlog-files", deployment_name="update-github-log-worker", schedule=ScheduleDefinition(cron_expressions=["*/5 * * * *"], input={}))`.
