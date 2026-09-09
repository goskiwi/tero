import json

import pytest

from tero.completion import check_completion
from tero.config import Config
from tero.execution import Budget
from tero.session import Session, SessionStore
from tero.storage import Trace
from tero.tool_executor import ToolExecutor, ToolResult


def setup(root, *, command="true", mode="auto"):
    session = Session.create(root)
    session.user("Repair code")
    session.run = {"id": "run", "status": "running"}
    session.verification_required = bool(command)
    store = SessionStore(root / ".tero/sessions")
    executor = ToolExecutor(
        root,
        Config(mode=mode, verify_command=command),
        Budget(10),
        Trace(root / ".tero/trace", str),
    )
    return session, executor, store


def test_verified_result_is_bound_to_current_files(tmp_path):
    (tmp_path / "a.py").write_text("value = 1\n")
    session, executor, store = setup(tmp_path)
    result = check_completion(session, executor, store)
    assert result.status == "success"
    assert session.verification["status"] == "passed"
    assert session.verification["files"] == executor.snapshot()
    assert session.verification["command"] == "true"


def test_change_after_verification_requires_new_verification(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("value = 1\n")
    session, executor, store = setup(tmp_path)

    def external_save(event, data):
        if event == "verification_finished":
            target.write_text("value = 2\n")

    executor.trace.display = external_save
    result = check_completion(session, executor, store)
    assert result.status == "error"
    assert result.error == "verification_stale"
    assert session.verification["status"] == "stale"
    assert session.verification["files"] is None
    executor.trace.display = None
    assert check_completion(session, executor, store).status == "success"


def test_verifier_modifications_do_not_count_as_passing(tmp_path):
    session, executor, store = setup(tmp_path, command="printf changed > output.txt")
    result = check_completion(session, executor, store)
    assert result.status == "error"
    assert session.verification["status"] == "failed"


def test_required_verification_cannot_be_removed_on_resume(tmp_path):
    session, _executor, store = setup(tmp_path)
    store.save(session)
    loaded = store.load(session.id, tmp_path)
    executor = ToolExecutor(
        tmp_path, Config(mode="auto"), Budget(10), Trace(tmp_path / ".tero/trace", str)
    )
    result = check_completion(loaded, executor, store)
    assert result.status == "rejected"
    assert result.error == "verification_required"


def test_file_unknown_can_be_resolved_by_reading(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("value = 2\n")
    session, executor, store = setup(tmp_path)
    call = {"name": "edit_file", "call_id": "edit", "arguments": '{"path":"a.py"}'}
    session.observe_result(0, call, ToolResult("error", "interrupted", "unknown").to_dict())
    assert check_completion(session, executor, store).error == "file_observation_required"
    observed = executor.execute("read_file", {"path": "a.py"})
    session.observe_result(1, {"name": "read_file", "call_id": "read"}, observed.to_dict())
    assert session.unconfirmed == []
    assert check_completion(session, executor, store).status == "success"


def test_shell_unknown_allows_inspection_without_erasing_uncertainty(tmp_path):
    session, executor, store = setup(tmp_path)
    session.add_unconfirmed("tool:0:shell", "run_shell")
    result = check_completion(session, executor, store)
    assert result.status == "error"
    assert result.error == "effect_inspection_required"
    observed = executor.execute("list_files", {"path": "."})
    session.observe_result(1, {"name": "list_files", "call_id": "inspect"}, observed.to_dict())
    assert check_completion(session, executor, store).status == "success"
    # Local inspection and passing verification never erase unknown external effects.
    assert session.unconfirmed[0]["observations"] == [{"history_index": 1, "call_id": "inspect"}]
    assert session.verification_required


def test_failed_inspection_is_not_evidence(tmp_path):
    session, executor, store = setup(tmp_path)
    session.add_unconfirmed("tool:0:shell", "run_shell")
    session.observe_result(
        1,
        {"name": "run_shell", "call_id": "inspect"},
        ToolResult("error", "permission denied").to_dict(),
    )
    assert not session.unconfirmed[0]["observations"]
    assert check_completion(session, executor, store).error == "effect_inspection_required"


def test_partial_modification_can_finish_after_repair(tmp_path):
    session, executor, store = setup(tmp_path)
    call = {"name": "run_shell", "call_id": "shell", "arguments": "{}"}
    session.observe_result(
        0, call, ToolResult("partial_success", "failed after a write", "changed").to_dict()
    )
    assert session.verification_required
    assert not session.unconfirmed
    assert check_completion(session, executor, store).status == "success"


def test_interrupted_verifier_requires_review_after_load(tmp_path):
    session, _executor, store = setup(tmp_path)
    session.verification = {
        "status": "running",
        "command": "true",
        "call_id": "verify:run:0",
        "files": None,
    }
    store.save(session)
    loaded = store.load(session.id, tmp_path)
    assert loaded.recover() == 1
    assert loaded.verification["status"] == "interrupted"
    assert loaded.unconfirmed == [
        {"id": "verify:run:0", "tool": "verify", "path": None, "observations": []}
    ]
    assert loaded.recover() == 0


@pytest.mark.parametrize(
    "old_format",
    [
        "pico-session-2",
        "pico-session-3",
        "pico-session-4",
        "pico-session-5",
        "pico-session-6",
        "pico-session-7",
        "tracecode-session-7",
    ],
)
def test_old_session_is_rejected_without_migration(tmp_path, old_format):
    session, _executor, store = setup(tmp_path)
    store.save(session)
    path = store.path(session.id)
    value = json.loads(path.read_text())
    value["format"] = old_format
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="not migrated"):
        store.load(session.id, tmp_path)


def test_missing_final_observation_is_not_passed(tmp_path, monkeypatch):
    session, executor, store = setup(tmp_path)
    original = executor.snapshot
    calls = 0

    def observe():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise PermissionError("cannot reread tested files")
        return original()

    monkeypatch.setattr(executor, "snapshot", observe)
    result = check_completion(session, executor, store)
    assert result.status == "error"
    assert session.verification["status"] != "passed"
    assert session.unconfirmed
