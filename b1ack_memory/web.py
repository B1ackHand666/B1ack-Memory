from __future__ import annotations

import hmac
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from .llm import LlmError
from .plugin import get_service
from .service import MemoryService

STATIC = Path(__file__).with_name("static")
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _local_only(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="B1ack Memory WebUI is local-only")


async def _friendly_errors():
    try:
        yield
    except HTTPException:
        raise
    except (ValueError, KeyError, FileNotFoundError, LlmError, RuntimeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def create_router(
    service: MemoryService | None = None, *, local_only: bool = True
) -> APIRouter:
    memory = service or get_service(start_background=True)
    dependencies = [Depends(_friendly_errors)]
    if local_only:
        dependencies.insert(0, Depends(_local_only))
    router = APIRouter(dependencies=dependencies)

    def mutation_token(x_b1ack_memory_token: str = Header(default="")) -> None:
        if not hmac.compare_digest(x_b1ack_memory_token, memory.mutation_token):
            raise HTTPException(status_code=403, detail="Invalid mutation token")

    def mutate() -> list[Any]:
        return [Depends(mutation_token)]

    @router.get("/ui/", response_class=HTMLResponse)
    def ui() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @router.get("/ui/app.js")
    def javascript() -> FileResponse:
        return FileResponse(STATIC / "app.js", media_type="application/javascript")

    @router.get("/ui/style.css")
    def stylesheet() -> FileResponse:
        return FileResponse(STATIC / "style.css", media_type="text/css")

    @router.get("/ui-bundle")
    def ui_bundle() -> dict[str, str]:
        """Return standalone UI assets for an authenticated Dashboard embed."""
        return {
            "html": (STATIC / "index.html").read_text(encoding="utf-8"),
            "css": (STATIC / "style.css").read_text(encoding="utf-8"),
            "js": (STATIC / "app.js").read_text(encoding="utf-8"),
        }

    @router.get("/bootstrap")
    def bootstrap() -> dict[str, Any]:
        return {"token": memory.mutation_token, "status": memory.status()}

    @router.get("/status")
    def status() -> dict[str, Any]:
        return memory.status()

    @router.get("/settings")
    def settings() -> dict[str, Any]:
        values = memory.db.get_settings()
        values["secrets"] = {
            name: memory.secrets.masked_status(name)
            for name in ("llm_api_key", "embedding_api_key")
        }
        return values

    @router.get("/memories")
    def memories(status: str = "active", limit: int = 500) -> list[dict[str, Any]]:
        return memory.list_memories(status=status, limit=min(max(limit, 1), 5000))

    @router.get("/candidates")
    def candidates(status: str = "pending", limit: int = 500) -> list[dict[str, Any]]:
        return memory.list_candidates(status=status, limit=min(max(limit, 1), 5000))

    @router.get("/dream-runs")
    def dream_runs(limit: int = 100) -> list[dict[str, Any]]:
        return memory.list_dream_runs(min(max(limit, 1), 1000))

    @router.get("/recall-traces")
    def recall_traces(limit: int = 200) -> list[dict[str, Any]]:
        return memory.recall_traces(min(max(limit, 1), 2000))

    @router.get("/model-calls")
    def model_calls(limit: int = 100) -> list[dict[str, Any]]:
        return memory.model_calls(min(max(limit, 1), 1000))

    @router.get("/backups")
    def backups() -> list[dict[str, Any]]:
        return memory.list_backups()

    @router.get("/export", response_class=PlainTextResponse)
    def export() -> str:
        return memory.export_jsonl()

    @router.post("/settings/{section}", dependencies=mutate())
    def save_settings(section: str, value: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.save_settings(section, value)

    @router.post("/secrets/{name}", dependencies=mutate())
    def save_secret(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.set_secret(name, body.get("value"))

    @router.post("/model/test", dependencies=mutate())
    def test_model(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return memory.test_model(str(body.get("kind", "llm")))

    @router.post("/dream/run", dependencies=mutate())
    def run_dream(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return memory.run_dream(dry_run=bool(body.get("dry_run", False)))

    @router.post("/memories", dependencies=mutate())
    def add_memory(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.remember(
            str(body.get("content", "")),
            kind=str(body.get("kind", "fact")),
            allow_sensitive=bool(body.get("allow_sensitive", False)),
        )

    @router.patch("/memories/{record_id}", dependencies=mutate())
    def edit_memory(record_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.update_memory(record_id, str(body["content"]), str(body.get("kind", "fact")))

    @router.post("/memories/{record_id}/trash", dependencies=mutate())
    def trash_memory(record_id: str) -> dict[str, bool]:
        memory.trash_memory(record_id)
        return {"ok": True}

    @router.post("/memories/{record_id}/restore", dependencies=mutate())
    def restore_memory(record_id: str) -> dict[str, bool]:
        memory.restore_memory(record_id)
        return {"ok": True}

    @router.delete("/memories/{record_id}", dependencies=mutate())
    def purge_memory(record_id: str) -> dict[str, Any]:
        return memory.purge_memory(record_id)

    @router.post("/candidates/{candidate_id}/promote", dependencies=mutate())
    def promote(candidate_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return memory.promote_candidate(candidate_id, body.get("content"))

    @router.post("/candidates/{candidate_id}/reject", dependencies=mutate())
    def reject(candidate_id: str) -> dict[str, bool]:
        memory.reject_candidate(candidate_id)
        return {"ok": True}

    @router.post("/backup", dependencies=mutate())
    def backup() -> dict[str, str]:
        return {"name": memory.create_backup().name}

    @router.post("/backups/{name}/restore", dependencies=mutate())
    def restore_backup(name: str) -> dict[str, bool]:
        memory.restore_backup(name)
        return {"ok": True}

    @router.post("/maintenance", dependencies=mutate())
    def maintenance(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return memory.maintenance(
            vacuum=bool(body.get("vacuum", False)), cleanup=bool(body.get("cleanup", False))
        )

    @router.post("/rebuild", dependencies=mutate())
    def rebuild(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return memory.rebuild_derived(embeddings=bool(body.get("embeddings", False)))

    return router


def create_app(service: MemoryService | None = None) -> FastAPI:
    app = FastAPI(title="B1ack Memory", docs_url=None, redoc_url=None)
    app.include_router(create_router(service), prefix="/api")
    return app
