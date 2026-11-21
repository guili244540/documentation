"""``GET /download``: the current test-log workbook as a direct file download.

Authentication is the platform ingress's: the gateway in front of this module puts the
caller's token on each request, and ``caller_credentials()`` resolves it per request. No
caller credential means ``401``; an authenticated caller the allowlist does not name
means ``403``. The GitHub credential stays server-side: the workbook is fetched by
starting a ``download-testlog`` execution on the worker deployment and awaiting its
result, so the read reuses the workflow's rate-limited activity and the token never
appears in a response, a log line, or client-visible output. Every successful download
is audit-logged (caller, blob sha, size; the timestamp comes from the logging pipeline).
"""

from __future__ import annotations

import base64
import os

import structlog
from fastapi import APIRouter, Response
from mistralai_capabilities.workflows.client import get_client

from env.workflows import env as workflows_env
from utils.mistral import (
    MistralNotConfiguredError,
    caller_credentials,
    caller_mistral_client,
)

logger = structlog.get_logger("api.download")

router = APIRouter()

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOWNLOAD_TIMEOUT_SECONDS = 60


class WorkbookFetchError(Exception):
    """The workbook could not be fetched; ``retryable`` maps to 503 vs 502."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _allowlist() -> set[str]:
    """Caller ids allowed to download, from ``APP_DOWNLOAD_ALLOWLIST``; unset means any authenticated caller."""
    raw = os.environ.get("APP_DOWNLOAD_ALLOWLIST", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


async def _caller_id() -> str | None:
    """The caller's principal id, or None when it cannot be resolved. Never raises."""
    try:
        whoami = await caller_mistral_client().users.whoami_async()
        return getattr(getattr(whoami, "principal", None), "id", None)
    except Exception:
        return None


async def _fetch_workbook() -> dict:
    """Start a ``download-testlog`` execution on the worker deployment and return its result.

    This keeps one code path that owns "how the workbook is read": the GitHub read happens
    in the workflow's ``read_testlog`` activity, under its rate limit, with the worker's
    service credential.
    """
    client = await get_client()
    run = await client.workflows.execute_workflow_async(
        workflow_identifier="download-testlog",
        input={},
        deployment_name=workflows_env.deployment_name,
        wait_for_result=True,
        timeout_seconds=DOWNLOAD_TIMEOUT_SECONDS,
    )
    result = getattr(run, "result", None)
    if not isinstance(result, dict) or "base64_content" not in result:
        raise WorkbookFetchError("download-testlog returned no result", retryable=True)
    return result


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe for the container's own healthcheck; not a declared app route."""
    return {"status": "ok"}


@router.get("/download")
async def download() -> Response:
    try:
        credentials = caller_credentials()
    except MistralNotConfiguredError:
        credentials = None
    if credentials is None:
        logger.info("download rejected: no caller credential")
        return Response(status_code=401)
    caller = await _caller_id()
    allowlist = _allowlist()
    if allowlist and caller not in allowlist:
        logger.info("download forbidden", caller=caller)
        return Response(status_code=403)
    try:
        result = await _fetch_workbook()
        content = base64.b64decode(result["base64_content"])
        file_name = str(result.get("file_name") or "workbook.xlsx")
        blob_sha = str(result.get("blob_sha") or "")
    except WorkbookFetchError as error:
        logger.error("download fetch failed", error=str(error), retryable=error.retryable)
        return Response(status_code=503 if error.retryable else 502)
    except (ValueError, KeyError, TypeError):
        logger.error("download result was not a workbook payload")
        return Response(status_code=502)
    logger.info("download served", caller=caller, blob_sha=blob_sha, size_bytes=len(content))
    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )
