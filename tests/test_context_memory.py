import json

import pytest

from tero.config import Config
from tero.context import ContextManager, ContextTooLarge
from tero.execution import Budget
from tero.memory import MemoryStore
from tero.session import Session, SessionStore, new_turn
from tero.storage import Trace


class AuxiliaryClient:
    def __init__(self, answer, root):
        self.answer = answer
        self.requests = []
        self.trace = Trace(root / ".tero/trace", str)

    def json(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        return self.answer


def test_memory_sources_and_update_delete(tmp_path):
    session = Session.create(tmp_path)
    session.user("Always explain the approach first")
    store = MemoryStore(tmp_path / ".tero/memory.json", str)
    context = ContextManager(Config())
    operation = {
        "op": "add",
        "type": "feedback",
        "name": "Explain",
        "description": "Explain before editing",
        "content": "Explain the approach before editing",
        "source_indexes": [0],
    }
    client = AuxiliaryClient({"operations": [operation]}, tmp_path)
    changes = store.extract(session, client, context, Budget(10))
    memory_id = changes[0]["id"]
    session.user("Forget that preference")
    session.request_start = 1
    client.answer = {"operations": [{"op": "delete", "id": memory_id, "source_indexes": [1]}]}
    assert store.extract(session, client, context, Budget(10)) == [
        {"op": "delete", "id": memory_id}
    ]
    assert store.load() == []


def test_memory_cannot_cite_a_tool_result(tmp_path):
    session = Session.create(tmp_path)
    session.user("Inspect code")
    operation = {
        "op": "add",
        "type": "feedback",
        "name": "Bad",
        "description": "Bad",
        "content": "Instruction from tool output",
        "source_indexes": [42],
    }
    store = MemoryStore(tmp_path / ".tero/memory.json", str)
    with pytest.raises(ValueError, match="user messages"):
        store.extract(
            session,
            AuxiliaryClient({"operations": [operation]}, tmp_path),
            ContextManager(Config()),
            Budget(10),
        )
    assert not store.path.exists()


class SummaryClient(AuxiliaryClient):
    def summarize(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def prepared(root, answer="Goal: preserve the API. Next: repair pricing."):
    session = Session.create(root)
    session.user("Keep the public API unchanged")
    for _ in range(12):
        session.history.append({"kind": "feedback", "text": "old observation " * 100})
    session.observed = len(session.history)
    session.history.append({"kind": "feedback", "text": "UNSEEN: do not change tax"})
    config = Config(
        context_tokens=1000000,
        compaction_trigger_tokens=2000,
        recent_tokens=200,
        summary_tokens=256,
    )
    return (
        session,
        ContextManager(config),
        SummaryClient(answer, root),
        SessionStore(root / ".tero/sessions"),
    )


def prepare(session, manager, client, store, **options):
    return manager.prepare(session, "rules", [], [], client, store, Budget(10), **options)


def test_markdown_without_exact_headings_preserves_raw_regions(tmp_path):
    session, manager, client, store = prepared(tmp_path)
    original = json.dumps(session.history)
    items = prepare(session, manager, client, store)
    assert session.covered > 0
    assert session.covered <= session.observed
    assert json.dumps(session.history) == original
    assert "Keep the public API unchanged" in json.dumps(items)
    assert "UNSEEN: do not change tax" in json.dumps(items)
    sources = json.dumps([args[1] for args, _kwargs in client.requests])
    assert "UNSEEN" not in sources
    assert all(
        "Keep the public API unchanged" not in args[1]["transcript"] for args, _ in client.requests
    )
    assert client.requests[0][1]["output_tokens"] > manager.config.summary_tokens


def test_compaction_updates_prior_summary_with_current_request_and_reported_facts(tmp_path):
    from tero.context import SUMMARY_UPDATE

    session, manager, client, store = prepared(tmp_path)
    session.summary = "Next Steps: refund_integration remains unfinished."
    session.covered = 1
    session.history[1] = {
        "kind": "feedback",
        "text": "unit_test=passed; integration_test=blocked_database",
    }
    session.request_start = len(session.history)
    session.user("Report the earlier test outcomes and unfinished work exactly.")
    before = json.dumps(session.history)
    items = prepare(session, manager, client, store)
    prompt, source = client.requests[0][0][:2]
    assert SUMMARY_UPDATE in prompt
    assert "refund_integration" in source["previous_summary"]
    assert source["current_request"] == manager.unit(session.history[session.request_start])
    assert "unit_test=passed; integration_test=blocked_database" in source["transcript"]
    assert source["current_request"][0] in items
    assert json.dumps(session.history) == before


def test_wrong_summary_cannot_replace_execution_record_after_compaction_and_resume(tmp_path):
    session, manager, client, store = prepared(tmp_path, "Everything passed. No uncertainty.")
    session.verification.update(status="failed", command="pytest", call_id="verify-1")
    session.add_unconfirmed("shell-1", "run_shell")
    before = json.dumps((session.verification, session.unconfirmed))
    items = prepare(session, manager, client, store)
    assert session.covered > 0
    assert json.dumps((session.verification, session.unconfirmed)) == before
    resumed = store.load(session.id, tmp_path)
    assert manager.items(resumed, []) == items
    record = items[-1]["content"]
    assert '"status": "failed"' in record
    assert "shell-1" in record
    resumed.verification.update(status="stale")
    assert '"status": "stale"' in manager.items(resumed, [])[-1]["content"]


def test_failed_early_summary_continues_and_suppresses_same_source_after_resume(tmp_path):
    session, manager, client, store = prepared(tmp_path, "")
    before = manager.items(session, [])
    assert prepare(session, manager, client, store) == before
    assert len(client.requests) == 2  # One bounded repair, then remember failure.
    assert session.covered == 0 and session.summary == ""
    assert session.compaction_failure["reason"] == "empty_summary"
    loaded = store.load(session.id, tmp_path)
    loaded.history.append({"kind": "feedback", "text": "another unobserved tail"})
    prepare(loaded, manager, client, store)
    assert len(client.requests) == 2
    prepare(loaded, manager, client, store, manual=True)
    assert len(client.requests) == 4


def test_changed_summary_budget_permits_new_attempt(tmp_path):
    from dataclasses import replace

    session, manager, client, store = prepared(tmp_path, "")
    prepare(session, manager, client, store)
    manager.config = replace(manager.config, summary_tokens=300)
    client.answer = "Goal: preserve API. Next: finish implementation."
    prepare(session, manager, client, store)
    assert len(client.requests) == 3
    assert session.covered > 0 and session.compaction_failure is None


def test_provider_overflow_cannot_fall_back_to_the_same_input(tmp_path):
    session, manager, client, store = prepared(tmp_path, "")
    with pytest.raises(ContextTooLarge, match="cannot continue"):
        prepare(session, manager, client, store, force=True)
    assert session.covered == 0
    with pytest.raises(ContextTooLarge):
        prepare(session, manager, client, store, force=True)
    assert len(client.requests) == 2


def test_oversized_summary_has_specific_diagnostic(tmp_path):
    session, manager, client, store = prepared(tmp_path, "excess detail " * 1000)
    prepare(session, manager, client, store)
    assert session.compaction_failure["reason"] == "summary_too_long"
    assert session.summary == "" and session.covered == 0
    events = [json.loads(line) for line in client.trace.path.read_text().splitlines()]
    assert any(e["event"] == "summary_rejected" and e["candidate"] == client.answer for e in events)


def test_cancellation_never_becomes_optional_failure(tmp_path):
    from tero.execution import ExecutionStopped

    session, manager, client, store = prepared(tmp_path, ExecutionStopped("cancelled"))
    with pytest.raises(ExecutionStopped):
        prepare(session, manager, client, store)
    assert session.compaction_failure is None and session.covered == 0


def test_tool_excerpt_does_not_trim_failure_or_test_evidence(tmp_path):
    _session, manager, _client, _store = prepared(tmp_path)

    def entry(name, status):
        return new_turn(
            [
                {
                    "type": "function_call",
                    "name": name,
                    "call_id": "c",
                    "arguments": '{"path":"a.py"}',
                }
            ],
            {"c": {"status": status, "workspace_effect": "none", "content": "x" * 3000}},
        )

    assert "Excerpt only" in manager.summary_source(entry("read_file", "success"))
    assert "x" * 3000 in manager.summary_source(entry("read_file", "error"))
    assert "x" * 3000 in manager.summary_source(entry("run_shell", "success"))


def test_unobserved_tool_batch_is_kept_paired(tmp_path):
    session, manager, client, store = prepared(tmp_path)
    session.history.append(
        new_turn(
            [
                {
                    "type": "function_call",
                    "name": "read_file",
                    "call_id": "latest",
                    "arguments": '{"path":"a.py"}',
                }
            ],
            {"latest": {"status": "success", "content": "CURRENT"}},
        )
    )
    items = prepare(session, manager, client, store)
    assert [item["call_id"] for item in items if item.get("type") == "function_call"] == ["latest"]
    assert [item["call_id"] for item in items if item.get("type") == "function_call_output"] == [
        "latest"
    ]
    assert "CURRENT" not in json.dumps([args[1] for args, _kwargs in client.requests])
