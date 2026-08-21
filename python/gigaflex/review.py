from __future__ import annotations

from dataclasses import dataclass
import hashlib
from html import escape
from pathlib import PurePosixPath
import re

from .signals import REVIEW_DONE


FINDING_BLOCK_RE = re.compile(r"<FINDING>\s*(.*?)\s*</FINDING>", re.DOTALL)
FINDING_OPEN_RE = re.compile(r"<FINDING>")
FINDING_CLOSE_RE = re.compile(r"</FINDING>")
NO_FINDINGS_LINE_RE = re.compile(r"^[ \t]*NO FINDINGS[ \t]*$", re.MULTILINE)
SYNTHESIS_DECISION_BLOCK_RE = re.compile(
    r"<SYNTHESIS_DECISION>\s*(.*?)\s*</SYNTHESIS_DECISION>",
    re.DOTALL,
)
SYNTHESIS_DECISION_OPEN_RE = re.compile(r"<SYNTHESIS_DECISION>")
SYNTHESIS_DECISION_CLOSE_RE = re.compile(r"</SYNTHESIS_DECISION>")
FIELD_RE = re.compile(r"^([a-z_]+):[ \t]*(.*)$")
ALLOWED_SEVERITIES = {"blocker", "major", "minor"}
ALLOWED_SYNTHESIS_DECISIONS = {"confirmed", "rejected", "fixed", "blocked"}
BLOCKED_CONTRADICTION_PHRASES = (
    "already correct",
    "already fixed",
    "no change is needed",
    "no changes are needed",
    "no fix is needed",
    "no fix needed",
    "исправление не требуется",
    "изменение не требуется",
    "изменения не требуются",
    "уже исправлен",
    "уже исправлена",
    "уже исправлено",
    "уже корректен",
    "уже корректна",
    "уже корректно",
)
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
SYNTHESIS_DECISION_FIELDS = ("finding_id", "decision", "reason")


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


@dataclass(frozen=True)
class IdentifiedReviewFinding:
    finding_id: str
    agent: str
    finding: ReviewFinding


@dataclass(frozen=True)
class SynthesisDecision:
    finding_id: str
    decision: str
    reason: str


@dataclass(frozen=True)
class ReviewDecisionRecord:
    fingerprint: str
    agent: str
    severity: str
    category: str
    file: str
    evidence: str
    decision: str
    reason: str


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


def recover_review_output(text: str) -> str:
    """Extract an unambiguous review payload while keeping field validation strict."""
    stripped = text.strip()
    if not stripped:
        raise ReviewOutputError("empty output; expected NO FINDINGS or <FINDING> blocks")
    if len(stripped) > MAX_OUTPUT_CHARS:
        raise ReviewOutputError("output is too large")

    matches = list(FINDING_BLOCK_RE.finditer(stripped))
    no_findings = list(NO_FINDINGS_LINE_RE.finditer(stripped))

    opening_tags = len(FINDING_OPEN_RE.findall(stripped))
    closing_tags = len(FINDING_CLOSE_RE.findall(stripped))
    if opening_tags != len(matches) or closing_tags != len(matches):
        raise ReviewOutputError("output contains an incomplete or nested <FINDING> block")

    if matches and no_findings:
        if all(not match.group(1).strip() for match in matches):
            return "NO FINDINGS"
        raise ReviewOutputError(
            "output ambiguously contains both NO FINDINGS and non-empty <FINDING> blocks"
        )

    if matches:
        if len(matches) > MAX_FINDINGS:
            raise ReviewOutputError(f"too many findings; maximum is {MAX_FINDINGS}")
        return render_review_output(
            [_parse_finding_block(match.group(1)) for match in matches]
        )
    if no_findings:
        return "NO FINDINGS"
    raise ReviewOutputError("could not recover NO FINDINGS or a complete <FINDING> block")


def identify_review_findings(outputs: dict[str, str]) -> list[IdentifiedReviewFinding]:
    identified: list[IdentifiedReviewFinding] = []
    for agent, output in outputs.items():
        try:
            findings = parse_review_output(output)
        except ReviewOutputError as exc:
            raise ReviewOutputError(f"{agent}: {exc}") from exc
        for finding in findings:
            identified.append(
                IdentifiedReviewFinding(
                    finding_id=f"F{len(identified) + 1:03d}",
                    agent=agent,
                    finding=finding,
                )
            )
    return identified


def build_review_decision_records(
    findings: list[IdentifiedReviewFinding],
    decisions: list[SynthesisDecision],
) -> list[ReviewDecisionRecord]:
    decisions_by_id = {decision.finding_id: decision for decision in decisions}
    records: list[ReviewDecisionRecord] = []
    for item in findings:
        decision = decisions_by_id.get(item.finding_id)
        if decision is None:
            continue
        finding = item.finding
        records.append(
            ReviewDecisionRecord(
                fingerprint=review_finding_fingerprint(finding),
                agent=item.agent,
                severity=finding.severity,
                category=finding.category,
                file=finding.file,
                evidence=finding.evidence,
                decision=decision.decision,
                reason=decision.reason,
            )
        )
    return records


def review_finding_fingerprint(finding: ReviewFinding) -> str:
    normalized = "\n".join(
        _normalize_fingerprint_text(value)
        for value in (finding.category, finding.file, finding.evidence)
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _normalize_fingerprint_text(value: str) -> str:
    return " ".join(value.casefold().split())


def parse_synthesis_output(
    text: str,
    expected_finding_ids: list[str],
) -> list[SynthesisDecision]:
    stripped = text.strip()
    if len(stripped) > MAX_OUTPUT_CHARS:
        raise ReviewOutputError("synthesis output is too large")

    body = stripped
    if body == REVIEW_DONE:
        body = ""
    elif body.endswith(f"\n{REVIEW_DONE}"):
        body = body[: -len(REVIEW_DONE)].rstrip()

    matches = list(SYNTHESIS_DECISION_BLOCK_RE.finditer(body))
    remainder = SYNTHESIS_DECISION_BLOCK_RE.sub("", body)
    if remainder.strip():
        raise ReviewOutputError(
            "synthesis output contains text outside <SYNTHESIS_DECISION> blocks"
        )

    decisions = [_parse_synthesis_decision(match.group(1)) for match in matches]
    _validate_synthesis_decision_ids(decisions, expected_finding_ids)
    return decisions


def _validate_synthesis_decision_ids(
    decisions: list[SynthesisDecision],
    expected_finding_ids: list[str],
) -> None:
    actual_ids = [decision.finding_id for decision in decisions]
    duplicate_ids = sorted(
        finding_id for finding_id in set(actual_ids) if actual_ids.count(finding_id) > 1
    )
    if duplicate_ids:
        raise ReviewOutputError(
            f"duplicate synthesis finding ids: {', '.join(duplicate_ids)}"
        )

    expected = set(expected_finding_ids)
    actual = set(actual_ids)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    problems: list[str] = []
    if missing:
        problems.append(f"missing finding ids: {', '.join(missing)}")
    if unexpected:
        problems.append(f"unexpected finding ids: {', '.join(unexpected)}")
    if problems:
        raise ReviewOutputError("; ".join(problems))
    if len(decisions) != len(expected_finding_ids):
        raise ReviewOutputError(
            "synthesis decision count does not match the input finding count"
        )


def recover_synthesis_output(
    text: str,
    expected_finding_ids: list[str],
) -> list[SynthesisDecision]:
    """Extract a complete decision ledger from prose without guessing field values."""
    stripped = text.strip()
    if len(stripped) > MAX_OUTPUT_CHARS:
        raise ReviewOutputError("synthesis output is too large")

    matches = list(SYNTHESIS_DECISION_BLOCK_RE.finditer(stripped))
    opening_tags = len(SYNTHESIS_DECISION_OPEN_RE.findall(stripped))
    closing_tags = len(SYNTHESIS_DECISION_CLOSE_RE.findall(stripped))
    if opening_tags != len(matches) or closing_tags != len(matches):
        raise ReviewOutputError(
            "synthesis output contains an incomplete or nested <SYNTHESIS_DECISION> block"
        )
    if not matches:
        if expected_finding_ids:
            raise ReviewOutputError("synthesis output contains no complete decision blocks")
        if not any(line.strip() == REVIEW_DONE for line in stripped.splitlines()):
            raise ReviewOutputError(
                "synthesis output contains neither decisions nor the completion signal"
            )

    decisions = [_parse_synthesis_decision(match.group(1)) for match in matches]
    _validate_synthesis_decision_ids(decisions, expected_finding_ids)
    return decisions


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


def _parse_synthesis_decision(block: str) -> SynthesisDecision:
    values: dict[str, str] = {}
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = FIELD_RE.match(line)
        if not match:
            raise ReviewOutputError(f"invalid synthesis decision line: {line[:80]}")
        field, value = match.groups()
        if field not in SYNTHESIS_DECISION_FIELDS:
            raise ReviewOutputError(f"unknown synthesis decision field: {field}")
        if field in values:
            raise ReviewOutputError(f"duplicate synthesis decision field: {field}")
        value = value.strip()
        if not value:
            raise ReviewOutputError(f"empty synthesis decision field: {field}")
        if len(value) > MAX_FIELD_CHARS:
            raise ReviewOutputError(f"synthesis decision field is too long: {field}")
        values[field] = value

    missing = [field for field in SYNTHESIS_DECISION_FIELDS if field not in values]
    if missing:
        raise ReviewOutputError(
            f"missing synthesis decision fields: {', '.join(missing)}"
        )

    finding_id = values["finding_id"]
    if not re.fullmatch(r"F[0-9]{3}", finding_id):
        raise ReviewOutputError("finding_id must use the runner-assigned FNNN form")
    decision = values["decision"].lower()
    if decision not in ALLOWED_SYNTHESIS_DECISIONS:
        allowed = ", ".join(sorted(ALLOWED_SYNTHESIS_DECISIONS))
        raise ReviewOutputError(
            f"invalid synthesis decision {values['decision']!r}; expected one of: {allowed}"
        )
    reason = values["reason"]
    if decision == "blocked" and any(
        phrase in reason.casefold() for phrase in BLOCKED_CONTRADICTION_PHRASES
    ):
        raise ReviewOutputError(
            "blocked decision reason says the deliverable is already correct or "
            "needs no fix; use rejected"
        )
    return SynthesisDecision(
        finding_id=finding_id,
        decision=decision,
        reason=reason,
    )


def _normalize_repository_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or normalized in {"", ".", ".."} or ".." in path.parts:
        raise ReviewOutputError("file must be a repository-relative path without parent traversal")
    return path.as_posix()
