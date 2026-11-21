"""The ASGI app of the ``api`` module: one authenticated route, ``GET /download``.

The module is served behind the platform's authenticated ingress, which forwards the
route declared for this module in ``mistral.json`` and puts the caller's token on each
request; ``install_gateway_credentials`` turns that into the caller credential the
handler authenticates with. The worker module stays a pure workflow worker: no HTTP
listener lives in it.
"""

from fastapi import FastAPI
from mistralai_capabilities.apps.credentials import install_gateway_credentials

from api.download import router
from env.logging import env as logging_env
from utils.logging import configure_logging


def create_app() -> FastAPI:
    configure_logging(log_level=logging_env.log_level, log_format=logging_env.log_format)
    app = FastAPI(title="update-github-log-api")
    # Installs the gateway token middleware and the caller-credential provider where a
    # gateway is in front (deployed app, `mistral apps dev`); without one, no provider is
    # installed and every request stays unauthenticated (401).
    install_gateway_credentials(app)
    app.include_router(router)
    return app


app = create_app()
