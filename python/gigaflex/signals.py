ALL_TASKS_DONE = "<<<GIGAFLEX:ALL_TASKS_DONE>>>"
TASK_FAILED = "<<<GIGAFLEX:TASK_FAILED>>>"
REVIEW_DONE = "<<<GIGAFLEX:REVIEW_DONE>>>"
FINALIZE_DONE = "<<<GIGAFLEX:FINALIZE_DONE>>>"
FINALIZE_FAILED = "<<<GIGAFLEX:FINALIZE_FAILED>>>"


def detect_signal(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    last_line = lines[-1]
    known_signals = {
        ALL_TASKS_DONE,
        TASK_FAILED,
        REVIEW_DONE,
        FINALIZE_DONE,
        FINALIZE_FAILED,
    }
    return last_line if last_line in known_signals else ""
