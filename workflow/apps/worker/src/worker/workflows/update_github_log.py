"""Append a test entry to (or update an existing entry of) ``Response Repository Matrix.xlsx`` in the ``guili244540/documentation`` GitHub repository.

The GitHub connection follows Scenario 2 of the External API Integration guide (fixed
service credential): the deployment binds the workspace secret ``githubtoken`` as
``GITHUB_TOKEN``, and every GitHub call happens inside an activity, so the token never
crosses the workflow boundary. The repo, path and branch are worker configuration read
from the environment, not workflow input.

The workbook has several sheets; only the configured log sheet (``Test Log``) receives
new rows. The header row is located by configuration (``GITHUB_LOG_HEADER_ROW``) and the
columns are matched **by header name**, so extra columns do not break the append. The
``Lists`` sheet drives two things: allowed values for the categorical columns, and the
rebuild of the sheet dropdowns (openpyxl cannot round-trip the extension-based data
validations Excel stores, so they are recreated as standard validations on save).

Run shape (deterministic workflow, side effects in activities)::

    read_testlog        GET the workbook (bytes + blob sha + existing test IDs)
    [dry-run gate]      return the planned row without writing
    commit_testlog      edit the workbook with openpyxl (append a new row, or
                        update an existing Test ID row in place when
                        ``update_existing`` is set), PUT it with the blob sha
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

#: Headers that must exist on the log sheet, in any column order. A file whose
#: log sheet is missing any of them is rejected (``unexpected_sheet_structure``).
EXPECTED_HEADERS = [
    "Test ID",
    "Service / Team",
    "Question received",
    "Mistral prompt used",
    "Mistral answer (original)",
    "Quality rating (1–5)",
    "Error category",
    "Correction level",
    "Human-edited answer / final response",
    "Outcome",
    "Validated for reuse?",
    "Average of prompt review",
]

#: Normalised aliases so a log-sheet header finds its ``Lists`` sheet column even
#: when the wording differs slightly (e.g. the "(1–5)" suffix on the rating).
LISTS_HEADER_ALIASES = {
    "quality rating (1–5)": "quality rating",
}

GITHUB_API_RATE_LIMIT = workflows.RateLimit(
    time_window_in_sec=1, max_execution=30, key="github-api"
)


class NewTestEntry(BaseModel):
    """One row to append to the log.

    ``question`` and ``quality`` are required; every other column is optional and
    left empty when omitted. Categorical fields are checked against the allowed
    values of the workbook's ``Lists`` sheet during the commit.
    """

    model_config = ConfigDict(hide_input_in_errors=True)
    question: str = Field(min_length=1, max_length=2000, description="Question received")
    quality: int = Field(ge=1, le=5, description="Quality rating of the original answer")
    service_team: str | None = Field(default=None, max_length=200)
    prompt_used: str | None = Field(default=None, max_length=10000, description="Original prompt")
    mistral_answer: str | None = Field(default=None, max_length=10000, description="Original Mistral answer")
    error_category: str | None = Field(default=None, max_length=200)
    correction_level: str | None = Field(default=None, max_length=200)
    human_edited_answer: str | None = Field(default=None, max_length=10000)
    outcome: str | None = Field(default=None, max_length=200)
    validated_for_reuse: bool | None = Field(default=None)
    prompt_review_count: int | None = Field(
        default=None, ge=1, le=999, description="Prompts needed to obtain the final result"
    )
    test_id: int | None = Field(
        default=None, ge=1, description="Explicit Test ID; next free ID when omitted"
    )


class UpdateGitHubLogInput(BaseModel):
    """Input of the ``update-github-log`` workflow.

    With ``update_existing`` the workflow updates the row of the explicit
    ``entry.test_id`` in place instead of appending a new row; fields left
    ``None`` keep the values already stored in that row.
    """

    entry: NewTestEntry
    commit_message: str | None = Field(default=None, max_length=500)
    dry_run: bool = Field(
        default=False, description="Plan the change without committing anything"
    )
    update_existing: bool = Field(
        default=False,
        description="Update the existing Test ID row in place instead of appending",
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
    update_existing: bool = False


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


def _github_config() -> dict[str, object]:
    """Read the worker's GitHub configuration from the environment.

    The token only goes to ``GITHUB_API_BASE_URL``, which must be https.
    """

    def env_int(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, default))
        except (TypeError, ValueError):
            raise workflows.WorkflowError(f"{name.lower()}_not_an_integer", non_retryable=True) from None

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
        "path": os.environ.get("GITHUB_LOG_PATH", "Response Repository Matrix.xlsx"),
        "branch": os.environ.get("GITHUB_LOG_BRANCH", "main"),
        "sheet": os.environ.get("GITHUB_LOG_SHEET", "Test Log"),
        "lists_sheet": os.environ.get("GITHUB_LOG_LISTS_SHEET", "Lists"),
        "header_row": env_int("GITHUB_LOG_HEADER_ROW", 4),
        "last_row": env_int("GITHUB_LOG_LAST_ROW", 204),
    }


def _github_async_client(cfg: dict[str, object]) -> "httpx.AsyncClient":
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


def _contents_url(cfg: dict[str, object]) -> str:
    return f"/repos/{cfg['owner']}/{cfg['repo']}/contents/{cfg['path']}"


def _auth_headers(cfg: dict[str, object]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {cfg['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _norm_header(value: object) -> str:
    """Normalise a header cell so log-sheet and ``Lists`` headers can be matched."""
    if value is None:
        return ""
    text = " ".join(str(value).split()).strip().lower()
    text = text.rstrip("?").strip()
    return LISTS_HEADER_ALIASES.get(text, text)


def _header_column_map(ws: object, header_row: int) -> dict[str, int]:
    """Map every header cell of the row to its 1-based column index."""
    mapping: dict[str, int] = {}
    for row in ws.iter_rows(min_row=header_row, max_row=header_row):
        for cell in row:
            if cell.value is not None:
                mapping[str(cell.value)] = cell.column
    return mapping


def _require_headers(ws: object, header_row: int) -> dict[str, int]:
    """Return the header map after checking every expected header is present."""
    mapping = _header_column_map(ws, header_row)
    missing = [h for h in EXPECTED_HEADERS if h not in mapping]
    if missing:
        raise workflows.WorkflowError(
            f"unexpected_sheet_structure:missing headers {missing}", non_retryable=True
        )
    return mapping


def _lists_values(workbook: object, lists_sheet: str) -> dict[str, list[str]]:
    """Read the ``Lists`` sheet: normalised header -> list of allowed values."""
    if lists_sheet not in workbook.sheetnames:
        return {}
    ws = workbook[lists_sheet]
    columns: dict[str, list[str]] = {}
    for cell in next(ws.iter_rows(min_row=1, max_row=1)):
        norm = _norm_header(cell.value)
        if norm:
            columns[norm] = []
            for row in ws.iter_rows(min_row=2, min_col=cell.column, max_col=cell.column):
                value = row[0].value
                if value is None:
                    continue
                columns[norm].append(str(value).strip())
    return {k: v for k, v in columns.items() if v}


def _normalise_entry(
    entry: NewTestEntry, allowed: dict[str, list[str]]
) -> dict[str, object]:
    """Validate categorical fields against the Lists sheet and canonicalise them.

    Returns the values to write, keyed by log-sheet header. Unknown categories fail
    the run with a clear error instead of landing as silent dashboard mismatches.
    """

    def canonical(field: str, header_norm: str, value: str | None) -> str | None:
        if value is None:
            return None
        options = allowed.get(header_norm, [])
        for option in options:
            if option.casefold() == value.casefold():
                return option
        raise workflows.WorkflowError(
            f"invalid_{field}:{value} (allowed: {options})", non_retryable=True
        )

    reuse = entry.validated_for_reuse
    reuse_value = None if reuse is None else ("Yes" if reuse else "No")
    return {
        "Test ID": None,  # filled by the caller
        "Service / Team": canonical("service_team", "service / team", entry.service_team),
        "Question received": entry.question,
        "Mistral prompt used": entry.prompt_used,
        "Mistral answer (original)": entry.mistral_answer,
        "Quality rating (1–5)": entry.quality,
        "Error category": canonical("error_category", "error category", entry.error_category),
        "Correction level": canonical("correction_level", "correction level", entry.correction_level),
        "Human-edited answer / final response": entry.human_edited_answer,
        "Outcome": canonical("outcome", "outcome", entry.outcome),
        "Validated for reuse?": canonical("validated_for_reuse", "validated for reuse", reuse_value),
        "Average of prompt review": entry.prompt_review_count,
    }


def _rebuild_data_validations(
    workbook: object, lists_sheet: str, last_row: int
) -> None:
    """Recreate the dropdowns openpyxl drops (Excel stores them as extensions).

    For every sheet except the ``Lists`` sheet itself, find the row (within the
    first 10) whose cells match ``Lists`` headers, and attach one standard list
    validation per matching column, filled with the Lists values. Literal lists
    are used so every Excel version keeps the dropdown working.
    """
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    allowed = _lists_values(workbook, lists_sheet)
    if not allowed:
        return
    for ws in workbook.worksheets:
        if ws.title == lists_sheet:
            continue
        header_row = None
        for row in ws.iter_rows(min_row=1, max_row=min(10, ws.max_row)):
            norms = [_norm_header(cell.value) for cell in row]
            if any(norm in allowed for norm in norms):
                header_row = row[0].row
                break
        if header_row is None:
            continue
        ws.data_validations.dataValidation = []
        for cell in next(ws.iter_rows(min_row=header_row, max_row=header_row)):
            options = allowed.get(_norm_header(cell.value))
            if not options:
                continue
            if any("," in option or '"' in option for option in options):
                continue  # cannot be expressed as a literal list; leave as-is
            formula = '"' + ",".join(options) + '"'
            if len(formula) > 255:
                continue
            validation = DataValidation(
                type="list",
                formula1=formula,
                allow_blank=True,
                showErrorMessage=True,
                errorTitle="Invalid selection",
                error="Please select a value from the dropdown list.",
                showInputMessage=True,
                promptTitle="Choose a value",
                prompt="Select an option from the list.",
            )
            letter = get_column_letter(cell.column)
            validation.add(f"{letter}{header_row + 1}:{letter}{max(last_row, header_row + 1)}")
            ws.add_data_validation(validation)


def _last_data_row(ws: object, header_row: int) -> int:
    """Highest row with any value below the header row; the header row itself when empty."""
    last = header_row
    for row in ws.iter_rows(min_row=header_row + 1, max_row=ws.max_row):
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
        if body.get("encoding") != "base64":
            raise ValueError("not base64")
    except (ValueError, KeyError, TypeError):
        raise workflows.WorkflowError("github_log_file_too_large_or_unexpected_response", non_retryable=True) from None

    import base64

    import openpyxl

    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(base64.b64decode(content_b64)), rich_text=True
        )
        ws = workbook[cfg["sheet"]]
        mapping = _require_headers(ws, cfg["header_row"])
    except workflows.WorkflowError:
        raise
    except (KeyError, ValueError, OSError):
        raise workflows.WorkflowError("github_log_file_not_a_workbook", non_retryable=True) from None

    id_column = mapping["Test ID"]
    existing: list[int] = []
    for row in ws.iter_rows(min_row=cfg["header_row"] + 1, max_row=ws.max_row, max_col=id_column):
        value = row[-1].value
        if isinstance(value, (int, float)):
            existing.append(int(value))
    next_test_id = max(existing, default=0) + 1
    return SheetState(
        base64_content=content_b64,
        blob_sha=blob_sha,
        next_test_id=next_test_id,
        existing_test_ids=existing,
        headers=list(mapping.keys()),
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
    """Write the entry to the log sheet (append, or update in place) and PUT the workbook."""
    import base64
    import copy
    import io

    import httpx
    import openpyxl

    cfg = _github_config()
    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(base64.b64decode(params.base64_content)), rich_text=True
        )
        ws = workbook[cfg["sheet"]]
        mapping = _require_headers(ws, cfg["header_row"])
    except workflows.WorkflowError:
        raise
    except (KeyError, ValueError, OSError):
        raise workflows.WorkflowError("github_log_file_not_a_workbook", non_retryable=True) from None

    id_column = mapping["Test ID"]
    if params.update_existing:
        row_index = None
        for row in ws.iter_rows(min_row=cfg["header_row"] + 1, max_row=ws.max_row, max_col=id_column):
            value = row[-1].value
            if isinstance(value, (int, float)) and int(value) == params.test_id:
                row_index = row[0].row
                break
        if row_index is None:
            raise workflows.WorkflowError(
                f"test_id_not_found:{params.test_id}", non_retryable=True
            )
        source_row = None
    else:
        for row in ws.iter_rows(min_row=cfg["header_row"] + 1, max_row=ws.max_row, max_col=id_column):
            value = row[-1].value
            if isinstance(value, (int, float)) and int(value) == params.test_id:
                raise workflows.WorkflowError(
                    f"test_id_already_exists:{params.test_id}", non_retryable=True
                )
        row_index = _last_data_row(ws, cfg["header_row"]) + 1
        source_row = row_index - 1

    values = _normalise_entry(params.entry, _lists_values(workbook, cfg["lists_sheet"]))
    values["Test ID"] = params.test_id

    for header, value in values.items():
        if header not in mapping:
            continue
        if params.update_existing and value is None:
            continue  # not provided: keep the existing cell content
        target = ws.cell(row=row_index, column=mapping[header], value=value)
        if params.update_existing:
            continue  # in-place update: keep the existing cell style
        if source_row > cfg["header_row"]:
            src = ws.cell(row=source_row, column=mapping[header])
            target.font = copy.copy(src.font)
            target.border = copy.copy(src.border)
            target.fill = copy.copy(src.fill)
            target.alignment = copy.copy(src.alignment)
            target.number_format = src.number_format

    _rebuild_data_validations(workbook, cfg["lists_sheet"], cfg["last_row"])

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
        mapping = _require_headers(ws, cfg["header_row"])
    except workflows.WorkflowError:
        raise
    except (KeyError, ValueError, TypeError, OSError):
        raise workflows.WorkflowError("github_unexpected_response", non_retryable=True) from None

    id_column = mapping["Test ID"]
    question_column = mapping["Question received"]
    for row in ws.iter_rows(min_row=cfg["header_row"] + 1, max_row=ws.max_row):
        test_id = row[id_column - 1].value if len(row) >= id_column else None
        if isinstance(test_id, (int, float)) and int(test_id) == params.test_id:
            question = row[question_column - 1].value if len(row) >= question_column else None
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
        if data.update_existing:
            if entry.test_id is None:
                raise ValueError("Updating requires an explicit test_id in the entry")
            if test_id not in state.existing_test_ids:
                raise ValueError(f"Test ID {test_id} not found in the log")
        elif test_id in state.existing_test_ids:
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
                update_existing=data.update_existing,
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
