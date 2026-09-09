import json

from tero import Config, Tero
from tero.tool_executor import ToolExecutor, ToolResult


def final(text):
    return [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    ]


def call(name, args, call_id):
    return [
        {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(args)}
    ]


class FakeClient:
    def __init__(self, trace, outputs):
        self.trace = trace
        self.outputs = iter(outputs)

    def request(self, *args, **kwargs):
        return next(self.outputs)


def test_verification_failure_returns_to_model(tmp_path, monkeypatch):
    def verify(self, command, timeout):
        passed = (tmp_path / "fixed.txt").exists()
        result = ToolResult(
            "success" if passed else "error", "passed" if passed else "repair required"
        )
        return result, self.snapshot()

    monkeypatch.setattr(ToolExecutor, "observe_command", verify)
    outputs = [
        final("done too soon"),
        call("write_file", {"path": "fixed.txt", "content": "fixed"}, "c1"),
        final("fixed"),
    ]
    runtime = Tero(
        tmp_path,
        Config(mode="auto", memory_enabled=False, verify_command="verify"),
        client_factory=lambda config, trace: FakeClient(trace, outputs),
    )
    result = runtime.ask("Fix it")
    assert result.status == "completed"
    assert result.verification == "passed"
    assert result.turns == 3
    assert (tmp_path / "fixed.txt").read_text() == "fixed"


def test_turn_limit_closes_without_success_claim(tmp_path):
    outputs = [call("list_files", {"path": "."}, "c1")]
    runtime = Tero(
        tmp_path,
        Config(max_turns=1, memory_enabled=False),
        client_factory=lambda config, trace: FakeClient(trace, outputs),
    )
    result = runtime.ask("Inspect")
    assert result.status == "stopped"
    assert result.stop_reason == "turn_limit"
    assert not runtime.session.pending()


def test_resume_does_not_execute_unconfirmed_write(tmp_path):
    runtime = Tero(
        tmp_path,
        Config(mode="auto", memory_enabled=False, verify_command="true"),
        client_factory=lambda config, trace: FakeClient(
            trace,
            [
                call("read_file", {"path": "never.txt"}, "read1"),
                final("Inspected the interruption"),
            ],
        ),
    )
    runtime.session.user("Old task")
    runtime.session.history.append(
        {
            "kind": "turn",
            "items": call("write_file", {"path": "never.txt", "content": "no"}, "c1"),
            "results": {},
        }
    )
    runtime.store.save(runtime.session)
    result = runtime.ask("Continue")
    assert result.status == "completed"
    assert not (tmp_path / "never.txt").exists()
    assert runtime.session.history[1]["results"]["c1"]["workspace_effect"] == "unknown"
    assert runtime.session.unconfirmed == []


def test_resume_shell_inspects_and_reports_remaining_uncertainty(tmp_path, monkeypatch):
    commands = []

    def verify(self, command, timeout):
        commands.append(command)
        return ToolResult("success", "checks passed"), self.snapshot()

    monkeypatch.setattr(ToolExecutor, "observe_command", verify)
    runtime = Tero(
        tmp_path,
        Config(mode="auto", memory_enabled=False, verify_command="verify"),
        client_factory=lambda config, trace: FakeClient(
            trace,
            [final("done"), call("list_files", {"path": "."}, "inspect"), final("Inspected")],
        ),
    )
    runtime.session.history.append(
        {
            "kind": "turn",
            "items": call("run_shell", {"command": "never replay this"}, "old"),
            "results": {},
        }
    )
    runtime.store.save(runtime.session)
    result = runtime.ask("Continue the local task")
    assert result.status == "completed"
    assert commands == ["verify"]
    assert result.unconfirmed
    assert "Remaining uncertainty" in result.answer
    evidence = result.unconfirmed[0]["observations"][0]
    entry = runtime.session.history[evidence["history_index"]]
    assert entry["results"][evidence["call_id"]]["status"] == "success"
    report_path = next((tmp_path / ".tero/runs").glob("*/report.json"))
    assert json.loads(report_path.read_text())["unconfirmed"] == result.unconfirmed


def test_eight_empty_answers_stop_before_general_turn_limit(tmp_path):
    runtime = Tero(
        tmp_path,
        Config(memory_enabled=False),
        client_factory=lambda config, trace: FakeClient(trace, [final("")] * 8),
    )
    result = runtime.ask("Inspect")
    assert result.stop_reason == "invalid_output_limit"
    assert result.turns == 8


def test_unrelated_reads_do_not_reset_completion_limit(tmp_path, monkeypatch):
    def verify(self, command, timeout):
        return ToolResult("error", "assertion failed", error="command_failed"), self.snapshot()

    monkeypatch.setattr(ToolExecutor, "observe_command", verify)
    outputs = [
        final("done"),
        call("list_files", {"path": "."}, "r1"),
        final("done"),
        call("list_files", {"path": "."}, "r2"),
        final("done"),
    ]
    runtime = Tero(
        tmp_path,
        Config(mode="auto", memory_enabled=False, verify_command="verify"),
        client_factory=lambda config, trace: FakeClient(trace, outputs),
    )
    result = runtime.ask("Repair")
    assert result.stop_reason == "completion_block_limit"
    assert result.turns == 5


def test_repeat_limit_closes_remaining_batch_without_executing_it(tmp_path):
    output = [
        item for i in range(3) for item in call("read_file", {"path": "missing.txt"}, f"r{i}")
    ]
    output += call("write_file", {"path": "never.txt", "content": "no"}, "write")
    runtime = Tero(
        tmp_path,
        Config(mode="auto", memory_enabled=False),
        client_factory=lambda config, trace: FakeClient(trace, [output]),
    )
    result = runtime.ask("Inspect missing file")
    assert result.stop_reason == "repeated_tool_failure"
    assert not (tmp_path / "never.txt").exists()
    assert not runtime.session.pending()
    assert not result.unconfirmed
    assert runtime.session.history[-1]["results"]["write"]["error"] == "loop_stopped"
