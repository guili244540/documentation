"""Tests for ``GET /download``: auth, response contract, audit log, and secret hygiene.

The caller credential comes from a fake provider installed through the same
``install_caller_credentials`` seam the gateway host uses, and the workbook fetch is
replaced at the ``_fetch_workbook`` seam, so no test reaches the network. The deployed
behaviour behind both seams is the platform's: per-request caller token in, a
``download-testlog`` execution on the worker deployment out.
"""

import base64
import io
import logging

import openpyxl
import pytest

import api.download as download_module
from api.download import XLSX_MEDIA_TYPE, WorkbookFetchError


def make_workbook_bytes() -> bytes:
    workbook = openpyxl.Workbook()
    workbook.active["A1"] = "Test Log"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def fake_result(content: bytes, blob_sha: str = "blob-1") -> dict:
    return {
        "file_name": "Response Repository Matrix.xlsx",
        "base64_content": base64.b64encode(content).decode("ascii"),
        "blob_sha": blob_sha,
        "size_bytes": len(content),
        "next_test_id": 2,
        "existing_test_ids": [1],
        "download_url": "",
    }


@pytest.fixture
def workbook(monkeypatch: pytest.MonkeyPatch) -> bytes:
    """Serve a fixed workbook through the fetch seam and a fixed caller id through the other."""
    content = make_workbook_bytes()

    async def fetch() -> dict:
        return fake_result(content)

    async def caller_id() -> str | None:
        return "user-1"

    monkeypatch.setattr(download_module, "_fetch_workbook", fetch)
    monkeypatch.setattr(download_module, "_caller_id", caller_id)
    return content


def test_health_probe(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_no_caller_credential_is_401(client, no_caller) -> None:
    response = client.get("/download")
    assert response.status_code == 401
    assert response.content == b""


def test_request_without_token_is_401(client, refusing_caller, workbook) -> None:
    response = client.get("/download")
    assert response.status_code == 401
    assert response.content == b""


def test_unauthorized_caller_is_403(client, caller, workbook, monkeypatch) -> None:
    monkeypatch.setenv("APP_DOWNLOAD_ALLOWLIST", "user-a,user-b")
    response = client.get("/download")
    assert response.status_code == 403
    assert response.content == b""


def test_authorized_caller_gets_the_workbook(client, caller, workbook, monkeypatch, caplog) -> None:
    monkeypatch.setenv("APP_DOWNLOAD_ALLOWLIST", "user-1")
    with caplog.at_level(logging.INFO):
        response = client.get("/download")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(XLSX_MEDIA_TYPE)
    assert response.headers["content-disposition"] == 'attachment; filename="Response Repository Matrix.xlsx"'
    assert response.content == workbook
    openpyxl.load_workbook(io.BytesIO(response.content))  # the body is a valid workbook
    # One audit line per successful download: caller, blob sha, size (timestamp from the pipeline).
    assert "download served" in caplog.text
    assert "user-1" in caplog.text
    assert "blob-1" in caplog.text
    assert str(len(workbook)) in caplog.text


def test_any_authenticated_caller_passes_without_allowlist(client, caller, workbook) -> None:
    response = client.get("/download")
    assert response.status_code == 200
    assert response.content == workbook


def test_transient_fetch_failure_is_503(client, caller, monkeypatch) -> None:
    async def fetch() -> dict:
        raise WorkbookFetchError("github_api_unavailable", retryable=True)

    monkeypatch.setattr(download_module, "_fetch_workbook", fetch)
    response = client.get("/download")
    assert response.status_code == 503
    assert response.content == b""


def test_unexpected_result_is_502(client, caller, monkeypatch) -> None:
    async def fetch() -> dict:
        return {"file_name": "x.xlsx", "base64_content": "!!!not-base64!!!"}

    monkeypatch.setattr(download_module, "_fetch_workbook", fetch)
    response = client.get("/download")
    assert response.status_code == 502
    assert response.content == b""


def test_no_secret_material_in_response_or_logs(client, caller, workbook, monkeypatch, caplog) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "sentinel-github-token-value")
    with caplog.at_level(logging.INFO):
        response = client.get("/download")
    assert response.status_code == 200
    assert b"sentinel-github-token-value" not in response.content
    assert all("sentinel-github-token-value" not in value for value in response.headers.values())
    assert "sentinel-github-token-value" not in caplog.text
