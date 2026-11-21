"""Tests for the ``resolve-download-url`` activity: override, name lookup, and list guess.

The activity is called directly (outside a workflow it runs locally), with the
deployments API replaced by an in-memory fake through the ``httpx.AsyncClient`` and
``mistral_app_token`` seams, so no test reaches the network.
"""

import httpx
import pytest

import utils.mistral
from env.app import env as app_env
from env.workflows import env as workflows_env
from worker.workflows import update_github_log
from worker.workflows.update_github_log import ResolveDownloadUrlInput, resolve_download_url


class FakeResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


def deployment_payload(name: str, endpoint: str | None) -> dict:
    payload: dict = {"name": name}
    if endpoint is not None:
        payload["managed"] = {"status": {"endpoint": endpoint}}
    return payload


class FakeDeploymentsAPI:
    """Serves the deployments API from memory and records the requested paths."""

    def __init__(self, detail: dict[str, FakeResponse], listed: FakeResponse) -> None:
        self.detail = detail
        self.listed = listed
        self.requests: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = self

        class FakeClient:
            def __init__(self, **kwargs) -> None:
                pass

            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *args) -> bool:
                return False

            async def get(self, path: str, params=None, headers=None) -> FakeResponse:
                api.requests.append(path)
                if path.startswith("/v1/workflows/deployments/"):
                    return api.detail.get(path.rsplit("/", 1)[-1], FakeResponse(404, {}))
                return api.listed

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(utils.mistral, "mistral_app_token", lambda: "app-token")


@pytest.fixture
def api_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_env, "app_name", "update-github-log")
    monkeypatch.setattr(workflows_env, "workflows_base_url", "https://api.mistral.ai")
    monkeypatch.setattr(update_github_log, "_api_module_url_cache", None)
    monkeypatch.delenv("APP_DOWNLOAD_URL", raising=False)


async def test_override_wins_without_any_http(api_env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDeploymentsAPI({}, FakeResponse(200, {"deployments": []}))
    fake.install(monkeypatch)
    monkeypatch.setenv("APP_DOWNLOAD_URL", "https://override.example/download")

    resolution = await resolve_download_url(ResolveDownloadUrlInput())

    assert resolution.url == "https://override.example/download"
    assert resolution.source == "override"
    assert "APP_DOWNLOAD_URL" in resolution.detail
    assert fake.requests == []


async def test_found_by_deployment_name(api_env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDeploymentsAPI(
        {"update-github-log-api": FakeResponse(200, deployment_payload("update-github-log-api", "https://ingress.example"))},
        FakeResponse(200, {"deployments": []}),
    )
    fake.install(monkeypatch)

    resolution = await resolve_download_url(ResolveDownloadUrlInput())

    assert resolution.url == "https://ingress.example/download"
    assert resolution.source == "deployment-name"
    assert "update-github-log-api" in resolution.detail
    assert fake.requests == ["/v1/workflows/deployments/update-github-log-api"]


async def test_guessed_from_the_deployments_list_when_name_lookup_misses(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDeploymentsAPI(
        {},
        FakeResponse(
            200,
            {
                "deployments": [
                    deployment_payload("update-github-log-worker", "https://worker.example"),
                    deployment_payload("update-github-log-api", "https://ingress.example"),
                ]
            },
        ),
    )
    fake.install(monkeypatch)
    monkeypatch.setattr(app_env, "app_name", "some-other-name")

    resolution = await resolve_download_url(ResolveDownloadUrlInput())

    assert resolution.url == "https://ingress.example/download"
    assert resolution.source == "deployments-list"
    assert "chose 'update-github-log-api'" in resolution.detail
    assert "/v1/workflows/deployments" in fake.requests


async def test_several_api_deployments_pick_deterministically(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDeploymentsAPI(
        {},
        FakeResponse(
            200,
            {
                "deployments": [
                    deployment_payload("zeta-api", "https://zeta.example"),
                    deployment_payload("alpha-api", "https://alpha.example"),
                ]
            },
        ),
    )
    fake.install(monkeypatch)

    resolution = await resolve_download_url(ResolveDownloadUrlInput())

    assert resolution.url == "https://alpha.example/download"
    assert resolution.source == "deployments-list"
    assert "chose 'alpha-api'" in resolution.detail


async def test_no_api_deployment_means_empty(api_env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDeploymentsAPI(
        {},
        FakeResponse(200, {"deployments": [deployment_payload("update-github-log-worker", "https://worker.example")]}),
    )
    fake.install(monkeypatch)

    resolution = await resolve_download_url(ResolveDownloadUrlInput())

    assert resolution.url == ""
    assert resolution.source == "unresolved"
    assert "none named *-api" in resolution.detail


async def test_non_https_endpoint_is_ignored(api_env, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = deployment_payload("update-github-log-api", "http://insecure.example")
    fake = FakeDeploymentsAPI(
        {"update-github-log-api": FakeResponse(200, payload)},
        FakeResponse(200, {"deployments": [payload]}),
    )
    fake.install(monkeypatch)

    resolution = await resolve_download_url(ResolveDownloadUrlInput())

    assert resolution.url == ""
    assert "http://insecure.example" not in resolution.detail


async def test_missing_app_credential_means_empty(api_env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDeploymentsAPI({}, FakeResponse(200, {"deployments": []}))
    fake.install(monkeypatch)

    def no_token() -> str:
        raise utils.mistral.MistralNotConfiguredError("no app credential")

    monkeypatch.setattr(utils.mistral, "mistral_app_token", no_token)

    resolution = await resolve_download_url(ResolveDownloadUrlInput())

    assert resolution.url == ""
    assert "MistralNotConfiguredError" in resolution.detail
    assert fake.requests == []


async def test_result_is_cached(api_env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDeploymentsAPI(
        {"update-github-log-api": FakeResponse(200, deployment_payload("update-github-log-api", "https://ingress.example"))},
        FakeResponse(200, {"deployments": []}),
    )
    fake.install(monkeypatch)

    first = await resolve_download_url(ResolveDownloadUrlInput())
    second = await resolve_download_url(ResolveDownloadUrlInput())

    assert first.url == second.url == "https://ingress.example/download"
    assert first.source == second.source == "deployment-name"
    assert fake.requests == ["/v1/workflows/deployments/update-github-log-api"]
