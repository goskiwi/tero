from tero.config import Config
from tero.execution import Budget
from tero.storage import Trace
from tero.tool_executor import ToolExecutor, ToolResult


def executor(root, **kwargs):
    return ToolExecutor(root, Config(**kwargs), Budget(10), Trace(root / ".tero/trace", str))


def test_conflict_returns_versions_and_concrete_read(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("old")
    tool = executor(tmp_path, mode="auto")
    read = tool.execute("read_file", {"path": "a.py"})
    target.write_text("external")
    result = tool.execute("edit_file", {"path": "a.py", "old_text": "old", "new_text": "new"})
    assert result.error == "revision_conflict"
    assert result.data["expected_revision"] == read.data["revision"]
    assert result.data["actual_revision"] != read.data["revision"]
    assert result.recovery["condition"] == "retry_after_change"
    assert result.recovery["suggested_call"] == {
        "name": "read_file",
        "arguments": {"path": "a.py", "start": 1, "end": 200},
    }
    assert target.read_text() == "external"


def test_scope_denial_cannot_be_reported_as_bad_arguments(tmp_path):
    result = executor(tmp_path, mode="auto", allowed_write_paths=("allowed.py",)).execute(
        "write_file", {"path": "other.py", "content": "no"}
    )
    assert result.error == "write_scope_denied"
    assert result.recovery["condition"] == "user_action_required"
    assert result.recovery["suggested_call"] is None
    assert "Shell" in result.recovery["action"]
    assert not (tmp_path / "other.py").exists()


def test_denied_approval_is_not_retry_after_wait(tmp_path):
    result = executor(tmp_path).execute("write_file", {"path": "a.py", "content": "no"})
    assert result.error == "approval_denied"
    assert result.recovery["condition"] == "no_retry"
    assert not (tmp_path / "a.py").exists()


def test_missing_file_recommends_directory_observation(tmp_path):
    result = executor(tmp_path).execute("read_file", {"path": "missing.py"})
    assert result.error == "missing_path"
    assert result.recovery["suggested_call"] == {"name": "list_files", "arguments": {"path": "."}}


def test_invalid_range_is_not_edit_conflict(tmp_path):
    (tmp_path / "a.py").write_text("x")
    result = executor(tmp_path).execute("read_file", {"path": "a.py", "start": 10, "end": 1})
    assert result.error == "invalid_range"
    assert "2000" in result.recovery["requirement"]


def test_unknown_command_effects_require_inspection_and_cancel_stops():
    result = ToolResult("partial_success", "timeout", "unknown", "tool_timeout")
    assert "Do not blindly rerun" in result.recovery["action"]
    assert result.recovery["suggested_call"] is None
    stopped = ToolResult("error", "cancelled", "unknown", "cancelled")
    assert stopped.recovery["condition"] == "user_action_required"
    assert "Stop now" in stopped.recovery["action"]


def test_success_has_no_recovery_and_bad_schema_omits_input(tmp_path):
    assert ToolResult("success", "done").to_dict()["recovery"] is None
    result = executor(tmp_path).execute("read_file", {"path": "a.py", "start": "sensitive-input"})
    assert result.error == "invalid_arguments"
    assert "sensitive-input" not in str(result.to_dict())
