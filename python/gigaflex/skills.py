from __future__ import annotations

from importlib import resources
from pathlib import Path


PLANNING_SKILL_NAME = "planning"
PLANNING_SKILL_RESOURCE = "assets/planning/SKILL.md"
SUPERPOWERS_CONVERTER_SKILL_NAME = "superpowers-to-gigaflex"
SUPERPOWERS_CONVERTER_SKILL_RESOURCE = "assets/superpowers-to-gigaflex/SKILL.md"


def _skill_path(skills_dir: Path, name: str) -> Path:
    return skills_dir.expanduser() / name / "SKILL.md"


def _resource_text(resource_path: str) -> str:
    resource = resources.files("gigaflex")
    for part in resource_path.split("/"):
        resource = resource.joinpath(part)
    return resource.read_text(encoding="utf-8")


def _install_skill(skills_dir: Path, name: str, content: str, force: bool = False) -> tuple[Path, bool]:
    target = _skill_path(skills_dir, name)
    if target.exists():
        if target.read_text(encoding="utf-8") == content:
            return target, False
        if not force:
            raise FileExistsError(
                f"{name} skill already exists and differs from the bundled version: {target}"
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target, True


def planning_skill_path(skills_dir: Path) -> Path:
    return _skill_path(skills_dir, PLANNING_SKILL_NAME)


def planning_skill_text() -> str:
    return _resource_text(PLANNING_SKILL_RESOURCE)


def planning_skill_installed(skills_dir: Path) -> bool:
    return planning_skill_path(skills_dir).is_file()


def install_planning_skill(skills_dir: Path, force: bool = False) -> tuple[Path, bool]:
    return _install_skill(
        skills_dir,
        PLANNING_SKILL_NAME,
        planning_skill_text(),
        force=force,
    )


def superpowers_converter_skill_path(skills_dir: Path) -> Path:
    return _skill_path(skills_dir, SUPERPOWERS_CONVERTER_SKILL_NAME)


def superpowers_converter_skill_text() -> str:
    return _resource_text(SUPERPOWERS_CONVERTER_SKILL_RESOURCE)


def superpowers_converter_skill_installed(skills_dir: Path) -> bool:
    return superpowers_converter_skill_path(skills_dir).is_file()


def install_superpowers_converter_skill(
    skills_dir: Path,
    force: bool = False,
) -> tuple[Path, bool]:
    return _install_skill(
        skills_dir,
        SUPERPOWERS_CONVERTER_SKILL_NAME,
        superpowers_converter_skill_text(),
        force=force,
    )
