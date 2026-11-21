"""Fixtures for the api tests: caller-credential providers and a test client."""

import pytest
from fastapi.testclient import TestClient

from utils.mistral import CallerCredentials, MistralNotConfiguredError, install_caller_credentials


@pytest.fixture
def client() -> TestClient:
    from api.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def caller() -> CallerCredentials:
    credentials = CallerCredentials(server_url="https://api.mistral.ai", token="caller-token")
    install_caller_credentials(lambda: credentials)
    yield credentials
    install_caller_credentials(None)


@pytest.fixture
def no_caller() -> None:
    install_caller_credentials(None)
    yield


@pytest.fixture
def refusing_caller() -> None:
    def provider() -> CallerCredentials:
        raise MistralNotConfiguredError("this request carries no gateway token")

    install_caller_credentials(provider)
    yield
    install_caller_credentials(None)
