"""Tests for the ``download-testlog`` workflow against a mocked GitHub API.

The fixtures of ``test_update_github_log`` are reused, so this file exercises the
same mocked contents endpoint through the ``_github_async_client`` seam: the GET
runs for real, nothing reaches the network, and no PUT ever happens.
"""

import asyncio
import base64
import io
from datetime import timedelta

import openpyxl
import pytest

from mistralai.workflows.testing import create_test_worker

from test_update_github_log import (
    CLIENT_TIMEOUT,
    WORKFLOW_EXECUTION_TIMEOUT,
    FakeFilesAPI,
    FakeGitHub,
    install_fake_files,
    install_fake_github,
    make_workbook_b64,
)
from worker.workflows.download_testlog import DownloadTestLog
from worker.workflows.update_github_log import EXPECTED_HEADERS, read_testlog


async def start_and_run(temporal_env, input: dict) -> dict:
    async with create_test_worker(
        temporal_env,
        workflows=[DownloadTestLog],
        activities=[read_testlog],
    ):
        handle = await temporal_env.client.start_workflow(
            "download-testlog",
            input,
            id="test-download-testlog",
            task_queue="test-task-queue",
            execution_timeout=WORKFLOW_EXECUTION_TIMEOUT,
        )
        return await asyncio.wait_for(handle.result(), timeout=CLIENT_TIMEOUT)


async def test_download_returns_current_workbook(
    temporal_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        [1, "Accounting", "Old question", None, None, 4, "No error", "None", None, "Used as generated", "Yes", 2],
    ]
    fake = FakeGitHub(make_workbook_b64(rows))
    install_fake_github(monkeypatch, fake)

    result = await start_and_run(temporal_env, {})

    assert result["file_name"] == "Response Repository Matrix.xlsx"
    assert result["blob_sha"] == "blob-1"
    assert result["next_test_id"] == 2
    assert result["existing_test_ids"] == [1]

    # The returned bytes are the untouched workbook: it reloads with the log sheet.
    workbook = openpyxl.load_workbook(io.BytesIO(base64.b64decode(result["base64_content"])))
    ws = workbook["Test Log"]
    assert [c.value for c in ws[4]][:12] == EXPECTED_HEADERS
    assert result["size_bytes"] == len(base64.b64decode(result["base64_content"]))
    assert fake.put_payloads == []


async def test_download_writes_nothing(
    temporal_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub(make_workbook_b64([[1, None, "old", None, None, 5]]))
    install_fake_github(monkeypatch, fake)

    await start_and_run(temporal_env, {})

    assert fake.put_payloads == []


async def test_download_output_carries_file_id(
    temporal_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub(make_workbook_b64([[1, None, "old", None, None, 5]]))
    install_fake_github(monkeypatch, fake)
    files = FakeFilesAPI()
    install_fake_files(monkeypatch, files)

    result = await start_and_run(temporal_env, {})

    assert result["file_id"] == "file-1"
    assert result["signed_url"] == "https://files.test/signed/file-1?expiry=1"
    assert "test-token" not in result["file_id"]
    assert "test-token" not in result["signed_url"]
    assert len(files.uploads) == 1
    upload = files.uploads[0]
    assert upload["file"]["file_name"] == "Response Repository Matrix.xlsx"
    assert upload["purpose"] == "ocr"
    assert upload["visibility"] == "workspace"
    assert files.signed_urls == [{"file_id": "file-1", "expiry": 1}]


async def test_download_output_has_empty_file_id_when_upload_fails(
    temporal_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub(make_workbook_b64([[1, None, "old", None, None, 5]]))
    install_fake_github(monkeypatch, fake)
    install_fake_files(monkeypatch, FakeFilesAPI(fail=True))

    result = await start_and_run(temporal_env, {})

    assert result["file_id"] == ""
    assert result["signed_url"] == ""
    assert result["base64_content"]  # the inline artifact path still works
