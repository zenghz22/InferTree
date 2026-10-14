import asyncio
import tempfile
import unittest
from pathlib import Path

import fitz
import httpx

from backend.paper_downloader import (
    PaperDownloadError,
    download_paper,
    normalize_paper_source,
)


def sample_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "A Downloaded Test Paper")
    payload = document.tobytes()
    document.close()
    return payload


class PaperDownloaderTests(unittest.TestCase):
    def test_normalizes_arxiv_identifiers_and_pages(self):
        expected = "https://arxiv.org/pdf/1706.03762"
        self.assertEqual(expected, normalize_paper_source("1706.03762"))
        self.assertEqual(expected, normalize_paper_source("arXiv:1706.03762"))
        self.assertEqual(
            expected, normalize_paper_source("https://arxiv.org/abs/1706.03762")
        )

    def test_rejects_arbitrary_or_non_https_downloads(self):
        with self.assertRaisesRegex(PaperDownloadError, "Unsupported paper repository"):
            normalize_paper_source("https://example.com/paper.pdf")
        with self.assertRaisesRegex(PaperDownloadError, "Only HTTPS"):
            normalize_paper_source("http://arxiv.org/pdf/1706.03762")

    def test_downloads_valid_pdf_into_content_addressed_storage(self):
        pdf = sample_pdf()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "export.arxiv.org":
                atom = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>Attention Is All You Need</title></entry>
</feed>"""
                return httpx.Response(200, content=atom, request=request)
            self.assertEqual("https://arxiv.org/pdf/1706.03762", str(request.url))
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=pdf,
                request=request,
            )

        async def exercise(paper_root: Path):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await download_paper(
                    "1706.03762", paper_root=paper_root, client=client
                )

        with tempfile.TemporaryDirectory() as temporary:
            paper_root = Path(temporary) / "papers"
            result = asyncio.run(exercise(paper_root))
            self.assertEqual("Attention Is All You Need", result["title"])
            self.assertEqual(1, result["page_count"])
            self.assertTrue((paper_root / f"{result['paper_id']}.pdf").exists())
            self.assertFalse(list(paper_root.glob(".download-*.tmp")))

    def test_rejects_redirect_to_untrusted_host(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302,
                headers={"location": "https://example.com/paper.pdf"},
                request=request,
            )

        async def exercise(paper_root: Path):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await download_paper(
                    "1706.03762", paper_root=paper_root, client=client
                )

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(PaperDownloadError, "allowlist"):
                asyncio.run(exercise(Path(temporary) / "papers"))


if __name__ == "__main__":
    unittest.main()
