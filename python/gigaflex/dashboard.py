from __future__ import annotations

from datetime import datetime
import html
import json
from pathlib import Path
import threading
import time
from typing import Optional

from .plan import Plan, parse_plan_file


PHASE_LABELS = {
    "startup": "Preparing run",
    "tasks": "Tasks",
    "review": "Review",
    "finalize": "Finalize",
    "done": "Done",
}


class ProgressDashboard:
    """Maintain a machine-readable snapshot and a self-contained live HTML view."""

    def __init__(
        self,
        json_path: Path,
        html_path: Path,
        *,
        name: str,
        plan_file: Optional[Path],
        plan_kind: str = "gigaflex",
        progress_file: Optional[Path] = None,
        branch: str = "",
        tasks_enabled: bool = True,
        review_enabled: bool = True,
        review_iterations: int = 0,
        parallel_review: bool = True,
        finalize_enabled: bool = True,
    ) -> None:
        self.json_path = json_path
        self.html_path = html_path
        self.plan_file = plan_file
        self.plan_kind = plan_kind
        self._lock = threading.RLock()
        self._last_activity_write: dict[str, float] = {}
        now = _timestamp()
        self._state: dict[str, object] = {
            "version": 2,
            "name": name,
            "title": name,
            "status": "running",
            "phase": "startup",
            "message": "Preparing the run",
            "error": "",
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "plan_file": str(plan_file.resolve()) if plan_file else "",
            "progress_file": str(progress_file.resolve()) if progress_file else "",
            "branch": branch,
            "phases": [
                {
                    "id": "tasks",
                    "label": "Tasks",
                    "status": "pending" if tasks_enabled else "skipped",
                },
                {
                    "id": "review",
                    "label": "Review",
                    "status": "pending" if review_enabled else "skipped",
                },
                {
                    "id": "finalize",
                    "label": "Finalize",
                    "status": "pending" if finalize_enabled else "skipped",
                },
            ],
            "tasks": [],
            "current_task": None,
            "review": {
                "enabled": review_enabled,
                "mode": "parallel" if parallel_review else "single",
                "status": "pending" if review_enabled else "skipped",
                "current_attempt": 0,
                "max_attempts": max(0, review_iterations),
                "stage": "",
                "findings": None,
                "attempts": [],
            },
            "sessions": {},
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "known_calls": 0,
            },
        }
        self._refresh_plan_locked()

    def start(self) -> None:
        with self._lock:
            self._write_locked()

    def phase_started(self, phase: str, message: str = "") -> None:
        with self._lock:
            self._complete_running_phases_locked(except_phase=phase)
            self._set_phase_status_locked(phase, "running")
            self._state["phase"] = phase
            self._state["message"] = message or PHASE_LABELS.get(phase, phase.title())
            self._write_locked()

    def phase_reused(self, phase: str, message: str = "") -> None:
        with self._lock:
            self._complete_running_phases_locked(except_phase=phase)
            self._set_phase_status_locked(phase, "completed")
            self._state["phase"] = phase
            self._state["message"] = message or f"Reused {phase} checkpoint"
            if phase == "review":
                review = self._state["review"]
                assert isinstance(review, dict)
                review["status"] = "passed"
                review["stage"] = "checkpoint"
            self._write_locked()

    def task_started(self, number: int, title: str, iteration: int) -> None:
        with self._lock:
            self._refresh_plan_locked()
            self._state["phase"] = "tasks"
            self._set_phase_status_locked("tasks", "running")
            self._state["current_task"] = {
                "number": number,
                "title": title,
                "iteration": iteration,
            }
            self._state["message"] = f"Working on task {number}: {title}"
            self._write_locked()

    def task_finished(self) -> None:
        with self._lock:
            self._refresh_plan_locked()
            self._state["current_task"] = None
            self._write_locked()

    def review_attempt_started(
        self,
        iteration: int,
        max_iterations: int,
        *,
        parallel: bool,
    ) -> None:
        with self._lock:
            review = self._review_state_locked()
            review["enabled"] = True
            review["mode"] = "parallel" if parallel else "single"
            review["status"] = "running"
            review["current_attempt"] = iteration
            review["max_attempts"] = max(0, max_iterations)
            review["stage"] = "reviewers" if parallel else "reviewer"
            review["findings"] = None
            attempts = review["attempts"]
            assert isinstance(attempts, list)
            attempts.append(
                {
                    "number": iteration,
                    "status": "running",
                    "stage": review["stage"],
                    "findings": None,
                    "message": "",
                    "started_at": _timestamp(),
                    "completed_at": None,
                }
            )
            reviewer_label = "specialist reviewers" if parallel else "reviewer"
            self._state["message"] = (
                f"Review attempt {iteration} of {max_iterations}: running {reviewer_label}"
            )
            self._write_locked()

    def review_synthesis_started(self, iteration: int, findings: int) -> None:
        with self._lock:
            review = self._review_state_locked()
            review["status"] = "running"
            review["stage"] = "synthesis"
            review["findings"] = max(0, findings)
            attempt = self._review_attempt_locked(review, iteration)
            if attempt is not None:
                attempt["stage"] = "synthesis"
                attempt["findings"] = max(0, findings)
            self._state["message"] = (
                f"Review attempt {iteration}: synthesizing {findings} "
                f"{'finding' if findings == 1 else 'findings'}"
            )
            self._write_locked()

    def review_attempt_finished(
        self,
        iteration: int,
        status: str,
        *,
        findings: Optional[int] = None,
        message: str = "",
    ) -> None:
        if status not in {"passed", "needs_another_pass"}:
            raise ValueError(f"unsupported review attempt status: {status}")
        with self._lock:
            review = self._review_state_locked()
            review["status"] = status
            review["current_attempt"] = iteration
            review["stage"] = "complete" if status == "passed" else "waiting_retry"
            if findings is not None:
                review["findings"] = max(0, findings)
            attempt = self._review_attempt_locked(review, iteration)
            if attempt is not None:
                attempt["status"] = status
                attempt["stage"] = review["stage"]
                if findings is not None:
                    attempt["findings"] = max(0, findings)
                attempt["message"] = message
                attempt["completed_at"] = _timestamp()
            self._state["message"] = message or (
                f"Review passed on attempt {iteration}"
                if status == "passed"
                else f"Review attempt {iteration} requested another pass"
            )
            self._write_locked()

    def executor_event(
        self,
        session: str,
        event: str,
        fields: dict[str, object],
    ) -> None:
        with self._lock:
            now_monotonic = time.monotonic()
            if event == "activity":
                last_write = self._last_activity_write.get(session, 0.0)
                if now_monotonic - last_write < 1.0:
                    return
                self._last_activity_write[session] = now_monotonic
                if session == "task":
                    self._refresh_plan_locked()

            sessions = self._state["sessions"]
            assert isinstance(sessions, dict)
            item = sessions.setdefault(
                session,
                {
                    "name": session,
                    "status": "preparing",
                    "attempt": 0,
                    "attempts": 0,
                    "started_at": None,
                    "last_activity_at": None,
                    "finished_at": None,
                    "duration_ms": None,
                    "error": "",
                },
            )
            assert isinstance(item, dict)
            now = _timestamp()

            if event == "attempt_started":
                item["status"] = "preparing"
                item["attempt"] = fields.get("attempt", 0)
                item["attempts"] = fields.get("attempts", 0)
                item["started_at"] = None
                item["last_activity_at"] = None
                item["finished_at"] = None
                item["duration_ms"] = None
                item["error"] = ""
            elif event == "started":
                item["status"] = "running"
                item["started_at"] = now
                item["last_activity_at"] = now
                item["pid"] = fields.get("pid")
                item["error"] = ""
            elif event in {"first_output", "activity", "stdin_sent"}:
                item["last_activity_at"] = now
            elif event == "model_fallback":
                item["status"] = "waiting_retry"
                item["error"] = (
                    f"Configured model {fields.get('configured_model', 'unknown')} "
                    "is unavailable; using the default model"
                )
            elif event == "retry_scheduled":
                item["status"] = "waiting_retry"
                item["retry_at"] = fields.get("next_attempt")
                item["retry_delay_seconds"] = fields.get("delay_seconds")
                item["error"] = _human_failure(fields.get("reason"))
            elif event in {"terminating", "termination_escalated"}:
                item["status"] = "stopping"
                item["error"] = _human_failure(fields.get("reason"))
            elif event == "approval_warning_detected":
                item["status"] = "needs_attention"
                item["error"] = "Approval is unavailable in non-interactive mode"
            elif event == "finished":
                failed = any(
                    _is_truthy(fields.get(key))
                    for key in (
                        "timed_out",
                        "idle_timed_out",
                        "transient_error",
                        "rate_limited",
                        "api_error",
                        "approval_unavailable",
                        "dependency_crash",
                    )
                ) or fields.get("returncode", 0) != 0
                item["status"] = "failed" if failed else "completed"
                item["finished_at"] = now
                item["duration_ms"] = fields.get("duration_ms")
                item["signal"] = fields.get("signal")
                if failed:
                    item["error"] = _executor_failure(fields)
                self._add_usage_locked(fields)
            elif event in {"launch_failed", "attempts_exhausted"}:
                item["status"] = "failed"
                item["finished_at"] = now
                item["error"] = _human_failure(fields.get("reason") or fields.get("error"))
            elif event == "attempt_succeeded":
                item["status"] = "completed"

            self._write_locked()

    def complete(self, message: str = "Run completed successfully") -> None:
        with self._lock:
            self._refresh_plan_locked()
            self._complete_running_phases_locked()
            self._state["status"] = "success"
            self._state["phase"] = "done"
            self._state["message"] = message
            self._state["current_task"] = None
            self._state["completed_at"] = _timestamp()
            self._write_locked()

    def fail(self, error: str) -> None:
        with self._lock:
            self._state["status"] = "failed"
            self._state["message"] = "Run failed"
            self._state["error"] = error
            self._state["completed_at"] = _timestamp()
            self._mark_current_phase_failed_locked()
            self._mark_current_review_failed_locked("failed", error)
            self._write_locked()

    def interrupt(self) -> None:
        with self._lock:
            self._state["status"] = "interrupted"
            self._state["message"] = "Run interrupted"
            self._state["completed_at"] = _timestamp()
            self._mark_current_phase_failed_locked()
            self._mark_current_review_failed_locked("interrupted", "Run interrupted")
            self._write_locked()

    @property
    def state(self) -> dict[str, object]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def _refresh_plan_locked(self) -> None:
        if self.plan_file is None or not self.plan_file.is_file():
            return
        try:
            plan = parse_plan_file(self.plan_file, plan_format=self.plan_kind)
        except (OSError, ValueError):
            return
        if plan.title:
            self._state["title"] = plan.title
        self._state["tasks"] = _tasks_state(plan)

    def _set_phase_status_locked(self, phase: str, status: str) -> None:
        phases = self._state["phases"]
        assert isinstance(phases, list)
        for item in phases:
            if isinstance(item, dict) and item.get("id") == phase:
                item["status"] = status

    def _complete_running_phases_locked(self, except_phase: str = "") -> None:
        phases = self._state["phases"]
        assert isinstance(phases, list)
        for item in phases:
            if (
                isinstance(item, dict)
                and item.get("status") == "running"
                and item.get("id") != except_phase
            ):
                item["status"] = "completed"

    def _mark_current_phase_failed_locked(self) -> None:
        phase = self._state.get("phase")
        if isinstance(phase, str):
            self._set_phase_status_locked(phase, "failed")

    def _review_state_locked(self) -> dict[str, object]:
        review = self._state["review"]
        assert isinstance(review, dict)
        return review

    def _review_attempt_locked(
        self,
        review: dict[str, object],
        iteration: int,
    ) -> Optional[dict[str, object]]:
        attempts = review["attempts"]
        assert isinstance(attempts, list)
        for item in reversed(attempts):
            if isinstance(item, dict) and item.get("number") == iteration:
                return item
        return None

    def _mark_current_review_failed_locked(self, status: str, message: str) -> None:
        if self._state.get("phase") != "review":
            return
        review = self._review_state_locked()
        review["status"] = status
        review["stage"] = "complete"
        iteration = _integer_or_none(review.get("current_attempt")) or 0
        attempt = self._review_attempt_locked(review, iteration)
        if attempt is not None and attempt.get("status") == "running":
            attempt["status"] = status
            attempt["message"] = message
            attempt["completed_at"] = _timestamp()

    def _add_usage_locked(self, fields: dict[str, object]) -> None:
        total = _integer_or_none(fields.get("total_tokens"))
        if total is None:
            return
        usage = self._state["usage"]
        assert isinstance(usage, dict)
        usage["input_tokens"] = int(usage["input_tokens"]) + (_integer_or_none(fields.get("input_tokens")) or 0)
        usage["output_tokens"] = int(usage["output_tokens"]) + (_integer_or_none(fields.get("output_tokens")) or 0)
        usage["total_tokens"] = int(usage["total_tokens"]) + total
        usage["known_calls"] = int(usage["known_calls"]) + 1

    def _write_locked(self) -> None:
        self._state["updated_at"] = _timestamp()
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            self.json_path,
            json.dumps(self._state, ensure_ascii=False, indent=2) + "\n",
        )
        _atomic_write(self.html_path, _render_html(self._state))


def dashboard_paths(progress_file: Path) -> tuple[Path, Path]:
    stem = progress_file.stem
    name = stem[len("progress-") :] if stem.startswith("progress-") else stem
    return (
        progress_file.with_name(f"status-{name}.json"),
        progress_file.with_name(f"status-{name}.html"),
    )


def _tasks_state(plan: Plan) -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    for task in plan.tasks:
        checkboxes = [item for item in task.checkboxes if item.actionable]
        completed = sum(1 for item in checkboxes if item.checked)
        tasks.append(
            {
                "number": task.number,
                "title": task.title,
                "status": "completed" if task.complete else "pending",
                "completed_items": completed,
                "total_items": len(checkboxes),
                "items": [
                    {"text": item.text, "checked": item.checked}
                    for item in checkboxes
                ],
            }
        )
    return tasks


def _render_html(state: dict[str, object]) -> str:
    title = html.escape(str(state.get("title") or state.get("name") or "GigaFlex run"))
    status = str(state.get("status", "running"))
    status_label = {
        "running": "Running",
        "success": "Completed",
        "failed": "Failed",
        "interrupted": "Interrupted",
    }.get(status, status.title())
    phases = state.get("phases", [])
    tasks = state.get("tasks", [])
    sessions = state.get("sessions", {})
    review = state.get("review", {})
    usage = state.get("usage", {})
    current = state.get("current_task")

    phase_html = "".join(
        _phase_html(item)
        for item in phases
        if isinstance(item, dict)
    )
    task_html = "".join(
        _task_html(item, current)
        for item in tasks
        if isinstance(item, dict)
    ) or '<p class="empty">No plan tasks to display.</p>'
    session_html = "".join(
        _session_html(item)
        for item in sessions.values()
        if isinstance(item, dict)
    ) or '<p class="empty">Waiting for GigaCode…</p>'
    review_html = _review_html(review) if isinstance(review, dict) else ""
    known_calls = usage.get("known_calls", 0) if isinstance(usage, dict) else 0
    token_text = (
        f"{int(usage.get('total_tokens', 0)):,}" if isinstance(usage, dict) and known_calls else "—"
    )
    message = html.escape(str(state.get("message", "")))
    error = html.escape(str(state.get("error", "")))
    branch = html.escape(str(state.get("branch", "")))
    progress_file = html.escape(str(state.get("progress_file", "")))
    started_at = html.escape(str(state.get("started_at", "")))
    updated_at = html.escape(str(state.get("updated_at", "")))
    completed_at = html.escape(str(state.get("completed_at") or ""))
    refresh_meta = '<meta http-equiv="refresh" content="2">' if status == "running" else ""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_meta}
  <title>{title} · GigaFlex</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0b0d10; --panel:#12161b; --line:#28313b; --muted:#8d99a6; --text:#eef3f7; --accent:#7ce2b1; --blue:#79b8ff; --warn:#f6c177; --bad:#ff7b72; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:radial-gradient(circle at 20% 0%, #17212a 0, var(--bg) 34rem); color:var(--text); font:15px/1.5 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ width:min(1120px, calc(100% - 32px)); margin:0 auto; padding:48px 0 64px; }}
    header {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-start; margin-bottom:28px; }}
    .eyebrow {{ color:var(--accent); font-size:12px; font-weight:750; letter-spacing:.14em; text-transform:uppercase; }}
    h1 {{ margin:7px 0 6px; max-width:780px; font-size:clamp(28px,5vw,48px); line-height:1.08; letter-spacing:-.035em; }}
    .message,.muted,.empty {{ color:var(--muted); }}
    .status {{ display:inline-flex; align-items:center; gap:9px; padding:9px 13px; border:1px solid var(--line); border-radius:999px; background:#101419; font-weight:700; white-space:nowrap; }}
    .dot {{ width:9px; height:9px; border-radius:50%; background:var(--muted); }}
    .status-running .dot,.state-running .dot {{ background:var(--blue); box-shadow:0 0 0 5px #79b8ff18; }}
    .status-success .dot,.state-completed .dot,.state-passed .dot {{ background:var(--accent); }}
    .state-needs_another_pass .dot {{ background:var(--warn); }}
    .status-failed .dot,.status-interrupted .dot,.state-failed .dot,.state-interrupted .dot {{ background:var(--bad); }}
    .phases {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-bottom:18px; }}
    .phase,.panel {{ border:1px solid var(--line); background:linear-gradient(180deg,#14191f,#101419); border-radius:16px; }}
    .phase {{ padding:14px 16px; display:flex; align-items:center; gap:11px; }}
    .phase strong {{ display:block; }} .phase small {{ color:var(--muted); text-transform:capitalize; }}
    .grid {{ display:grid; grid-template-columns:minmax(0,1.55fr) minmax(280px,.85fr); gap:18px; }}
    .panel {{ padding:22px; }}
    .review-panel {{ margin-bottom:18px; }}
    .review-summary {{ display:flex; align-items:flex-start; justify-content:space-between; gap:18px; }}
    .review-status {{ display:flex; align-items:center; gap:10px; font-weight:750; }}
    .review-meta {{ color:var(--muted); font-size:13px; text-align:right; }}
    .review-attempts {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:9px; margin-top:16px; }}
    .review-attempt {{ padding:12px 13px; border:1px solid var(--line); border-radius:12px; background:#0d1116; }}
    .review-attempt.state-passed {{ border-color:#7ce2b155; }}
    .review-attempt.state-needs_another_pass {{ border-color:#f6c17755; }}
    .review-attempt.state-failed,.review-attempt.state-interrupted {{ border-color:#ff7b7255; }}
    .review-attempt-head {{ display:flex; justify-content:space-between; gap:10px; font-weight:700; }}
    .review-attempt small {{ display:block; margin-top:4px; color:var(--muted); }}
    .review-message {{ margin-top:5px; color:#b9c4ce; font-size:12px; }}
    h2 {{ margin:0 0 17px; font-size:17px; letter-spacing:-.01em; }}
    .task {{ padding:15px 0; border-top:1px solid var(--line); }} .task:first-of-type {{ border-top:0; padding-top:0; }}
    .task-head {{ display:flex; gap:11px; align-items:flex-start; }}
    .mark {{ width:23px; height:23px; flex:0 0 auto; border:1px solid #40505e; border-radius:50%; display:grid; place-items:center; color:var(--accent); font-size:13px; margin-top:1px; }}
    .task.current .mark {{ border-color:var(--blue); color:var(--blue); box-shadow:0 0 0 4px #79b8ff12; }}
    .task-title {{ font-weight:700; }} .task-meta {{ color:var(--muted); font-size:13px; }}
    .items {{ list-style:none; margin:10px 0 0 34px; padding:0; color:var(--muted); font-size:13px; }} .items li {{ margin:4px 0; }} .items .checked {{ color:#aab5be; text-decoration:line-through; }}
    .session {{ padding:13px 0; border-top:1px solid var(--line); }} .session:first-of-type {{ border-top:0; padding-top:0; }}
    .session-row {{ display:flex; justify-content:space-between; gap:10px; }} .session-name {{ font-weight:700; overflow-wrap:anywhere; }} .session-state {{ color:var(--muted); text-transform:capitalize; }}
    .session-error,.error {{ color:var(--bad); margin-top:5px; font-size:13px; }}
    .metrics {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:18px; }}
    .metric {{ background:#0d1116; border:1px solid var(--line); border-radius:12px; padding:13px; }} .metric b {{ display:block; font-size:20px; }} .metric span {{ color:var(--muted); font-size:12px; }}
    footer {{ margin-top:18px; display:flex; flex-wrap:wrap; gap:8px 22px; color:var(--muted); font-size:12px; }} footer code {{ color:#b9c4ce; overflow-wrap:anywhere; }}
    @media (max-width:760px) {{ main {{ padding-top:28px; }} header {{ display:block; }} .status {{ margin-top:16px; }} .grid {{ grid-template-columns:1fr; }} .phases {{ grid-template-columns:1fr; }} .review-summary {{ display:block; }} .review-meta {{ margin-top:5px; text-align:left; }} }}
    @media (prefers-reduced-motion:no-preference) {{ .status-running .dot {{ animation:pulse 1.8s ease-in-out infinite; }} @keyframes pulse {{ 50% {{ opacity:.42; }} }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <div><div class="eyebrow">GigaFlex progress</div><h1>{title}</h1><div class="message">{message}</div>{f'<div class="error">{error}</div>' if error else ''}</div>
      <div class="status status-{html.escape(status)}"><span class="dot"></span>{html.escape(status_label)} · <span id="elapsed">—</span></div>
    </header>
    <section class="phases" aria-label="Run phases">{phase_html}</section>
    {review_html}
    <div class="grid">
      <section class="panel"><h2>Plan progress</h2>{task_html}</section>
      <aside class="panel"><h2>Active sessions</h2>{session_html}<div class="metrics"><div class="metric"><b>{token_text}</b><span>tokens</span></div><div class="metric"><b>{sum(1 for item in tasks if isinstance(item, dict) and item.get('status') == 'completed')} / {len(tasks)}</b><span>tasks complete</span></div></div></aside>
    </div>
    <footer>{f'<span>Branch: <code>{branch}</code></span>' if branch else ''}<span>Updated: <time>{updated_at}</time></span>{f'<span>Detailed log: <code>{progress_file}</code></span>' if progress_file else ''}</footer>
  </main>
  <script>
    const started = Date.parse({json.dumps(started_at)});
    const completed = Date.parse({json.dumps(completed_at)});
    function duration(ms) {{ const s=Math.max(0,Math.floor(ms/1000)); const h=Math.floor(s/3600); const m=Math.floor((s%3600)/60); const r=s%60; return h ? `${{h}}h ${{m}}m` : m ? `${{m}}m ${{r}}s` : `${{r}}s`; }}
    function tick() {{
      const now = Date.now(); const end = Number.isNaN(completed) ? now : completed;
      document.getElementById('elapsed').textContent = Number.isNaN(started) ? '—' : duration(end-started);
      document.querySelectorAll('[data-time]').forEach((node) => {{ const value=Date.parse(node.dataset.time); node.textContent=Number.isNaN(value) ? '' : `activity ${{duration(now-value)}} ago`; }});
    }}
    tick(); setInterval(tick,1000);
  </script>
</body>
</html>
"""


def _phase_html(item: dict[str, object]) -> str:
    status = str(item.get("status", "pending"))
    symbol = {"completed": "✓", "running": "●", "failed": "!", "skipped": "–"}.get(status, "")
    return (
        f'<div class="phase state-{html.escape(status)}"><span class="mark">{symbol}</span>'
        f'<div><strong>{html.escape(str(item.get("label", "")))}</strong>'
        f'<small>{html.escape(status.replace("_", " "))}</small></div></div>'
    )


def _task_html(item: dict[str, object], current: object) -> str:
    is_current = isinstance(current, dict) and current.get("number") == item.get("number")
    status = str(item.get("status", "pending"))
    completed = item.get("completed_items", 0)
    total = item.get("total_items", 0)
    symbol = "✓" if status == "completed" else ("●" if is_current else "")
    items = item.get("items", [])
    items_html = "".join(
        f'<li class="{"checked" if child.get("checked") else ""}">{"✓" if child.get("checked") else "○"} {html.escape(str(child.get("text", "")))}</li>'
        for child in items
        if isinstance(child, dict)
    )
    item_list = f'<ul class="items">{items_html}</ul>' if items_html else ""
    current_class = "current" if is_current else ""
    return (
        f'<article class="task {current_class}"><div class="task-head"><span class="mark">{symbol}</span><div>'
        f'<div class="task-title">{html.escape(str(item.get("number", "")))}. {html.escape(str(item.get("title", "")))}</div>'
        f'<div class="task-meta">{completed} of {total} checklist items</div></div></div>'
        f'{item_list}</article>'
    )


def _review_html(review: dict[str, object]) -> str:
    if not review.get("enabled"):
        return ""
    status = str(review.get("status", "pending"))
    status_label = {
        "pending": "Pending",
        "running": "In progress",
        "passed": "Passed",
        "needs_another_pass": "Another pass required",
        "failed": "Failed",
        "interrupted": "Interrupted",
    }.get(status, status.replace("_", " ").title())
    stage = str(review.get("stage", ""))
    stage_label = {
        "reviewers": "Specialist reviewers",
        "reviewer": "Reviewer",
        "synthesis": "Synthesis",
        "waiting_retry": "Next pass queued",
        "complete": "Complete",
    }.get(stage, stage.replace("_", " ").title())
    current = _integer_or_none(review.get("current_attempt")) or 0
    maximum = _integer_or_none(review.get("max_attempts")) or 0
    mode = "Parallel" if review.get("mode") == "parallel" else "Single reviewer"
    attempt_progress = (
        f"Attempt {current} of {maximum}"
        if current and maximum
        else (f"Up to {maximum} attempts" if maximum else "Not started")
    )
    findings = _integer_or_none(review.get("findings"))
    finding_text = (
        f" · {findings} {'finding' if findings == 1 else 'findings'}"
        if findings is not None
        else ""
    )
    attempts = review.get("attempts", [])
    attempts_html = "".join(
        _review_attempt_html(item)
        for item in attempts
        if isinstance(item, dict)
    )
    if not attempts_html:
        attempts_html = '<p class="empty">Review has not started yet.</p>'
    return (
        '<section class="panel review-panel"><h2>Review status</h2>'
        '<div class="review-summary">'
        f'<div class="review-status state-{html.escape(status)}"><span class="dot"></span>{html.escape(status_label)}</div>'
        f'<div class="review-meta">{html.escape(attempt_progress)} · {html.escape(mode)}'
        f'{finding_text}<br>{html.escape(stage_label)}</div></div>'
        f'<div class="review-attempts">{attempts_html}</div></section>'
    )


def _review_attempt_html(item: dict[str, object]) -> str:
    status = str(item.get("status", "running"))
    status_label = {
        "running": "In progress",
        "passed": "Passed",
        "needs_another_pass": "Another pass required",
        "failed": "Failed",
        "interrupted": "Interrupted",
    }.get(status, status.replace("_", " ").title())
    stage = str(item.get("stage", ""))
    stage_label = {
        "reviewers": "Specialist reviewers",
        "reviewer": "Reviewer",
        "synthesis": "Synthesis",
        "waiting_retry": "Complete",
        "complete": "Complete",
    }.get(stage, stage.replace("_", " ").title())
    findings = _integer_or_none(item.get("findings"))
    finding_text = (
        f" · {findings} {'finding' if findings == 1 else 'findings'}"
        if findings is not None
        else ""
    )
    message = html.escape(str(item.get("message", "")))
    message_html = f'<div class="review-message">{message}</div>' if message else ""
    return (
        f'<div class="review-attempt state-{html.escape(status)}">'
        f'<div class="review-attempt-head"><span>Attempt {html.escape(str(item.get("number", "")))}</span><span>{html.escape(status_label)}</span></div>'
        f'<small>{html.escape(stage_label)}{finding_text}</small>'
        f'{message_html}</div>'
    )


def _session_html(item: dict[str, object]) -> str:
    status = str(item.get("status", "preparing"))
    attempt = item.get("attempt", 0)
    attempts = item.get("attempts", 0)
    attempt_text = f" · attempt {attempt}/{attempts}" if attempt and attempts else ""
    error = html.escape(str(item.get("error", "")))
    error_html = f'<div class="session-error">{error}</div>' if error else ""
    last_activity = html.escape(str(item.get("last_activity_at") or ""))
    activity_html = (
        f'<span class="relative-time" data-time="{last_activity}">activity just now</span>'
        if last_activity and status in {"preparing", "running"}
        else ""
    )
    details = " · ".join(
        part for part in (attempt_text.lstrip(" ·"), activity_html) if part
    )
    return (
        f'<div class="session"><div class="session-row"><span class="session-name">{html.escape(str(item.get("name", "")))}</span>'
        f'<span class="session-state">{html.escape(status.replace("_", " "))}</span></div>'
        f'<div class="task-meta">{details}</div>'
        f'{error_html}</div>'
    )


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _integer_or_none(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _is_truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.lower() not in {"", "false", "0", "none"}
    return bool(value)


def _human_failure(value: object) -> str:
    text = str(value or "unknown error").replace("_", " ")
    return text[:1].upper() + text[1:]


def _executor_failure(fields: dict[str, object]) -> str:
    if _is_truthy(fields.get("rate_limited")):
        return "Rate limit reached"
    if _is_truthy(fields.get("dependency_crash")):
        return "GigaCode crashed in an external dependency"
    if _is_truthy(fields.get("idle_timed_out")):
        return "Stopped after no output was received"
    if _is_truthy(fields.get("timed_out")):
        return "Session timed out"
    if _is_truthy(fields.get("approval_unavailable")):
        return "Approval is unavailable in non-interactive mode"
    if _is_truthy(fields.get("api_error")):
        return str(fields["api_error"])
    return f"GigaCode exited with code {fields.get('returncode', 'unknown')}"
