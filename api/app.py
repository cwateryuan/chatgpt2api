from __future__ import annotations

from contextlib import asynccontextmanager
from threading import Event

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from api import accounts, ai, image_tasks, register, system
from api.errors import install_exception_handlers
from api.support import (
    resolve_web_asset,
    start_chat_keepalive_worker,
    start_full_account_refresh_worker,
    start_limited_account_watcher,
)
from services.backup_service import backup_service
from services.config import config
from services.debug_memory import start_memory_diagnostic_scheduler
from services.image_service import start_image_cleanup_scheduler
from services.memory import start_memory_trim_scheduler
from services.memory_recycle import start_memory_recycle_scheduler
from services.register_service import register_service
from services.request_activity import RequestActivityMiddleware
from services.runtime_config import configure_thread_stack_size, configure_threadpool_tokens


def create_app() -> FastAPI:
    app_version = config.app_version

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        configure_thread_stack_size()
        configure_threadpool_tokens()
        stop_event = Event()
        thread = start_limited_account_watcher(stop_event)
        full_refresh_thread = start_full_account_refresh_worker(stop_event)
        chat_keepalive_thread = start_chat_keepalive_worker(stop_event)
        cleanup_thread = start_image_cleanup_scheduler(stop_event)
        memory_trim_thread = start_memory_trim_scheduler(stop_event)
        memory_diag_thread = start_memory_diagnostic_scheduler(stop_event)
        memory_recycle_thread = start_memory_recycle_scheduler(stop_event)
        register_service.start_supervisor()
        backup_service.start()
        config.cleanup_old_images()
        try:
            yield
        finally:
            stop_event.set()
            thread.join(timeout=1)
            full_refresh_thread.join(timeout=1)
            chat_keepalive_thread.join(timeout=1)
            cleanup_thread.join(timeout=1)
            memory_trim_thread.join(timeout=1)
            memory_diag_thread.join(timeout=1)
            memory_recycle_thread.join(timeout=1)
            register_service.stop_supervisor(timeout=1)
            backup_service.stop()

    app = FastAPI(title="chatgpt2api", version=app_version, lifespan=lifespan)
    install_exception_handlers(app)
    app.add_middleware(RequestActivityMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(ai.create_router())
    app.include_router(accounts.create_router())
    app.include_router(image_tasks.create_router())
    app.include_router(register.create_router())
    app.include_router(system.create_router(app_version))

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_web(full_path: str):
        asset = resolve_web_asset(full_path)
        if asset is not None:
            return FileResponse(asset)
        if full_path.strip("/").startswith("_next/"):
            raise HTTPException(status_code=404, detail="Not Found")
        fallback = resolve_web_asset("")
        if fallback is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(fallback)

    return app
