import unittest

from backend.skill_loader import (
    load_paper_downloader_skill,
    load_paper_reviewer_skill,
    load_skill_catalog,
    load_paper_visual_extractor_skill,
)


class SkillLoaderTests(unittest.TestCase):
    def test_catalog_discovers_all_skill_entrypoints(self):
        catalog = load_skill_catalog()
        names = {item["name"] for item in catalog}
        self.assertEqual(
            {"paper-downloader", "paper-reviewer", "paper-search", "paper-visual-extractor"},
            names,
        )
        self.assertTrue(all(item["description"] for item in catalog))

    def test_loads_all_skill_parts(self):
        skill = load_paper_reviewer_skill()
        self.assertEqual(
            {"instructions", "rubric", "output_schema", "story_lecture"}, set(skill)
        )
        self.assertIn("name: paper-reviewer", skill["instructions"])
        self.assertIn("Technical soundness", skill["rubric"])
        self.assertIn("expert professor", skill["story_lecture"])

    def test_loads_visual_extractor(self):
        skill = load_paper_visual_extractor_skill()
        self.assertEqual({"instructions", "classification"}, set(skill))
        self.assertIn("name: paper-visual-extractor", skill["instructions"])
        self.assertIn("Main mechanism", skill["classification"])

    def test_loads_paper_downloader(self):
        skill = load_paper_downloader_skill()
        self.assertEqual({"instructions"}, set(skill))
        self.assertIn("name: paper-downloader", skill["instructions"])
        self.assertIn("download_paper", skill["instructions"])


if __name__ == "__main__":
    unittest.main()
