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

    @router.get("/search")
    def search(query: str, limit: int = 20, project_id: str | None = None) -> list[dict[str, Any]]:
        return [
            item.to_dict()
            for item in memory.search(
                query, limit=min(max(limit, 1), 20), injected=False, project_id=project_id
            )
        ]

    @router.get("/projects")
    def projects(status: str | None = None) -> list[dict[str, Any]]:
        return memory.list_projects(status=status or None)

    @router.get("/projects/{project_id}")
    def project(project_id: str) -> dict[str, Any]:
        return memory.get_project(project_id)

    @router.get("/projects/{project_id}/context-preview")
    def project_context(project_id: str, query: str = "", session_id: str = "") -> dict[str, Any]:
        return memory.context_preview(query, project_id=project_id, session_id=session_id)

    @router.get("/projects/{project_id}/export")
    def export_project(project_id: str) -> dict[str, Any]:
        return memory.export_project(project_id)

    @router.get("/subjects")
    def subjects(subject_type: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        return memory.list_subjects(subject_type=subject_type or None, status=status or None)

    @router.get("/subjects/{subject_id}")
    def subject(subject_id: str) -> dict[str, Any]:
        return memory.workspace.get_subject(subject_id)

    @router.get("/work-items")
    def work_items(
        status: str | None = None,
        subject_id: str | None = None,
        item_type: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        return memory.list_work_items(
            status=status or None,
            subject_id=subject_id or None,
            item_type=item_type or None,
            limit=min(max(limit, 1), 5000),
        )

    @router.get("/summaries/{scope}/{subject_id}")
    def summaries(scope: str, subject_id: str) -> list[dict[str, Any]]:
        return memory.summary_versions(scope, None if subject_id == "global" else subject_id)

    @router.get("/context/preview")
    def context_preview(
        query: str,
        project_id: str | None = None,
        session_id: str = "",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        return memory.context_preview(
            query, project_id=project_id or None, session_id=session_id, workspace=workspace or None
        )

    @router.get("/maintenance/storage")
    def storage_health() -> dict[str, Any]:
        return memory.workspace.storage_health()

    @router.get("/candidates")
    def candidates(
        status: str = "pending",
        admission_state: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        return memory.list_candidates(
            status=status,
            admission_state=admission_state,
            limit=min(max(limit, 1), 5000),
        )

    @router.get("/reviews")
    def reviews(
        status: str | None = "open",
        issue_type: str | None = None,
        queue: str | None = None,
        subject_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        return memory.list_reviews(
            status=status or None,
            issue_type=issue_type or None,
            queue=queue or None,
            subject_id=subject_id or None,
            limit=min(max(limit, 1), 5000),
        )

    @router.get("/dream-runs")
    def dream_runs(limit: int = 100) -> list[dict[str, Any]]:
        return memory.list_dream_runs(min(max(limit, 1), 1000))

    @router.get("/recall-traces")
    def recall_traces(limit: int = 200) -> list[dict[str, Any]]:
        return memory.recall_traces(min(max(limit, 1), 2000))

    @router.get("/model-calls")
    def model_calls(limit: int = 100) -> list[dict[str, Any]]:
        return memory.model_calls(min(max(limit, 1), 1000))

    @router.get("/analytics/memory-flow")
    def memory_flow(range: str = "30d") -> dict[str, Any]:
        return memory.memory_flow(range)

    @router.get("/lineage/candidate/{candidate_id}")
    def candidate_lineage(candidate_id: str) -> dict[str, Any]:
        return memory.candidate_lineage(candidate_id)

    @router.get("/lineage/memory/{memory_id}")
    def memory_lineage(memory_id: str) -> dict[str, Any]:
        return memory.memory_lineage(memory_id)

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

    @router.post("/projects", dependencies=mutate())
    def create_project(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.create_project(body)

    @router.patch("/projects/{project_id}", dependencies=mutate())
    def update_project(project_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.update_project(project_id, body)

    @router.post("/projects/{project_id}/archive", dependencies=mutate())
    def archive_project(project_id: str) -> dict[str, Any]:
        return memory.archive_project(project_id)

    @router.post("/projects/{project_id}/session", dependencies=mutate())
    def set_session_project(project_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.set_session_project(str(body.get("session_id", "")), project_id)

    @router.post("/subjects", dependencies=mutate())
    def create_subject(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.create_subject(body)

    @router.post("/subjects/{subject_id}/merge", dependencies=mutate())
    def merge_subject(subject_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.merge_subjects(subject_id, str(body.get("source_id", "")))

    @router.patch("/subjects/{subject_id}", dependencies=mutate())
    def update_subject(subject_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.update_subject(subject_id, body)

    @router.post("/subjects/{subject_id}/split", dependencies=mutate())
    def split_subject(subject_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.split_subject(subject_id, body)

    @router.post("/subjects/{subject_id}/links", dependencies=mutate())
    def link_subject(subject_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.link_subject(subject_id, body)

    @router.delete("/subjects/{subject_id}/links/{object_type}/{object_id}", dependencies=mutate())
    def unlink_subject(subject_id: str, object_type: str, object_id: str) -> dict[str, Any]:
        return memory.unlink_subject(subject_id, object_type, object_id)

    @router.post("/subjects/{subject_id}/relations", dependencies=mutate())
    def add_subject_relation(subject_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.add_subject_relation(subject_id, body)

    @router.patch("/work-items/{item_id}", dependencies=mutate())
    def update_work_item(item_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.update_work_item(item_id, body)

    @router.post("/work-items/{item_id}/{action}", dependencies=mutate())
    def work_item_action(item_id: str, action: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        if action not in {"confirm", "resolve", "archive", "restore", "promote"}:
            raise HTTPException(status_code=404, detail="Unsupported work item action")
        return memory.work_item_action(item_id, action, body)

    @router.post("/summaries/{scope}/{subject_id}/regenerate", dependencies=mutate())
    def regenerate_summary(scope: str, subject_id: str) -> dict[str, Any]:
        return memory.regenerate_summary(scope, None if subject_id == "global" else subject_id)

    @router.patch("/summaries/{scope}/{subject_id}", dependencies=mutate())
    def override_summary(scope: str, subject_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.override_summary(
            scope, None if subject_id == "global" else subject_id, str(body.get("content", ""))
        )

    @router.post("/summaries/{scope}/{subject_id}/rollback", dependencies=mutate())
    def rollback_summary(scope: str, subject_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.rollback_summary(
            scope, None if subject_id == "global" else subject_id, str(body.get("version_id", ""))
        )

    @router.post("/summaries/{scope}/{subject_id}/resume", dependencies=mutate())
    def resume_summary(scope: str, subject_id: str) -> dict[str, Any]:
        return memory.resume_summary(scope, None if subject_id == "global" else subject_id)

    @router.post("/reviews/scan", dependencies=mutate())
    def scan_reviews(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return memory.run_memory_audit(scope=str(body.get("scope", "full")))

    @router.post("/reviews/{review_id}/resolve", dependencies=mutate())
    def resolve_review(review_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.resolve_review(
            review_id,
            action=str(body.get("action", "")),
            content=body.get("content"),
            canonical_id=body.get("canonical_id"),
            kind=body.get("kind"),
        )

    @router.post("/reviews/{review_id}/dismiss", dependencies=mutate())
    def dismiss_review(review_id: str) -> dict[str, Any]:
        return memory.dismiss_review(review_id)

    @router.post("/memories", dependencies=mutate())
    def add_memory(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.remember(
            str(body.get("content", "")),
            kind=str(body.get("kind", "fact")),
            allow_sensitive=bool(body.get("allow_sensitive", False)),
            project_id=str(body.get("project_id", "") or "") or None,
        )

    @router.patch("/memories/{record_id}", dependencies=mutate())
    def edit_memory(record_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.update_memory(
            record_id,
            str(body["content"]),
            str(body.get("kind", "fact")),
            valid_from=body.get("valid_from"),
            valid_to=body.get("valid_to"),
            temporal_status=body.get("temporal_status"),
            temporal_reason=body.get("temporal_reason"),
            project_id=str(body.get("project_id", "") or "") or None,
        )

    @router.post("/memories/{record_id}/trash", dependencies=mutate())
    def trash_memory(record_id: str) -> dict[str, bool]:
        memory.trash_memory(record_id)
        return {"ok": True}

    @router.post("/memories/{record_id}/restore", dependencies=mutate())
    def restore_memory(record_id: str) -> dict[str, Any]:
        return memory.restore_memory(record_id)

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

    @router.post("/candidates/{candidate_id}/restore", dependencies=mutate())
    def restore_candidate(candidate_id: str) -> dict[str, Any]:
        return memory.restore_candidate(candidate_id)

    @router.delete("/candidates/{candidate_id}", dependencies=mutate())
    def purge_candidate(candidate_id: str) -> dict[str, Any]:
        return memory.purge_candidate(candidate_id)

    @router.post("/candidates/purge", dependencies=mutate())
    def purge_candidates(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return memory.purge_candidates(str(body.get("status", "")))

    @router.post("/backup", dependencies=mutate())
    def backup() -> dict[str, str]:
        return {"name": memory.create_backup().name}

    @router.post("/backups/{name}/restore", dependencies=mutate())
    def restore_backup(name: str) -> dict[str, bool]:
        memory.restore_backup(name)
        return {"ok": True}

    @router.post("/backups/{name}/preview-restore", dependencies=mutate())
    def preview_restore(name: str) -> dict[str, Any]:
        return memory.preview_restore(name)

    @router.post("/maintenance", dependencies=mutate())
    def maintenance(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return memory.maintenance(
            vacuum=bool(body.get("vacuum", False)), cleanup=bool(body.get("cleanup", False))
        )

    @router.post("/maintenance/validate", dependencies=mutate())
    def validate_storage() -> dict[str, Any]:
        return memory.workspace.storage_health()

    @router.post("/maintenance/rebuild-projections", dependencies=mutate())
    def rebuild_projections() -> dict[str, Any]:
        return memory.workspace.rebuild_projections()

    @router.post("/rebuild", dependencies=mutate())
    def rebuild(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return memory.rebuild_derived(embeddings=bool(body.get("embeddings", False)))

    return router


def create_app(service: MemoryService | None = None) -> FastAPI:
    app = FastAPI(title="B1ack Memory", docs_url=None, redoc_url=None)
    app.include_router(create_router(service), prefix="/api")
    return app
