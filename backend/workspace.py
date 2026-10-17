from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from fastapi import HTTPException


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT / "data" / "workspaces"
WORKSPACE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
PAPER_ID_RE = re.compile(r"[0-9a-f]{64}")
VISUAL_ID_RE = re.compile(r"p\d+-(?:figure|table)-[A-Za-z0-9_-]+")
MAX_WORKSPACE_BYTES = 12 * 1024 * 1024
MAX_NODES = 5000
SCHEMA_VERSION = 2
KNOWN_SCHEMA_VERSIONS = {1, 2}
_WRITE_LOCK = threading.Lock()

# Document / evidence / proposal limits.
MAX_DOCUMENTS = 500
MAX_BLOCKS_PER_DOCUMENT = 1000
MAX_DOCUMENT_TITLE_CHARS = 200
MAX_BLOCK_TITLE_CHARS = 200
MAX_BLOCK_MARKDOWN_CHARS = 100_000
MAX_BLOCK_SOURCE_REFS = 20
MAX_BLOCK_EVIDENCE_IDS = 50
MAX_EVIDENCE = 5000
MAX_PROPOSALS = 2000
MAX_QUOTE_CHARS = 20_000
MAX_EXPLANATION_CHARS = 8000
DEDUP_LIMIT = 500

BLOCK_TYPES = {"markdown", "figure"}
EVIDENCE_SOURCE_TYPES = {"paper", "figure", "table"}
EVIDENCE_LOCATOR_STATUSES = {"located", "paper_only", "missing"}
EVIDENCE_REVIEW_STATES = {"unreviewed", "needs_review", "user_checked"}
CREATED_BY = {"human", "agent"}
PROPOSAL_STATUSES = {"pending", "accepted", "rejected", "conflicted"}
ALLOWED_OPS = {
    "document.create",
    "document.update",
    "block.add",
    "block.update",
    "block.move",
    "block.remove",
    "block.evidence.set",
    "evidence.upsert",
    "proposal.create",
    "proposal.accept",
    "proposal.reject",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _block_content_hash(block: Mapping[str, Any]) -> str:
    """Canonical, unambiguous block hash. Fixed fields, fixed serialization."""
    payload: Dict[str, Any] = {
        "type": block.get("type"),
        "title": block.get("title") or "",
        "body": block.get("markdown") or "",
    }
    if block.get("type") == "figure":
        payload["assetRef"] = block.get("assetRef")
    return _sha256(_stable_json(payload))


def _request_fingerprint(op: str, params: Mapping[str, Any]) -> str:
    return _sha256(_stable_json({"op": op, **params}))


def _workspace_paths(workspace_id: str) -> Tuple[Path, Path, Path, Path]:
    if not isinstance(workspace_id, str) or not WORKSPACE_ID_RE.fullmatch(workspace_id):
        raise HTTPException(status_code=422, detail="Invalid workspace_id.")
    directory = WORKSPACE_ROOT / workspace_id
    return (
        directory / "workspace.json",
        directory / "papers.json",
        directory / "artifacts",
        WORKSPACE_ROOT / f"{workspace_id}.json",
    )


def _migrate_legacy_workspace(workspace_id: str) -> Tuple[Path, Path, Path]:
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


def _normalize_evidence(ev_id: str, ev: Any) -> Dict[str, Any]:
    if not isinstance(ev_id, str) or not ev_id or len(ev_id) > 128:
        raise HTTPException(status_code=422, detail="Evidence has an invalid id.")
    if not isinstance(ev, Mapping):
        raise HTTPException(status_code=422, detail=f"Evidence {ev_id} is invalid.")
    if "id" in ev and ev.get("id") != ev_id:
        raise HTTPException(status_code=422, detail=f"Evidence {ev_id} has an inconsistent id.")
    paper_id = ev.get("paperId")
    if not isinstance(paper_id, str) or not PAPER_ID_RE.fullmatch(paper_id):
        raise HTTPException(status_code=422, detail=f"Evidence {ev_id} has an invalid paperId.")
    source_type = ev.get("sourceType")
    if source_type not in EVIDENCE_SOURCE_TYPES:
        raise HTTPException(status_code=422, detail=f"Evidence {ev_id} has an invalid sourceType.")
    validated: Dict[str, Any] = {"id": ev_id, "paperId": paper_id, "sourceType": source_type}
    page = ev.get("page")
    if page is not None:
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise HTTPException(status_code=422, detail=f"Evidence {ev_id} has an invalid page.")
        validated["page"] = page
    page_label = ev.get("pageLabel")
    if page_label is not None:
        if not isinstance(page_label, str) or len(page_label) > 64:
            raise HTTPException(status_code=422, detail=f"Evidence {ev_id} has an invalid pageLabel.")
        validated["pageLabel"] = page_label
    quote = ev.get("quote")
    if quote is not None:
        if not isinstance(quote, str) or len(quote) > MAX_QUOTE_CHARS:
            raise HTTPException(status_code=422, detail=f"Evidence {ev_id} has an invalid quote.")
        validated["quote"] = quote
    visual_id = ev.get("visualId")
    if visual_id is not None:
        if not isinstance(visual_id, str) or not VISUAL_ID_RE.fullmatch(visual_id):
            raise HTTPException(status_code=422, detail=f"Evidence {ev_id} has an invalid visualId.")
        validated["visualId"] = visual_id
    asset_ref = ev.get("assetRef")
    if asset_ref is not None:
        if not isinstance(asset_ref, Mapping):
            raise HTTPException(status_code=422, detail=f"Evidence {ev_id} has an invalid assetRef.")
        validated["assetRef"] = dict(asset_ref)
    bbox = ev.get("bbox")
    if bbox is not None:
        if (not isinstance(bbox, list) or len(bbox) != 4
                or any(not isinstance(c, (int, float)) or isinstance(c, bool) for c in bbox)):
            raise HTTPException(status_code=422, detail=f"Evidence {ev_id} has an invalid bbox.")
        validated["bbox"] = [float(c) for c in bbox]
    locator = ev.get("locatorStatus")
    if locator not in EVIDENCE_LOCATOR_STATUSES:
        raise HTTPException(status_code=422, detail=f"Evidence {ev_id} has an invalid locatorStatus.")
    validated["locatorStatus"] = locator
    return validated


def _validate_evidence(evidence: Any) -> Dict[str, Any]:
    if evidence is None:
        evidence = {}
    if not isinstance(evidence, Mapping):
        raise HTTPException(status_code=422, detail="Workspace evidence must be an object.")
    if len(evidence) > MAX_EVIDENCE:
        raise HTTPException(status_code=422, detail="Workspace evidence exceeds the limit.")
    return {ev_id: _normalize_evidence(ev_id, ev) for ev_id, ev in evidence.items()}


def _validate_block(block_id: Any, block: Any, evidence_keys: set) -> Dict[str, Any]:
    if not isinstance(block_id, str) or not block_id or len(block_id) > 128:
        raise HTTPException(status_code=422, detail="Document block has an invalid id.")
    if not isinstance(block, Mapping) or block.get("id") != block_id:
        raise HTTPException(status_code=422, detail=f"Block {block_id} is invalid.")
    block_type = block.get("type")
    if block_type not in BLOCK_TYPES:
        raise HTTPException(status_code=422, detail=f"Block {block_id} has an invalid type.")
    title = block.get("title")
    if not isinstance(title, str) or len(title) > MAX_BLOCK_TITLE_CHARS:
        raise HTTPException(status_code=422, detail=f"Block {block_id} has an invalid title.")
    markdown = block.get("markdown")
    if not isinstance(markdown, str) or len(markdown) > MAX_BLOCK_MARKDOWN_CHARS:
        raise HTTPException(status_code=422, detail=f"Block {block_id} has invalid text.")
    validated: Dict[str, Any] = {"id": block_id, "type": block_type, "title": title, "markdown": markdown}
    if block_type == "figure":
        asset_ref = block.get("assetRef")
        if not isinstance(asset_ref, Mapping):
            raise HTTPException(status_code=422, detail=f"Figure block {block_id} requires an assetRef.")
        validated["assetRef"] = dict(asset_ref)
    revision = block.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise HTTPException(status_code=422, detail=f"Block {block_id} has an invalid revision.")
    validated["revision"] = revision
    content_hash = block.get("contentHash")
    if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise HTTPException(status_code=422, detail=f"Block {block_id} has an invalid contentHash.")
    if content_hash != _block_content_hash(validated):
        raise HTTPException(status_code=422, detail=f"Block {block_id} content hash mismatch.")
    validated["contentHash"] = content_hash
    created_by = block.get("createdBy", "human")
    updated_by = block.get("updatedBy", "human")
    if created_by not in CREATED_BY or updated_by not in CREATED_BY:
        raise HTTPException(status_code=422, detail=f"Block {block_id} has an invalid author.")
    validated["createdBy"] = created_by
    validated["updatedBy"] = updated_by
    for field in ("createdAt", "updatedAt"):
        value = block.get(field)
        if not isinstance(value, str):
            raise HTTPException(status_code=422, detail=f"Block {block_id} has an invalid {field}.")
        validated[field] = value
    source_refs = block.get("sourceRefs", [])
    if not isinstance(source_refs, list) or len(source_refs) > MAX_BLOCK_SOURCE_REFS:
        raise HTTPException(status_code=422, detail=f"Block {block_id} has invalid sourceRefs.")
    for ref in source_refs:
        if not isinstance(ref, Mapping) or not isinstance(ref.get("nodeId"), str):
            raise HTTPException(status_code=422, detail=f"Block {block_id} has an invalid sourceRef.")
    validated["sourceRefs"] = [dict(ref) for ref in source_refs]
    evidence_ids = block.get("evidenceIds", [])
    if not isinstance(evidence_ids, list) or len(evidence_ids) > MAX_BLOCK_EVIDENCE_IDS:
        raise HTTPException(status_code=422, detail=f"Block {block_id} has invalid evidenceIds.")
    for ev_id in evidence_ids:
        if not isinstance(ev_id, str) or ev_id not in evidence_keys:
            raise HTTPException(status_code=422, detail=f"Block {block_id} references unknown evidence {ev_id}.")
    validated["evidenceIds"] = [str(i) for i in evidence_ids]
    review_state = block.get("evidenceReviewState", "unreviewed")
    if review_state not in EVIDENCE_REVIEW_STATES:
        raise HTTPException(status_code=422, detail=f"Block {block_id} has an invalid evidenceReviewState.")
    validated["evidenceReviewState"] = review_state
    return validated


def _validate_document(doc_id: Any, doc: Any, evidence_keys: set) -> Dict[str, Any]:
    if not isinstance(doc_id, str) or not doc_id or len(doc_id) > 128:
        raise HTTPException(status_code=422, detail="Document has an invalid id.")
    if not isinstance(doc, Mapping) or doc.get("id") != doc_id:
        raise HTTPException(status_code=422, detail=f"Document {doc_id} is invalid.")
    title = doc.get("title")
    if not isinstance(title, str) or len(title) > MAX_DOCUMENT_TITLE_CHARS:
        raise HTTPException(status_code=422, detail=f"Document {doc_id} has an invalid title.")
    revision = doc.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise HTTPException(status_code=422, detail=f"Document {doc_id} has an invalid revision.")
    block_order = doc.get("blockOrder")
    if not isinstance(block_order, list) or len(block_order) != len(set(block_order)):
        raise HTTPException(status_code=422, detail=f"Document {doc_id} has an invalid blockOrder.")
    blocks = doc.get("blocks")
    if not isinstance(blocks, Mapping) or len(blocks) > MAX_BLOCKS_PER_DOCUMENT:
        raise HTTPException(status_code=422, detail=f"Document {doc_id} has invalid blocks.")
    if set(block_order) != set(blocks.keys()):
        raise HTTPException(status_code=422, detail=f"Document {doc_id} blockOrder is inconsistent with blocks.")
    validated_blocks = {
        bid: _validate_block(bid, b, evidence_keys) for bid, b in blocks.items()
    }
    validated: Dict[str, Any] = {
        "id": doc_id,
        "title": title,
        "revision": revision,
        "blockOrder": list(block_order),
        "blocks": validated_blocks,
    }
    for field in ("createdAt", "updatedAt"):
        value = doc.get(field)
        if not isinstance(value, str):
            raise HTTPException(status_code=422, detail=f"Document {doc_id} has an invalid {field}.")
        validated[field] = value
    return validated


def _validate_documents(documents: Any, evidence_keys: set) -> Dict[str, Any]:
    if documents is None:
        documents = {}
    if not isinstance(documents, Mapping):
        raise HTTPException(status_code=422, detail="Workspace documents must be an object.")
    if len(documents) > MAX_DOCUMENTS:
        raise HTTPException(status_code=422, detail="Workspace documents exceed the limit.")
    return {doc_id: _validate_document(doc_id, doc, evidence_keys) for doc_id, doc in documents.items()}


def _normalize_proposal(prop_id: str, prop: Any, evidence_keys: set) -> Dict[str, Any]:
    if not isinstance(prop_id, str) or not prop_id or len(prop_id) > 128:
        raise HTTPException(status_code=422, detail="Proposal has an invalid id.")
    if not isinstance(prop, Mapping):
        raise HTTPException(status_code=422, detail=f"Proposal {prop_id} is invalid.")
    if "id" in prop and prop.get("id") != prop_id:
        raise HTTPException(status_code=422, detail=f"Proposal {prop_id} has an inconsistent id.")
    document_id = prop.get("documentId")
    if not isinstance(document_id, str) or not document_id:
        raise HTTPException(status_code=422, detail=f"Proposal {prop_id} has an invalid documentId.")
    block_id = prop.get("blockId")
    if not isinstance(block_id, str) or not block_id:
        raise HTTPException(status_code=422, detail=f"Proposal {prop_id} has an invalid blockId.")
    base_revision = prop.get("baseBlockRevision")
    if not isinstance(base_revision, int) or isinstance(base_revision, bool) or base_revision < 1:
        raise HTTPException(status_code=422, detail=f"Proposal {prop_id} has an invalid baseBlockRevision.")
    base_hash = prop.get("baseContentHash")
    if not isinstance(base_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", base_hash):
        raise HTTPException(status_code=422, detail=f"Proposal {prop_id} has an invalid baseContentHash.")
    replacement_markdown = prop.get("replacementMarkdown")
    if not isinstance(replacement_markdown, str) or len(replacement_markdown) > MAX_BLOCK_MARKDOWN_CHARS:
        raise HTTPException(status_code=422, detail=f"Proposal {prop_id} has invalid replacementMarkdown.")
    validated: Dict[str, Any] = {
        "id": prop_id,
        "documentId": document_id,
        "blockId": block_id,
        "baseBlockRevision": base_revision,
        "baseContentHash": base_hash,
        "replacementMarkdown": replacement_markdown,
    }
    replacement_title = prop.get("replacementTitle")
    if replacement_title is not None:
        if not isinstance(replacement_title, str) or len(replacement_title) > MAX_BLOCK_TITLE_CHARS:
            raise HTTPException(status_code=422, detail=f"Proposal {prop_id} has an invalid replacementTitle.")
        validated["replacementTitle"] = replacement_title
    evidence_ids = prop.get("evidenceIds", [])
    if not isinstance(evidence_ids, list) or len(evidence_ids) > MAX_BLOCK_EVIDENCE_IDS:
        raise HTTPException(status_code=422, detail=f"Proposal {prop_id} has invalid evidenceIds.")
    for ev_id in evidence_ids:
        if ev_id not in evidence_keys:
            raise HTTPException(status_code=422, detail=f"Proposal {prop_id} references unknown evidence {ev_id}.")
    validated["evidenceIds"] = [str(i) for i in evidence_ids]
    explanation = prop.get("explanation", "")
    if not isinstance(explanation, str) or len(explanation) > MAX_EXPLANATION_CHARS:
        raise HTTPException(status_code=422, detail=f"Proposal {prop_id} has an invalid explanation.")
    validated["explanation"] = explanation
    for field in ("sourceNodeId", "requestId"):
        value = prop.get(field)
        if value is not None:
            if not isinstance(value, str) or len(value) > 128:
                raise HTTPException(status_code=422, detail=f"Proposal {prop_id} has an invalid {field}.")
            validated[field] = value
    created_at = prop.get("createdAt")
    if not isinstance(created_at, str):
        raise HTTPException(status_code=422, detail=f"Proposal {prop_id} has an invalid createdAt.")
    validated["createdAt"] = created_at
    status = prop.get("status")
    if status not in PROPOSAL_STATUSES:
        raise HTTPException(status_code=422, detail=f"Proposal {prop_id} has an invalid status.")
    validated["status"] = status
    accepted_by = prop.get("acceptedBy")
    if accepted_by is not None:
        if not isinstance(accepted_by, str) or len(accepted_by) > 64:
            raise HTTPException(status_code=422, detail=f"Proposal {prop_id} has an invalid acceptedBy.")
        validated["acceptedBy"] = accepted_by
    return validated


def _validate_proposals(proposals: Any, evidence_keys: set) -> Dict[str, Any]:
    if proposals is None:
        proposals = {}
    if not isinstance(proposals, Mapping):
        raise HTTPException(status_code=422, detail="Workspace proposals must be an object.")
    if len(proposals) > MAX_PROPOSALS:
        raise HTTPException(status_code=422, detail="Workspace proposals exceed the limit.")
    return {pid: _normalize_proposal(pid, prop, evidence_keys) for pid, prop in proposals.items()}


def _validate_dedup(dedup: Any) -> Dict[str, Any]:
    if dedup is None:
        dedup = {}
    if not isinstance(dedup, Mapping):
        raise HTTPException(status_code=422, detail="Workspace dedup must be an object.")
    if len(dedup) > DEDUP_LIMIT:
        raise HTTPException(status_code=422, detail="Workspace dedup exceeds the limit.")
    result: Dict[str, Any] = {}
    for op_id, record in dedup.items():
        if not isinstance(op_id, str) or not op_id or len(op_id) > 128:
            raise HTTPException(status_code=422, detail="Dedup has an invalid operationId.")
        if not isinstance(record, Mapping):
            raise HTTPException(status_code=422, detail=f"Dedup {op_id} is invalid.")
        fingerprint = record.get("fingerprint")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise HTTPException(status_code=422, detail=f"Dedup {op_id} has an invalid fingerprint.")
        committed_revision = record.get("committed_revision")
        if not isinstance(committed_revision, int) or isinstance(committed_revision, bool) or committed_revision < 1:
            raise HTTPException(status_code=422, detail=f"Dedup {op_id} has an invalid committed_revision.")
        ts = record.get("ts")
        if not isinstance(ts, str):
            raise HTTPException(status_code=422, detail=f"Dedup {op_id} has an invalid ts.")
        result[op_id] = {
            "fingerprint": fingerprint,
            "result": record.get("result"),
            "committed_revision": committed_revision,
            "ts": ts,
        }
    return result


def _validate_full_state(state: Any, version: int) -> Dict[str, Any]:
    if not isinstance(state, Mapping):
        raise HTTPException(status_code=422, detail="Workspace state must be an object.")
    tree = _validate_tree(state.get("tree"))
    active_paper = _validate_active_paper(state.get("active_paper"))
    if version == 1:
        return {
            "tree": tree,
            "active_paper": active_paper,
            "documents": {},
            "evidence": {},
            "proposals": {},
            "dedup": {},
        }
    evidence = _validate_evidence(state.get("evidence"))
    evidence_keys = set(evidence.keys())
    documents = _validate_documents(state.get("documents"), evidence_keys)
    proposals = _validate_proposals(state.get("proposals"), evidence_keys)
    dedup = _validate_dedup(state.get("dedup"))
    return {
        "tree": tree,
        "active_paper": active_paper,
        "documents": documents,
        "evidence": evidence,
        "proposals": proposals,
        "dedup": dedup,
    }


def _read_envelope(path: Path) -> Dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=500, detail="Stored workspace is unreadable.") from exc
    if not isinstance(value, Mapping):
        raise HTTPException(status_code=500, detail="Stored workspace is invalid.")
    return dict(value)


def _papers_manifest(
    workspace_id: str, state: Mapping[str, Any], updated_at: str | None
) -> Dict[str, Any]:
    indexed: Dict[str, Dict[str, Any]] = {}

    def ensure(paper_id: str) -> Dict[str, Any]:
        if paper_id not in indexed:
            indexed[paper_id] = {"paperId": paper_id, "nodeIds": [], "documentIds": []}
        return indexed[paper_id]

    nodes = state["tree"]["nodes"]
    for node_id, node in nodes.items():
        paper_id = node.get("paperId")
        paper = node.get("paper")
        if paper_id is None and isinstance(paper, Mapping):
            paper_id = paper.get("paperId")
        if not isinstance(paper_id, str):
            continue
        entry = ensure(paper_id)
        entry["nodeIds"].append(node_id)
        if isinstance(paper, Mapping):
            for key in ("filename", "title", "sourceUrl", "pageCount"):
                if key in paper:
                    entry[key] = paper[key]

    active_paper = state.get("active_paper")
    if isinstance(active_paper, Mapping):
        paper_id = active_paper.get("paperId")
        if isinstance(paper_id, str):
            entry = ensure(paper_id)
            for key in ("filename", "title", "sourceUrl", "pageCount"):
                if key in active_paper and key not in entry:
                    entry[key] = active_paper[key]

    evidence = state.get("evidence", {})
    for doc_id, doc in state.get("documents", {}).items():
        for block in doc.get("blocks", {}).values():
            for ev_id in block.get("evidenceIds", []):
                ev = evidence.get(ev_id)
                if isinstance(ev, Mapping) and isinstance(ev.get("paperId"), str):
                    ensure(ev["paperId"])["documentIds"].append(doc_id)

    papers = sorted(indexed.values(), key=lambda item: item["paperId"])
    return {
        "schema_version": SCHEMA_VERSION,
        "workspace_id": workspace_id,
        "updated_at": updated_at,
        "paper_count": len(papers),
        "papers": papers,
    }


def _write_envelope(
    path: Path,
    papers_path: Path,
    artifacts_path: Path,
    workspace_id: str,
    envelope: Dict[str, Any],
) -> None:
    encoded = json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")
    if len(encoded) > MAX_WORKSPACE_BYTES:
        raise HTTPException(status_code=413, detail="Workspace exceeds the 12 MB limit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    artifacts_path.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, envelope, ".workspace.tmp")
    _atomic_write_json(
        papers_path,
        _papers_manifest(workspace_id, envelope["state"], envelope["updated_at"]),
        ".papers.tmp",
    )


def load_workspace(workspace_id: str) -> Dict[str, Any]:
    with _WRITE_LOCK:
        path, papers_path, artifacts_path = _migrate_legacy_workspace(workspace_id)
        envelope = _read_envelope(path)
        if envelope is None:
            return {
                "workspace_id": workspace_id,
                "exists": False,
                "schema_version": SCHEMA_VERSION,
                "revision": 0,
                "state": None,
            }
        version = envelope.get("schema_version")
        if version is None:
            version = 1
        if not isinstance(version, int) or isinstance(version, bool) or version not in KNOWN_SCHEMA_VERSIONS:
            raise HTTPException(status_code=413, detail="Workspace schema version is unsupported.")
        state = _validate_full_state(envelope.get("state"), version)
        artifacts_path.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            papers_path,
            _papers_manifest(workspace_id, state, envelope.get("updated_at")),
            ".papers.tmp",
        )
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
    if not isinstance(payload, Mapping):
        raise HTTPException(status_code=422, detail="Workspace payload must be an object.")
    base_revision = payload.get("baseRevision")
    if not isinstance(base_revision, int) or isinstance(base_revision, bool) or base_revision < 0:
        raise HTTPException(status_code=422, detail="baseRevision is required for workspace writes.")
    state_payload = payload.get("state")
    if not isinstance(state_payload, Mapping):
        raise HTTPException(status_code=422, detail="Workspace state must be an object.")
    for forbidden in ("documents", "evidence", "proposals", "dedup"):
        if forbidden in state_payload:
            raise HTTPException(status_code=422, detail=f"Tree save must not include {forbidden}.")
    tree = _validate_tree(state_payload.get("tree"))
    has_active_paper = "active_paper" in state_payload
    active_paper = _validate_active_paper(state_payload.get("active_paper"))

    with _WRITE_LOCK:
        path, papers_path, artifacts_path = _migrate_legacy_workspace(workspace_id)
        envelope = _read_envelope(path)
        current_revision = int(envelope.get("revision", 0)) if envelope else 0
        if base_revision != current_revision:
            raise HTTPException(status_code=409, detail={
                "message": "Workspace revision conflict.",
                "current_revision": current_revision,
            })
        if envelope is None:
            current_state: Dict[str, Any] = {
                "tree": tree,
                "active_paper": active_paper,
                "documents": {},
                "evidence": {},
                "proposals": {},
                "dedup": {},
            }
        else:
            version = envelope.get("schema_version") or 1
            if not isinstance(version, int) or isinstance(version, bool) or version not in KNOWN_SCHEMA_VERSIONS:
                raise HTTPException(status_code=413, detail="Workspace schema version is unsupported.")
            current_state = _validate_full_state(envelope.get("state"), version)
        new_state = dict(current_state)
        new_state["tree"] = tree
        if has_active_paper:
            new_state["active_paper"] = active_paper
        new_revision = current_revision + 1
        new_envelope = {
            "schema_version": SCHEMA_VERSION,
            "workspace_id": workspace_id,
            "revision": new_revision,
            "updated_at": _now_iso(),
            "state": new_state,
        }
        _write_envelope(path, papers_path, artifacts_path, workspace_id, new_envelope)
    return {
        "workspace_id": workspace_id,
        "exists": True,
        "schema_version": SCHEMA_VERSION,
        "revision": new_revision,
        "updated_at": new_envelope["updated_at"],
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


def _trim_dedup(dedup: Dict[str, Any]) -> None:
    """FIFO eviction, oldest first. Window boundary: once evicted, an operationId
    can no longer be recognized as a duplicate; clients must never replay an
    evicted operation by refreshing the base revision."""
    while len(dedup) > DEDUP_LIMIT:
        oldest = min(dedup.items(), key=lambda item: item[1].get("committed_revision", 0))[0]
        dedup.pop(oldest, None)


def _build_block(
    block_input: Any,
    evidence: Mapping[str, Any],
    now: str,
    created_by: str,
    block_id: str | None = None,
) -> Dict[str, Any]:
    if not isinstance(block_input, Mapping):
        raise HTTPException(status_code=422, detail="block is required.")
    block_type = block_input.get("type")
    if block_type not in BLOCK_TYPES:
        raise HTTPException(status_code=422, detail="block.type is invalid.")
    title = block_input.get("title")
    if not isinstance(title, str) or len(title) > MAX_BLOCK_TITLE_CHARS:
        raise HTTPException(status_code=422, detail="block.title is invalid.")
    markdown = block_input.get("markdown")
    if not isinstance(markdown, str) or len(markdown) > MAX_BLOCK_MARKDOWN_CHARS:
        raise HTTPException(status_code=422, detail="block.markdown is invalid.")
    block: Dict[str, Any] = {
        "id": block_id or _new_id("blk"),
        "type": block_type,
        "title": title,
        "markdown": markdown,
        "revision": 1,
        "createdBy": created_by,
        "updatedBy": created_by,
        "createdAt": now,
        "updatedAt": now,
    }
    if block_type == "figure":
        asset_ref = block_input.get("assetRef")
        if not isinstance(asset_ref, Mapping):
            raise HTTPException(status_code=422, detail="figure block requires assetRef.")
        block["assetRef"] = dict(asset_ref)
    source_refs = block_input.get("sourceRefs", [])
    if not isinstance(source_refs, list) or len(source_refs) > MAX_BLOCK_SOURCE_REFS:
        raise HTTPException(status_code=422, detail="block.sourceRefs is invalid.")
    for ref in source_refs:
        if not isinstance(ref, Mapping) or not isinstance(ref.get("nodeId"), str):
            raise HTTPException(status_code=422, detail="block.sourceRefs has an invalid entry.")
    block["sourceRefs"] = [dict(ref) for ref in source_refs]
    evidence_ids = block_input.get("evidenceIds", [])
    if not isinstance(evidence_ids, list) or len(evidence_ids) > MAX_BLOCK_EVIDENCE_IDS:
        raise HTTPException(status_code=422, detail="block.evidenceIds is invalid.")
    for ev_id in evidence_ids:
        if ev_id not in evidence:
            raise HTTPException(status_code=422, detail=f"block.evidenceIds references unknown evidence {ev_id}.")
    block["evidenceIds"] = [str(i) for i in evidence_ids]
    block["evidenceReviewState"] = block_input.get("evidenceReviewState", "unreviewed")
    if block["evidenceReviewState"] not in EVIDENCE_REVIEW_STATES:
        raise HTTPException(status_code=422, detail="block.evidenceReviewState is invalid.")
    block["contentHash"] = _block_content_hash(block)
    return block


def _execute_operation(
    state: Dict[str, Any], op: str, params: Mapping[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    new_state: Dict[str, Any] = {
        "tree": state["tree"],
        "active_paper": state["active_paper"],
        "documents": copy.deepcopy(state["documents"]),
        "evidence": copy.deepcopy(state["evidence"]),
        "proposals": copy.deepcopy(state["proposals"]),
        "dedup": copy.deepcopy(state["dedup"]),
    }
    documents = new_state["documents"]
    evidence = new_state["evidence"]
    proposals = new_state["proposals"]
    now = _now_iso()

    if op == "document.create":
        title = params.get("title")
        if not isinstance(title, str) or len(title) > MAX_DOCUMENT_TITLE_CHARS:
            raise HTTPException(status_code=422, detail="document.create requires a valid title.")
        doc_id = _new_id("doc")
        documents[doc_id] = {
            "id": doc_id,
            "title": title,
            "revision": 1,
            "blockOrder": [],
            "blocks": {},
            "createdAt": now,
            "updatedAt": now,
        }
        return new_state, {"documentId": doc_id, "documentRevision": 1}

    if op == "document.update":
        doc_id = params.get("documentId")
        doc = documents.get(doc_id)
        if doc is None:
            raise HTTPException(status_code=422, detail="Document does not exist.")
        title = params.get("title")
        if not isinstance(title, str) or len(title) > MAX_DOCUMENT_TITLE_CHARS:
            raise HTTPException(status_code=422, detail="document.update requires a valid title.")
        doc["title"] = title
        doc["revision"] += 1
        doc["updatedAt"] = now
        return new_state, {"documentId": doc_id, "documentRevision": doc["revision"]}

    if op == "block.add":
        doc_id = params.get("documentId")
        doc = documents.get(doc_id)
        if doc is None:
            raise HTTPException(status_code=422, detail="Document does not exist.")
        block = _build_block(params.get("block"), evidence, now, "human")
        block_id = block["id"]
        doc["blocks"][block_id] = block
        doc["blockOrder"].append(block_id)
        doc["revision"] += 1
        doc["updatedAt"] = now
        return new_state, {
            "documentId": doc_id,
            "blockId": block_id,
            "blockRevision": block["revision"],
            "documentRevision": doc["revision"],
        }

    if op == "block.update":
        doc_id = params.get("documentId")
        doc = documents.get(doc_id)
        if doc is None:
            raise HTTPException(status_code=422, detail="Document does not exist.")
        block_id = params.get("blockId")
        block = doc["blocks"].get(block_id)
        if block is None:
            raise HTTPException(status_code=422, detail="Block does not exist.")
        base_block_revision = params.get("baseBlockRevision")
        base_hash = params.get("baseContentHash")
        if not isinstance(base_block_revision, int) or isinstance(base_block_revision, bool) or base_block_revision < 1:
            raise HTTPException(status_code=422, detail="block.update requires baseBlockRevision.")
        if not isinstance(base_hash, str):
            raise HTTPException(status_code=422, detail="block.update requires baseContentHash.")
        if base_block_revision != block["revision"] or base_hash != block["contentHash"]:
            raise HTTPException(status_code=409, detail={
                "message": "Block changed since it was read.",
                "block_revision": block["revision"],
                "block_content_hash": block["contentHash"],
            })
        if "assetRef" in params:
            raise HTTPException(status_code=422, detail="block.update cannot change assetRef.")
        has_change = False
        if "title" in params:
            title = params["title"]
            if not isinstance(title, str) or len(title) > MAX_BLOCK_TITLE_CHARS:
                raise HTTPException(status_code=422, detail="block.update title is invalid.")
            block["title"] = title
            has_change = True
        if "markdown" in params:
            markdown = params["markdown"]
            if not isinstance(markdown, str) or len(markdown) > MAX_BLOCK_MARKDOWN_CHARS:
                raise HTTPException(status_code=422, detail="block.update markdown is invalid.")
            block["markdown"] = markdown
            has_change = True
        if not has_change:
            raise HTTPException(status_code=422, detail="block.update requires title or markdown.")
        block["revision"] += 1
        block["updatedAt"] = now
        block["updatedBy"] = "human"
        block["contentHash"] = _block_content_hash(block)
        doc["revision"] += 1
        doc["updatedAt"] = now
        return new_state, {
            "documentId": doc_id,
            "blockId": block_id,
            "blockRevision": block["revision"],
            "documentRevision": doc["revision"],
        }

    if op == "block.move":
        doc_id = params.get("documentId")
        doc = documents.get(doc_id)
        if doc is None:
            raise HTTPException(status_code=422, detail="Document does not exist.")
        block_id = params.get("blockId")
        if block_id not in doc["blocks"]:
            raise HTTPException(status_code=422, detail="Block does not exist.")
        index = params.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise HTTPException(status_code=422, detail="block.move requires a valid index.")
        order = doc["blockOrder"]
        order.remove(block_id)
        order.insert(min(index, len(order)), block_id)
        doc["revision"] += 1
        doc["updatedAt"] = now
        return new_state, {
            "documentId": doc_id,
            "blockOrder": list(order),
            "documentRevision": doc["revision"],
        }

    if op == "block.remove":
        doc_id = params.get("documentId")
        doc = documents.get(doc_id)
        if doc is None:
            raise HTTPException(status_code=422, detail="Document does not exist.")
        block_id = params.get("blockId")
        if block_id not in doc["blocks"]:
            raise HTTPException(status_code=422, detail="Block does not exist.")
        doc["blocks"].pop(block_id, None)
        doc["blockOrder"] = [item for item in doc["blockOrder"] if item != block_id]
        for prop in proposals.values():
            if prop.get("blockId") == block_id and prop.get("status") == "pending":
                prop["status"] = "rejected"
        doc["revision"] += 1
        doc["updatedAt"] = now
        return new_state, {
            "documentId": doc_id,
            "blockId": block_id,
            "removed": True,
            "documentRevision": doc["revision"],
        }

    if op == "block.evidence.set":
        doc_id = params.get("documentId")
        doc = documents.get(doc_id)
        if doc is None:
            raise HTTPException(status_code=422, detail="Document does not exist.")
        block_id = params.get("blockId")
        block = doc["blocks"].get(block_id)
        if block is None:
            raise HTTPException(status_code=422, detail="Block does not exist.")
        evidence_ids = params.get("evidenceIds")
        if not isinstance(evidence_ids, list) or len(evidence_ids) > MAX_BLOCK_EVIDENCE_IDS:
            raise HTTPException(status_code=422, detail="block.evidence.set evidenceIds is invalid.")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise HTTPException(status_code=422, detail="block.evidence.set evidenceIds contains duplicates.")
        for ev_id in evidence_ids:
            if not isinstance(ev_id, str) or ev_id not in evidence:
                raise HTTPException(
                    status_code=422, detail=f"block.evidence.set references unknown evidence {ev_id}."
                )
        # 只改变引用关系、不改正文：不推进块 revision，避免让既有提案基线失效。
        block["evidenceIds"] = [str(i) for i in evidence_ids]
        block["evidenceReviewState"] = params.get(
            "evidenceReviewState", block.get("evidenceReviewState", "unreviewed")
        )
        if block["evidenceReviewState"] not in EVIDENCE_REVIEW_STATES:
            raise HTTPException(status_code=422, detail="block.evidence.set evidenceReviewState is invalid.")
        block["updatedAt"] = now
        block["updatedBy"] = "human"
        doc["revision"] += 1
        doc["updatedAt"] = now
        return new_state, {
            "documentId": doc_id,
            "blockId": block_id,
            "evidenceIds": list(block["evidenceIds"]),
            "blockRevision": block["revision"],
            "documentRevision": doc["revision"],
        }

    if op == "evidence.upsert":
        ev_id = params.get("evidenceId")
        if ev_id is None:
            ev_id = _new_id("ev")
        elif not isinstance(ev_id, str) or len(ev_id) > 128:
            raise HTTPException(status_code=422, detail="evidence.upsert evidenceId is invalid.")
        evidence[ev_id] = _normalize_evidence(ev_id, params)
        return new_state, {"evidenceId": ev_id}

    if op == "proposal.create":
        doc_id = params.get("documentId")
        doc = documents.get(doc_id)
        if doc is None:
            raise HTTPException(status_code=422, detail="Document does not exist.")
        block_id = params.get("blockId")
        block = doc["blocks"].get(block_id)
        if block is None:
            raise HTTPException(status_code=422, detail="Block does not exist.")
        # 迟到的模型响应：提案必须基于模型实际看到的那个版本，
        # 否则接受时会用旧正文覆盖用户刚做的新编辑。
        expected_hash = params.get("expectedBaseContentHash")
        if expected_hash is not None:
            if not isinstance(expected_hash, str) or expected_hash != block["contentHash"]:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Block changed while the proposal was being drafted; "
                        "re-read the block before proposing again."
                    ),
                )
        expected_revision = params.get("expectedBaseBlockRevision")
        if expected_revision is not None:
            if (
                not isinstance(expected_revision, int)
                or isinstance(expected_revision, bool)
                or expected_revision != block["revision"]
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Block changed while the proposal was being drafted; "
                        "re-read the block before proposing again."
                    ),
                )
        replacement_markdown = params.get("replacementMarkdown")
        if not isinstance(replacement_markdown, str) or len(replacement_markdown) > MAX_BLOCK_MARKDOWN_CHARS:
            raise HTTPException(status_code=422, detail="proposal.create replacementMarkdown is invalid.")
        replacement_title = params.get("replacementTitle")
        if replacement_title is not None and (
            not isinstance(replacement_title, str) or len(replacement_title) > MAX_BLOCK_TITLE_CHARS
        ):
            raise HTTPException(status_code=422, detail="proposal.create replacementTitle is invalid.")
        evidence_ids = params.get("evidenceIds", [])
        if not isinstance(evidence_ids, list) or len(evidence_ids) > MAX_BLOCK_EVIDENCE_IDS:
            raise HTTPException(status_code=422, detail="proposal.create evidenceIds is invalid.")
        for ev_id in evidence_ids:
            if ev_id not in evidence:
                raise HTTPException(status_code=422, detail=f"proposal.create references unknown evidence {ev_id}.")
        explanation = params.get("explanation", "")
        if not isinstance(explanation, str) or len(explanation) > MAX_EXPLANATION_CHARS:
            raise HTTPException(status_code=422, detail="proposal.create explanation is invalid.")
        prop_id = _new_id("prop")
        prop: Dict[str, Any] = {
            "id": prop_id,
            "documentId": doc_id,
            "blockId": block_id,
            "baseBlockRevision": block["revision"],
            "baseContentHash": block["contentHash"],
            "replacementMarkdown": replacement_markdown,
            "evidenceIds": [str(i) for i in evidence_ids],
            "explanation": explanation,
            "createdAt": now,
            "status": "pending",
        }
        if replacement_title is not None:
            prop["replacementTitle"] = replacement_title
        source_node_id = params.get("sourceNodeId")
        if source_node_id is not None:
            if not isinstance(source_node_id, str) or len(source_node_id) > 128:
                raise HTTPException(status_code=422, detail="proposal.create sourceNodeId is invalid.")
            prop["sourceNodeId"] = source_node_id
        request_id = params.get("requestId")
        if request_id is not None:
            if not isinstance(request_id, str) or len(request_id) > 128:
                raise HTTPException(status_code=422, detail="proposal.create requestId is invalid.")
            prop["requestId"] = request_id
        proposals[prop_id] = prop
        return new_state, {"proposalId": prop_id, "status": "pending"}

    if op == "proposal.accept":
        prop_id = params.get("proposalId")
        prop = proposals.get(prop_id)
        if prop is None:
            raise HTTPException(status_code=422, detail="Proposal does not exist.")
        if prop["status"] != "pending":
            raise HTTPException(status_code=422, detail=f"Proposal is {prop['status']}.")
        doc = documents.get(prop["documentId"])
        if doc is None:
            raise HTTPException(status_code=422, detail="Proposal document does not exist.")
        block = doc["blocks"].get(prop["blockId"])
        if block is None:
            raise HTTPException(status_code=422, detail="Proposal block does not exist.")
        if prop["baseBlockRevision"] != block["revision"] or prop["baseContentHash"] != block["contentHash"]:
            prop["status"] = "conflicted"
            return new_state, {
                "proposalId": prop_id,
                "status": "conflicted",
                "blockRevision": block["revision"],
                "conflicted": True,
            }
        if prop.get("replacementTitle") is not None:
            block["title"] = prop["replacementTitle"]
        block["markdown"] = prop["replacementMarkdown"]
        block["revision"] += 1
        block["updatedAt"] = now
        block["updatedBy"] = "agent"
        block["contentHash"] = _block_content_hash(block)
        doc["revision"] += 1
        doc["updatedAt"] = now
        prop["status"] = "accepted"
        prop["acceptedBy"] = "human"
        return new_state, {
            "proposalId": prop_id,
            "status": "accepted",
            "blockRevision": block["revision"],
            "documentRevision": doc["revision"],
        }

    if op == "proposal.reject":
        prop_id = params.get("proposalId")
        prop = proposals.get(prop_id)
        if prop is None:
            raise HTTPException(status_code=422, detail="Proposal does not exist.")
        if prop["status"] == "accepted":
            raise HTTPException(status_code=422, detail="Accepted proposal cannot be rejected.")
        prop["status"] = "rejected"
        return new_state, {"proposalId": prop_id, "status": "rejected"}

    raise HTTPException(status_code=422, detail=f"Unsupported operation: {op}")


def apply_operation(workspace_id: str, payload: Any) -> Dict[str, Any]:
    _workspace_paths(workspace_id)
    if not isinstance(payload, Mapping):
        raise HTTPException(status_code=422, detail="Operation payload must be an object.")
    operation_id = payload.get("operationId")
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
        raise HTTPException(status_code=422, detail="operationId is required.")
    op = payload.get("op")
    if op not in ALLOWED_OPS:
        raise HTTPException(status_code=422, detail="Unknown operation.")
    base_revision = payload.get("baseRevision")
    if not isinstance(base_revision, int) or isinstance(base_revision, bool) or base_revision < 0:
        raise HTTPException(status_code=422, detail="baseRevision is required.")
    params = {k: v for k, v in payload.items() if k not in ("operationId", "op", "baseRevision")}
    fingerprint = _request_fingerprint(op, params)

    with _WRITE_LOCK:
        path, papers_path, artifacts_path = _migrate_legacy_workspace(workspace_id)
        envelope = _read_envelope(path)
        current_revision = int(envelope.get("revision", 0)) if envelope else 0
        if envelope is None:
            current_state: Dict[str, Any] = {
                "tree": None,
                "active_paper": None,
                "documents": {},
                "evidence": {},
                "proposals": {},
                "dedup": {},
            }
        else:
            version = envelope.get("schema_version") or 1
            if not isinstance(version, int) or isinstance(version, bool) or version not in KNOWN_SCHEMA_VERSIONS:
                raise HTTPException(status_code=413, detail="Workspace schema version is unsupported.")
            current_state = _validate_full_state(envelope.get("state"), version)

        dedup = current_state.get("dedup", {})
        existing = dedup.get(operation_id)
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise HTTPException(status_code=409, detail={
                    "message": "operationId was reused with different content.",
                    "current_revision": current_revision,
                })
            stored_result = existing.get("result")
            if isinstance(stored_result, Mapping) and stored_result.get("conflicted"):
                raise HTTPException(status_code=409, detail={
                    "message": "Block changed since the proposal was created.",
                    "proposal_status": "conflicted",
                    "committed_revision": existing.get("committed_revision"),
                    "current_revision": current_revision,
                })
            return {
                "operation_id": operation_id,
                "duplicate": True,
                "committed_revision": existing.get("committed_revision"),
                "current_revision": current_revision,
                "result": stored_result,
            }

        if base_revision != current_revision:
            raise HTTPException(status_code=409, detail={
                "message": "Workspace revision conflict.",
                "current_revision": current_revision,
            })

        if current_state["tree"] is None:
            raise HTTPException(status_code=422, detail="Workspace has no tree yet; create the conversation tree first.")

        new_state, result = _execute_operation(current_state, op, params)
        new_revision = current_revision + 1
        new_state["dedup"] = dict(new_state.get("dedup", {}))
        new_state["dedup"][operation_id] = {
            "fingerprint": fingerprint,
            "result": result,
            "committed_revision": new_revision,
            "ts": _now_iso(),
        }
        _trim_dedup(new_state["dedup"])
        new_envelope = {
            "schema_version": SCHEMA_VERSION,
            "workspace_id": workspace_id,
            "revision": new_revision,
            "updated_at": _now_iso(),
            "state": new_state,
        }
        _write_envelope(path, papers_path, artifacts_path, workspace_id, new_envelope)

        if isinstance(result, Mapping) and result.get("conflicted"):
            raise HTTPException(status_code=409, detail={
                "message": "Block changed since the proposal was created.",
                "proposal_status": "conflicted",
                "committed_revision": new_revision,
                "current_revision": new_revision,
            })

    return {
        "operation_id": operation_id,
        "duplicate": False,
        "committed_revision": new_revision,
        "current_revision": new_revision,
        "result": result,
    }


def _agent_operation_id(op: str, params: Mapping[str, Any]) -> str:
    digest = _sha256(_stable_json({"op": op, **params}))
    return f"agent_{digest[:40]}"


def run_server_operation(
    workspace_id: str, op: str, params: Dict[str, Any]
) -> Dict[str, Any]:
    """Run an operation on behalf of a server-side actor (an agent tool).

    The operationId is deterministic, so a repeated identical tool call is deduped
    instead of creating a second proposal. The revision is read immediately before
    the call and retried once if a concurrent human write bumped it; a baseline
    conflict is never retried.
    """
    if op not in ALLOWED_OPS:
        raise HTTPException(status_code=422, detail="Unknown operation.")
    operation_id = _agent_operation_id(op, params)
    last_error: Any = None
    for _ in range(2):
        workspace = load_workspace(workspace_id)
        if not workspace.get("exists"):
            raise HTTPException(status_code=422, detail="Workspace does not exist.")
        revision = int(workspace.get("revision") or 0)
        try:
            return apply_operation(
                workspace_id,
                {"operationId": operation_id, "baseRevision": revision, "op": op, **params},
            )
        except HTTPException as exc:
            detail = exc.detail
            is_revision_race = (
                exc.status_code == 409
                and isinstance(detail, Mapping)
                and detail.get("message") == "Workspace revision conflict."
            )
            if not is_revision_race:
                raise
            last_error = exc
    raise last_error
