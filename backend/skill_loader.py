import re
from pathlib import Path
from typing import Dict, List


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = PROJECT_ROOT / "skills"
SKILL_ROOT = SKILLS_ROOT / "paper-reviewer"
VISUAL_SKILL_ROOT = SKILLS_ROOT / "paper-visual-extractor"
DOWNLOAD_SKILL_ROOT = SKILLS_ROOT / "paper-downloader"
SEARCH_SKILL_ROOT = SKILLS_ROOT / "paper-search"


def load_skill_catalog() -> List[Dict[str, str]]:
    """Return discovery metadata for every valid local Skill entrypoint."""
    catalog: List[Dict[str, str]] = []
    if not SKILLS_ROOT.is_dir():
        return catalog
    for entrypoint in sorted(SKILLS_ROOT.glob("*/SKILL.md")):
        text = entrypoint.read_text(encoding="utf-8")
        frontmatter = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|\Z)", text, re.DOTALL)
        if not frontmatter:
            continue
        fields: Dict[str, str] = {}
        for line in frontmatter.group(1).splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in {"name", "description"}:
                fields[key.strip()] = value.strip().strip('"\'')
        name = fields.get("name", "")
        description = fields.get("description", "")
        if not name or not description or entrypoint.parent.name != name:
            continue
        catalog.append({"name": name, "description": description})
    return catalog


def load_paper_reviewer_skill() -> Dict[str, str]:
    files = {
        "instructions": SKILL_ROOT / "SKILL.md",
        "rubric": SKILL_ROOT / "references" / "review-rubric.md",
        "output_schema": SKILL_ROOT / "references" / "output-schema.md",
        "story_lecture": SKILL_ROOT / "references" / "story-lecture.md",
    }
    return {name: path.read_text(encoding="utf-8") for name, path in files.items()}


def load_paper_visual_extractor_skill() -> Dict[str, str]:
    files = {
        "instructions": VISUAL_SKILL_ROOT / "SKILL.md",
        "classification": VISUAL_SKILL_ROOT / "references" / "classification.md",
    }
    return {name: path.read_text(encoding="utf-8") for name, path in files.items()}


def load_paper_downloader_skill() -> Dict[str, str]:
    return {
        "instructions": (DOWNLOAD_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    }


def load_paper_search_skill() -> Dict[str, str]:
    return {
        "instructions": (SEARCH_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    }
