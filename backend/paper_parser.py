from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List

import fitz


@dataclass
class PageText:
    page: int
    text: str


def _guess_title(pages: List[PageText], fallback: str) -> str:
    if not pages:
        return fallback
    for line in pages[0].text.splitlines():
        candidate = re.sub(r"\s+", " ", line).strip()
        if 8 <= len(candidate) <= 240:
            return candidate
    return fallback


def parse_pdf(path: Path) -> Dict[str, Any]:
    """Extract page-addressable text from a PDF without inventing structure."""
    pages: List[PageText] = []
    with fitz.open(path) as document:
        metadata = document.metadata or {}
        for index, page in enumerate(document):
            text = page.get_text("text").strip()
            pages.append(PageText(page=index + 1, text=text))

    fallback = path.stem
    title = (metadata.get("title") or "").strip() or _guess_title(pages, fallback)
    return {
        "title": title,
        "page_count": len(pages),
        "character_count": sum(len(item.text) for item in pages),
        "pages": [asdict(item) for item in pages],
    }

