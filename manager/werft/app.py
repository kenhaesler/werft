"""Composition root. The only module allowed to import from every layer."""

from fastapi import FastAPI

from werft.api.routes import router


def create_app() -> FastAPI:
    app = FastAPI(title="werft-manager")
    app.include_router(router)
    return app
