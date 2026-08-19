from __future__ import annotations

from datetime import datetime
from pathlib import Path
import hashlib
import re
import unicodedata


FENCED_MARKDOWN_RE = re.compile(r"```(?:markdown|md)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
SLUG_PART_RE = re.compile(r"[^a-z0-9]+")


def clean_plan_output(text: str) -> str:
    stripped = text.strip()
    match = FENCED_MARKDOWN_RE.search(stripped)
    if match:
        stripped = match.group(1).strip()
    return stripped + "\n"


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = SLUG_PART_RE.sub("-", ascii_text).strip("-")
    if slug:
        return slug[:60].strip("-")
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return f"plan-{digest}"


def next_plan_path(plans_dir: Path, request: str, now: datetime | None = None) -> Path:
    plans_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d")
    slug = slugify(request)
    path = plans_dir / f"{stamp}-{slug}.md"
    if not path.exists():
        return path

    index = 2
    while True:
        candidate = plans_dir / f"{stamp}-{slug}-{index}.md"
        if not candidate.exists():
            return candidate
        index += 1
