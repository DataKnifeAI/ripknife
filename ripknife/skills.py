"""Repo skills the pipeline sends to the model.

Call sites load the skill body (frontmatter stripped) and append only the
per-call facts. Prompt files live under ``skills/`` at the repo root.
"""

from __future__ import annotations

import re
from pathlib import Path

from ripknife.paths import INSTALL_ROOT

SKILLS_ROOT = INSTALL_ROOT / "skills"
_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)


def skill_path(name: str) -> Path:
    """Path of one project skill. ``name`` is the directory under ``skills/``."""
    return SKILLS_ROOT / name / "SKILL.md"


def read_skill(path: Path) -> str:
    """Instruction body of a skill file. Raises when the file is missing."""
    if not path.is_file():
        raise FileNotFoundError(f"skill not found: {path}")
    text = path.read_text(encoding="utf-8")
    body = _FRONTMATTER.sub("", text, count=1).strip()
    return body + "\n"


def load_skill(name: str) -> str:
    """Load ``skills/<name>/SKILL.md`` and return the instruction body."""
    return read_skill(skill_path(name))
