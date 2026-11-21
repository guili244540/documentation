# documentation
A place to store our documentation.

## Contents

- `Response Repository Matrix.xlsx` — the test log: one row per tested question/answer, with quality rating, error category, correction level and the final human-edited answer.
- `TestLog.xlsx` — an earlier log workbook.
- `workflow/` — the Mistral Workflows app (`update-github-log`) that maintains the matrix from a chat.

## The workflow app (`workflow/`)

**Goal.** Let a chat agent record each new prompt/answer test in `Response Repository Matrix.xlsx` in this repository — and hand out the current workbook as a download — without anyone needing GitHub access of their own. The credential lives in the workspace secret `githubtoken`, bound as `GITHUB_TOKEN` at deploy time, never in the conversation or the code.

**How it works.** `workflow/apps/worker` is a Python worker serving two workflows:

- `update-github-log` — appends a new row to the `Test Log` sheet, or updates an existing `Test ID` row in place (`update_existing`); `dry_run` plans the change without committing. Run shape: `read_testlog` (GET the workbook) → dry-run gate → `commit_testlog` (edit with openpyxl, PUT using the blob sha as an optimistic lock) → `verify_testlog` (GET the file back and confirm the row landed).
- `download-testlog` — read-only sibling that returns the current workbook as base64 plus file name, blob sha, size and the existing/next Test IDs, through the same `read_testlog` activity, so the chat can publish it as a downloadable artifact.

**Design rules.**

- Workflow code stays deterministic: every side effect and every environment read happens inside an activity, never in the workflow body (the file name comes from the activity, not from `os.environ` in the workflow).
- Repo, path, branch, sheet and header row are worker configuration (`GITHUB_LOG_*` variables, declared in `workflow/.env.example`), not workflow input.
- Columns are matched by header name and categorical fields are validated against the `Lists` sheet, so extra or reordered columns do not break the append.

**Layout and tests.** Workflow definitions and activities live in `workflow/apps/worker/src/worker/workflows/`; tests run against a mocked GitHub API in `workflow/apps/worker/tests/` (`cd workflow && bash tools/uv.sh run --all-packages pytest apps/worker`). The worker image is built from `workflow/deploy/docker/Dockerfile.worker`; app wiring is in `workflow/mistral.json`.

**Direct download (`GET /download`).** Next to the worker, the app declares a second module, `api` (`workflow/apps/api`, image `workflow/deploy/docker/Dockerfile.api`), which serves one route — `GET /download` — behind the platform's authenticated ingress. A signed-in, authorized user gets the current workbook as a file download in one request, no chat and no GitHub access of their own:

- `https://<update-github-log-api deployment URL>/download` in a browser, or
  `curl -H "Authorization: Bearer <their workspace token>" "<app-url>/download" -o "Response Repository Matrix.xlsx"`.

The module URL is the `update-github-log-api` deployment shown in the Console / `mistral apps deploy` output. Not signed in → `401`; signed in but not authorized → `403` (an optional `APP_DOWNLOAD_ALLOWLIST` of caller ids narrows who may download; unset, any authenticated workspace caller may). Every successful download is audit-logged (caller, blob sha, size, timestamp). The handler authenticates the caller with the per-request credential the platform installs and fetches the workbook by running `download-testlog` on the worker deployment, so the GitHub token (`githubtoken` → `GITHUB_TOKEN`) stays server-side and the read keeps the worker's rate-limited activity path. The `worker` module itself stays a pure workflow worker — no HTTP listener.

**The chat shows the link too.** Both workflow outputs (`download-testlog` and `update-github-log`) carry a `download_url` field: the deployed `…-api` URL, auto-discovered from the deployments API inside the `resolve-download-url` activity, or the explicit `APP_DOWNLOAD_URL` override (empty when neither resolves → artifact-only behaviour). After each successful `update-github-log` and whenever the user asks for the workbook, the chat reply includes that link (a `ButtonLink` where the UI supports it), while still publishing the artifact as a fallback.
