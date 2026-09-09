"""One execution boundary: validate, approve, operate, and describe observed effects."""

import codecs
import shlex
import shutil
import subprocess
import difflib
import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pydantic import ValidationError

from .artifacts import CAPTURE_BYTES, PREVIEW_BYTES, ArtifactStore
from .commands import run_command
from .execution import ExecutionStopped
from .storage import atomic_write
from .tools import READ_TOOLS, TOOLS, effective_tools
from .workspace import git_state

IGNORED = {".git", ".tero", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
OUTPUT_CHARS = 24000


class ToolError(ValueError):
    """A classified rejection before this operation changes a file."""

    def __init__(self, code, message, *, data=None):
        super().__init__(message)
        self.code = code
        self.data = data or {}


@dataclass
class ToolResult:
    status: str
    content: str
    workspace_effect: str = "none"
    error: str = ""
    data: dict = field(default_factory=dict)

    @property
    def recovery(self):
        if self.status == "success" and self.workspace_effect != "unknown":
            return None
        return recovery_for(self.error, self.workspace_effect, self.data)

    def to_dict(self):
        return {**asdict(self), "recovery": self.recovery}


def recovery_for(code, effect, data):
    """Explain established recovery conditions. Never execute the suggested action."""
    path = data.get("path")
    read = (
        {
            "name": "read_file",
            "arguments": {
                "path": path,
                "start": data.get("read_start", 1),
                "end": data.get("read_end", 200),
            },
        }
        if path
        else None
    )

    def advice(condition, action, suggested_call=None):
        category = "retry_after_change"
        if code in {"approval_denied", "verification_denied"}:
            category = "no_retry"
        elif code in {
            "cancelled",
            "time_limit",
            "permission_denied",
            "write_scope_denied",
            "shell_scope_denied",
            "filesystem_permission_denied",
            "verification_required",
        }:
            category = "user_action_required"
        return {
            "condition": category,
            "requirement": condition,
            "action": action,
            "suggested_call": suggested_call,
        }

    if code in {"cancelled", "time_limit"}:
        return advice(
            "The user resumes the task or supplies a new execution budget.",
            "Stop now. On resume inspect any uncertain effects before retrying; do not replay automatically.",
        )
    if effect == "unknown" or code in {"unconfirmed_effects", "effect_inspection_required"}:
        return advice(
            "Relevant file, process or remote state has been observed after the interruption.",
            "Inspect current state before deciding whether to retry. Do not blindly rerun the command. "
            "Local tests cannot establish remote effects; ask the user if essential state cannot be queried.",
            read,
        )
    if code in {
        "revision_conflict",
        "read_required",
        "target_changed",
        "file_observation_required",
    }:
        return advice(
            "The target has been read again and the edit rebuilt from current content.",
            "Read the target, preserve external edits, then construct a new exact edit. Do not resubmit stale content.",
            read,
        )
    if code in {"ambiguous_text_match", "text_not_found"}:
        return advice(
            "An exact block uniquely identifies the intended location in current content.",
            "Read the suggested range and match locations; expand the surrounding text until unique. "
            "A similar fragment is only a navigation hint, never permission for fuzzy replacement.",
            read,
        )
    if code == "missing_path":
        parent = str(Path(path).parent) if path else "."
        return advice(
            "An existing intended path has been located, or creation has been explicitly requested.",
            "List the parent directory and correct the path; if the parent is missing, start from the workspace root. "
            "Do not create a replacement merely to hide a read failure.",
            {"name": "list_files", "arguments": {"path": parent}},
        )
    rules = {
        "unknown_tool": (
            "An available tool is selected.",
            "Choose from the supplied tool schemas; do not repeat an unknown tool name.",
        ),
        "invalid_arguments": (
            "Arguments satisfy the tool schema.",
            "Correct the reported fields and types before retrying the requested operation.",
        ),
        "invalid_range": (
            "1 <= start <= end and the range contains at most 2000 lines.",
            "Choose a valid read range; split a large read into separate ranges.",
        ),
        "not_file": (
            "The path identifies a regular file.",
            "Inspect the directory and select a file; do not overwrite a directory.",
        ),
        "not_directory": (
            "The path identifies a directory.",
            "Use list_files on its parent, or read_file if the intended target is a file.",
        ),
        "path_outside_workspace": (
            "The intended target is inside the configured workspace.",
            "Use an in-workspace path, or ask the user to change the workspace. Do not bypass the boundary using Shell or links.",
        ),
        "protected_path": (
            "A permitted target is selected.",
            "Do not edit Runtime state or Git internals, including through Shell; choose the intended project file.",
        ),
        "write_scope_denied": (
            "The user authorizes a write scope containing this path.",
            "Report the blocked path or work on an allowed alternative. Do not bypass the scope using Shell.",
        ),
        "shell_scope_denied": (
            "The user explicitly changes the restricted-write policy.",
            "Use file tools within the allowed scope. General Shell cannot enforce that scope and must not be used as a workaround.",
        ),
        "permission_denied": (
            "The user selects a mode or capability allowing this operation.",
            "Continue permitted observation or explain the blocker; the model cannot grant itself permission.",
        ),
        "approval_denied": (
            "The user explicitly changes the decision or approves a different legitimate operation.",
            "Stop requesting the same denied operation. Explain the blocker or use an already-authorized alternative.",
        ),
        "filesystem_permission_denied": (
            "Filesystem permissions allow the intended access.",
            "Report the affected path and ask the user to fix access; do not automatically elevate privileges.",
        ),
        "invalid_encoding": (
            "A suitable reader is available for the file encoding.",
            "This tool expects UTF-8. Do not overwrite undecodable content; inspect the format with an authorized alternative.",
        ),
        "tool_timeout": (
            "The previous process/effects have been checked and a viable bounded command is chosen.",
            "Inspect the timeout output and changed paths; narrow the command or explicitly adjust its budget before retrying.",
        ),
        "command_failed": (
            "The cause shown by the exit code and output has been addressed.",
            "Inspect stdout/stderr and changed paths. Diagnose code, command or environment before rerunning; do not guess a patch from the exit code alone.",
        ),
        "verification_failed": (
            "Reported verification failures or verifier-created changes have been addressed.",
            "Read the Runtime verification output, inspect relevant code and repair the cause. Then submit completion for fresh verification.",
        ),
        "verification_stale": (
            "Post-verification file changes have been inspected.",
            "Inspect changed_paths and submit completion again so Runtime verifies the current state.",
        ),
        "verification_required": (
            "The user configures the required verifier and a mode permitting it.",
            "Explain that this task cannot complete without verification; do not remove the requirement on resume.",
        ),
        "verification_denied": (
            "The user approves verification.",
            "Stop repeated approval requests and report that completion is blocked by denied verification.",
        ),
    }
    if code in rules:
        return advice(*rules[code])
    return advice(
        "The reported failure has been investigated and a concrete recovery condition identified.",
        "Inspect the recorded error and current state. Do not automatically repeat the operation or invent a root cause.",
    )


def filesystem_failure_code(exc):
    for kind, code in (
        (FileNotFoundError, "missing_path"),
        (PermissionError, "filesystem_permission_denied"),
        (NotADirectoryError, "not_directory"),
        (IsADirectoryError, "not_file"),
        (UnicodeError, "invalid_encoding"),
    ):
        if isinstance(exc, kind):
            return code
    return "filesystem_error"


def content_revision(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def file_revision(path, budget):
    try:
        with path.open("rb") as handle:
            digest = hashlib.sha256()
            while chunk := handle.read(1024 * 1024):
                budget.check()
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    except FileNotFoundError:
        return "absent"


class ToolExecutor:
    def __init__(
        self,
        root,
        config,
        budget,
        trace,
        approve=None,
        delegate=None,
        *,
        child=False,
        denied=None,
        save_denial=None,
        artifacts=None,
        save_mutation=None,
    ):
        self.root = Path(root).resolve()
        self.config, self.budget, self.trace = config, budget, trace
        self.available_tools = effective_tools(config.mode, child=child, allowed_tools=config.allowed_tools)
        self.approve, self.delegate = approve, delegate
        self.read_versions = {}
        self.denied = denied if denied is not None else []
        self.save_denial = save_denial
        self.artifacts = artifacts or ArtifactStore(
            self.root / ".tero/artifacts" / uuid.uuid4().hex, trace.redact
        )
        self.save_mutation = save_mutation
        self.operation = None

    def resolve(self, value):
        path = Path(value)
        target = (path if path.is_absolute() else self.root / path).resolve()
        try:
            relative = target.relative_to(self.root)
        except ValueError as exc:
            raise ToolError("path_outside_workspace", "Path escapes workspace") from exc
        if any(part in {".tero", ".git"} for part in relative.parts):
            raise ToolError(
                "protected_path", "Agent state and Git internals are not file-tool targets"
            )
        return target

    def _require_target(self, target):
        if target.resolve() != target:
            raise ToolError(
                "target_changed", "File target changed; inspect and authorize the new target"
            )

    def _require_revision(self, target, expected):
        self._require_target(target)
        actual = file_revision(target, self.budget)
        if actual != expected:
            raise ToolError(
                "revision_conflict",
                "File changed since it was read; use read_file before editing again",
                data={"expected_revision": expected, "actual_revision": actual},
            )

    def execute(self, name, arguments, *, on_start=None):
        prepared = self.admit(name, arguments)
        if isinstance(prepared, ToolResult):
            return self.finish(name, prepared)
        args, target = prepared
        if on_start is not None:
            on_start()
        self.trace.record("tool_started", tool=name)
        return self.finish(name, self.run_admitted(name, args, target))

    def finish(self, name, result):
        """Main-thread boundary for read cache, durable artifacts, preview and Trace."""
        original = json.dumps(result.to_dict(), ensure_ascii=False)
        redacted = self.trace.redact(original) != original
        result.content = self.trace.redact(result.content)
        result.data = json.loads(self.trace.redact(json.dumps(result.data, ensure_ascii=False)))
        if name == "read_file" and result.status == "success":
            self.read_versions[result.data["path"]] = result.data["revision"]
        full = json.dumps(result.to_dict(), ensure_ascii=False)
        capture_truncated = result.data.get("capture_truncated", False)
        if name != "read_artifact" and (len(full.encode()) > PREVIEW_BYTES or capture_truncated):
            artifact = self.artifacts.write_text(
                full, capture_truncated=capture_truncated, redacted=redacted
            )
            result.data.update(
                artifact_id=artifact["id"],
                capture_truncated=artifact["capture_truncated"],
                projection_truncated=True,
            )
            for key, value in list(result.data.items()):
                if isinstance(value, list) and len(value) > 50:
                    result.data[key] = value[:50]
                    result.data[key + "_total"] = len(value)
            text = result.content
            size = 2000
            while True:
                result.content = (
                    text[:size]
                    + "\n[Preview; use read_artifact: "
                    + artifact["id"]
                    + "]\n"
                    + text[-size:]
                )
                if len(json.dumps(result.to_dict(), ensure_ascii=False).encode()) <= PREVIEW_BYTES:
                    break
                size //= 2
                if size < 1:
                    raise ValueError("Essential tool metadata exceeds the result budget")
        else:
            result.data.setdefault("capture_truncated", False)
            result.data.setdefault("projection_truncated", False)
        self.trace.record("tool_finished", tool=name, result=result.to_dict())
        return result

    def admit(self, name, arguments):
        if name not in TOOLS:
            return ToolResult("rejected", "Unknown tool", error="unknown_tool")
        if name not in self.available_tools:
            return ToolResult(
                "rejected", "Tool is unavailable under the current mode, role or whitelist", error="permission_denied"
            )
        target = None
        try:
            args = TOOLS[name][0].model_validate(arguments).model_dump()
            target = self.resolve(args["path"]) if "path" in args else None
            mutating = name in {"edit_file", "write_file", "run_shell"}
            if target is not None and name != "write_file" and not target.exists():
                return ToolResult(
                    "error",
                    "File does not exist",
                    error="missing_path",
                    data={"path": target.relative_to(self.root).as_posix(), "revision": "absent"},
                )
            if name == "list_files" and not target.is_dir():
                raise ToolError("not_directory", "List target is not a directory")
            if name in {"read_file", "edit_file"} and not target.is_file():
                raise ToolError("not_file", "Target is not an existing file")
            if name == "write_file" and target.exists() and not target.is_file():
                raise ToolError("not_file", "Existing target is not a file")
            if name in {"edit_file", "write_file"}:
                logical = target.relative_to(self.root).as_posix()
                if (
                    self.config.allowed_write_paths is not None
                    and logical not in self.config.allowed_write_paths
                ):
                    raise ToolError("write_scope_denied", "File is outside the allowed write scope")
            if name == "run_shell" and self.config.allowed_write_paths is not None:
                raise ToolError(
                    "shell_scope_denied",
                    "A general shell cannot enforce a restricted file write scope",
                )
            approval_key = {
                "tool": name,
                "arguments": {
                    **args,
                    **({"path": target.relative_to(self.root).as_posix()} if target else {}),
                },
            }
            if mutating and approval_key in self.denied:
                return ToolResult(
                    "rejected",
                    "This operation was already denied in the current task. "
                    "Only an explicit user decision can reopen approval.",
                    error="approval_denied",
                    data={"path": target.relative_to(self.root).as_posix()} if target else {},
                )
            if mutating and self.config.mode != "auto":
                if self.approve is None or not self.approve(name, {**args, "resolved_target": str(target) if target else str(self.root), "operation": name}):
                    self.denied.append(approval_key)
                    if self.save_denial is not None:
                        self.save_denial()
                    return ToolResult(
                        "rejected",
                        "Operation was not approved",
                        error="approval_denied",
                        data={"path": target.relative_to(self.root).as_posix()} if target else {},
                    )
                if target is not None and self.resolve(args["path"]) != target:
                    raise ToolError(
                        "target_changed",
                        "Target changed during approval; propose the operation again",
                    )
            self.budget.check()
        except ToolError as exc:
            return ToolResult(
                "rejected",
                str(exc),
                error=exc.code,
                data={
                    **exc.data,
                    **({"path": target.relative_to(self.root).as_posix()} if target else {}),
                },
            )
        except ValidationError as exc:
            return ToolResult(
                "rejected",
                "Arguments do not satisfy the tool schema",
                error="invalid_arguments",
                data={"issues": exc.errors(include_input=False, include_url=False)},
            )
        except ValueError as exc:
            return ToolResult("rejected", str(exc), error="invalid_arguments")
        except OSError as exc:
            return ToolResult(
                "error",
                str(exc),
                error=filesystem_failure_code(exc),
                data={"path": target.relative_to(self.root).as_posix()} if target else {},
            )
        return args, target

    def run_admitted(self, name, args, target):
        """Read runners do no Session, Trace or read-cache writes; mutations remain serial."""
        mutating = name in {"edit_file", "write_file", "run_shell"}
        try:
            if name == "read_artifact":
                page_bytes = args["max_bytes"]
                while True:
                    page = self.artifacts.read_page(args["artifact_id"], args["offset"], page_bytes)
                    page["projection_truncated"] = page["has_more"] or page["offset"] > 0
                    result = ToolResult("success", page.pop("content"), data=page)
                    if (
                        len(json.dumps(result.to_dict(), ensure_ascii=False).encode())
                        <= PREVIEW_BYTES
                    ):
                        break
                    page_bytes = max(4, page_bytes // 2)
            elif name == "read_file":
                result = self._read(target, args)
            elif name == "edit_file":
                result = self._edit(target, args)
            elif name == "write_file":
                result = self._write(target, args)
            elif name == "list_files":
                result = self._list(target, args)
            elif name == "search":
                result = self._search(target, args["pattern"])
            elif name == "run_shell":
                result, _files = self.observe_command(
                    args["command"], min(args["timeout"], self.config.tool_seconds)
                )
            elif self.delegate is not None:
                result = self.delegate(args)
            else:
                result = ToolResult(
                    "rejected", "Nested delegation is unavailable", error="permission_denied"
                )
        except ToolError as exc:
            result = ToolResult("error", str(exc), error=exc.code, data=exc.data)
        except ExecutionStopped as exc:
            result = ToolResult("error", str(exc), "unknown" if mutating else "none", str(exc))
        except (OSError, UnicodeError) as exc:
            result = ToolResult(
                "error", str(exc), "unknown" if mutating else "none", filesystem_failure_code(exc)
            )
        except Exception as exc:  # noqa: BLE001 - tool exceptions become explicit results
            result = ToolResult(
                "error", str(exc), "unknown" if mutating else "none", "execution_failed"
            )
        if target is not None:
            result.data.setdefault("path", target.relative_to(self.root).as_posix())
        return result

    def _bytes(self, target):
        self._require_target(target)
        data = bytearray()
        with target.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                self.budget.check()
                data.extend(chunk)
        return bytes(data)

    def _read(self, target, args):
        if args["end"] < args["start"] or args["end"] - args["start"] >= 2000:
            raise ToolError("invalid_range", "Choose a valid range of at most 2000 lines")
        self._require_target(target)
        digest = hashlib.sha256()
        decoder = codecs.getincrementaldecoder("utf-8")()
        output = bytearray()
        line_number, total, line_start, truncated = 1, 0, True, False
        with target.open("rb") as handle:
            while chunk := handle.readline(64 * 1024):
                self.budget.check()
                digest.update(chunk)
                decoder.decode(chunk)
                total = line_number
                if args["start"] <= line_number <= args["end"]:
                    piece = (f"{line_number}: ".encode() if line_start else b"") + chunk
                    remaining = CAPTURE_BYTES - len(output)
                    output.extend(piece[:remaining])
                    truncated |= len(piece) > remaining
                line_start = chunk.endswith(b"\n")
                if line_start:
                    line_number += 1
        decoder.decode(b"", final=True)
        return ToolResult("success", output.decode("utf-8", errors="replace").rstrip("\n"), data={
            "path": target.relative_to(self.root).as_posix(),
            "revision": "sha256:" + digest.hexdigest(),
            "start": args["start"], "end": min(args["end"], total),
            "total_lines": total, "capture_truncated": truncated,
        })

    def _edit(self, target, args):
        logical = target.relative_to(self.root).as_posix()
        expected = self.read_versions.get(logical)
        if expected is None:
            raise ToolError("read_required", "Read the file before editing")
        raw = self._bytes(target)
        actual = content_revision(raw)
        if actual != expected:
            raise ToolError(
                "revision_conflict",
                "File changed since read; read it again",
                data={"expected_revision": expected, "actual_revision": actual},
            )
        text = raw.decode("utf-8")
        old = args["old_text"].replace("\r\n", "\n")
        new = args["new_text"].replace("\r\n", "\n")
        pattern = re.compile(r"\r?\n".join(re.escape(line) for line in old.split("\n")))
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            locations = [text.count("\n", 0, match.start()) + 1 for match in matches[:10]]
            # Suggestions only: replacement still requires one exact match on the next call.
            if locations:
                start = max(1, locations[0] - 3)
            else:
                needle = next((line.strip() for line in old.splitlines() if line.strip()), "")
                start = 1
                best = 0.0
                for number, line in enumerate(text.splitlines(), 1):
                    self.budget.check()
                    score = difflib.SequenceMatcher(None, needle[:200], line.strip()[:200]).ratio()
                    if score > best:
                        best, start = score, max(1, number - 3)
            return ToolResult(
                "rejected",
                f"old_text must match once, found {len(matches)}. Read the suggested range "
                "and choose an exact unique block; suggestions are not replacement matches.",
                error="ambiguous_text_match" if matches else "text_not_found",
                data={
                    "path": logical,
                    "match_count": len(matches),
                    "actual_revision": actual,
                    "match_lines": locations,
                    "match_lines_truncated": len(matches) > 10,
                    "read_start": start,
                    "read_end": start + 199,
                },
            )
        if old == new:
            return ToolResult(
                "success", "No content changes", data={"path": logical, "revision": expected}
            )
        match = matches[0]
        ending = re.search(r"\r?\n", text[match.start() :]) or re.search(r"\r?\n", text)
        replacement = new.replace("\n", ending.group() if ending else "\n")
        payload = (text[: match.start()] + replacement + text[match.end() :]).encode("utf-8")
        return self._publish(target, raw, payload, expected)

    def _write(self, target, args):
        logical = target.relative_to(self.root).as_posix()
        if target.exists():
            expected = self.read_versions.get(logical)
            if expected is None:
                raise ToolError("read_required", "Read the existing file before replacing it")
            raw = self._bytes(target)
            actual = content_revision(raw)
            if actual != expected:
                raise ToolError(
                    "revision_conflict",
                    "File changed since read; read it again",
                    data={"expected_revision": expected, "actual_revision": actual},
                )
        else:
            raw, expected = b"", "absent"
        payload = args["content"].encode("utf-8")
        if expected != "absent" and raw == payload:
            return ToolResult(
                "success", "No content changes", data={"path": logical, "revision": expected}
            )
        return self._publish(target, raw, payload, expected)

    def _publish(self, target, before, after, expected):
        mode = target.stat().st_mode & 0o777 if expected != "absent" else 0o644
        logical = target.relative_to(self.root).as_posix()
        revision = content_revision(after)
        preimage = (
            self.artifacts.write_bytes(before, kind="preimage") if expected != "absent" else None
        )
        receipt = {
            "id": uuid.uuid4().hex,
            "operation": self.operation,
            "path": logical,
            "before_revision": expected,
            "after_revision": revision,
            "preimage_id": preimage["id"] if preimage else None,
            "before_mode": mode,
            "status": "prepared",
        }
        if self.save_mutation is not None:
            self.save_mutation(receipt)
        try:
            atomic_write(
                target,
                after,
                create=expected == "absent",
                mode=mode,
                before_replace=lambda: self._require_revision(target, expected),
            )
        except (ToolError, FileExistsError) as exc:
            receipt["status"] = "not_applied"
            if self.save_mutation is not None:
                self.save_mutation(receipt)
            if isinstance(exc, FileExistsError):
                raise ToolError(
                    "revision_conflict", "Another writer created the file; reread before editing"
                ) from exc
            raise
        except BaseException:
            receipt["status"] = "unknown"
            if self.save_mutation is not None:
                self.save_mutation(receipt)
            raise
        receipt["status"] = "applied"
        if self.save_mutation is not None:
            self.save_mutation(receipt)
        diff = "".join(
            difflib.unified_diff(
                before.decode("utf-8").splitlines(True),
                after.decode("utf-8").splitlines(True),
                fromfile=logical if expected != "absent" else "/dev/null",
                tofile=logical,
            )
        )
        descriptor = self.artifacts.write_text(diff, kind="diff")
        receipt["diff_id"] = descriptor["id"]
        if self.save_mutation is not None:
            self.save_mutation(receipt)
        self.read_versions[logical] = revision
        observed = file_revision(target, self.budget)
        data = {
            "path": logical,
            "revision": revision,
            "receipt": dict(receipt),
            "diff_id": descriptor["id"],
        }
        if observed != revision:
            self.read_versions.pop(logical, None)
            return ToolResult(
                "partial_success",
                "File changed again after writing; reread before continuing",
                "unknown",
                "post_write_change",
                data,
            )
        return ToolResult("success", diff or "File written", "changed", data=data)

    def _list(self, target, args):
        entries = sorted(
            path.name + ("/" if path.is_dir() else "")
            for path in target.iterdir()
            if path.name not in IGNORED
        )
        offset = args["offset"]
        if offset > len(entries):
            raise ToolError(
                "invalid_range", "Directory changed or offset is invalid; restart at offset zero"
            )
        end = min(len(entries), offset + args["limit"])
        return ToolResult(
            "success",
            "\n".join(entries[offset:end]),
            data={
                "offset": offset,
                "next_offset": end if end < len(entries) else None,
                "total_entries": len(entries),
                "has_more": end < len(entries),
            },
        )

    def _files(self, target, *, include_links=False):
        if target.is_file():
            yield target
            return

        def fail(error):
            raise error

        for directory, dirs, files in os.walk(target, followlinks=False, onerror=fail):
            self.budget.check()
            if include_links:
                for name in dirs:
                    path = Path(directory) / name
                    if name not in IGNORED and path.is_symlink():
                        yield path
            dirs[:] = [
                name
                for name in dirs
                if name not in IGNORED and not (Path(directory) / name).is_symlink()
            ]
            for name in files:
                path = Path(directory) / name
                if path.is_symlink() and not include_links:
                    continue
                yield path

    def _search(self, target, pattern):
        if shutil.which("rg") is None:
            raise ToolError("search_unavailable", "Install ripgrep (rg) to use search")
        args = ["rg", "--fixed-strings", "--line-number", "--with-filename", "--color=never",
                "--no-heading", "--max-count=201", "--max-columns=1000", "--max-columns-preview"]
        for name in sorted(IGNORED):
            args.extend(["--glob", "!" + name + "/**", "--glob", "!" + name])
        args.extend(["--", pattern, target.relative_to(self.root).as_posix()])
        details = run_command(shlex.join(args), self.root, self.config.tool_seconds, self.budget)
        if details["stop_reason"]:
            raise ToolError(details["stop_reason"], details["stderr"] or "Search interrupted")
        if details["exit_code"] not in (0, 1):
            raise ToolError("search_failed", details["stderr"] or "ripgrep failed")
        lines = details["stdout"].splitlines()
        limited = len(lines) > 200 or details["capture_truncated"]
        content = "\n".join(lines[:200]) or "No matches"
        if limited:
            content += "\n[Search output truncated; narrow path or pattern.]"
        return ToolResult("success", content, data={"capture_truncated": limited})

    def snapshot(self):
        state = {
            path.relative_to(self.root).as_posix(): (
                "symlink:" + os.readlink(path)
                if path.is_symlink()
                else file_revision(path, self.budget)
            )
            for path in self._files(self.root, include_links=True)
        }
        try:
            state.update(git_state(self.root, self.budget))
        except FileNotFoundError:
            pass  # Git is optional; file observations still apply.
        except subprocess.SubprocessError as exc:
            raise OSError("Git observation failed") from exc
        return state

    def observe_command(self, command, timeout):
        """Return the command result and its final file states for completion checks."""
        try:
            before = self.snapshot()
        except (OSError, ExecutionStopped):
            before = None
        details = run_command(
            command, self.root, min(timeout, self.config.tool_seconds), self.budget
        )
        try:
            after = self.snapshot()
        except (OSError, ExecutionStopped):
            after = None
        changes = (
            []
            if before is None or after is None
            else sorted(
                path for path in set(before) | set(after) if before.get(path) != after.get(path)
            )
        )
        effect = (
            "unknown" if before is None or after is None else ("changed" if changes else "none")
        )
        failed = bool(details["stop_reason"] or details["exit_code"] != 0)
        status = (
            "partial_success"
            if effect == "unknown" or (failed and changes)
            else ("error" if failed else "success")
        )
        for path in changes:
            self.read_versions.pop(path, None)
        if effect == "unknown":
            self.read_versions.clear()

        content = f"exit_code: {details['exit_code']}\nstdout:\n{details['stdout']}\nstderr:\n{details['stderr']}"
        result = ToolResult(
            status,
            self.trace.redact(content),
            effect,
            details["stop_reason"]
            or (
                "command_failed"
                if failed
                else ("observation_unknown" if effect == "unknown" else "")
            ),
            {
                "command": command,
                "exit_code": details["exit_code"],
                "stop_reason": details["stop_reason"],
                "capture_truncated": details["capture_truncated"],
                "capture": details["capture"],
                "changed_paths": [path for path in changes if not path.startswith(".git/")],
                "git_changed": any(path.startswith(".git/") for path in changes),
            },
        )
        return result, after
