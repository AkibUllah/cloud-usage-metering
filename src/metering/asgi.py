"""ASGI entrypoint: `uvicorn metering.asgi:app`."""

from .main import create_app

app = create_app()
