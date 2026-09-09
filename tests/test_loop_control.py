from tero.agent_loop import AgentLoop
from tero.config import Config
from tero.execution import Budget
from tero.runtime import Tero
from tero.session import Session, SessionStore, new_loop_control
from tero.storage import Trace
from tero.tool_executor import ToolExecutor, ToolResult


def executor(root, **options):
    return ToolExecutor(root, Config(), Budget(10), Trace(root / ".tero/trace", str), **options)


def test_denied_operation_is_saved_and_not_asked_again_after_load(tmp_path):
    session = Session.create(tmp_path)
    store = SessionStore(tmp_path / ".tero/sessions")
    asked = []
    tool = executor(
        tmp_path,
        approve=lambda *args: asked.append(args) or False,
        denied=session.loop_control["denied"],
        save_denial=lambda: store.save(session),
    )
    args = {"path": "a.txt", "content": "x"}
    assert tool.execute("write_file", args).error == "approval_denied"
    loaded = store.load(session.id, tmp_path)
    resumed = executor(
        tmp_path,
        approve=lambda *args: asked.append(args) or True,
        denied=loaded.loop_control["denied"],
    )
    assert resumed.execute("write_file", args).error == "approval_denied"
    assert len(asked) == 1 and not (tmp_path / "a.txt").exists()


def test_unrelated_read_does_not_clear_same_missing_file_failure(tmp_path):
    tool = executor(tmp_path)
    control = new_loop_control()
    (tmp_path / "other.txt").write_text("unrelated")
    for number in range(3):
        args = {"path": "missing.txt"}
        outcome = tool.execute("read_file", args)
        stop = AgentLoop._track_failure(control, tool, "read_file", args, outcome)
        unrelated = tool.execute("read_file", {"path": "other.txt"})
        AgentLoop._track_failure(control, tool, "read_file", {"path": "other.txt"}, unrelated)
        assert bool(stop) == (number == 2)


def test_related_read_resolves_read_requirement(tmp_path):
    tool = executor(tmp_path)
    control = new_loop_control()
    args = {"path": "a.txt", "old_text": "x", "new_text": "y"}
    failed = ToolResult("error", "read first", error="read_required", data={"path": "a.txt"})
    AgentLoop._track_failure(control, tool, "edit_file", args, failed)
    (tmp_path / "a.txt").write_text("x")
    observed = tool.execute("read_file", {"path": "a.txt"})
    AgentLoop._track_failure(control, tool, "read_file", {"path": "a.txt"}, observed)
    assert control["last_failure"] is None


def test_command_failures_warn_without_automatic_stop(tmp_path):
    tool = executor(tmp_path)
    control = new_loop_control()
    for _ in range(4):
        outcome = ToolResult("error", "exit 1", error="command_failed")
        assert (
            AgentLoop._track_failure(control, tool, "run_shell", {"command": "test"}, outcome) == ""
        )
    assert "Repeated failure" in outcome.content


def test_irrelevant_tool_success_does_not_clear_completion_refusals(tmp_path):
    tool = executor(tmp_path)
    control = new_loop_control()
    control.update(
        completion_blocks=2, completion_reason="verification_stale", completion_paths=["a.txt"]
    )
    AgentLoop._track_failure(
        control,
        tool,
        "read_file",
        {"path": "other.txt"},
        ToolResult("success", "ok", data={"path": "other.txt", "revision": "v1"}),
    )
    assert control["completion_blocks"] == 2
    AgentLoop._track_failure(
        control,
        tool,
        "edit_file",
        {"path": "a.txt"},
        ToolResult("success", "changed", "changed", data={"path": "a.txt"}),
    )
    assert control["completion_blocks"] == 0


def test_user_reopens_denial_without_approving_execution(tmp_path):
    runtime = Tero(tmp_path, Config(memory_enabled=False))
    runtime.session.loop_control["denied"] = [
        {"tool": "write_file", "arguments": {"path": "a.txt", "content": "x"}}
    ]
    runtime.retry_denied()
    assert runtime.session.loop_control["denied"] == []
    assert not (tmp_path / "a.txt").exists()


def test_successful_same_command_ends_previous_failure_streak(tmp_path):
    tool = executor(tmp_path)
    control = new_loop_control()
    arguments = {"command": "check"}
    AgentLoop._track_failure(
        control, tool, "run_shell", arguments, ToolResult("error", "failed", error="command_failed")
    )
    AgentLoop._track_failure(control, tool, "run_shell", arguments, ToolResult("success", "passed"))
    assert control["last_failure"] is None
    result = ToolResult("error", "new failure", error="command_failed")
    AgentLoop._track_failure(control, tool, "run_shell", arguments, result)
    assert control["last_failure"]["count"] == 1
    assert "Repeated failure" not in result.content
