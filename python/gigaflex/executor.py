from __future__ import annotations

import codecs
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
import io
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import shlex
import sys
import threading
import time
from typing import Callable, Iterable, Optional

from .defaults import DEFAULT_GIGACODE_ARGS
from .signals import detect_signal
from .stats import InvocationStat, RunStatistics, TokenUsage


APPROVAL_UNAVAILABLE_TEXT = (
    "requires user approval but cannot execute in non-interactive mode"
)
PROCESS_TERMINATION_GRACE_SECONDS = 2.0
PROCESS_TERMINATION_POLL_SECONDS = 0.05
API_ERROR_RE = re.compile(
    r"\A\s*\[?(API Error:\s*\d{3}(?:\s+[^\r\n\]]*)?)\]?\s*\Z",
    re.IGNORECASE,
)


DEFAULT_TRANSIENT_RETRY_PATTERNS = [
    "FYA_TRANSIENT_TIMEOUT",
    "API Error: 529",
    "API Error: 502",
    "API Error: 503",
    "API Error: 504",
    "502 Bad Gateway",
    "503 Service Unavailable",
    "504 Gateway Timeout",
]

DEFAULT_RATE_LIMIT_PATTERNS = [
    "Rate limit exceeded",
    "rate limit reached",
    "429 Too Many Requests",
    "quota exceeded",
    "insufficient_quota",
    "You've hit your usage limit",
]

DEPENDENCY_CRASH_RETURN_CODES = {139, -11}
DEPENDENCY_CRASH_PATTERNS = (
    "Segmentation fault",
    "Ошибка сегментирования",
    "libsecret-CRITICAL",
    "secret_value_get_text",
)


def is_dependency_crash(returncode: int, output: str) -> bool:
    """Return whether a process failure matches a known dependency crash."""
    return (
        returncode in DEPENDENCY_CRASH_RETURN_CODES
        or matches_any(output, DEPENDENCY_CRASH_PATTERNS)
    )


@dataclass
class ExecResult:
    output: str
    error_output: str = ""
    signal: str = ""
    returncode: int = 0
    timed_out: bool = False
    idle_timed_out: bool = False
    transient_error: bool = False
    rate_limited: bool = False
    api_error: str = ""
    attempts: int = 1
    wall_duration_ms: int = 0
    reported_duration_ms: Optional[int] = None
    api_duration_ms: Optional[int] = None
    session_id: str = ""
    models: tuple[str, ...] = ()
    usage: Optional[TokenUsage] = None
    approval_denied: bool = False
    dependency_crash: bool = False

    @property
    def ok(self) -> bool:
        return (
            self.returncode == 0
            and not self.timed_out
            and not self.idle_timed_out
            and not self.transient_error
            and not self.rate_limited
            and not self.api_error
            and not self.approval_unavailable
        )

    @property
    def approval_unavailable(self) -> bool:
        return (
            self.approval_denied
            or APPROVAL_UNAVAILABLE_TEXT in self.output
            or APPROVAL_UNAVAILABLE_TEXT in self.error_output
        )


RetryGuard = Callable[[ExecResult], bool]
EventCallback = Callable[[str, str, dict[str, object]], None]


@dataclass(frozen=True)
class _ActiveProcess:
    session: str
    proc: subprocess.Popen[bytes]
    process_group: Optional[int]


class GigaCodeExecutor:
    def __init__(
        self,
        command: str = "gigacode",
        args: Optional[list[str]] = None,
        timeout: Optional[int] = None,
        idle_timeout: Optional[int] = None,
        retry_count: int = 0,
        retry_delay: float = 2.0,
        retry_patterns: Optional[list[str]] = None,
        rate_limit_patterns: Optional[list[str]] = None,
        wait_on_rate_limit: Optional[float] = None,
        max_workers: int = 5,
        output: Optional[Callable[[str], None]] = None,
        diagnostic: Optional[Callable[[str], None]] = None,
        event_callback: Optional[EventCallback] = None,
        name: str = "gigacode",
        statistics: Optional[RunStatistics] = None,
    ) -> None:
        self.command = command
        self.args = args if args is not None else DEFAULT_GIGACODE_ARGS.copy()
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self.retry_count = max(0, retry_count)
        self.retry_delay = max(0.0, retry_delay)
        self.retry_patterns = retry_patterns if retry_patterns is not None else DEFAULT_TRANSIENT_RETRY_PATTERNS.copy()
        self.rate_limit_patterns = (
            rate_limit_patterns if rate_limit_patterns is not None else DEFAULT_RATE_LIMIT_PATTERNS.copy()
        )
        self.wait_on_rate_limit = wait_on_rate_limit
        self.max_workers = max(1, max_workers)
        self.output = output or (lambda line: print(line, end=""))
        self.diagnostic = diagnostic or (lambda _line: None)
        self.event_callback = event_callback
        self.name = name
        self.statistics = statistics
        self._active_lock = threading.Lock()
        self._active_processes: dict[int, _ActiveProcess] = {}
        self._model_fallback_lock = threading.Lock()
        self._use_default_model = False
        self._args_without_model, self._configured_model = _without_model_args(
            self.args
        )

    def run(
        self,
        prompt: str,
        *,
        retry_guard: Optional[RetryGuard] = None,
        cwd: Optional[Path] = None,
    ) -> ExecResult:
        return self._run_with_retries(
            prompt,
            self.output,
            self.name,
            retry_guard=retry_guard,
            cwd=cwd,
        )

    def run_interactive(self, prompt: str) -> ExecResult:
        session = self.name
        started = time.monotonic()
        terminal_state = _capture_terminal_state()
        argv, stdin_prompt = self._build_invocation(
            prompt,
            require_placeholder=True,
        )
        if stdin_prompt:
            raise ValueError(
                "interactive GigaCode args must include {prompt}; "
                "configure gigacode_interactive_args"
            )
        self._event(
            session,
            "prepared",
            mode="interactive",
            command=self._safe_command(argv, prompt),
            prompt_chars=len(prompt),
            prompt_transport="argv",
            cwd=Path.cwd(),
            stdin_tty=sys.stdin.isatty(),
            stdout_tty=sys.stdout.isatty(),
        )
        try:
            self._event(session, "launching")
            proc = subprocess.run(
                argv,
                timeout=self.timeout if self.timeout and self.timeout > 0 else None,
            )
        except FileNotFoundError as exc:
            self._event(session, "launch_failed", error="command_not_found")
            raise RuntimeError(f"gigacode command not found: {self.command}") from exc
        except subprocess.TimeoutExpired:
            self._event(
                session,
                "finished",
                returncode=-1,
                duration_ms=_elapsed_ms(started),
                timed_out=True,
            )
            return ExecResult(output="", returncode=-1, timed_out=True)
        finally:
            _restore_terminal_state(terminal_state)
        self._event(
            session,
            "finished",
            returncode=proc.returncode,
            duration_ms=_elapsed_ms(started),
            timed_out=False,
        )
        return ExecResult(output="", returncode=proc.returncode)

    def run_batch(
        self,
        prompts: dict[str, str],
        *,
        workdirs: Optional[dict[str, Path]] = None,
    ) -> dict[str, ExecResult]:
        if not prompts:
            return {}
        if workdirs is not None:
            missing = sorted(set(prompts) - set(workdirs))
            if missing:
                raise ValueError(
                    "missing batch workdirs for: " + ", ".join(missing)
                )
        results: dict[str, ExecResult] = {}
        pool = ThreadPoolExecutor(max_workers=min(self.max_workers, len(prompts)))
        futures = {}
        interrupted = False
        try:
            futures = {
                pool.submit(
                    self._run_with_retries,
                    prompt,
                    lambda _line: None,
                    f"{self.name}:{name}",
                    cwd=workdirs[name] if workdirs is not None else None,
                ): name
                for name, prompt in prompts.items()
            }
            for future in as_completed(futures):
                name = futures[future]
                results[name] = future.result()
        except KeyboardInterrupt:
            interrupted = True
            self._event(self.name, "batch_interrupted", active_futures=len(futures))
            for future in futures:
                future.cancel()
            self.terminate_active("interrupted")
            raise
        finally:
            pool.shutdown(wait=not interrupted, cancel_futures=interrupted)
        return results

    def command_line(self) -> str:
        transport_args = _with_stream_json_output(self.args)
        safe_args = [arg.replace("{prompt}", "<prompt>") for arg in transport_args]
        if not any("{prompt}" in arg for arg in self.args):
            safe_args.extend(["-p", "<prompt>"])
        return shlex.join([self.command, *safe_args])

    def _run_with_retries(
        self,
        prompt: str,
        output: Callable[[str], None],
        session: str,
        retry_guard: Optional[RetryGuard] = None,
        cwd: Optional[Path] = None,
    ) -> ExecResult:
        attempts = self.retry_count + 1
        last: Optional[ExecResult] = None
        for attempt in range(1, attempts + 1):
            self._event(session, "attempt_started", attempt=attempt, attempts=attempts)
            if attempt > 1:
                output(f"retrying gigacode command, attempt {attempt}/{attempts}\n")
            using_default_model = self._default_model_fallback_active()
            if using_default_model:
                result = (
                    self._run_without_configured_model(
                        prompt,
                        output,
                        session,
                        cwd=cwd,
                    )
                    if cwd is not None
                    else self._run_without_configured_model(prompt, output, session)
                )
            else:
                result = (
                    self._run_once(prompt, output, session, cwd=cwd)
                    if cwd is not None
                    else self._run_once(prompt, output, session)
                )
            result.attempts = attempt
            self._record_statistics(session, attempt, result)
            if result.ok:
                self._event(session, "attempt_succeeded", attempt=attempt)
                return result
            if result.approval_unavailable:
                self._event(
                    session,
                    "retry_stopped",
                    attempt=attempt,
                    reason="approval_unavailable",
                )
                return result
            if (
                not using_default_model
                and self._configured_model
                and _is_model_not_found(result)
            ):
                self._activate_default_model_fallback()
                self._event(
                    session,
                    "model_fallback",
                    configured_model=self._configured_model,
                    reason=result.api_error,
                )
                if retry_guard is not None and not retry_guard(result):
                    self._event(
                        session,
                        "retry_stopped",
                        attempt=attempt,
                        reason="retry_guard_rejected",
                    )
                    return result
                output(
                    f"configured GigaCode model {self._configured_model!r} "
                    "was not found; retrying with the default model\n"
                )
                result = (
                    self._run_without_configured_model(
                        prompt,
                        output,
                        session,
                        cwd=cwd,
                    )
                    if cwd is not None
                    else self._run_without_configured_model(prompt, output, session)
                )
                result.attempts = attempt
                self._record_statistics(session, attempt, result)
                if result.ok:
                    self._event(session, "attempt_succeeded", attempt=attempt)
                    return result
                if result.approval_unavailable:
                    self._event(
                        session,
                        "retry_stopped",
                        attempt=attempt,
                        reason="approval_unavailable",
                    )
                    return result
            last = result
            if attempt < attempts:
                if retry_guard is not None and not retry_guard(result):
                    self._event(
                        session,
                        "retry_stopped",
                        attempt=attempt,
                        reason="retry_guard_rejected",
                    )
                    return result
                delay = self._retry_delay(result)
                self._event(
                    session,
                    "retry_scheduled",
                    next_attempt=attempt + 1,
                    delay_seconds=delay,
                    reason=_failure_reason(result),
                )
                if result.rate_limited and delay > 0:
                    output(f"rate limit detected; waiting {delay:g}s before retry\n")
                elif result.transient_error and delay > 0:
                    output(f"transient error detected; waiting {delay:g}s before retry\n")
                time.sleep(delay)
        assert last is not None
        self._event(
            session,
            "attempts_exhausted",
            attempts=attempts,
            reason=_failure_reason(last),
        )
        return last

    def _run_once(
        self,
        prompt: str,
        output: Callable[[str], None],
        session: str,
        *,
        cwd: Optional[Path] = None,
    ) -> ExecResult:
        return self._run(prompt, output, session, cwd=cwd)

    def _run_without_configured_model(
        self,
        prompt: str,
        output: Callable[[str], None],
        session: str,
        *,
        cwd: Optional[Path] = None,
    ) -> ExecResult:
        return self._run(
            prompt,
            output,
            session,
            invocation_args=self._args_without_model,
            cwd=cwd,
        )

    def _default_model_fallback_active(self) -> bool:
        with self._model_fallback_lock:
            return self._use_default_model

    def _activate_default_model_fallback(self) -> None:
        with self._model_fallback_lock:
            self._use_default_model = True

    def _retry_delay(self, result: ExecResult) -> float:
        if result.rate_limited and self.wait_on_rate_limit is not None:
            return max(0.0, self.wait_on_rate_limit)
        return self.retry_delay

    def _run(
        self,
        prompt: str,
        output: Callable[[str], None],
        session: str,
        *,
        invocation_args: Optional[list[str]] = None,
        cwd: Optional[Path] = None,
    ) -> ExecResult:
        started = time.monotonic()
        argv, stdin_prompt = self._build_invocation(
            prompt,
            args=invocation_args,
        )
        pipe_stdin = bool(stdin_prompt)
        self._event(
            session,
            "prepared",
            mode="noninteractive",
            command=self._safe_command(argv, prompt),
            prompt_chars=len(prompt),
            prompt_transport="stdin" if pipe_stdin else "argv",
            cwd=cwd.resolve() if cwd is not None else Path.cwd(),
            stdin_tty=sys.stdin.isatty(),
            stdout_tty=sys.stdout.isatty(),
            stdout_capture=True,
            timeout_seconds=self.timeout or 0,
            idle_timeout_seconds=self.idle_timeout or 0,
        )
        try:
            self._event(session, "launching")
            popen_kwargs: dict[str, object] = {
                "stdin": subprocess.PIPE if pipe_stdin else subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "start_new_session": os.name == "posix",
            }
            if cwd is not None:
                popen_kwargs["cwd"] = str(cwd)
            proc = subprocess.Popen(argv, **popen_kwargs)
        except FileNotFoundError as exc:
            self._event(session, "launch_failed", error="command_not_found")
            raise RuntimeError(f"gigacode command not found: {self.command}") from exc

        self._event(session, "started", pid=getattr(proc, "pid", "unknown"))
        process_group = proc.pid if os.name == "posix" else None
        self._register_process(session, proc, process_group)
        assert proc.stdout is not None
        assert proc.stderr is not None
        if pipe_stdin:
            assert proc.stdin is not None
            proc.stdin.write(stdin_prompt.encode("utf-8"))
            proc.stdin.close()
            self._event(session, "stdin_sent", chars=len(stdin_prompt))

        chunks: list[str] = []
        stderr_chunks: list[str] = []
        stdout_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        stderr_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        stream_decoder = _StreamJsonDecoder()
        approval_scan_tails = {"stdout": "", "stderr": ""}
        approval_denied = False
        timed_out = False
        idle_timed_out = False
        first_output_seen = False
        termination_lock = threading.Lock()
        termination_started = False

        def terminate_process(reason: str) -> None:
            nonlocal termination_started
            with termination_lock:
                if termination_started:
                    return
                termination_started = True
                if process_group is not None:
                    _terminate_process_group(
                        proc,
                        process_group=process_group,
                        grace_seconds=PROCESS_TERMINATION_GRACE_SECONDS,
                        poll_seconds=PROCESS_TERMINATION_POLL_SECONDS,
                        event=lambda event, **fields: self._event(
                            session,
                            event,
                            reason=reason,
                            **fields,
                        ),
                    )
                    return

                if proc.poll() is not None:
                    return
                self._event(
                    session,
                    "terminating",
                    reason=reason,
                    signal="terminate",
                    target="process",
                )
                proc.terminate()
                try:
                    proc.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    self._event(
                        session,
                        "termination_escalated",
                        reason=reason,
                        signal="kill",
                        target="process",
                    )
                    proc.kill()

        def kill_on_timeout() -> None:
            nonlocal timed_out
            timed_out = True
            terminate_process("session_timeout")

        def kill_on_idle_timeout() -> None:
            nonlocal idle_timed_out
            idle_timed_out = True
            terminate_process("idle_timeout")

        timer: Optional[threading.Timer] = None
        idle_timer: Optional[threading.Timer] = None
        idle_generation = 0

        def reset_idle_timer(*, startup: bool = False) -> None:
            nonlocal idle_generation, idle_timer
            if self.idle_timeout is None or self.idle_timeout <= 0:
                return
            idle_generation += 1
            generation = idle_generation
            if idle_timer is not None:
                idle_timer.cancel()
            delay = max(self.idle_timeout, 1.0) if startup else self.idle_timeout

            def kill_if_current() -> None:
                if generation == idle_generation:
                    kill_on_idle_timeout()

            idle_timer = threading.Timer(delay, kill_if_current)
            idle_timer.daemon = True
            idle_timer.start()

        if self.timeout is not None and self.timeout > 0:
            timer = threading.Timer(self.timeout, kill_on_timeout)
            timer.daemon = True
            timer.start()
        try:
            stream_queue: queue.Queue[tuple[str, object]] = queue.Queue()

            def read_stream(name: str, stream: io.BufferedReader) -> None:
                try:
                    for raw_chunk in _read_output_chunks(stream):
                        stream_queue.put((name, raw_chunk))
                finally:
                    stream_queue.put((name, None))

            readers = [
                threading.Thread(
                    target=read_stream,
                    args=("stdout", proc.stdout),
                    daemon=True,
                ),
                threading.Thread(
                    target=read_stream,
                    args=("stderr", proc.stderr),
                    daemon=True,
                ),
            ]
            for reader in readers:
                reader.start()
            reset_idle_timer(startup=True)

            completed_streams = 0
            while completed_streams < len(readers):
                source, raw_chunk = stream_queue.get()
                if raw_chunk is None:
                    completed_streams += 1
                    continue
                reset_idle_timer()
                if not first_output_seen:
                    first_output_seen = True
                    self._event(
                        session,
                        "first_output",
                        elapsed_ms=_elapsed_ms(started),
                    )
                decoder = stdout_decoder if source == "stdout" else stderr_decoder
                chunk = raw_chunk if isinstance(raw_chunk, str) else decoder.decode(raw_chunk)
                self._notify_event(session, "activity", {"source": source})
                if source == "stdout" and chunk:
                    for visible in stream_decoder.feed(chunk):
                        chunks.append(visible)
                        output(visible)
                elif chunk:
                    stderr_chunks.append(chunk)
                    output(chunk)
                approval_scan = approval_scan_tails[source] + chunk
                approval_scan_tails[source] = approval_scan[-len(APPROVAL_UNAVAILABLE_TEXT):]
                if APPROVAL_UNAVAILABLE_TEXT in approval_scan:
                    approval_denied = True
                    self._event(session, "approval_warning_detected")
                    terminate_process("approval_unavailable")

            final_stdout = stdout_decoder.decode(b"", final=True)
            if final_stdout:
                for visible in stream_decoder.feed(final_stdout):
                    chunks.append(visible)
                    output(visible)
            final_stderr = stderr_decoder.decode(b"", final=True)
            if final_stderr:
                stderr_chunks.append(final_stderr)
                output(final_stderr)
            for visible in stream_decoder.finish():
                chunks.append(visible)
                output(visible)
            returncode = proc.wait()
        except KeyboardInterrupt:
            self._event(session, "interrupted")
            terminate_process("interrupted")
            proc.wait()
            raise
        finally:
            if timer is not None:
                timer.cancel()
            if idle_timer is not None:
                idle_timer.cancel()
            self._unregister_process(proc)
            proc.stdout.close()
            proc.stderr.close()

        if chunks and not chunks[-1].endswith("\n"):
            chunks.append("\n")
            output("\n")

        visible_text = "".join(chunks)
        text = stream_decoder.result_output
        if text is None:
            text = visible_text
        elif text and not text.endswith("\n"):
            text += "\n"
        error_text = "".join(stderr_chunks)
        wall_duration_ms = _elapsed_ms(started)
        failure_text = f"{text}\n{error_text}"
        api_error = _extract_api_error(text) or _extract_api_error(error_text)
        failed = returncode != 0 or timed_out or idle_timed_out or bool(api_error)
        dependency_crash = failed and is_dependency_crash(returncode, failure_text)
        result = ExecResult(
            output=text,
            error_output=error_text,
            signal=detect_signal(text),
            returncode=returncode,
            timed_out=timed_out,
            idle_timed_out=idle_timed_out,
            transient_error=failed and matches_any(failure_text, self.retry_patterns),
            rate_limited=failed and matches_any(failure_text, self.rate_limit_patterns),
            api_error=api_error,
            wall_duration_ms=wall_duration_ms,
            reported_duration_ms=stream_decoder.reported_duration_ms,
            api_duration_ms=stream_decoder.api_duration_ms,
            session_id=stream_decoder.session_id,
            models=stream_decoder.models,
            usage=stream_decoder.usage,
            approval_denied=approval_denied,
            dependency_crash=dependency_crash,
        )
        self._event(
            session,
            "finished",
            returncode=returncode,
            duration_ms=wall_duration_ms,
            output_chars=len(text),
            stderr_chars=len(error_text),
            input_tokens=result.usage.input_tokens if result.usage else "unknown",
            output_tokens=result.usage.output_tokens if result.usage else "unknown",
            total_tokens=result.usage.total_tokens if result.usage else "unknown",
            signal=result.signal or "none",
            timed_out=timed_out,
            idle_timed_out=idle_timed_out,
            transient_error=result.transient_error,
            rate_limited=result.rate_limited,
            api_error=result.api_error or False,
            approval_unavailable=result.approval_unavailable,
            dependency_crash=result.dependency_crash,
        )
        return result

    def terminate_active(self, reason: str) -> None:
        with self._active_lock:
            active = list(self._active_processes.values())
        for item in active:
            self._terminate_registered_process(item, reason)

    def _register_process(
        self,
        session: str,
        proc: subprocess.Popen[bytes],
        process_group: Optional[int],
    ) -> None:
        with self._active_lock:
            self._active_processes[id(proc)] = _ActiveProcess(
                session=session,
                proc=proc,
                process_group=process_group,
            )

    def _unregister_process(self, proc: subprocess.Popen[bytes]) -> None:
        with self._active_lock:
            self._active_processes.pop(id(proc), None)

    def _terminate_registered_process(
        self,
        item: _ActiveProcess,
        reason: str,
    ) -> None:
        proc = item.proc
        if proc.poll() is not None:
            return
        if item.process_group is not None:
            _terminate_process_group(
                proc,
                process_group=item.process_group,
                grace_seconds=PROCESS_TERMINATION_GRACE_SECONDS,
                poll_seconds=PROCESS_TERMINATION_POLL_SECONDS,
                event=lambda event, **fields: self._event(
                    item.session,
                    event,
                    reason=reason,
                    **fields,
                ),
            )
            return

        self._event(
            item.session,
            "terminating",
            reason=reason,
            signal="terminate",
            target="process",
        )
        proc.terminate()
        try:
            proc.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            self._event(
                item.session,
                "termination_escalated",
                reason=reason,
                signal="kill",
                target="process",
            )
            proc.kill()

    def _record_statistics(
        self,
        session: str,
        attempt: int,
        result: ExecResult,
    ) -> None:
        if self.statistics is None:
            return
        self.statistics.add(
            InvocationStat(
                session=session,
                attempt=attempt,
                status=_result_status(result),
                returncode=result.returncode,
                wall_duration_ms=result.wall_duration_ms,
                reported_duration_ms=result.reported_duration_ms,
                api_duration_ms=result.api_duration_ms,
                session_id=result.session_id,
                models=result.models,
                usage=result.usage,
            )
        )

    def _build_invocation(
        self,
        prompt: str,
        *,
        require_placeholder: bool = False,
        args: Optional[list[str]] = None,
    ) -> tuple[list[str], str]:
        used_placeholder = False
        rendered_args: list[str] = []
        for arg in self.args if args is None else args:
            if "{prompt}" in arg:
                rendered_args.append(arg.replace("{prompt}", prompt))
                used_placeholder = True
            else:
                rendered_args.append(arg)
        if not used_placeholder:
            if require_placeholder:
                return [self.command, *rendered_args], prompt
            rendered_args.extend(["-p", prompt])
        if not require_placeholder:
            rendered_args = _with_stream_json_output(rendered_args)
        return [self.command, *rendered_args], ""

    def _safe_command(self, argv: list[str], prompt: str) -> str:
        safe_args = [
            arg.replace(prompt, "<prompt>") if prompt and prompt in arg else arg
            for arg in argv
        ]
        return shlex.join(safe_args)

    def _event(self, session: str, event: str, **fields: object) -> None:
        self._notify_event(session, event, fields)
        details = " ".join(
            f"{key}={_diagnostic_value(value)}"
            for key, value in fields.items()
        )
        suffix = f" {details}" if details else ""
        self.diagnostic(f"session={session} event={event}{suffix}")

    def _notify_event(
        self,
        session: str,
        event: str,
        fields: dict[str, object],
    ) -> None:
        if self.event_callback is not None:
            self.event_callback(session, event, fields)


def matches_any(text: str, patterns: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(pattern and pattern.lower() in lowered for pattern in patterns)


def _extract_api_error(text: str) -> str:
    match = API_ERROR_RE.search(text)
    return match.group(1).strip() if match else ""


def _is_model_not_found(result: ExecResult) -> bool:
    api_error = result.api_error.casefold()
    return api_error.startswith("api error: 404") and "model not found" in api_error


def _without_model_args(args: list[str]) -> tuple[list[str], str]:
    normalized: list[str] = []
    models: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-m", "--model"}:
            if index + 1 < len(args):
                models.append(args[index + 1])
                index += 2
            else:
                index += 1
            continue
        if arg.startswith("-m=") or arg.startswith("--model="):
            models.append(arg.split("=", 1)[1])
            index += 1
            continue
        normalized.append(arg)
        index += 1
    return normalized, ", ".join(model for model in models if model)


def _terminate_process_group(
    proc: subprocess.Popen[bytes],
    *,
    process_group: int,
    grace_seconds: float,
    poll_seconds: float,
    event: Callable[..., None],
) -> None:
    event("terminating", signal="SIGTERM", target="process_group")
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        event("termination_fallback", signal="SIGKILL", target="process")
        proc.kill()
        return

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if not _process_group_exists(process_group):
            return
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

    if not _process_group_exists(process_group):
        return
    event("termination_escalated", signal="SIGKILL", target="process_group")
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _with_stream_json_output(args: list[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-o", "--output-format"}:
            index += 2 if index + 1 < len(args) else 1
            continue
        if arg.startswith("--output-format="):
            index += 1
            continue
        normalized.append(arg)
        index += 1

    insertion_index = len(normalized)
    for index, arg in enumerate(normalized):
        if arg in {"-p", "--prompt"} or arg.startswith("--prompt="):
            insertion_index = index
            break
    return [
        *normalized[:insertion_index],
        "--output-format",
        "stream-json",
        *normalized[insertion_index:],
    ]


class _StreamJsonDecoder:
    def __init__(self) -> None:
        self._buffer = ""
        self._assistant_text_seen = False
        self.reported_duration_ms: Optional[int] = None
        self.api_duration_ms: Optional[int] = None
        self.session_id = ""
        self.models: tuple[str, ...] = ()
        self.usage: Optional[TokenUsage] = None
        self.result_output: Optional[str] = None

    def feed(self, text: str) -> list[str]:
        self._buffer += text
        visible: list[str] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            visible.extend(self._process_line(line, terminated=True))
        return visible

    def finish(self) -> list[str]:
        if not self._buffer:
            return []
        line = self._buffer
        self._buffer = ""
        return self._process_line(line, terminated=False)

    def _process_line(self, line: str, *, terminated: bool) -> list[str]:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return [line + ("\n" if terminated else "")]
        if not isinstance(event, dict):
            return [line + ("\n" if terminated else "")]

        event_type = event.get("type")
        if event_type == "system":
            self.session_id = _string_value(event.get("session_id")) or self.session_id
            model = _string_value(event.get("model"))
            if model and not self.models:
                self.models = (model,)
            return []
        if event_type == "assistant":
            self.session_id = _string_value(event.get("session_id")) or self.session_id
            message = event.get("message")
            if not isinstance(message, dict):
                return []
            model = _string_value(message.get("model"))
            if model:
                self.models = tuple(dict.fromkeys([*self.models, model]))
            text = _assistant_text(message.get("content"))
            if not text:
                return []
            self._assistant_text_seen = True
            return [text if text.endswith("\n") else text + "\n"]
        if event_type == "result":
            self.session_id = _string_value(event.get("session_id")) or self.session_id
            self.reported_duration_ms = _optional_int(event.get("duration_ms"))
            self.api_duration_ms = _optional_int(event.get("duration_api_ms"))
            self.usage = _parse_usage(event.get("usage"))
            models = _models_from_stats(event.get("stats"))
            if models:
                self.models = models
            result = _string_value(event.get("result"))
            self.result_output = result
            if result and not self._assistant_text_seen:
                return [result if result.endswith("\n") else result + "\n"]
            return []
        return []


def _assistant_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _parse_usage(value: object) -> Optional[TokenUsage]:
    if not isinstance(value, dict):
        return None
    input_tokens = _optional_int(value.get("input_tokens"))
    output_tokens = _optional_int(value.get("output_tokens"))
    cache_tokens = _optional_int(value.get("cache_read_input_tokens")) or 0
    total_tokens = _optional_int(value.get("total_tokens"))
    if input_tokens is None or output_tokens is None:
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_tokens,
        total_tokens=total_tokens if total_tokens is not None else input_tokens + output_tokens,
    )


def _models_from_stats(value: object) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    models = value.get("models")
    if not isinstance(models, dict):
        return ()
    return tuple(str(name) for name in models)


def _optional_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value)
    return None


def _string_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def _read_output_chunks(
    stream: io.BufferedReader,
):
    if isinstance(stream, io.BufferedReader):
        file_descriptor = stream.fileno()
        while chunk := os.read(file_descriptor, 4096):
            yield chunk
        return

    # Test doubles and unusual stream wrappers may only support iteration.
    yield from stream


def _capture_terminal_state() -> Optional[tuple[int, object]]:
    if os.name != "posix":
        return None
    try:
        import termios

        file_descriptor = sys.stdin.fileno()
        if not os.isatty(file_descriptor):
            return None
        return file_descriptor, termios.tcgetattr(file_descriptor)
    except (AttributeError, io.UnsupportedOperation, OSError, ValueError):
        return None


def _restore_terminal_state(state: Optional[tuple[int, object]]) -> None:
    if state is None or os.name != "posix":
        return
    file_descriptor, attributes = state
    try:
        import termios

        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, attributes)
    except (AttributeError, OSError, ValueError):
        pass


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _failure_reason(result: ExecResult) -> str:
    if result.approval_unavailable:
        return "approval_unavailable"
    if result.timed_out:
        return "session_timeout"
    if result.idle_timed_out:
        return "idle_timeout"
    if result.dependency_crash:
        return "dependency_crash"
    if result.rate_limited:
        return "rate_limited"
    if result.transient_error:
        return "transient_error"
    if result.api_error:
        return "api_error"
    return f"exit_{result.returncode}"


def _result_status(result: ExecResult) -> str:
    if result.ok:
        return "success"
    return _failure_reason(result)


def _diagnostic_value(value: object) -> str:
    text = str(value)
    if text and all(char.isalnum() or char in "._:/-+" for char in text):
        return text
    return json.dumps(text, ensure_ascii=False)


class DryRunExecutor:
    def __init__(self, output: Optional[Callable[[str], None]] = None) -> None:
        self.output = output or (lambda line: sys.stdout.write(line))
        self.prompts: list[str] = []

    def run(
        self,
        prompt: str,
        *,
        retry_guard: Optional[RetryGuard] = None,
    ) -> ExecResult:
        self.prompts.append(prompt)
        self.output("--- DRY RUN PROMPT ---\n")
        self.output(prompt)
        self.output("\n--- END PROMPT ---\n")
        return ExecResult(output="", returncode=0)
