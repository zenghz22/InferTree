from __future__ import annotations

import hashlib
import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

from .paper_parser import parse_pdf


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAPERS_ROOT = PROJECT_ROOT / "data" / "papers"
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_REDIRECTS = 4
ALLOWED_PAPER_HOSTS = {
    "arxiv.org",
    "export.arxiv.org",
    "openreview.net",
    "api2.openreview.net",
    "aclanthology.org",
    "proceedings.mlr.press",
    "openaccess.thecvf.com",
    "papers.nips.cc",
    "jmlr.org",
    "www.jmlr.org",
}
ARXIV_ID_RE = re.compile(
    r"(?:arxiv\s*:\s*)?(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[a-z]{2})?/\d{7}(?:v\d+)?)",
    re.IGNORECASE,
)


class PaperDownloadError(ValueError):
    pass


def _allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and (parsed.hostname or "").lower() in ALLOWED_PAPER_HOSTS
    )


def normalize_paper_source(source: Any) -> str:
    text = str(source or "").strip()
    if not text:
        raise PaperDownloadError("A paper URL or arXiv identifier is required")

    direct_id = ARXIV_ID_RE.fullmatch(text)
    if direct_id:
        return f"https://arxiv.org/pdf/{direct_id.group(1)}"

    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.hostname:
        raise PaperDownloadError("Only HTTPS URLs or arXiv identifiers are supported")
    if not _allowed_url(text):
        raise PaperDownloadError(f"Unsupported paper repository: {parsed.hostname}")

    host = parsed.hostname.lower()
    if host in {"arxiv.org", "export.arxiv.org"}:
        path = parsed.path.rstrip("/")
        match = re.fullmatch(r"/(?:abs|pdf)/(.+?)(?:\.pdf)?", path, re.IGNORECASE)
        if not match or not ARXIV_ID_RE.fullmatch(match.group(1)):
            raise PaperDownloadError("The arXiv URL does not contain a valid paper identifier")
        return f"https://arxiv.org/pdf/{match.group(1)}"

    if host in {"openreview.net", "api2.openreview.net"}:
        if parsed.path.rstrip("/") not in {"/pdf", "/attachment"} and not parsed.path.startswith("/pdf/"):
            raise PaperDownloadError("The OpenReview URL is not a PDF endpoint")
        if parsed.path.rstrip("/") == "/pdf" and not parse_qs(parsed.query).get("id"):
            raise PaperDownloadError("The OpenReview PDF URL is missing its id parameter")
        return text

    if not parsed.path.lower().endswith(".pdf"):
        raise PaperDownloadError("The repository URL must point directly to a PDF")
    return text


def _arxiv_id_from_pdf_url(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in {"arxiv.org", "export.arxiv.org"}:
        return ""
    match = re.fullmatch(r"/pdf/(.+?)(?:\.pdf)?", parsed.path, re.IGNORECASE)
    return match.group(1) if match and ARXIV_ID_RE.fullmatch(match.group(1)) else ""


async def _arxiv_title(client: httpx.AsyncClient, pdf_url: str) -> str:
    arxiv_id = _arxiv_id_from_pdf_url(pdf_url)
    if not arxiv_id:
        return ""
    metadata_url = "https://export.arxiv.org/api/query?" + urlencode(
        {"id_list": arxiv_id, "max_results": 1}
    )
    try:
        response = await client.get(metadata_url, follow_redirects=False)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        title = root.findtext(
            "{http://www.w3.org/2005/Atom}entry/{http://www.w3.org/2005/Atom}title",
            default="",
        )
        return " ".join(title.split())
    except (httpx.HTTPError, ET.ParseError):
        return ""


async def _download_with_client(
    source: Any,
    client: httpx.AsyncClient,
    paper_root: Path = PAPERS_ROOT,
) -> Dict[str, Any]:
    current_url = normalize_paper_source(source)
    paper_root.mkdir(parents=True, exist_ok=True)
    temporary = paper_root / f".download-{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    total_bytes = 0
    final_url = current_url
    created_stored = False
    stored: Optional[Path] = None
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not _allowed_url(current_url):
                raise PaperDownloadError("Download redirect left the supported repository allowlist")
            async with client.stream("GET", current_url, follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "")
                    if not location:
                        raise PaperDownloadError("Paper repository returned an empty redirect")
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                final_url = str(response.url)
                with temporary.open("wb") as target:
                    async for chunk in response.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > MAX_DOWNLOAD_BYTES:
                            raise PaperDownloadError("PDF exceeds the 50 MB download limit")
                        digest.update(chunk)
                        target.write(chunk)
                break
        else:
            raise PaperDownloadError("Paper repository redirected too many times")

        with temporary.open("rb") as downloaded_file:
            signature = downloaded_file.read(5)
        if total_bytes < 5 or signature != b"%PDF-":
            raise PaperDownloadError("Downloaded content is not a valid PDF file")

        paper_id = digest.hexdigest()
        stored = paper_root / f"{paper_id}.pdf"
        if stored.exists():
            temporary.unlink()
        else:
            temporary.replace(stored)
            created_stored = True
        parsed = parse_pdf(stored)
        metadata_title = await _arxiv_title(client, final_url)
        if metadata_title:
            parsed["title"] = metadata_title
        return {
            "paper_id": paper_id,
            "filename": Path(urlparse(final_url).path).name or f"{paper_id}.pdf",
            "title": parsed["title"],
            "page_count": parsed["page_count"],
            "character_count": parsed["character_count"],
            "source_url": final_url,
            "parsed": parsed,
        }
    except httpx.HTTPStatusError as exc:
        raise PaperDownloadError(
            f"Paper repository returned HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise PaperDownloadError(f"Paper download failed: {exc}") from exc
    except Exception:
        if created_stored and stored is not None:
            stored.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)


async def download_paper(
    source: Any,
    *,
    paper_root: Path = PAPERS_ROOT,
    client: Optional[httpx.AsyncClient] = None,
    use_system_proxy: bool = False,
) -> Dict[str, Any]:
    if client is not None:
        return await _download_with_client(source, client, paper_root)
    timeout = httpx.Timeout(connect=10, read=30, write=10, pool=10)
    async with httpx.AsyncClient(
        timeout=timeout,
        trust_env=use_system_proxy,
        headers={"User-Agent": "InferTree/0.6 paper downloader"},
    ) as owned_client:
        return await _download_with_client(source, owned_client, paper_root)
