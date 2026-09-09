"""Ten deterministic fault cases using real processes/files and fresh Runtime recovery."""

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from tero import Config, Tero
from tero.artifacts import ArtifactStore
from tero.execution import Budget
from tero.session import Session, SessionStore
from tero.storage import Trace, save_json
from tero.tool_executor import ToolExecutor


def call(name, args, identity):
    return [
        {"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": identity}
    ]


def final():
    return [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Verified current state."}],
        }
    ]


class Scripted:
    def __init__(self, trace, outputs):
        self.trace = trace
        self.outputs = iter(outputs)

    def request(self, *args, **kwargs):
        return next(self.outputs)


def config():
    return Config(
        mode="auto",
        memory_enabled=False,
        repo_map_enabled=False,
        verify_command=f"{shlex.quote(sys.executable)} checks.py",
    )


def crash_child(root, stage):
    import tero.agent_loop as loop
    import tero.tool_executor as tools

    outputs = [
        call("read_file", {"path": "subject.py"}, "read"),
        call(
            "edit_file",
            {"path": "subject.py", "old_text": "VALUE = 'before'", "new_text": "VALUE = 'after'"},
            "change",
        ),
        final(),
    ]
    if stage == "pending":
        original = loop.execute_batch

        def execute(executor, calls, *args):
            if any(c["name"] == "edit_file" for c in calls):
                os._exit(23)
            yield from original(executor, calls, *args)

        loop.execute_batch = execute
    elif stage == "running":
        original = tools.ToolExecutor.run_admitted

        def admitted(self, name, *args):
            if name == "edit_file":
                os._exit(23)
            return original(self, name, *args)

        tools.ToolExecutor.run_admitted = admitted
    else:
        original = tools.atomic_write

        def write(path, payload, **kwargs):
            original(path, payload, **kwargs)
            if path == root / "subject.py":
                os._exit(23)

        tools.atomic_write = write
    runtime = Tero(root, config(), workspace_root=root, client_factory=lambda c, t: Scripted(t, outputs))
    runtime.ask("Change VALUE to after, then verify.")
    raise RuntimeError("The selected crash point was not reached")


def recover_case(root, stage, external=False):
    (root / "subject.py").write_text("VALUE = 'before'\n")
    (root / "checks.py").write_text("from subject import VALUE\nassert VALUE == 'after'\n")
    process = subprocess.run(
        [sys.executable, "-m", "evaluations.module_recovery", "--crash-child", str(root), stage],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 23:
        raise RuntimeError("Fault injection failed: " + process.stderr[-1000:])
    path = next((root / ".tero/sessions").glob("*.json"))
    persisted = json.loads(path.read_text())
    if external:
        (root / "subject.py").write_text("VALUE = 'external'\n")
    before = (root / "subject.py").read_text()
    outputs = [call("read_file", {"path": "subject.py"}, "fresh")]
    if stage in {"pending", "running"}:
        outputs.append(
            call(
                "edit_file",
                {
                    "path": "subject.py",
                    "old_text": "VALUE = 'before'",
                    "new_text": "VALUE = 'after'",
                },
                "new-change",
            )
        )
    outputs.append(final())
    runtime = Tero(
        root,
        config(), workspace_root=root, session_id=persisted["id"], client_factory=lambda c, t: Scripted(t, outputs)
    )
    runtime.session.recover()
    runtime.store.save(runtime.session)
    recovered = runtime.session.history[-1]["results"]["change"]
    expected = (
        "unknown" if external or stage == "running" else "none" if stage == "pending" else "changed"
    )
    checks = {
        "child_really_exited": process.returncode == 23,
        "correct_recovered_effect": recovered["workspace_effect"] == expected,
        "recovery_did_not_write": (root / "subject.py").read_text() == before,
    }
    resumed = None
    if not external:
        resumed = runtime.ask(
            "Continue from current state and verify; do not replay prior calls."
        ).__dict__
        checks["resumed_verified"] = (
            resumed["status"] == "completed" and resumed["verification"] == "passed"
        )
        checks["single_mutation_receipt"] = len(runtime.session.mutations) == 1
    else:
        checks["external_edit_preserved"] = (
            root / "subject.py"
        ).read_text() == "VALUE = 'external'\n"
    return {
        "checks": checks,
        "automatic_completion": bool(resumed and resumed["status"] == "completed"),
        "expected_safe_stop": external,
        "persisted_phase": persisted["history"][-1]["phases"]["change"],
        "recovered_result": recovered,
        "resumed_result": resumed,
    }


def executor(root, budget=None, **options):
    return ToolExecutor(
        root,
        Config(mode="auto"),
        budget or Budget(10),
        Trace(root / ".tero/tool-trace.jsonl", str),
        **options,
    )


def alive(pid):
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


def process_case(root, cancel=False):
    (root / "sleeper.py").write_text(
        "import os,time\nfrom pathlib import Path\nPath('pid.txt').write_text(str(os.getpid()))\ntime.sleep(20)\n"
    )
    budget = Budget(10)
    tool = executor(root, budget)
    results = []
    errors = []

    def run():
        try:
            results.append(
                tool.execute(
                    "run_shell",
                    {
                        "command": f"{shlex.quote(sys.executable)} sleeper.py",
                        "timeout": 5 if cancel else 1,
                    },
                ).to_dict()
            )
        except BaseException as exc:  # noqa: BLE001 - retain cross-thread outcome
            errors.append(str(exc))

    if cancel:
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 3
        while not (root / "pid.txt").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        budget.cancelled.set()
        thread.join(3)
        ended = not thread.is_alive()
    else:
        run()
        ended = True
    pid = int((root / "pid.txt").read_text())
    return {
        "checks": {
            "call_ended": ended,
            "owned_process_stopped": not alive(pid),
            "correct_stop_reason": bool(results)
            and results[0]["error"] == ("cancelled" if cancel else "tool_timeout"),
        },
        "result": results,
        "errors": errors,
        "automatic_completion": False,
    }


def denial_case(root):
    approvals = []
    outputs = [
        call("write_file", {"path": "denied.txt", "content": "no"}, f"c{i}") for i in range(3)
    ]
    runtime = Tero(
        root,
        Config(mode="code", memory_enabled=False, repo_map_enabled=False),
        workspace_root=root,
        approve=lambda *args: approvals.append(1) or False,
        client_factory=lambda c, t: Scripted(t, outputs),
    )
    result = runtime.ask("Create the requested file if approved.")
    return {
        "checks": {
            "single_approval_request": len(approvals) == 1,
            "no_file_written": not (root / "denied.txt").exists(),
            "repeat_limit_stopped": result.stop_reason == "repeated_tool_failure",
        },
        "result": result.__dict__,
        "automatic_completion": False,
    }


def output_case(root):
    (root / "emit.py").write_text(
        "from pathlib import Path\np=Path('runs.txt')\np.write_text(str(int(p.read_text())+1) if p.exists() else '1')\nprint('头部'*5000+'DIAGNOSTIC-EXPECTED'+'尾部'*5000)\n"
    )
    tool = executor(root)
    result = tool.execute("run_shell", {"command": f"{shlex.quote(sys.executable)} emit.py"})
    offset = 0
    parts = []
    while True:
        page = tool.execute(
            "read_artifact", {"artifact_id": result.data["artifact_id"], "offset": offset}
        )
        if page.status != "success":
            raise RuntimeError(page.content)
        parts.append(page.content)
        if page.data["next_offset"] is None:
            break
        offset = page.data["next_offset"]
    retained = json.loads("".join(parts))
    return {
        "checks": {
            "preview_is_bounded": result.data["projection_truncated"],
            "saved_output_retrieved": "DIAGNOSTIC-EXPECTED" in retained["content"],
            "no_command_reexecution": (root / "runs.txt").read_text() == "1",
        },
        "automatic_completion": False,
    }


def schema_case(root):
    session = Session.create(root)
    store = SessionStore(root / ".tero/sessions")
    store.save(session)
    path = store.path(session.id)
    value = json.loads(path.read_text())
    del value["verification"]
    path.write_text(json.dumps(value))
    rejected = False
    try:
        Tero(root, config(), workspace_root=root, session_id=session.id)
    except ValueError:
        rejected = True
    return {"checks": {"missing_execution_state_rejected": rejected}, "automatic_completion": False}


def artifact_scope_case(root):
    first = ArtifactStore(root / ".tero/artifacts/first", str)
    second = ArtifactStore(root / ".tero/artifacts/second", str)
    saved = first.write_text("session-private-content")
    result = executor(root, artifacts=second).execute("read_artifact", {"artifact_id": saved["id"]})
    return {
        "checks": {
            "other_session_not_read": result.status != "success"
            and "session-private-content" not in result.content
        },
        "automatic_completion": False,
    }


CASES = [
    ("pending_before_execution", lambda r: recover_case(r, "pending")),
    ("running_before_mutation", lambda r: recover_case(r, "running")),
    ("replace_before_result", lambda r: recover_case(r, "after_replace")),
    ("external_edit_on_recovery", lambda r: recover_case(r, "after_replace", True)),
    ("shell_timeout", lambda r: process_case(r)),
    ("user_cancel_process", lambda r: process_case(r, True)),
    ("approval_repeated_denial", denial_case),
    ("large_result_paging", output_case),
    ("incompatible_session", schema_case),
    ("artifact_session_scope", artifact_scope_case),
]


def run_suite(output):
    if (output / "rows.json").exists():
        raise ValueError(
            "Choose a fresh output directory; existing measurements are not overwritten"
        )
    rows = []
    for name, case in CASES:
        root = (output / name / "workspace").resolve()
        root.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            row = case(root)
            row.update(case=name, passed=all(row["checks"].values()), error=None)
        except Exception as exc:  # noqa: BLE001 - preserve failed scenario
            row = {"case": name, "passed": False, "error": str(exc), "checks": {}}
        row["seconds"] = time.monotonic() - started
        save_json(output / name / "result.json", row)
        rows.append(row)
        save_json(output / "rows.json", rows)
        print(
            json.dumps(
                {"case": name, "passed": row["passed"], "error": row["error"]}, ensure_ascii=False
            ),
            flush=True,
        )
    return rows


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--crash-child":
        crash_child(Path(sys.argv[2]).resolve(), sys.argv[3])
    else:
        raise SystemExit("Use evaluations.module_benchmarks --suite recovery")
