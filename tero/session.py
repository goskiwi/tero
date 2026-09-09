"""The single recoverable conversation snapshot, separate from long-term memory."""

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .changes import observe_interrupted_file
from .storage import now, save_json
from .tool_executor import ToolResult
from .tools import READ_TOOLS


def new_verification():
    return {"status": "not_run", "command": "", "call_id": "", "files": None}


def new_loop_control():
    return {
        "invalid_outputs": 0,
        "completion_blocks": 0,
        "completion_reason": "",
        "completion_paths": [],
        "last_failure": None,
        "recent_failures": [],
        "denied": [],
    }


def new_turn(items, results=None):
    results = dict(results or {})
    return {
        "kind": "turn",
        "items": items,
        "results": results,
        "phases": {
            item["call_id"]: "finished" if item["call_id"] in results else "pending"
            for item in items
            if item.get("type") == "function_call"
        },
    }


@dataclass
class Session:
    id: str
    workspace: str
    history: list[dict] = field(default_factory=list)
    summary: str = ""
    covered: int = 0
    observed: int = 0
    compaction_failure: dict | None = None
    request_start: int = 0
    run: dict = field(default_factory=dict)
    loop_control: dict = field(default_factory=new_loop_control)
    verification_required: bool = False
    verification: dict = field(default_factory=new_verification)
    unconfirmed: list[dict] = field(default_factory=list)
    mutations: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=now)

    @classmethod
    def create(cls, workspace):
        return cls(uuid.uuid4().hex[:16], str(Path(workspace).resolve()))

    def user(self, text):
        self.history.append({"kind": "message", "items": [{"role": "user", "content": text}]})

    def pending(self):
        return [
            entry
            for entry in self.history
            if entry["kind"] == "turn"
            and any(
                item.get("type") == "function_call" and item["call_id"] not in entry["results"]
                for item in entry["items"]
            )
        ]

    def recover(self):
        """Close ambiguous calls; never repeat their external actions."""
        count = 0
        for index, entry in enumerate(self.history):
            if entry["kind"] != "turn":
                continue
            for item in entry["items"]:
                if item.get("type") != "function_call" or item["call_id"] in entry["results"]:
                    continue
                phase = entry["phases"][item["call_id"]]
                effect, data = "none", {}
                if phase == "running" and item["name"] not in READ_TOOLS:
                    effect, data = (
                        observe_interrupted_file(self, index, item)
                        if item["name"] in {"edit_file", "write_file"}
                        else ("unknown", {})
                    )
                entry["results"][item["call_id"]] = ToolResult(
                    "partial_success" if effect == "changed" else "error",
                    "Not started; no old action will be replayed."
                    if phase == "pending"
                    else "Execution interrupted; current observation is recorded, not a replayed success.",
                    effect,
                    "not_started" if phase == "pending" else "interrupted",
                    data,
                ).to_dict()
                entry["phases"][item["call_id"]] = "finished"
                self.observe_result(index, item, entry["results"][item["call_id"]])
                count += 1
        if self.verification["status"] == "running":
            self.add_unconfirmed(self.verification["call_id"], "verify")
            self.verification.update(status="interrupted", files=None)
            count += 1
        return count

    def add_unconfirmed(self, effect_id, tool, path=None):
        if not any(item["id"] == effect_id for item in self.unconfirmed):
            self.unconfirmed.append(
                {"id": effect_id, "tool": tool, "path": path, "observations": []}
            )
        self.verification_required = True

    def observe_result(self, index, call, result):
        """Track only unresolved effects; historical failures do not block forever."""
        data = result.get("data", {})
        name = call["name"]
        if result.get("workspace_effect") == "unknown":
            path = data.get("path") if name in {"edit_file", "write_file"} else None
            if path is None and name in {"edit_file", "write_file"}:
                try:
                    raw = Path(json.loads(call["arguments"])["path"])
                    logical = raw.relative_to(self.workspace) if raw.is_absolute() else raw
                    if ".." not in logical.parts and logical.parts:
                        path = logical.as_posix()
                except (ValueError, KeyError, TypeError):
                    pass
            self.add_unconfirmed(f"tool:{index}:{call['call_id']}", name, path)
        elif result.get("workspace_effect") == "changed" and result.get("status") != "success":
            self.verification_required = True
        # Store references to actual tool results, never a model-written acknowledgement.
        # These observations do not establish that all external effects are resolved.
        if (
            name in READ_TOOLS | {"run_shell"}
            and result.get("status") == "success"
            and result.get("workspace_effect") == "none"
        ):
            for item in self.unconfirmed:
                evidence = {"history_index": index, "call_id": call["call_id"]}
                if evidence not in item["observations"]:
                    item["observations"].append(evidence)
        if (
            name == "read_file"
            and data.get("revision")
            and (result.get("status") == "success" or result.get("error") == "missing_path")
        ):
            resolved = [
                item
                for item in self.unconfirmed
                if item["tool"] in {"edit_file", "write_file"} and item["path"] == data.get("path")
            ]
            for item in resolved:
                self.unconfirmed.remove(item)

    def user_sources(self, start=None):
        start = self.request_start if start is None else start
        return [
            {"index": i, "text": item["content"]}
            for i, entry in enumerate(self.history)
            if i >= start and entry["kind"] == "message"
            for item in entry["items"]
            if item.get("role") == "user"
        ]


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, session_id):
        if not re.fullmatch(r"[a-f0-9]{16}", session_id):
            raise ValueError("Invalid session ID")
        return self.root / (session_id + ".json")

    def save(self, session):
        save_json(self.path(session.id), asdict(session))

    def load(self, session_id, workspace):
        path = self.path(session_id)
        if path.resolve() != path.absolute():
            raise ValueError("Session path must not be redirected")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("Session must be an object")
        if (
            not {
                "verification_required",
                "verification",
                "unconfirmed",
                "observed",
                "compaction_failure",
                "loop_control",
                "mutations",
            }
            <= value.keys()
        ):
            raise ValueError("Missing completion state in session")
        session = Session(**value)
        if session.id != session_id:
            raise ValueError("Session identity does not match requested ID")
        if type(session.request_start) is not int or (
            session.history and not 0 <= session.request_start < len(session.history)
        ) or (not session.history and session.request_start != 0):
            raise ValueError("Invalid current request position")
        if session.history:
            request = session.history[session.request_start]
            if request.get("kind") != "message" or not any(
                item.get("role") == "user" for item in request.get("items", [])
            ):
                raise ValueError("Current request must identify a user message")

        control = session.loop_control
        if (
            not isinstance(control, dict)
            or set(control) != set(new_loop_control())
            or any(
                type(control[name]) is not int or control[name] < 0
                for name in ("invalid_outputs", "completion_blocks")
            )
            or not isinstance(control["denied"], list)
            or any(
                not isinstance(item, dict)
                or set(item) != {"tool", "arguments"}
                or not isinstance(item["tool"], str)
                or not isinstance(item["arguments"], dict)
                for item in control["denied"]
            )
            or not isinstance(control["completion_reason"], str)
            or not isinstance(control["completion_paths"], list)
            or any(not isinstance(path, str) for path in control["completion_paths"])
            or (
                control["last_failure"] is not None
                and (
                    not isinstance(control["last_failure"], dict)
                    or set(control["last_failure"]) != {"key", "count"}
                    or not isinstance(control["last_failure"]["key"], dict)
                    or set(control["last_failure"]["key"])
                    != {"tool", "arguments", "error", "actual_revision", "read_revision"}
                    or type(control["last_failure"]["count"]) is not int
                    or control["last_failure"]["count"] < 1
                )
            )
        ):
            raise ValueError("Invalid loop control state")
        if not isinstance(control["recent_failures"], list) or len(control["recent_failures"]) > 8:
            raise ValueError("Invalid recent failure history")
        for failure in control["recent_failures"]:
            if not isinstance(failure, dict) or set(failure) != {"key", "count"} or not isinstance(failure["key"], dict) or type(failure["count"]) is not int or failure["count"] < 1:
                raise ValueError("Invalid recent failure record")
        if not isinstance(session.verification_required, bool) or not isinstance(
            session.unconfirmed, list
        ):
            raise TypeError("Invalid completion state")
        if not isinstance(session.verification, dict) or set(session.verification) != set(
            new_verification()
        ):
            raise ValueError("Invalid verification record")
        if session.verification["status"] not in {
            "not_run",
            "running",
            "passed",
            "failed",
            "interrupted",
            "stale",
        }:
            raise ValueError("Invalid verification status")
        files = session.verification["files"]
        if files is not None and (
            not isinstance(files, dict)
            or any(
                not isinstance(key, str) or not isinstance(revision, str)
                for key, revision in files.items()
            )
        ):
            raise ValueError("Invalid verification file states")
        if session.verification["status"] == "passed" and (
            files is None or not session.verification["command"]
        ):
            raise ValueError("Passed verification needs its command and observed file states")
        if any(
            not isinstance(item, dict)
            or set(item) != {"id", "tool", "path", "observations"}
            or not isinstance(item["id"], str)
            or not isinstance(item["tool"], str)
            or not isinstance(item["observations"], list)
            or any(
                not isinstance(observation, dict)
                or set(observation) != {"history_index", "call_id"}
                or not isinstance(observation["history_index"], int)
                or not isinstance(observation["call_id"], str)
                for observation in item["observations"]
            )
            or (item["path"] is not None and not isinstance(item["path"], str))
            for item in session.unconfirmed
        ):
            raise ValueError("Invalid unconfirmed effect record")
        if not isinstance(session.mutations, list):
            raise TypeError("Invalid mutation receipts")
        for receipt in session.mutations:
            if (
                not isinstance(receipt, dict)
                or not {
                    "id",
                    "operation",
                    "path",
                    "before_revision",
                    "after_revision",
                    "preimage_id",
                    "status",
                }
                <= receipt.keys()
                or receipt["status"] not in {"prepared", "applied", "not_applied", "unknown"}
            ):
                raise ValueError("Invalid mutation receipt")
        for entry in session.history:
            if entry["kind"] == "turn":
                ids = {
                    item["call_id"]
                    for item in entry["items"]
                    if item.get("type") == "function_call"
                }
                phases = entry.get("phases")
                if (
                    not isinstance(phases, dict)
                    or set(phases) != ids
                    or any(
                        phase not in {"pending", "running", "finished"}
                        or (phase == "finished") != (call_id in entry["results"])
                        for call_id, phase in phases.items()
                    )
                ):
                    raise ValueError("Invalid persisted tool execution phases")
            for result in entry.get("results", {}).values():
                if "recovery" not in result:
                    raise ValueError("Tool result is missing recovery information")
                recovery = result["recovery"]
                if recovery is not None and (
                    not isinstance(recovery, dict)
                    or set(recovery) != {"condition", "requirement", "action", "suggested_call"}
                    or recovery["condition"]
                    not in {
                        "retry_after_change",
                        "retry_after_wait",
                        "user_action_required",
                        "no_retry",
                    }
                    or not isinstance(recovery["requirement"], str)
                    or not isinstance(recovery["action"], str)
                    or (
                        recovery["suggested_call"] is not None
                        and not isinstance(recovery["suggested_call"], dict)
                    )
                ):
                    raise ValueError("Invalid tool recovery information")
        if session.workspace != str(Path(workspace).resolve()):
            raise ValueError("Session belongs to another workspace")
        if session.compaction_failure is not None and (
            not isinstance(session.compaction_failure, dict)
            or set(session.compaction_failure) != {"key", "reason", "detail"}
            or not isinstance(session.compaction_failure["key"], dict)
            or not isinstance(session.compaction_failure["reason"], str)
            or not isinstance(session.compaction_failure["detail"], str)
        ):
            raise ValueError("Invalid compaction failure record")
        if not isinstance(
            session.observed, int
        ) or not 0 <= session.covered <= session.observed <= len(session.history):
            raise ValueError("Invalid summary coverage")
        return session

    def latest(self):
        paths = list(self.root.glob("*.json")) if self.root.exists() else []
        return max(paths, key=lambda path: path.stat().st_mtime).stem if paths else None
