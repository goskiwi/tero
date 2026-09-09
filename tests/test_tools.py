from tero.config import Config
from tero.execution import Budget
from tero.storage import Trace
from tero.tool_executor import ToolExecutor


def executor(root, **options):
    return ToolExecutor(
        root,
        Config(mode="auto", **options),
        Budget(10),
        Trace(root / ".tero" / "trace.jsonl", str),
    )


def test_read_edit_preserves_other_bytes(tmp_path):
    target = tmp_path / "source.py"
    target.write_bytes(b"a = 1\r\nb = 2\r\n")
    tools = executor(tmp_path)
    tools.execute("read_file", {"path": "source.py"})
    result = tools.execute(
        "edit_file", {"path": "source.py", "old_text": "a = 1\nb = 2", "new_text": "a = 3\nb = 2"}
    )
    assert result.status == "success"
    assert result.workspace_effect == "changed"
    assert target.read_bytes() == b"a = 3\r\nb = 2\r\n"


def test_external_edit_requires_reread(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("old")
    tools = executor(tmp_path)
    tools.execute("read_file", {"path": "a.txt"})
    target.write_text("user changed")
    result = tools.execute("edit_file", {"path": "a.txt", "old_text": "old", "new_text": "new"})
    assert result.status == "error"
    assert result.workspace_effect == "none"
    assert target.read_text() == "user changed"


def test_write_cannot_bypass_read_requirement(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("user content")
    tools = executor(tmp_path)
    assert (
        tools.execute("write_file", {"path": "a.txt", "content": "replacement"}).status == "error"
    )
    assert target.read_text() == "user content"


def test_new_file_and_noop(tmp_path):
    tools = executor(tmp_path)
    assert (
        tools.execute("write_file", {"path": "a.txt", "content": "a"}).workspace_effect == "changed"
    )
    assert (
        tools.execute(
            "edit_file", {"path": "a.txt", "old_text": "a", "new_text": "a"}
        ).workspace_effect
        == "none"
    )


def test_ambiguous_match_does_not_write(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("same same")
    tools = executor(tmp_path)
    tools.execute("read_file", {"path": "a.txt"})
    result = tools.execute("edit_file", {"path": "a.txt", "old_text": "same", "new_text": "new"})
    assert result.status == "rejected"
    assert result.data["match_lines"] == [1, 1]
    assert target.read_text() == "same same"


def test_path_and_scope_are_checked(tmp_path):
    tools = executor(tmp_path, allowed_write_paths=("allowed.txt",))
    assert tools.execute("write_file", {"path": "other.txt", "content": "no"}).status == "rejected"
    assert tools.execute("read_file", {"path": "../outside.txt"}).status == "rejected"
    assert (
        tools.execute("write_file", {"path": ".tero/state.json", "content": "no"}).status
        == "rejected"
    )
    assert tools.execute("run_shell", {"command": "true"}).status == "rejected"


def test_approval_cannot_redirect_target(tmp_path):
    first, second, alias = (tmp_path / name for name in ("first", "second", "alias"))
    first.write_text("same")
    second.write_text("same")
    alias.symlink_to(first)

    def approve(_name, _args):
        alias.unlink()
        alias.symlink_to(second)
        return True

    tools = ToolExecutor(
        tmp_path, Config(), Budget(10), Trace(tmp_path / ".tero/trace", str), approve
    )
    tools.execute("read_file", {"path": "alias"})
    result = tools.execute("edit_file", {"path": "alias", "old_text": "same", "new_text": "new"})
    assert result.status == "rejected"
    assert first.read_text() == second.read_text() == "same"


def test_command_failure_preserves_partial_effect(tmp_path):
    tools = executor(tmp_path)
    result = tools.execute("run_shell", {"command": "printf changed > changed.txt; exit 1"})
    assert result.status == "partial_success"
    assert result.data["changed_paths"] == ["changed.txt"]


def test_observation_failure_is_unknown(tmp_path, monkeypatch):
    tools = executor(tmp_path)

    def unavailable():
        raise PermissionError("cannot observe workspace")

    monkeypatch.setattr(tools, "snapshot", unavailable)
    result = tools.execute("run_shell", {"command": "true"})
    assert result.workspace_effect == "unknown"
    assert result.status != "success"


def test_large_stdout_does_not_hide_stderr(tmp_path):
    tools = executor(tmp_path)
    result = tools.execute(
        "run_shell", {"command": "yes output | head -c 50000; printf important-error >&2; exit 1"}
    )
    assert "important-error" in result.content
    assert result.data["projection_truncated"]


def test_shell_timeout_returns_result(tmp_path):
    tools = executor(tmp_path, tool_seconds=0.05)
    result = tools.execute("run_shell", {"command": "sleep 5"})
    assert result.status == "error"
    assert result.data["stop_reason"] == "tool_timeout"


def test_missing_edit_match_suggests_read_without_fuzzy_write(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("heading\nvalue = 123\nfooter\n")
    tools = executor(tmp_path)
    tools.execute("read_file", {"path": "a.txt"})
    result = tools.execute(
        "edit_file", {"path": "a.txt", "old_text": "value = 124", "new_text": "value = 125"}
    )
    assert result.error == "text_not_found"
    assert result.recovery["suggested_call"]["arguments"]["path"] == "a.txt"
    assert result.workspace_effect == "none"
    assert target.read_text() == "heading\nvalue = 123\nfooter\n"
