"""Tests for the ``publish-testlog-file`` activity against a fake Files API.

The activity is called directly (outside a workflow it runs locally) with the
``_files_client`` seam replaced by an in-memory fake, so no test reaches the network.
"""

import base64

import pytest

import utils.mistral
from test_update_github_log import FakeFilesAPI, install_fake_files
from worker.workflows import update_github_log
from worker.workflows.update_github_log import (
    PublishedFile,
    PublishTestLogFileInput,
    publish_testlog_file,
)

CONTENT = b"fake-xlsx-bytes"


def make_input(blob_sha: str = "blob-1") -> PublishTestLogFileInput:
    return PublishTestLogFileInput(
        base64_content=base64.b64encode(CONTENT).decode("ascii"),
        blob_sha=blob_sha,
        file_name="Response Repository Matrix.xlsx",
    )


async def test_upload_returns_file_id_and_signed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    files = FakeFilesAPI()
    install_fake_files(monkeypatch, files)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    published = await publish_testlog_file(make_input())

    assert published.file_id == "file-1"
    assert published.signed_url == "https://files.test/signed/file-1?expiry=1"
    assert "test-token" not in published.file_id
    assert "test-token" not in published.signed_url
    assert len(files.uploads) == 1
    upload = files.uploads[0]
    assert upload["file"]["file_name"] == "Response Repository Matrix.xlsx"
    assert upload["file"]["content"] == CONTENT
    assert upload["purpose"] == "ocr"
    assert upload["visibility"] == "workspace"
    assert upload["expiry"] is None  # not sent: the proven upload call omits it
    assert files.signed_urls == [{"file_id": "file-1", "expiry": 1}]  # shortest accepted TTL
    assert list(update_github_log._published_registry) == ["file-1"]  # registered for the purge


async def test_unchanged_blob_is_not_reuploaded(monkeypatch: pytest.MonkeyPatch) -> None:
    files = FakeFilesAPI()
    install_fake_files(monkeypatch, files)

    first = await publish_testlog_file(make_input("blob-1"))
    second = await publish_testlog_file(make_input("blob-1"))

    assert first == second
    assert first.file_id == "file-1"
    assert len(files.uploads) == 1  # cache hit: no re-upload
    assert files.retrieved == ["file-1"]  # but the existence check ran
    assert len(files.signed_urls) == 2  # and the signed URL was re-minted (full TTL on the hit)


async def test_cache_hit_reuploads_when_the_file_was_deleted_remotely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = FakeFilesAPI()
    install_fake_files(monkeypatch, files)

    first = await publish_testlog_file(make_input("blob-1"))
    assert first.file_id == "file-1"

    files.delete_remotely("file-1")  # manual deletion outside the worker

    second = await publish_testlog_file(make_input("blob-1"))
    assert second.file_id == "file-2"  # re-uploaded, not the dead file-1
    assert len(files.uploads) == 2
    assert files.retrieved == ["file-1"]
    assert files.signed_urls[-1]["file_id"] == "file-2"
    assert update_github_log._file_id_cache["blob-1"].file_id == "file-2"
    assert list(update_github_log._published_registry) == ["file-2"]
    assert update_github_log._last_published_file_id == "file-2"


async def test_cache_hit_with_transient_retrieve_error_keeps_the_cached_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = FakeFilesAPI()
    install_fake_files(monkeypatch, files)

    first = await publish_testlog_file(make_input("blob-1"))
    files.fail_retrieve = True  # existence check now fails transiently

    second = await publish_testlog_file(make_input("blob-1"))
    assert second.file_id == first.file_id == "file-1"
    assert len(files.uploads) == 1  # no re-upload on a transient error


async def test_changed_blob_uploads_again_and_deletes_the_superseded_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = FakeFilesAPI()
    install_fake_files(monkeypatch, files)

    first = await publish_testlog_file(make_input("blob-1"))
    second = await publish_testlog_file(make_input("blob-2"))

    assert first.file_id == "file-1"
    assert second.file_id == "file-2"
    assert len(files.uploads) == 2
    assert files.deleted == ["file-1"]
    # The superseded upload left the purge registry; the new one is registered.
    assert list(update_github_log._published_registry) == ["file-2"]


async def test_signed_url_failure_keeps_the_file_id(monkeypatch: pytest.MonkeyPatch) -> None:
    files = FakeFilesAPI(fail_sign=True)
    install_fake_files(monkeypatch, files)

    published = await publish_testlog_file(make_input())

    assert published.file_id == "file-1"
    assert published.signed_url == ""
    assert files.signed_urls == []
    assert list(update_github_log._published_registry) == ["file-1"]


async def test_upload_failure_returns_empty_and_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = FakeFilesAPI(fail=True)
    install_fake_files(monkeypatch, files)

    published = await publish_testlog_file(make_input())
    assert published == PublishedFile()
    assert update_github_log._file_id_cache == {}
    assert update_github_log._published_registry == {}

    # A later attempt with the API back up succeeds and is cached.
    files.fail = False
    published = await publish_testlog_file(make_input())
    assert published.file_id == "file-1"
    assert update_github_log._file_id_cache["blob-1"].file_id == "file-1"


def test_files_client_prefers_the_files_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    spent: list[str] = []
    monkeypatch.setattr(
        utils.mistral, "mistral_client_with_api_key", lambda key: spent.append(key) or "key-client"
    )
    monkeypatch.setattr(utils.mistral, "mistral_client", lambda: "sa-token-client")

    monkeypatch.setenv("FILES_API_KEY", "files-key")
    assert update_github_log._files_client() == "key-client"
    assert spent == ["files-key"]

    monkeypatch.delenv("FILES_API_KEY")
    assert update_github_log._files_client() == "sa-token-client"
