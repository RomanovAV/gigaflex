from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import PurePosixPath
import re


FINDING_BLOCK_RE = re.compile(r"<FINDING>\s*(.*?)\s*</FINDING>", re.DOTALL)
FIELD_RE = re.compile(r"^([a-z_]+):[ \t]*(.*)$")
ALLOWED_SEVERITIES = {"blocker", "major", "minor"}
ALLOWED_CATEGORIES = {
    "complexity",
    "correctness",
    "data_quality",
    "documentation",
    "methodology",
    "performance",
    "regression",
    "reliability",
    "requirements",
    "security",
    "testing",
    "traceability",
    "validation",
}
REQUIRED_FIELDS = (
    "severity",
    "category",
    "file",
    "line",
    "evidence",
    "impact",
    "suggested_fix",
)
MAX_FINDINGS = 20
MAX_OUTPUT_CHARS = 100_000
MAX_FIELD_CHARS = 4_000


class ReviewOutputError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewFinding:
    severity: str
    category: str
    file: str
    line: str
    evidence: str
    impact: str
    suggested_fix: str


def parse_review_output(text: str) -> list[ReviewFinding]:
    stripped = text.strip()
    if stripped == "NO FINDINGS":
        return []
    if not stripped:
        raise ReviewOutputError("empty output; expected NO FINDINGS or <FINDING> blocks")
    if len(stripped) > MAX_OUTPUT_CHARS:
        raise ReviewOutputError("output is too large")

    matches = list(FINDING_BLOCK_RE.finditer(stripped))
    if not matches:
        raise ReviewOutputError("expected NO FINDINGS or at least one <FINDING> block")
    if len(matches) > MAX_FINDINGS:
        raise ReviewOutputError(f"too many findings; maximum is {MAX_FINDINGS}")

    remainder = FINDING_BLOCK_RE.sub("", stripped)
    if remainder.strip():
        raise ReviewOutputError("text outside <FINDING> blocks is not allowed")

    return [_parse_finding_block(match.group(1)) for match in matches]


def render_review_output(findings: list[ReviewFinding]) -> str:
    if not findings:
        return "NO FINDINGS"

    blocks: list[str] = []
    for finding in findings:
        values = {
            "severity": finding.severity,
            "category": finding.category,
            "file": finding.file,
            "line": finding.line,
            "evidence": finding.evidence,
            "impact": finding.impact,
            "suggested_fix": finding.suggested_fix,
        }
        lines = ["<FINDING>"]
        lines.extend(f"{field}: {escape(values[field], quote=False)}" for field in REQUIRED_FIELDS)
        lines.append("</FINDING>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def normalize_review_output(text: str) -> str:
    return render_review_output(parse_review_output(text))


def _parse_finding_block(block: str) -> ReviewFinding:
    values: dict[str, str] = {}
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = FIELD_RE.match(line)
        if not match:
            raise ReviewOutputError(f"invalid finding line: {line[:80]}")
        field, value = match.groups()
        if field not in REQUIRED_FIELDS:
            raise ReviewOutputError(f"unknown finding field: {field}")
        if field in values:
            raise ReviewOutputError(f"duplicate finding field: {field}")
        value = value.strip()
        if not value:
            raise ReviewOutputError(f"empty finding field: {field}")
        if len(value) > MAX_FIELD_CHARS:
            raise ReviewOutputError(f"finding field is too long: {field}")
        values[field] = value

    missing = [field for field in REQUIRED_FIELDS if field not in values]
    if missing:
        raise ReviewOutputError(f"missing finding fields: {', '.join(missing)}")

    severity = values["severity"].lower()
    if severity not in ALLOWED_SEVERITIES:
        raise ReviewOutputError(
            f"invalid severity {values['severity']!r}; expected blocker, major, or minor"
        )

    category = values["category"].lower()
    if category not in ALLOWED_CATEGORIES:
        allowed = ", ".join(sorted(ALLOWED_CATEGORIES))
        raise ReviewOutputError(f"invalid category {values['category']!r}; expected one of: {allowed}")

    file = _normalize_repository_path(values["file"])
    line = values["line"].lower()
    if line != "unknown" and (not line.isdigit() or int(line) < 1):
        raise ReviewOutputError("line must be a positive integer or unknown")

    return ReviewFinding(
        severity=severity,
        category=category,
        file=file,
        line=line,
        evidence=values["evidence"],
        impact=values["impact"],
        suggested_fix=values["suggested_fix"],
    )


def _normalize_repository_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or normalized in {"", ".", ".."} or ".." in path.parts:
        raise ReviewOutputError("file must be a repository-relative path without parent traversal")
    return path.as_posix()
