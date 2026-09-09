"""Small atomic writes and readable diagnostics; no replay engine."""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def now():
    return datetime.now(UTC).isoformat()


def atomic_write(path: Path, payload: bytes, *, before_replace=None, create=False, mode=0o600):
    if path.resolve() != path.absolute():
        raise ValueError("Storage target is redirected; refusing to follow a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        if before_replace:
            before_replace()
        if create:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_json(path, value):
    atomic_write(Path(path), (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


class Trace:
    def __init__(self, path, redactor, display=None):
        self.path = Path(path)
        self.redact = redactor
        self.display = display
        self.metrics = {
            "model_requests": 0,
            "model_responses": 0,
            "retries": 0,
            "compactions": 0,
            "input_tokens": None,
            "output_tokens": None,
            "cached_tokens": None,
            "usage_complete": True,
        }

    def record(self, event, **data):
        entry = {"time": now(), "event": event, **data}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = self.redact(json.dumps(entry, ensure_ascii=False))
        with self.path.open("a") as handle:
            handle.write(text + "\n")
        if event == "model_requested":
            self.metrics["model_requests"] += 1
        elif event == "model_retry":
            self.metrics["retries"] += 1
        elif event == "compacted":
            self.metrics["compactions"] += 1
        elif event == "model_finished":
            self.metrics["model_responses"] += 1
            usage = data.get("usage") or {}
            values = {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens"),
            }
            for key, value in values.items():
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    self.metrics[key] = (self.metrics[key] or 0) + value
                else:
                    self.metrics["usage_complete"] = False
        if self.display:
            try:
                self.display(event, json.loads(text))
            except (OSError, ValueError):
                # A broken terminal must not change the outcome of an executed operation.
                self.display = None
