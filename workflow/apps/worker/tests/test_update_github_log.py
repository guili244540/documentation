"""Tests for the ``update-github-log`` workflow against a mocked GitHub API.

The GitHub REST API is replaced with an ``httpx.MockTransport`` through the
``_github_async_client`` seam, so no test reaches the network. The workbook
editing itself (openpyxl) runs for real, exactly as it will in the worker.
The fixture mirrors ``Response Repository Matrix.xlsx``: a ``Test Log`` sheet
with its header row on row 4 plus a ``Lists`` sheet holding the dropdown values.
"""

import asyncio
import base64
import io
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import openpyxl
import pytest

from mistralai.workflows.testing import create_test_worker

from worker.workflows import update_github_log
from worker.workflows.update_github_log import (
    EXPECTED_HEADERS,
    UpdateGitHubLog,
    commit_testlog,
    read_testlog,
    verify_testlog,
)

WORKFLOW_EXECUTION_TIMEOUT = timedelta(seconds=10)
CLIENT_TIMEOUT = 15

LISTS_SHEET = {
    "Service / Team": ["CFU", "Invoice-Supplier Team", "Inventory Team", "Budget Unit", "Accounting"],
    "Question Type": ["How-to / procedure", "Contract / rule", "System issue", "Accounting / budget", "Other"],
    "Quality Rating": [1, 2, 3, 4, 5],
    "Correction Level": ["None", "Minor wording", "Moderate changes", "Major changes", "Rewritten / unusable"],
    "Outcome": ["Used as generated", "Used after editing", "Not used", "Escalated / expert review"],
    "Validated for Reuse": ["Yes", "No"],
    "Error Category": [
        "No error",
        "Incorrect fact",
        "Missing information",
        "Outdated information",
        "Misinterpretation",
        "Unsupported / invented claim",
        "Wrong tone / wording",
        "Other",
    ],
}


def make_workbook_b64(rows: list[list[object]]) -> str:
    """A workbook shaped like the repository matrix: headers on row 4 of ``Test Log``."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Test Log"
    ws["A1"] = "Test Log"
    ws["A2"] = "One row per real-life test."
    for col, header in enumerate(EXPECTED_HEADERS, start=1):
        ws.cell(row=4, column=col, value=header)
    for offset, row in enumerate(rows):
        for col, value in enumerate(row, start=1):
            ws.cell(row=5 + offset, column=col, value=value)
    lists = wb.create_sheet("Lists")
    for col, (header, values) in enumerate(LISTS_SHEET.items(), start=1):
        lists.cell(row=1, column=col, value=header)
        for offset, value in enumerate(values):
            lists.cell(row=2 + offset, column=col, value=value)
    buffer = io.BytesIO()
    wb.save(buffer)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class FakeGitHub:
    """Serves the GitHub contents API for one file, from memory."""

    def __init__(self, content_b64: str, blob_sha: str = "blob-1") -> None:
        self.content = content_b64
        self.blob_sha = blob_sha
        self.put_payloads: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"sha": self.blob_sha, "content": self.content, "encoding": "base64"},
            )
        if request.method == "PUT":
            payload = json.loads(request.content)
            self.put_payloads.append(payload)
            self.content = payload["content"]
            self.blob_sha = "blob-2"
            return httpx.Response(
                200,
                json={
                    "commit": {"sha": "commit-1", "html_url": "https://github.com/commit-1"},
                    "content": {
                        "sha": "blob-2",
                        "html_url": "https://github.com/TestLog.xlsx",
                    },
                },
            )
        return httpx.Response(405, json={"message": "not handled"})

    def client_factory(self, cfg: dict) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler),
            base_url=cfg["base_url"],
            timeout=15,
        )


def install_fake_github(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(update_github_log, "_github_async_client", fake.client_factory)


class FakeFilesAPI:
    """Serves the Mistral Files API from memory: uploads return sequential file ids."""

    def __init__(self, fail: bool = False) -> None:
        self.uploads: list[dict] = []
        self.deleted: list[str] = []
        self.fail = fail

    def client(self) -> SimpleNamespace:
        api = self

        class _Files:
            async def upload_async(self, *, file, purpose=None, visibility=None, expiry=None):
                if api.fail:
                    raise RuntimeError("files api unavailable")
                api.uploads.append(
                    {"file": file, "purpose": purpose, "visibility": visibility, "expiry": expiry}
                )
                return SimpleNamespace(id=f"file-{len(api.uploads)}")

            async def delete_async(self, *, file_id) -> None:
                api.deleted.append(file_id)

        return SimpleNamespace(files=_Files())


def install_fake_files(monkeypatch: pytest.MonkeyPatch, fake: FakeFilesAPI) -> None:
    monkeypatch.setattr(update_github_log, "_files_client", lambda: fake.client())
    monkeypatch.setattr(update_github_log, "_file_id_cache", {})
    monkeypatch.setattr(update_github_log, "_last_published_file_id", None)


async def start_and_run(temporal_env, input: dict) -> dict:
    async with create_test_worker(
        temporal_env,
        workflows=[UpdateGitHubLog],
        activities=[read_testlog, commit_testlog, verify_testlog],
    ):
        handle = await temporal_env.client.start_workflow(
            "update-github-log",
            input,
            id="test-update-github-log",
            task_queue="test-task-queue",
            execution_timeout=WORKFLOW_EXECUTION_TIMEOUT,
        )
        return await asyncio.wait_for(handle.result(), timeout=CLIENT_TIMEOUT)


async def test_dry_run_writes_nothing(temporal_env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub(make_workbook_b64([[1, "Invoice-Supplier Team", None, None, None, 5]]))
    install_fake_github(monkeypatch, fake)

    result = await start_and_run(
        temporal_env,
        {"entry": {"question": "Can it work?", "quality": 5}, "dry_run": True},
    )

    assert result["dry_run"] is True
    assert result["test_id"] == 2
    assert result["commit_sha"] is None
    assert fake.put_payloads == []


async def test_appends_next_test_id_and_verifies(
    temporal_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        [1, "Invoice-Supplier Team", None, None, None, 5, None, None, None, None, None, 3],
        [2, None, None, None, None, 3, None, None, None, None, None, 2],
    ]
    fake = FakeGitHub(make_workbook_b64(rows))
    install_fake_github(monkeypatch, fake)

    entry = {
        "question": "Can Mistral Vibe Work write to a github repo ?",
        "quality": 5,
        "service_team": "invoice-supplier team",
        "prompt_used": "Please answer the question.",
        "mistral_answer": "Yes, via a deployed workflow.",
        "error_category": "No error",
        "correction_level": "None",
        "outcome": "Used as generated",
        "validated_for_reuse": True,
        "prompt_review_count": 1,
    }
    result = await start_and_run(temporal_env, {"entry": entry})

    assert result["dry_run"] is False
    assert result["test_id"] == 3
    assert result["commit_sha"] == "commit-1"
    assert result["verified"] is True
    assert "quality 5" in result["message"]

    # The PUT payload holds the edited workbook: check the new row values.
    assert len(fake.put_payloads) == 1
    wb = openpyxl.load_workbook(
        io.BytesIO(base64.b64decode(fake.put_payloads[0]["content"]))
    )
    ws = wb["Test Log"]
    assert [c.value for c in ws[4]][:12] == EXPECTED_HEADERS
    stored = [c.value for c in ws[7]][:12]
    assert stored == [
        3,
        "Invoice-Supplier Team",  # canonicalised from the lower-case input
        "Can Mistral Vibe Work write to a github repo ?",
        "Please answer the question.",
        "Yes, via a deployed workflow.",
        5,
        "No error",
        "None",
        None,
        "Used as generated",
        "Yes",  # boolean written as the sheet's Yes/No vocabulary
        1,
    ]
    # The dropdowns openpyxl drops have been rebuilt from the Lists sheet.
    validations = {str(d.sqref): d.formula1 for d in ws.data_validations.dataValidation}
    assert '"CFU,Invoice-Supplier Team,Inventory Team,Budget Unit,Accounting"' in validations.values()
    assert '"Used as generated,Used after editing,Not used,Escalated / expert review"' in validations.values()


async def test_explicit_test_id_is_used(temporal_env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub(make_workbook_b64([[1, None, "old", None, None, 5]]))
    install_fake_github(monkeypatch, fake)

    result = await start_and_run(
        temporal_env, {"entry": {"question": "New question", "quality": 4, "test_id": 7}}
    )

    assert result["test_id"] == 7


async def test_duplicate_test_id_fails_the_run(
    temporal_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub(make_workbook_b64([[1, None, "old", None, None, 5]]))
    install_fake_github(monkeypatch, fake)

    with pytest.raises(Exception, match="Test ID 1 already exists"):
        await start_and_run(
            temporal_env, {"entry": {"question": "Dup", "quality": 3, "test_id": 1}}
        )

    assert fake.put_payloads == []


async def test_quality_rating_is_validated(
    temporal_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub(make_workbook_b64([[1, None, "old", None, None, 5]]))
    install_fake_github(monkeypatch, fake)

    with pytest.raises(Exception):
        await start_and_run(
            temporal_env, {"entry": {"question": "Bad rating", "quality": 9}}
        )

    assert fake.put_payloads == []


async def test_unknown_service_team_is_rejected(
    temporal_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub(make_workbook_b64([[1, None, "old", None, None, 5]]))
    install_fake_github(monkeypatch, fake)

    with pytest.raises(Exception, match="invalid_service_team"):
        await start_and_run(
            temporal_env,
            {"entry": {"question": "Who?", "quality": 5, "service_team": "Zen Team"}},
        )

    assert fake.put_payloads == []


async def test_missing_headers_fail_the_read(
    temporal_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Test Log"
    ws["A1"] = "Not the expected header"
    buffer = io.BytesIO()
    wb.save(buffer)
    fake = FakeGitHub(base64.b64encode(buffer.getvalue()).decode("ascii"))
    install_fake_github(monkeypatch, fake)

    with pytest.raises(Exception, match="unexpected_sheet_structure"):
        await start_and_run(
            temporal_env, {"entry": {"question": "?", "quality": 5}, "dry_run": True}
        )


async def test_update_existing_test_id_fills_fields_in_place(
    temporal_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [[1, None, "Old question", None, None, 4, None, None, None, None, "Yes", None]]
    fake = FakeGitHub(make_workbook_b64(rows))
    install_fake_github(monkeypatch, fake)

    entry = {
        "question": "Old question",
        "quality": 4,
        "service_team": "Accounting",
        "error_category": "No error",
        "correction_level": "None",
        "outcome": "Used as generated",
        "validated_for_reuse": True,
        "prompt_review_count": 1,
        "test_id": 1,
    }
    result = await start_and_run(temporal_env, {"entry": entry, "update_existing": True})

    assert result["dry_run"] is False
    assert result["test_id"] == 1
    assert result["commit_sha"] == "commit-1"
    assert result["verified"] is True

    assert len(fake.put_payloads) == 1
    wb = openpyxl.load_workbook(
        io.BytesIO(base64.b64decode(fake.put_payloads[0]["content"]))
    )
    ws = wb["Test Log"]
    # The existing row 5 is updated in place, not appended: same row count.
    assert ws.max_row == 5
    stored = [c.value for c in ws[5]][:12]
    assert stored == [
        1,
        "Accounting",
        "Old question",
        None,  # never provided on update: existing content kept
        None,
        4,
        "No error",
        "None",
        None,
        "Used as generated",
        "Yes",
        1,
    ]


async def test_update_requires_existing_explicit_test_id(
    temporal_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub(make_workbook_b64([[1, None, "old", None, None, 5]]))
    install_fake_github(monkeypatch, fake)

    with pytest.raises(Exception, match="not found in the log"):
        await start_and_run(
            temporal_env,
            {"entry": {"question": "Missing", "quality": 4, "test_id": 9}, "update_existing": True},
        )

    with pytest.raises(Exception, match="explicit test_id"):
        await start_and_run(
            temporal_env, {"entry": {"question": "No id", "quality": 4}, "update_existing": True}
        )

    assert fake.put_payloads == []


async def test_committed_output_carries_file_id(
    temporal_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub(make_workbook_b64([[1, None, "old", None, None, 5]]))
    install_fake_github(monkeypatch, fake)
    files = FakeFilesAPI()
    install_fake_files(monkeypatch, files)

    result = await start_and_run(temporal_env, {"entry": {"question": "New?", "quality": 5}})

    assert result["verified"] is True
    # The output points at the post-commit blob: the read published file-1, the verify published file-2.
    assert result["file_id"] == "file-2"
    assert "test-token" not in result["file_id"]
    assert len(files.uploads) == 2
    assert files.deleted == ["file-1"]
