from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from fastapi import HTTPException


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT / "data" / "workspaces"
WORKSPACE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
PAPER_ID_RE = re.compile(r"[0-9a-f]{64}")
MAX_WORKSPACE_BYTES = 12 * 1024 * 1024
MAX_NODES = 5000
SCHEMA_VERSION = 1
_WRITE_LOCK = threading.Lock()


def _workspace_paths(workspace_id: str) -> tuple[Path, Path, Path, Path]:
    if not isinstance(workspace_id, str) or not WORKSPACE_ID_RE.fullmatch(workspace_id):
        raise HTTPException(status_code=422, detail="Invalid workspace_id.")
    directory = WORKSPACE_ROOT / workspace_id
    return (
        directory / "workspace.json",
        directory / "papers.json",
        directory / "artifacts",
        WORKSPACE_ROOT / f"{workspace_id}.json",
    )


def _migrate_legacy_workspace(workspace_id: str) -> tuple[Path, Path, Path]:
    workspace_path, papers_path, artifacts_path, legacy_path = _workspace_paths(workspace_id)
    if not workspace_path.exists() and legacy_path.exists():
        workspace_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.replace(workspace_path)
    return workspace_path, papers_path, artifacts_path


def _atomic_write_json(path: Path, value: Mapping[str, Any], temporary_name: str) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    temporary = path.parent / temporary_name
    try:
        temporary.write_bytes(encoded)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _papers_manifest(
    workspace_id: str, state: Mapping[str, Any], updated_at: str | None
) -> Dict[str, Any]:
    indexed: Dict[str, Dict[str, Any]] = {}
    nodes = state["tree"]["nodes"]
    for node_id, node in nodes.items():
        paper_id = node.get("paperId")
        paper = node.get("paper")
        if paper_id is None and isinstance(paper, Mapping):
            paper_id = paper.get("paperId")
        if not isinstance(paper_id, str):
            continue
        entry = indexed.setdefault(paper_id, {"paperId": paper_id, "nodeIds": []})
        entry["nodeIds"].append(node_id)
        if isinstance(paper, Mapping):
            for key in ("filename", "title", "sourceUrl", "pageCount"):
                if key in paper:
                    entry[key] = paper[key]

    active_paper = state.get("active_paper")
    if isinstance(active_paper, Mapping):
        paper_id = active_paper.get("paperId")
        if isinstance(paper_id, str):
            entry = indexed.setdefault(paper_id, {"paperId": paper_id, "nodeIds": []})
            for key in ("filename", "title", "sourceUrl", "pageCount"):
                if key in active_paper and key not in entry:
                    entry[key] = active_paper[key]

    papers = sorted(indexed.values(), key=lambda item: item["paperId"])
    return {
        "schema_version": SCHEMA_VERSION,
        "workspace_id": workspace_id,
        "updated_at": updated_at,
        "paper_count": len(papers),
        "papers": papers,
    }


def _validate_tree(tree: Any) -> Dict[str, Any]:
    if not isinstance(tree, Mapping):
        raise HTTPException(status_code=422, detail="Workspace tree must be an object.")
    root_id = tree.get("rootId")
    selected_id = tree.get("selectedId")
    nodes = tree.get("nodes")
    if not isinstance(root_id, str) or not isinstance(nodes, Mapping) or root_id not in nodes:
        raise HTTPException(status_code=422, detail="Workspace tree has no valid root.")
    if not nodes or len(nodes) > MAX_NODES:
        raise HTTPException(status_code=422, detail="Workspace node count is invalid.")
    if selected_id is not None and (not isinstance(selected_id, str) or selected_id not in nodes):
        raise HTTPException(status_code=422, detail="Workspace selectedId is invalid.")

    child_owner: Dict[str, str] = {}
    for node_id, node in nodes.items():
        if not isinstance(node_id, str) or not isinstance(node, Mapping) or node.get("id") != node_id:
            raise HTTPException(status_code=422, detail="Workspace contains an invalid node.")
        children = node.get("children")
        if (not isinstance(children, list)
                or any(not isinstance(child_id, str) for child_id in children)
                or len(children) != len(set(children))):
            raise HTTPException(status_code=422, detail=f"Node {node_id} has invalid children.")
        for child_id in children:
            if not isinstance(child_id, str) or child_id not in nodes or child_id in child_owner:
                raise HTTPException(status_code=422, detail=f"Node {node_id} has an invalid child.")
            child_owner[child_id] = node_id
        paper_id = node.get("paperId")
        if paper_id is not None and (
            not isinstance(paper_id, str) or not PAPER_ID_RE.fullmatch(paper_id)
        ):
            raise HTTPException(status_code=422, detail=f"Node {node_id} has an invalid paperId.")
        paper = node.get("paper")
        if paper is not None:
            validated_paper = _validate_active_paper(paper)
            if paper_id is not None and validated_paper["paperId"] != paper_id:
                raise HTTPException(
                    status_code=422, detail=f"Node {node_id} has inconsistent paper metadata."
                )

    if nodes[root_id].get("parentId") is not None or root_id in child_owner:
        raise HTTPException(status_code=422, detail="Workspace root parent is invalid.")
    for node_id, node in nodes.items():
        if node_id == root_id:
            continue
        if node.get("parentId") != child_owner.get(node_id):
            raise HTTPException(status_code=422, detail=f"Node {node_id} has an invalid parent.")

    visited: set[str] = set()
    stack = [root_id]
    while stack:
        node_id = stack.pop()
        if node_id in visited:
            raise HTTPException(status_code=422, detail="Workspace tree contains a cycle.")
        visited.add(node_id)
        stack.extend(nodes[node_id]["children"])
    if len(visited) != len(nodes):
        raise HTTPException(status_code=422, detail="Workspace tree contains unreachable nodes.")
    return dict(tree)


def _validate_active_paper(value: Any) -> Dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise HTTPException(status_code=422, detail="active_paper must be an object or null.")
    paper_id = value.get("paperId")
    if not isinstance(paper_id, str) or not PAPER_ID_RE.fullmatch(paper_id):
        raise HTTPException(status_code=422, detail="active_paper has an invalid paperId.")
    result: Dict[str, Any] = {"paperId": paper_id}
    for key in ("filename", "title", "sourceUrl"):
        item = value.get(key)
        if item is not None:
            if not isinstance(item, str) or len(item) > 4000:
                raise HTTPException(status_code=422, detail=f"active_paper.{key} is invalid.")
            result[key] = item
    page_count = value.get("pageCount")
    if page_count is not None:
        if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
            raise HTTPException(status_code=422, detail="active_paper.pageCount is invalid.")
        result["pageCount"] = page_count
    return result


def _validated_state(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise HTTPException(status_code=422, detail="Workspace payload must be an object.")
    state = payload.get("state")
    if not isinstance(state, Mapping):
        raise HTTPException(status_code=422, detail="Workspace state must be an object.")
    validated = {
        "tree": _validate_tree(state.get("tree")),
        "active_paper": _validate_active_paper(state.get("active_paper")),
    }
    try:
        encoded = json.dumps(validated, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Workspace is not JSON serializable.") from exc
    if len(encoded) > MAX_WORKSPACE_BYTES:
        raise HTTPException(status_code=413, detail="Workspace exceeds the 12 MB limit.")
    return validated


def load_workspace(workspace_id: str) -> Dict[str, Any]:
    with _WRITE_LOCK:
        path, papers_path, artifacts_path = _migrate_legacy_workspace(workspace_id)
        if not path.exists():
            return {
                "workspace_id": workspace_id,
                "exists": False,
                "schema_version": SCHEMA_VERSION,
                "revision": 0,
                "state": None,
            }
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            state = _validated_state({"state": envelope.get("state")})
            artifacts_path.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(
                papers_path,
                _papers_manifest(workspace_id, state, envelope.get("updated_at")),
                ".papers.tmp",
            )
        except HTTPException:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=500, detail="Stored workspace is unreadable.") from exc
    return {
        "workspace_id": workspace_id,
        "exists": True,
        "schema_version": SCHEMA_VERSION,
        "revision": int(envelope.get("revision", 0)),
        "updated_at": envelope.get("updated_at"),
        "state": state,
    }


def save_workspace(workspace_id: str, payload: Any) -> Dict[str, Any]:
    _workspace_paths(workspace_id)
    state = _validated_state(payload)
    with _WRITE_LOCK:
        path, papers_path, artifacts_path = _migrate_legacy_workspace(workspace_id)
        revision = 1
        if path.exists():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
                revision = int(previous.get("revision", 0)) + 1
            except (OSError, ValueError, TypeError):
                revision = 1
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "workspace_id": workspace_id,
            "revision": revision,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "state": state,
        }
        encoded = json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")
        if len(encoded) > MAX_WORKSPACE_BYTES:
            raise HTTPException(status_code=413, detail="Workspace exceeds the 12 MB limit.")
        path.parent.mkdir(parents=True, exist_ok=True)
        artifacts_path.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(path, envelope, ".workspace.tmp")
        _atomic_write_json(
            papers_path,
            _papers_manifest(workspace_id, state, envelope["updated_at"]),
            ".papers.tmp",
        )
    return {
        "workspace_id": workspace_id,
        "exists": True,
        "schema_version": SCHEMA_VERSION,
        "revision": revision,
        "updated_at": envelope["updated_at"],
    }


def load_workspace_papers(workspace_id: str) -> Dict[str, Any]:
    workspace = load_workspace(workspace_id)
    if not workspace["exists"]:
        return {
            "schema_version": SCHEMA_VERSION,
            "workspace_id": workspace_id,
            "updated_at": None,
            "paper_count": 0,
            "papers": [],
        }
    _, papers_path, _, _ = _workspace_paths(workspace_id)
    try:
        return json.loads(papers_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=500, detail="Workspace paper index is unreadable.") from exc
