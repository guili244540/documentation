"""Tests for the ``update-github-log`` workflow against a mocked GitHub API.

The GitHub REST API is replaced with an ``httpx.MockTransport`` through the
``_github_async_client`` seam, so no test reaches the network. The workbook
editing itself (openpyxl) runs for real, exactly as it will in the worker.
"""

import asyncio
import base64
import io
import json
from datetime import timedelta

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


def make_workbook_b64(rows: list[list[object]]) -> str:
    """A workbook shaped like TestLog.xlsx: header row + the given data rows."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(EXPECTED_HEADERS)
    for row in rows:
        ws.append(row)
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
                json={"sha": self.blob_sha, "content": self.content},
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
    fake = FakeGitHub(make_workbook_b64([[1, "Invoice-Supplier Team", None, 5, None]]))
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
        [1, "Invoice-Supplier Team", None, 5, None],
        [2, None, None, 3, None],
    ]
    fake = FakeGitHub(make_workbook_b64(rows))
    install_fake_github(monkeypatch, fake)

    entry = {
        "question": "Can Mistral Vibe Work write to a github repo ?",
        "quality": 5,
        "service_team": "Workflows Team",
        "error_category": None,
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
    ws = wb["Sheet1"]
    stored = [[c.value for c in row] for row in ws.iter_rows(max_col=5)]
    assert stored[-1] == [
        3,
        "Workflows Team",
        "Can Mistral Vibe Work write to a github repo ?",
        5,
        None,
    ]


async def test_explicit_test_id_is_used(temporal_env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub(make_workbook_b64([[1, None, "old", 5, None]]))
    install_fake_github(monkeypatch, fake)

    result = await start_and_run(
        temporal_env, {"entry": {"question": "New question", "quality": 4, "test_id": 7}}
    )

    assert result["test_id"] == 7


async def test_duplicate_test_id_fails_the_run(
    temporal_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub(make_workbook_b64([[1, None, "old", 5, None]]))
    install_fake_github(monkeypatch, fake)

    with pytest.raises(Exception, match="Test ID 1 already exists"):
        await start_and_run(
            temporal_env, {"entry": {"question": "Dup", "quality": 3, "test_id": 1}}
        )

    assert fake.put_payloads == []


async def test_quality_rating_is_validated(
    temporal_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub(make_workbook_b64([[1, None, "old", 5, None]]))
    install_fake_github(monkeypatch, fake)

    with pytest.raises(Exception):
        await start_and_run(
            temporal_env, {"entry": {"question": "Bad rating", "quality": 9}}
        )

    assert fake.put_payloads == []
