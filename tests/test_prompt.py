import json

import pytest

from tero import Config, Tero
from tero.execution import Budget
from tero.session import SessionStore


def test_prompt_matches_scope_and_enabled_capabilities(tmp_path):
    runtime = Tero(tmp_path, Config(memory_enabled=False, allowed_write_paths=("a.py",)))
    prompt = runtime.instructions()
    assert 'Only these workspace-relative files may be written: ["a.py"]' in prompt
    assert "General Shell is disabled" in prompt
    assert "/bin/sh" in prompt
    assert "Long-term memory is maintained" not in prompt
    assert "Delegate only" in prompt
    child_prompt = runtime.make_child(3).instructions()
    assert "Role: read-only subagent" in child_prompt
    assert "No file writes allowed" in child_prompt
    assert "Delegate only" not in child_prompt
    assert "you own all code edits" not in child_prompt
    readonly = Tero(tmp_path, Config(mode="ask", memory_enabled=False)).instructions()
    assert "Role: main agent" in readonly
    assert "Delegate only" not in readonly


def test_near_limit_notice_does_not_fill_normal_prompts(tmp_path):
    runtime = Tero(tmp_path, Config(memory_enabled=False))
    runtime.budget = Budget(100)
    assert "nearly exhausted" not in runtime.instructions()
    runtime.session.run["turns"] = runtime.config.max_turns - 1
    assert "1 model turns" in runtime.instructions()
    runtime.session.run["turns"] = 0
    runtime.budget = Budget(20)
    assert "nearly exhausted" in runtime.instructions()


@pytest.mark.parametrize("previous", ["not_run", "failed", "passed"])
def test_first_model_request_has_config_and_retains_previous_evidence(tmp_path, previous):
    class Client:
        def __init__(self, trace):
            self.trace = trace

        def request(self, instructions, items, tools, budget):
            record = items[-1]["content"]
            assert '"configured_verifier": "true"' in record
            assert '"verifier_enabled": true' in record
            expected = "stale" if previous == "passed" else previous
            assert f'"status": "{expected}"' in record
            if previous != "not_run":
                assert '"command": "old-command"' in record
            return [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Ready for verification"}
            ]}]

    runtime = Tero(
        tmp_path,
        Config(mode="auto", verify_command="true", memory_enabled=False, repo_map_enabled=False),
        client_factory=lambda config, trace: Client(trace),
    )
    if previous != "not_run":
        runtime.session.verification.update(
            status=previous, command="old-command", call_id="prior",
            files={} if previous == "passed" else None,
        )
    result = runtime.ask("Continue")
    assert result.status == "completed"
    assert result.verification == "passed"


def test_session_has_no_version_tag_and_requires_execution_state(tmp_path):
    runtime = Tero(tmp_path, Config(memory_enabled=False))
    runtime.store.save(runtime.session)
    path = tmp_path / ".tero/sessions" / (runtime.session.id + ".json")
    saved = json.loads(path.read_text())
    assert "format" not in saved
    del saved["verification"]
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="Missing completion state"):
        SessionStore(path.parent).load(runtime.session.id, tmp_path)
