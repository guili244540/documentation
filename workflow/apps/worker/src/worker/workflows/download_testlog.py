"""Return the current ``Response Repository Matrix.xlsx`` so a chat can offer it as a download.

Read-only sibling of ``update-github-log``: it GETs the workbook through the same
``read_testlog`` activity (same workspace credential, same rate limit, same structure
checks) and returns the untouched file bytes as base64. Anyone who can execute the
deployment therefore retrieves the workbook without a GitHub connection of their own,
which keeps working when the repository is private: the token lives in the workspace
secret ``githubtoken`` bound as ``GITHUB_TOKEN``, never in the conversation.

The chat agent that starts the execution decodes ``base64_content`` and publishes it
as a downloadable artifact server-side; reusing ``read_testlog`` guarantees the
returned bytes are a workbook the log updater would accept. The file name comes from
the activity too: deterministic workflow code must not read the environment.
"""

from __future__ import annotations

import base64

import mistralai.workflows as workflows
from pydantic import BaseModel

from worker.workflows.update_github_log import ReadTestLogInput, read_testlog


class DownloadTestLogInput(BaseModel):
    """``download-testlog`` takes no business input; its configuration is worker env."""


class DownloadTestLogOutput(BaseModel):
    file_name: str
    base64_content: str
    blob_sha: str
    size_bytes: int
    next_test_id: int
    existing_test_ids: list[int]


@workflows.workflow.define(name="download-testlog")
class DownloadTestLog:
    @workflows.workflow.entrypoint
    async def run(self, data: DownloadTestLogInput) -> DownloadTestLogOutput:
        state = await read_testlog(ReadTestLogInput())
        content = base64.b64decode(state.base64_content)
        return DownloadTestLogOutput(
            file_name=state.file_name,
            base64_content=state.base64_content,
            blob_sha=state.blob_sha,
            size_bytes=len(content),
            next_test_id=state.next_test_id,
            existing_test_ids=state.existing_test_ids,
        )
