from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Dict

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from .activity import SCHEMA as ACTIVITY_SCHEMA
from .agent import run_agent
from .config import config_status, context_policy, load_config, model_catalog, ui_theme
from .evals import (
    cancel_eval_run,
    eval_suite_info,
    get_eval_run,
    list_eval_runs,
    list_eval_suites,
    start_eval_run,
)
from .paper_parser import parse_pdf
from .paper_visuals import paper_path, visual_file
from .skill_loader import (
    load_paper_downloader_skill,
    load_paper_reviewer_skill,
    load_paper_search_skill,
    load_skill_catalog,
    load_paper_visual_extractor_skill,
)
from .workspace import apply_operation, load_workspace, load_workspace_papers, save_workspace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAPERS_ROOT = PROJECT_ROOT / "data" / "papers"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

app = FastAPI(title="InferTree Agent", version="0.11.0")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(
        PROJECT_ROOT / "InferTree.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/api/health")
def health() -> Dict[str, object]:
    return {
        "status": "ok",
        "service": "infertree-agent",
        "activity_schema": ACTIVITY_SCHEMA,
        "agent": config_status(),
    }


@app.post("/api/agent/chat")
async def agent_chat(payload: Dict[str, object] = Body(...)) -> Response:
    return await run_agent(payload, load_config())


@app.get("/api/models")
def models() -> Dict[str, object]:
    """Return the browser-safe model allowlist; credentials and endpoints stay private."""
    return model_catalog(load_config())


@app.get("/api/ui-config")
def ui_config() -> Dict[str, object]:
    """Return browser-safe presentation settings without exposing credentials."""
    config = load_config()
    return {"theme": ui_theme(config), "context": context_policy(config)}


@app.get("/api/skills")
def skills() -> Dict[str, object]:
    """Return Skill discovery metadata without loading full instructions."""
    return {"skills": load_skill_catalog()}


@app.get("/api/skills/paper-reviewer")
def skill_info() -> Dict[str, object]:
    skill = load_paper_reviewer_skill()
    return {
        "name": "paper-reviewer",
        "available": True,
        "invocation": "explicit",
        "loading": "progressive",
        "requires": ["paper_id"],
        "modes": ["auto", "summary", "full-review", "focused-check", "story-lecture"],
        "parts": list(skill),
    }


@app.get("/api/skills/paper-visual-extractor")
def visual_skill_info() -> Dict[str, object]:
    skill = load_paper_visual_extractor_skill()
    return {
        "name": "paper-visual-extractor",
        "available": True,
        "invocation": "explicit-or-composed",
        "loading": "progressive",
        "requires": ["paper_id"],
        "parts": list(skill),
    }


@app.get("/api/skills/paper-downloader")
def downloader_skill_info() -> Dict[str, object]:
    skill = load_paper_downloader_skill()
    return {
        "name": "paper-downloader",
        "available": True,
        "invocation": "explicit-or-composed",
        "loading": "single-file",
        "requires": ["arxiv_id_or_open_paper_reference"],
        "parts": list(skill),
    }


@app.get("/api/skills/paper-search")
def search_skill_info() -> Dict[str, object]:
    skill = load_paper_search_skill()
    return {
        "name": "paper-search",
        "available": True,
        "invocation": "explicit-or-composed",
        "loading": "single-file",
        "requires": ["tavily"],
        "parts": list(skill),
    }


@app.get("/api/evals/suites")
def eval_suites() -> Dict[str, object]:
    return list_eval_suites()


@app.get("/api/evals/suites/{suite_id}")
def eval_suite(suite_id: str) -> Dict[str, object]:
    return eval_suite_info(suite_id)


@app.post("/api/evals/runs")
def create_eval_run(payload: Dict[str, object] = Body(...)) -> Dict[str, object]:
    return start_eval_run(payload, load_config())


@app.get("/api/evals/runs")
def eval_runs(limit: int = 20) -> Dict[str, object]:
    return list_eval_runs(limit)


@app.get("/api/evals/runs/{run_id}")
def eval_run(run_id: str) -> Dict[str, object]:
    return get_eval_run(run_id)


@app.post("/api/evals/runs/{run_id}/cancel")
def stop_eval_run(run_id: str) -> Dict[str, object]:
    return cancel_eval_run(run_id)


@app.get("/api/workspaces/{workspace_id}")
def workspace(workspace_id: str) -> Dict[str, object]:
    return load_workspace(workspace_id)


@app.put("/api/workspaces/{workspace_id}")
def update_workspace(
    workspace_id: str, payload: Dict[str, object] = Body(...)
) -> Dict[str, object]:
    return save_workspace(workspace_id, payload)


@app.post("/api/workspaces/{workspace_id}/operations")
def run_workspace_operation(
    workspace_id: str, payload: Dict[str, object] = Body(...)
) -> Dict[str, object]:
    return apply_operation(workspace_id, payload)


@app.get("/api/workspaces/{workspace_id}/papers")
def workspace_papers(workspace_id: str) -> Dict[str, object]:
    return load_workspace_papers(workspace_id)


@app.get("/api/papers/{paper_id}/metadata")
def paper_metadata(paper_id: str) -> Dict[str, object]:
    parsed = parse_pdf(paper_path(paper_id))
    return {
        "paper_id": paper_id,
        "title": parsed["title"],
        "page_count": parsed["page_count"],
    }


@app.get("/api/papers/{paper_id}/file")
def paper_file(paper_id: str) -> FileResponse:
    return FileResponse(
        paper_path(paper_id),
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'inline; filename="paper.pdf"',
            "Cache-Control": "private, max-age=3600",
        },
    )


@app.get("/api/papers/{paper_id}/visuals/{filename}")
def paper_visual(paper_id: str, filename: str) -> FileResponse:
    return FileResponse(visual_file(paper_id, filename), media_type="image/png")


@app.post("/api/papers")
def upload_paper(file: UploadFile = File(...)) -> Dict[str, object]:
    filename = Path(file.filename or "paper.pdf").name
    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=415, detail="Only PDF uploads are supported")

    PAPERS_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = PAPERS_ROOT / f".upload-{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    total_bytes = 0
    stored: Path | None = None
    created_stored = False
    try:
        with temporary.open("wb") as target:
            while chunk := file.file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="PDF exceeds the 50 MB limit")
                digest.update(chunk)
                target.write(chunk)

        stored = PAPERS_ROOT / f"{digest.hexdigest()}.pdf"
        if stored.exists():
            temporary.unlink()
        else:
            temporary.replace(stored)
            created_stored = True
        parsed = parse_pdf(stored)
    except HTTPException:
        raise
    except Exception as exc:
        if created_stored and stored is not None:
            stored.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"PDF parsing failed: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "paper_id": digest.hexdigest(),
        "filename": filename,
        "title": parsed["title"],
        "page_count": parsed["page_count"],
        "character_count": parsed["character_count"],
    }
