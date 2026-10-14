import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from backend.paper_visuals import (
    _figure_clip,
    _table_clip,
    list_paper_visuals,
    render_paper_visuals,
)


class PaperVisualTests(unittest.TestCase):
    def test_lists_and_renders_captioned_figure(self):
        paper_id = "a" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            papers = root / "papers"
            visuals = root / "visuals"
            papers.mkdir()
            document = fitz.open()
            page = document.new_page()
            page.draw_rect(fitz.Rect(80, 80, 500, 350), color=(0, 0, 0))
            page.insert_textbox(
                fitz.Rect(80, 360, 500, 410),
                "Figure 1: Overview of the proposed architecture and pipeline.",
            )
            document.save(papers / f"{paper_id}.pdf")
            document.close()

            with patch("backend.paper_visuals.PAPERS_ROOT", papers), patch(
                "backend.paper_visuals.VISUAL_ROOT", visuals
            ):
                candidates = list_paper_visuals(paper_id)
                self.assertEqual("p1-figure-1", candidates[0]["id"])
                self.assertEqual("main_method", candidates[0]["category"])
                self.assertTrue(candidates[0]["recommended"])
                rendered = render_paper_visuals(paper_id, ["p1-figure-1"])
                self.assertEqual(1, len(rendered))
                self.assertTrue((visuals / paper_id / "p1-figure-1.png").exists())

    def test_accepts_period_separated_figure_and_table_captions(self):
        paper_id = "b" * 64
        with tempfile.TemporaryDirectory() as temporary:
            papers = Path(temporary) / "papers"
            papers.mkdir()
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 100), "Figure 2. Overview of the method.")
            page.insert_text((72, 160), "Table 1. Main experimental results.")
            document.save(papers / f"{paper_id}.pdf")
            document.close()

            with patch("backend.paper_visuals.PAPERS_ROOT", papers):
                candidates = list_paper_visuals(paper_id)

            self.assertEqual(
                ["p1-figure-2", "p1-table-1"],
                [candidate["id"] for candidate in candidates],
            )
            self.assertEqual("main_method", candidates[0]["category"])
            self.assertEqual("main_result", candidates[1]["category"])
            self.assertTrue(candidates[1]["recommended"])

    def test_ablation_table_is_supporting_not_main_result(self):
        paper_id = "d" * 64
        with tempfile.TemporaryDirectory() as temporary:
            papers = Path(temporary) / "papers"
            papers.mkdir()
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 100), "Table 3. Ablation study and sensitivity analysis.")
            document.save(papers / f"{paper_id}.pdf")
            document.close()

            with patch("backend.paper_visuals.PAPERS_ROOT", papers):
                candidate = list_paper_visuals(paper_id)[0]

            self.assertEqual("supporting_experiment", candidate["category"])
            self.assertFalse(candidate["recommended"])

    def test_ignores_figure_references_inside_body_text(self):
        paper_id = "c" * 64
        with tempfile.TemporaryDirectory() as temporary:
            papers = Path(temporary) / "papers"
            papers.mkdir()
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 100), "As shown in Figure 3, the method performs well.")
            document.save(papers / f"{paper_id}.pdf")
            document.close()

            with patch("backend.paper_visuals.PAPERS_ROOT", papers):
                self.assertEqual([], list_paper_visuals(paper_id))

    def test_tiny_embedded_icon_does_not_crop_vector_figure(self):
        document = fitz.open()
        page = document.new_page()
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
        pixmap.clear_with(0)
        page.insert_image(fitz.Rect(280, 210, 320, 250), pixmap=pixmap)
        clip = _figure_clip(page, (60, 350, 550, 380))
        self.assertLess(clip.y0, 100)
        document.close()

    def test_table_clip_includes_detected_table_above_caption(self):
        document = fitz.open()
        page = document.new_page()
        for y in (80, 120, 160, 200):
            page.draw_line((80, y), (520, y))
        for x in (80, 300, 520):
            page.draw_line((x, 80), (x, 200))
        for row, y in enumerate((100, 140, 180), start=1):
            page.insert_text((90, y), f"row {row}")
            page.insert_text((310, y), str(row * 10))
        clip = _table_clip(page, (60, 220, 550, 250))
        self.assertLessEqual(clip.y0, 80)
        self.assertGreaterEqual(clip.y1, 250)
        document.close()


if __name__ == "__main__":
    unittest.main()
