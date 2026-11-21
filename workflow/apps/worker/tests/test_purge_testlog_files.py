"""Tests for the ``purge-old-testlog-files`` activity against a fake Files API.

The activity is called directly (outside a workflow it runs locally) with the
``_files_client`` seam replaced by an in-memory fake and ``_now`` replaced by a
fake clock, so no test reaches the network and no real waiting happens.
"""

import base64

import pytest

from test_update_github_log import FakeFilesAPI, install_fake_files
from worker.workflows import update_github_log
from worker.workflows.update_github_log import (
    PURGE_MAX_AGE,
    PublishedFile,
    PublishTestLogFileInput,
    PurgeOldTestLogFilesInput,
    publish_testlog_file,
    purge_old_testlog_files,
)

CONTENT = b"fake-xlsx-bytes"


def make_input(blob_sha: str = "blob-1") -> PublishTestLogFileInput:
    return PublishTestLogFileInput(
        base64_content=base64.b64encode(CONTENT).decode("ascii"),
        blob_sha=blob_sha,
        file_name="Response Repository Matrix.xlsx",
    )


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def test_purge_deletes_only_registered_entries_older_than_5_min(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = FakeFilesAPI()
    install_fake_files(monkeypatch, files)
    clock = Clock()
    monkeypatch.setattr(update_github_log, "_now", clock)
    stale_at = clock.now - PURGE_MAX_AGE.total_seconds() - 1
    update_github_log._published_registry.update({"file-old": stale_at, "file-new": clock.now})
    files.existing.update({"file-old", "file-new"})  # both exist remotely

    deleted = await purge_old_testlog_files(PurgeOldTestLogFilesInput())

    assert deleted == 1
    # Only the registered stale id is deleted — a foreign same-named file would never be listed.
    assert files.deleted == ["file-old"]
    assert update_github_log._published_registry == {"file-new": clock.now}


async def test_purge_with_empty_registry_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = FakeFilesAPI()
    install_fake_files(monkeypatch, files)
    monkeypatch.setattr(update_github_log, "_now", Clock())

    # Post-restart state: the registry is empty, so the previous upload is orphaned
    # until manual deletion and the purge must not guess via files.list.
    assert await purge_old_testlog_files(PurgeOldTestLogFilesInput()) == 0
    assert files.deleted == []


async def test_purge_reconciles_the_cache_so_a_dead_file_id_is_reuploaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = FakeFilesAPI()
    install_fake_files(monkeypatch, files)
    clock = Clock()
    monkeypatch.setattr(update_github_log, "_now", clock)

    published = await publish_testlog_file(make_input("blob-1"))
    assert published.file_id == "file-1"
    clock.now += PURGE_MAX_AGE.total_seconds() + 1

    assert await purge_old_testlog_files(PurgeOldTestLogFilesInput()) == 1
    assert files.deleted == ["file-1"]
    assert update_github_log._published_registry == {}
    assert update_github_log._file_id_cache == {}
    assert update_github_log._last_published_file_id is None

    # A later read of the same blob re-uploads instead of returning the purged file-1.
    again = await publish_testlog_file(make_input("blob-1"))
    assert again.file_id == "file-2"
    assert len(files.uploads) == 2


async def test_purge_treats_a_404_delete_as_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = FakeFilesAPI()
    install_fake_files(monkeypatch, files)
    clock = Clock()
    monkeypatch.setattr(update_github_log, "_now", clock)
    update_github_log._published_registry["file-old"] = clock.now - PURGE_MAX_AGE.total_seconds() - 1
    update_github_log._file_id_cache["blob-1"] = PublishedFile(
        file_id="file-old", signed_url="https://files.test/signed/file-old?expiry=1"
    )
    files.delete_remotely("file-old")  # deleted manually: the worker's delete will 404

    assert await purge_old_testlog_files(PurgeOldTestLogFilesInput()) == 1
    assert files.deleted == []  # nothing was actually deleted remotely
    assert update_github_log._published_registry == {}
    assert update_github_log._file_id_cache == {}


async def test_failed_delete_keeps_the_entry_for_the_next_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = FakeFilesAPI(fail_delete=True)
    install_fake_files(monkeypatch, files)
    clock = Clock()
    monkeypatch.setattr(update_github_log, "_now", clock)
    update_github_log._published_registry["file-old"] = clock.now - PURGE_MAX_AGE.total_seconds() - 1
    files.existing.add("file-old")

    assert await purge_old_testlog_files(PurgeOldTestLogFilesInput()) == 0
    assert files.deleted == []  # the delete was attempted and failed
    assert list(update_github_log._published_registry) == ["file-old"]
