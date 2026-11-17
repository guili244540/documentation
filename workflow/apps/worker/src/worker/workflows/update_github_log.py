"""Append a test entry to ``TestLog.xlsx`` in the ``guili244540/documentation`` GitHub repository.

The GitHub connection follows Scenario 2 of the External API Integration guide (fixed
service credential): the deployment binds the workspace secret ``githubtoken`` as
``GITHUB_TOKEN``, and every GitHub call happens inside an activity, so the token never
crosses the workflow boundary. The repo, path and branch are worker configuration read
from the environment, not workflow input.

Run shape (deterministic workflow, side effects in activities)::

    read_testlog        GET the workbook (bytes + blob sha + existing test IDs)
    [dry-run gate]      return the planned row without writing
    commit_testlog      edit the workbook with openpyxl, PUT it with the blob sha
    verify_testlog      GET the file back and confirm the row landed
"""

from __future__ import annotations

import os
from datetime import timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

import mistralai.workflows as workflows
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    import httpx

#: Row 1 of the sheet, in column order. A file whose header differs is rejected.
EXPECTED_HEADERS = [
    "Test ID",
    "Service / Team",
    "Question received",
    "Quality rating (1–5)",
    "Error category",
]

GITHUB_API_RATE_LIMIT = workflows.RateLimit(
    time_window_in_sec=1, max_execution=30, key="github-api"
)


class NewTestEntry(BaseModel):
    """One row to append to the log."""

    model_config = ConfigDict(hide_input_in_errors=True)
    question: str = Field(min_length=1, max_length=2000, description="Question received")
    quality: int = Field(ge=1, le=5, description="Quality rating")
    service_team: str | None = Field(default=None, max_length=200)
    error_category: str | None = Field(default=None, max_length=200)
    test_id: int | None = Field(
        default=None, ge=1, description="Explicit Test ID; next free ID when omitted"
    )


class UpdateGitHubLogInput(BaseModel):
    """Input of the ``update-github-log`` workflow."""

    entry: NewTestEntry
    commit_message: str | None = Field(default=None, max_length=500)
    dry_run: bool = Field(
        default=False, description="Plan the change without committing anything"
    )


class UpdateGitHubLogOutput(BaseModel):
    dry_run: bool
    test_id: int
    message: str
    commit_sha: str | None = None
    commit_url: str | None = None
    file_url: str | None = None
    verified: bool | None = None


class ReadTestLogInput(BaseModel):
    """``read_testlog`` takes no business input; its configuration is worker env."""


class SheetState(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    base64_content: str
    blob_sha: str
    next_test_id: int
    existing_test_ids: list[int]
    headers: list[str]


class CommitTestLogInput(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    base64_content: str
    blob_sha: str
    entry: NewTestEntry
    test_id: int = Field(ge=1)
    commit_message: str


class CommitResult(BaseModel):
    commit_sha: str
    commit_url: str
    file_url: str
    new_blob_sha: str


class VerifyTestLogInput(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    expected_blob_sha: str
    test_id: int
    question: str


class VerifyResult(BaseModel):
    verified: bool
    detail: str


def _github_config() -> dict[str, str]:
    """Read the worker's GitHub configuration from the environment.

    The token only goes to ``GITHUB_API_BASE_URL``, which must be https.
    """
    base_url = os.environ.get("GITHUB_API_BASE_URL", "https://api.github.com")
    if not base_url.startswith("https://"):
        raise workflows.WorkflowError(
            "github_api_base_url_not_https", non_retryable=True
        )
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise workflows.WorkflowError("github_token_not_configured", non_retryable=True)
    return {
        "base_url": base_url,
        "token": token,
        "owner": os.environ.get("GITHUB_LOG_OWNER", "guili244540"),
        "repo": os.environ.get("GITHUB_LOG_REPO", "documentation"),
        "path": os.environ.get("GITHUB_LOG_PATH", "TestLog.xlsx"),
        "branch": os.environ.get("GITHUB_LOG_BRANCH", "main"),
        "sheet": os.environ.get("GITHUB_LOG_SHEET", "Sheet1"),
    }


def _github_async_client(cfg: dict[str, str]) -> "httpx.AsyncClient":
    """Build the HTTP client for the GitHub API. Tests monkeypatch this seam."""
    import httpx

    return httpx.AsyncClient(
        base_url=cfg["base_url"], timeout=15, follow_redirects=False
    )


def _retry_after(response: "httpx.Response") -> timedelta | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        delay = timedelta(seconds=int(value))
    except (ValueError, OverflowError):
        try:
            from datetime import UTC, datetime

            delay = parsedate_to_datetime(value) - datetime.now(UTC)
        except (TypeError, ValueError):
            return None
    return max(delay, timedelta(0))


def _require_ok(response: "httpx.Response", *ok: int) -> None:
    if response.status_code in ok:
        return
    if response.status_code == 401:
        raise workflows.WorkflowError(
            "github_token_invalid_or_expired", non_retryable=True
        )
    if response.status_code == 403:
        raise workflows.WorkflowError(
            "github_forbidden_check_token_scopes", non_retryable=True
        )
    if response.status_code == 404:
        raise workflows.WorkflowError("github_log_file_not_found", non_retryable=True)
    if response.status_code == 409:
        raise workflows.WorkflowError(
            "github_write_conflict_file_changed_since_read", non_retryable=True
        )
    retryable = response.status_code in {429, 502, 503, 504}
    raise workflows.WorkflowError(
        f"github_http_{response.status_code}",
        non_retryable=not retryable,
        next_retry_delay=_retry_after(response) if retryable else None,
    )


def _contents_url(cfg: dict[str, str]) -> str:
    return f"/repos/{cfg['owner']}/{cfg['repo']}/contents/{cfg['path']}"


def _auth_headers(cfg: dict[str, str]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {cfg['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _last_data_row(ws: "object") -> int:
    """Highest row with any value in columns A–E; 1 when only the header exists."""
    last = 1
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=5):
        if any(cell.value is not None for cell in row):
            last = row[0].row
    return last


@workflows.activity(
    name="github-read-testlog",
    start_to_close_timeout=timedelta(seconds=30),
    retry_policy_max_attempts=3,
    retry_policy_backoff_coefficient=2.0,
    rate_limit=GITHUB_API_RATE_LIMIT,
)
async def read_testlog(_: ReadTestLogInput) -> SheetState:
    """GET the workbook: base64 bytes, blob sha and the existing Test IDs."""
    import io

    import httpx

    cfg = _github_config()
    try:
        async with _github_async_client(cfg) as client:
            response = await client.get(
                _contents_url(cfg),
                params={"ref": cfg["branch"]},
                headers=_auth_headers(cfg),
            )
    except httpx.RequestError:
        raise workflows.WorkflowError("github_api_unavailable") from None
    _require_ok(response, 200)
    try:
        body = response.json()
        content_b64: str = body["content"]
        blob_sha: str = body["sha"]
    except (ValueError, KeyError, TypeError):
        raise workflows.WorkflowError("github_unexpected_response", non_retryable=True) from None

    import base64

    import openpyxl

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(base64.b64decode(content_b64)))
        ws = workbook[cfg["sheet"]]
    except (KeyError, ValueError, OSError):
        raise workflows.WorkflowError("github_log_file_not_a_workbook", non_retryable=True) from None

    headers = ["" if c.value is None else str(c.value) for c in next(ws.iter_rows(min_row=1, max_row=1, max_col=5))]
    if headers != EXPECTED_HEADERS:
        raise workflows.WorkflowError("unexpected_sheet_structure", non_retryable=True)

    existing: list[int] = []
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=1):
        value = row[0].value
        if isinstance(value, (int, float)):
            existing.append(int(value))
    next_test_id = max(existing, default=0) + 1
    return SheetState(
        base64_content=content_b64,
        blob_sha=blob_sha,
        next_test_id=next_test_id,
        existing_test_ids=existing,
        headers=headers,
    )


@workflows.activity(
    name="github-commit-testlog",
    # A repeat of a partially applied write must not run twice: the blob sha makes
    # a second PUT fail with 409, so one attempt keeps the failure honest.
    start_to_close_timeout=timedelta(seconds=30),
    retry_policy_max_attempts=1,
    rate_limit=GITHUB_API_RATE_LIMIT,
)
async def commit_testlog(params: CommitTestLogInput) -> CommitResult:
    """Append the entry to the workbook and PUT it with the blob sha read earlier."""
    import base64
    import copy
    import io

    import httpx
    import openpyxl

    cfg = _github_config()
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(base64.b64decode(params.base64_content)))
        ws = workbook[cfg["sheet"]]
    except (KeyError, ValueError, OSError):
        raise workflows.WorkflowError("github_log_file_not_a_workbook", non_retryable=True) from None

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=1):
        value = row[0].value
        if isinstance(value, (int, float)) and int(value) == params.test_id:
            raise workflows.WorkflowError(
                f"test_id_already_exists:{params.test_id}", non_retryable=True
            )

    row_index = _last_data_row(ws) + 1
    source_row = row_index - 1
    values = [
        params.test_id,
        params.entry.service_team,
        params.entry.question,
        params.entry.quality,
        params.entry.error_category,
    ]
    for col, value in enumerate(values, start=1):
        target = ws.cell(row=row_index, column=col, value=value)
        if source_row >= 2:
            src = ws.cell(row=source_row, column=col)
            target.font = copy.copy(src.font)
            target.border = copy.copy(src.border)
            target.fill = copy.copy(src.fill)
            target.alignment = copy.copy(src.alignment)
            target.number_format = src.number_format

    buffer = io.BytesIO()
    workbook.save(buffer)
    new_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")

    try:
        async with _github_async_client(cfg) as client:
            response = await client.put(
                _contents_url(cfg),
                json={
                    "message": params.commit_message,
                    "content": new_b64,
                    "sha": params.blob_sha,
                    "branch": cfg["branch"],
                },
                headers=_auth_headers(cfg),
            )
    except httpx.RequestError:
        raise workflows.WorkflowError("github_api_unavailable") from None
    _require_ok(response, 200, 201)
    try:
        body = response.json()
        return CommitResult(
            commit_sha=body["commit"]["sha"],
            commit_url=body["commit"]["html_url"],
            file_url=body["content"]["html_url"],
            new_blob_sha=body["content"]["sha"],
        )
    except (ValueError, KeyError, TypeError):
        raise workflows.WorkflowError("github_unexpected_response", non_retryable=True) from None


@workflows.activity(
    name="github-verify-testlog",
    start_to_close_timeout=timedelta(seconds=30),
    retry_policy_max_attempts=3,
    retry_policy_backoff_coefficient=2.0,
    rate_limit=GITHUB_API_RATE_LIMIT,
)
async def verify_testlog(params: VerifyTestLogInput) -> VerifyResult:
    """GET the file back and confirm the committed row is really in it."""
    import base64
    import io

    import httpx
    import openpyxl

    cfg = _github_config()
    try:
        async with _github_async_client(cfg) as client:
            response = await client.get(
                _contents_url(cfg),
                params={"ref": cfg["branch"]},
                headers=_auth_headers(cfg),
            )
    except httpx.RequestError:
        raise workflows.WorkflowError("github_api_unavailable") from None
    _require_ok(response, 200)
    try:
        body = response.json()
        if body.get("sha") != params.expected_blob_sha:
            return VerifyResult(verified=False, detail="blob sha changed after commit")
        workbook = openpyxl.load_workbook(io.BytesIO(base64.b64decode(body["content"])))
        ws = workbook[cfg["sheet"]]
    except (KeyError, ValueError, TypeError, OSError):
        raise workflows.WorkflowError("github_unexpected_response", non_retryable=True) from None

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=5):
        test_id, _, question = row[0].value, row[1].value, row[2].value
        if isinstance(test_id, (int, float)) and int(test_id) == params.test_id:
            if question == params.question:
                return VerifyResult(verified=True, detail="row found with matching question")
            return VerifyResult(verified=False, detail="row found but question differs")
    return VerifyResult(verified=False, detail="row not found in file after commit")


@workflows.workflow.define(name="update-github-log")
class UpdateGitHubLog:
    @workflows.workflow.entrypoint
    async def run(self, data: UpdateGitHubLogInput) -> UpdateGitHubLogOutput:
        state = await read_testlog(ReadTestLogInput())
        entry = data.entry
        question = entry.question.strip()
        test_id = entry.test_id if entry.test_id is not None else state.next_test_id
        if test_id in state.existing_test_ids:
            raise ValueError(f"Test ID {test_id} already exists in the log")
        message = data.commit_message or (
            f"Add test {test_id}: '{question[:80]}' (quality {entry.quality})"
        )
        if data.dry_run:
            return UpdateGitHubLogOutput(
                dry_run=True,
                test_id=test_id,
                message=f"DRY RUN — would commit: {message}",
            )
        commit = await commit_testlog(
            CommitTestLogInput(
                base64_content=state.base64_content,
                blob_sha=state.blob_sha,
                entry=entry.model_copy(update={"question": question}),
                test_id=test_id,
                commit_message=message,
            )
        )
        verify = await verify_testlog(
            VerifyTestLogInput(
                expected_blob_sha=commit.new_blob_sha,
                test_id=test_id,
                question=question,
            )
        )
        if not verify.verified:
            raise ValueError(f"Commit verification failed: {verify.detail}")
        return UpdateGitHubLogOutput(
            dry_run=False,
            test_id=test_id,
            message=message,
            commit_sha=commit.commit_sha,
            commit_url=commit.commit_url,
            file_url=commit.file_url,
            verified=True,
        )
