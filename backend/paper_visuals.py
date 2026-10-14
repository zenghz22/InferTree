from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Sequence

import fitz
from fastapi import HTTPException


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAPERS_ROOT = PROJECT_ROOT / "data" / "papers"
VISUAL_ROOT = PROJECT_ROOT / "data" / "visuals"
PAPER_ID_RE = re.compile(r"[0-9a-f]{64}")
VISUAL_ID_RE = re.compile(r"p\d+-(?:figure|table)-[A-Za-z0-9_-]+")
CAPTION_RE = re.compile(
    r"^(Figure|Fig\.?|Table)\s*([A-Za-z]?\d+[A-Za-z]?|[IVXLCDM]+)"
    r"(?:\s*[:.\-–—]\s*|\s+)(.*)",
    re.IGNORECASE | re.DOTALL,
)
METHOD_TERMS = (
    "overview", "framework", "architecture", "pipeline", "workflow",
    "overall method", "proposed method", "model structure", "system diagram",
    "method overview", "approach overview",
)
RESULT_TERMS = (
    "main result", "main experimental", "comparison with", "comparison against",
    "benchmark", "state-of-the-art", "quantitative result", "overall performance",
    "results on", "performance comparison",
)
SUPPORTING_TERMS = (
    "ablation", "sensitivity", "robustness", "efficiency", "qualitative",
    "case study", "error analysis", "additional result", "hyperparameter",
)
DEEMPHASIS_TERMS = (
    "dataset example", "visualization example", "supplementary", "appendix",
    "user interface", "prompt template",
)


def paper_path(paper_id: Any) -> Path:
    if not isinstance(paper_id, str) or not PAPER_ID_RE.fullmatch(paper_id):
        raise HTTPException(status_code=422, detail="A valid uploaded paper_id is required.")
    path = PAPERS_ROOT / f"{paper_id}.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="The selected paper is no longer available.")
    return path


def _caption_candidates(page: fitz.Page, page_number: int) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for block in page.get_text("blocks", sort=True):
        x0, y0, x1, y1, text = block[:5]
        normalized = " ".join(str(text).split())
        match = CAPTION_RE.search(normalized)
        if not match:
            continue
        kind = "table" if match.group(1).lower().startswith("table") else "figure"
        number = match.group(2)
        base_id = f"p{page_number}-{kind}-{number}"
        seen[base_id] = seen.get(base_id, 0) + 1
        visual_id = base_id if seen[base_id] == 1 else f"{base_id}-{seen[base_id]}"
        candidates.append(
            {
                "id": visual_id,
                "page": page_number,
                "type": kind,
                "label": f"{kind.title()} {number}",
                "caption": normalized[match.start(1):][:1600],
                "detection": "text-block",
                "_bbox": [float(x0), float(y0), float(x1), float(y1)],
            }
        )

    # Some publishers split a caption into independent text lines instead of a
    # normal PDF text block. A conservative line fallback recovers those labels
    # while preserving a real page location for later rendering.
    known = {(item["type"], item["label"].split()[-1].casefold()) for item in candidates}
    for raw_line in page.get_text("text", sort=True).splitlines():
        normalized = " ".join(raw_line.split())
        match = CAPTION_RE.match(normalized)
        if not match:
            continue
        kind = "table" if match.group(1).lower().startswith("table") else "figure"
        number = match.group(2)
        if (kind, number.casefold()) in known:
            continue
        locations = page.search_for(f"{match.group(1)} {number}")
        if not locations and match.group(1).lower().startswith("fig"):
            locations = page.search_for(f"Fig. {number}")
        if not locations:
            continue
        rect = locations[0]
        base_id = f"p{page_number}-{kind}-{number}"
        seen[base_id] = seen.get(base_id, 0) + 1
        visual_id = base_id if seen[base_id] == 1 else f"{base_id}-{seen[base_id]}"
        candidates.append(
            {
                "id": visual_id,
                "page": page_number,
                "type": kind,
                "label": f"{kind.title()} {number}",
                "caption": normalized[:1600],
                "detection": "text-line-fallback",
                "_bbox": [
                    float(max(0, rect.x0 - 4)),
                    float(max(0, rect.y0 - 3)),
                    float(min(page.rect.width, max(rect.x1 + 4, page.rect.width * 0.82))),
                    float(min(page.rect.height, rect.y1 + 24)),
                ],
            }
        )
        known.add((kind, number.casefold()))
    return candidates


def _matched_terms(text: str, terms: Sequence[str]) -> List[str]:
    return [term for term in terms if term in text]


def _classify_candidate(candidate: Dict[str, Any], page_count: int) -> Dict[str, Any]:
    caption = candidate["caption"].casefold()
    method_matches = _matched_terms(caption, METHOD_TERMS)
    result_matches = _matched_terms(caption, RESULT_TERMS)
    supporting_matches = _matched_terms(caption, SUPPORTING_TERMS)
    deemphasis_matches = _matched_terms(caption, DEEMPHASIS_TERMS)
    number_match = re.search(r"\d+", candidate["label"])
    number = int(number_match.group()) if number_match else 99
    early_page = candidate["page"] <= max(4, round(page_count * 0.35))

    method_score = min(70, len(method_matches) * 32)
    result_score = min(70, len(result_matches) * 32)
    supporting_score = min(75, len(supporting_matches) * 38)
    if candidate["type"] == "figure":
        method_score += 12
    if candidate["type"] == "table":
        result_score += 12
    if number <= 2:
        method_score += 10
        result_score += 8
    if early_page:
        method_score += 10
    if "proposed" in caption or "our method" in caption:
        method_score += 12
    if any(term in caption for term in ("performance", "accuracy", "comparison", "results")):
        result_score += 16
    if deemphasis_matches:
        method_score -= 22
        result_score -= 22

    if supporting_score >= 38 and supporting_score >= max(method_score, result_score):
        category = "supporting_experiment"
        score = supporting_score
        evidence = supporting_matches
    elif method_score >= 38 and method_score >= result_score:
        category = "main_method"
        score = method_score
        evidence = method_matches or ["early method figure evidence"]
    elif result_score >= 38:
        category = "main_result"
        score = result_score
        evidence = result_matches or ["quantitative result wording"]
    else:
        category = "other"
        score = max(0, method_score, result_score, supporting_score)
        evidence = deemphasis_matches

    score = max(0, min(100, score))
    confidence = "high" if score >= 72 else "medium" if score >= 50 else "low"
    return {
        **candidate,
        "category": category,
        "importance_score": score,
        "confidence": confidence,
        "evidence": evidence[:5],
    }


def _rank_candidates(candidates: List[Dict[str, Any]], page_count: int) -> List[Dict[str, Any]]:
    classified = [_classify_candidate(candidate, page_count) for candidate in candidates]
    for category in ("main_method", "main_result", "supporting_experiment", "other"):
        group = sorted(
            (item for item in classified if item["category"] == category),
            key=lambda item: (-item["importance_score"], item["page"], item["id"]),
        )
        for rank, item in enumerate(group, start=1):
            item["category_rank"] = rank
            item["recommended"] = (
                (category == "main_method" and rank == 1 and item["importance_score"] >= 38)
                or (category == "main_result" and rank <= 2 and item["importance_score"] >= 38)
            )
    return classified


def list_paper_visuals(paper_id: Any) -> List[Dict[str, Any]]:
    path = paper_path(paper_id)
    results: List[Dict[str, Any]] = []
    with fitz.open(path) as document:
        for index, page in enumerate(document):
            results.extend(_caption_candidates(page, index + 1))
        results = _rank_candidates(results, len(document))
    return [{key: value for key, value in item.items() if not key.startswith("_")} for item in results]


def _find_candidate(document: fitz.Document, visual_id: str) -> Dict[str, Any] | None:
    for index, page in enumerate(document):
        for candidate in _caption_candidates(page, index + 1):
            if candidate["id"] == visual_id:
                return candidate
    return None


def _figure_clip(page: fitz.Page, bbox: Sequence[float]) -> fitz.Rect:
    x0, y0, x1, y1 = bbox
    page_rect = page.rect
    image_rects: List[fitz.Rect] = []
    for image in page.get_images(full=True):
        try:
            image_rects.extend(page.get_image_rects(image[0]))
        except (ValueError, RuntimeError):
            continue
    nearby = [
        rect
        for rect in image_rects
        if rect.y1 <= y0 + 8 and y0 - rect.y1 <= page_rect.height * 0.48
    ]
    if nearby:
        chosen = max(nearby, key=lambda rect: rect.width * rect.height)
        # A tiny embedded icon inside a vector diagram is not the whole figure.
        # In that case the broad caption-relative fallback preserves the diagram.
        substantial = (
            chosen.width >= page_rect.width * 0.28
            or chosen.width * chosen.height >= page_rect.width * page_rect.height * 0.02
        )
        if substantial:
            clip = fitz.Rect(
                max(0, min(chosen.x0, x0) - 8),
                max(0, chosen.y0 - 8),
                min(page_rect.width, max(chosen.x1, x1) + 8),
                min(page_rect.height, y1 + 6),
            )
            return clip & page_rect
    if not nearby or not substantial:
        clip = fitz.Rect(
            max(0, x0 - 12),
            max(0, y0 - min(320, page_rect.height * 0.46)),
            min(page_rect.width, x1 + 12),
            min(page_rect.height, y1 + 6),
        )
    return clip & page_rect


def _table_clip(page: fitz.Page, bbox: Sequence[float]) -> fitz.Rect:
    x0, y0, x1, y1 = bbox
    page_rect = page.rect
    table_rects: List[fitz.Rect] = []
    try:
        table_rects = [fitz.Rect(table.bbox) for table in page.find_tables().tables]
    except (AttributeError, RuntimeError, ValueError):
        pass

    def horizontally_related(rect: fitz.Rect) -> bool:
        overlap = max(0.0, min(x1, rect.x1) - max(x0, rect.x0))
        return overlap >= min(x1 - x0, rect.width) * 0.45

    above = [
        rect
        for rect in table_rects
        if horizontally_related(rect)
        and rect.y1 <= y0 + 8
        and y0 - rect.y1 <= page_rect.height * 0.55
    ]
    below = [
        rect
        for rect in table_rects
        if horizontally_related(rect)
        and rect.y0 >= y1 - 8
        and rect.y0 - y1 <= page_rect.height * 0.55
    ]
    if above:
        return fitz.Rect(
            max(0, min(x0, *(rect.x0 for rect in above)) - 8),
            max(0, min(rect.y0 for rect in above) - 8),
            min(page_rect.width, max(x1, *(rect.x1 for rect in above)) + 8),
            min(page_rect.height, y1 + 6),
        ) & page_rect
    if below:
        return fitz.Rect(
            max(0, min(x0, *(rect.x0 for rect in below)) - 8),
            max(0, y0 - 6),
            min(page_rect.width, max(rect.x1 for rect in below) + 8),
            min(page_rect.height, max(rect.y1 for rect in below) + 8),
        ) & page_rect

    # When table detection is inconclusive, captions in the lower half usually
    # follow their table; captions near the top more often precede it.
    if y0 >= page_rect.height * 0.4:
        return fitz.Rect(
            max(0, x0 - 12),
            max(0, y0 - min(360, page_rect.height * 0.5)),
            min(page_rect.width, x1 + 12),
            min(page_rect.height, y1 + 6),
        ) & page_rect
    return fitz.Rect(
        max(0, x0 - 12),
        max(0, y0 - 6),
        min(page_rect.width, x1 + 12),
        min(page_rect.height, y1 + min(360, page_rect.height * 0.5)),
    ) & page_rect


def render_paper_visuals(paper_id: Any, visual_ids: Any) -> List[Dict[str, Any]]:
    path = paper_path(paper_id)
    if not isinstance(visual_ids, list) or not visual_ids:
        raise HTTPException(status_code=422, detail="visual_ids must be a non-empty array.")
    safe_ids = [str(item) for item in visual_ids[:4] if VISUAL_ID_RE.fullmatch(str(item))]
    if not safe_ids:
        raise HTTPException(status_code=422, detail="No valid visual identifiers were supplied.")

    output_dir = VISUAL_ROOT / str(paper_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered: List[Dict[str, Any]] = []
    with fitz.open(path) as document:
        for visual_id in safe_ids:
            candidate = _find_candidate(document, visual_id)
            if candidate is None:
                continue
            page = document[candidate["page"] - 1]
            clip = (
                _table_clip(page, candidate["_bbox"])
                if candidate["type"] == "table"
                else _figure_clip(page, candidate["_bbox"])
            )
            filename = f"{visual_id}.png"
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False)
            pixmap.save(output_dir / filename)
            rendered.append(
                {
                    "id": visual_id,
                    "page": candidate["page"],
                    "type": candidate["type"],
                    "label": candidate["label"],
                    "caption": candidate["caption"],
                    "url": f"/api/papers/{paper_id}/visuals/{filename}",
                }
            )
    return rendered


def visual_file(paper_id: str, filename: str) -> Path:
    paper_path(paper_id)
    if not re.fullmatch(r"p\d+-(?:figure|table)-[A-Za-z0-9_-]+\.png", filename):
        raise HTTPException(status_code=404, detail="Visual not found.")
    path = VISUAL_ROOT / paper_id / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Visual not found.")
    return path
