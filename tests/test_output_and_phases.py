import json
import threading

import pytest

from tero.artifacts import PREVIEW_BYTES, ArtifactStore
from tero.changes import build_task_diff
from tero.config import Config
from tero.execution import Budget
from tero.session import Session, SessionStore, new_turn
from tero.storage import Trace
from tero.tool_batch import execute_batch
from tero.tool_executor import ToolExecutor


def tool(root, session=None):
    session = session or Session.create(root)
    artifacts = ArtifactStore(root / ".tero/artifacts" / session.id, str)

    def receipt(value):
        session.mutations[:] = [r for r in session.mutations if r["id"] != value["id"]] + [
            dict(value)
        ]

    return ToolExecutor(
        root,
        Config(mode="auto"),
        Budget(10),
        Trace(root / ".tero/trace", str),
        artifacts=artifacts,
        save_mutation=receipt,
    ), session


def call(name, args, identity):
    return {
        "type": "function_call",
        "name": name,
        "arguments": json.dumps(args),
        "call_id": identity,
    }


def test_large_read_can_be_paged_without_rereading_source(tmp_path):
    text = "汉字 and code " * 6000
    source = tmp_path / "a.py"
    source.write_text(text)
    executor, _ = tool(tmp_path)
    result = executor.execute("read_file", {"path": "a.py"})
    assert result.data["projection_truncated"] and not result.data["capture_truncated"]
    assert len(json.dumps(result.to_dict(), ensure_ascii=False).encode()) <= PREVIEW_BYTES
    source.unlink()
    offset, chunks = 0, []
    while True:
        page = executor.execute(
            "read_artifact", {"artifact_id": result.data["artifact_id"], "offset": offset}
        )
        assert page.status == "success"
        chunks.append(page.content)
        if page.data["next_offset"] is None:
            break
        offset = page.data["next_offset"]
    assert text in json.loads("".join(chunks))["content"]


def test_directory_pages_do_not_lose_entries(tmp_path):
    for index in range(230):
        (tmp_path / f"f{index:03d}").touch()
    executor, _ = tool(tmp_path)
    first = executor.execute("list_files", {"path": "."})
    second = executor.execute("list_files", {"path": ".", "offset": first.data["next_offset"]})
    assert len(first.content.splitlines()) == 200
    assert len(second.content.splitlines()) == 30
    assert second.data["next_offset"] is None


def test_preimage_and_net_diff_preserve_uncommitted_start(tmp_path):
    source = tmp_path / "a.py"
    source.write_bytes(b"user uncommitted\r\n")
    executor, session = tool(tmp_path)
    executor.execute("read_file", {"path": "a.py"})
    result = executor.execute(
        "edit_file", {"path": "a.py", "old_text": "user uncommitted", "new_text": "fixed"}
    )
    receipt = result.data["receipt"]
    assert (
        executor.artifacts.read_bytes(receipt["preimage_id"], kind="preimage")
        == b"user uncommitted\r\n"
    )
    assert (
        executor.execute("read_artifact", {"artifact_id": receipt["preimage_id"]}).status
        != "success"
    )
    source.write_text("external later\n")
    diff = build_task_diff(session, executor.artifacts, Budget(10))
    assert diff["external_or_uncertain_paths"] == ["a.py"]
    assert source.read_text() == "external later\n"  # No automatic rollback.


def test_pending_and_running_recovery_are_distinct(tmp_path):
    session = Session.create(tmp_path)
    entry = new_turn(
        [
            call("write_file", {"path": "a.py", "content": "x"}, "a"),
            call("run_shell", {"command": "unknown"}, "b"),
        ]
    )
    entry["phases"]["b"] = "running"
    session.history.append(entry)
    store = SessionStore(tmp_path / ".tero/sessions")
    store.save(session)
    recovered = store.load(session.id, tmp_path)
    recovered.recover()
    a, b = recovered.history[0]["results"].values()
    assert a["error"] == "not_started" and a["workspace_effect"] == "none"
    assert b["error"] == "interrupted" and b["workspace_effect"] == "unknown"
    assert not (tmp_path / "a.py").exists()


def test_after_write_receipt_recovers_observation_without_replay(tmp_path):
    source = tmp_path / "a.py"
    source.write_text("before")
    executor, session = tool(tmp_path)
    session.history.append(new_turn([call("edit_file", {}, "edit")]))
    session.history[0]["phases"]["edit"] = "running"
    executor.operation = {"entry": 0, "call_id": "edit"}
    executor.execute("read_file", {"path": "a.py"})
    executor.execute("edit_file", {"path": "a.py", "old_text": "before", "new_text": "after"})
    session.recover()
    assert session.history[0]["results"]["edit"]["workspace_effect"] == "changed"
    assert session.verification_required
    assert source.read_text() == "after"


def test_parallel_reads_and_serial_write_preserve_order(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("old")
    (tmp_path / "b.py").write_text("other")
    executor, _ = tool(tmp_path)
    from dataclasses import replace

    executor.config = replace(executor.config, max_parallel_tools=4)
    barrier = threading.Barrier(2)
    original = executor._read
    main = threading.get_ident()
    finished = []

    def read(target, args):
        if threading.get_ident() != main:
            barrier.wait(timeout=2)
        return original(target, args)

    monkeypatch.setattr(executor, "_read", read)

    class MainCache(dict):
        def __setitem__(self, key, value):
            assert threading.get_ident() == main
            super().__setitem__(key, value)

    executor.read_versions = MainCache()
    calls = [
        call("read_file", {"path": "a.py"}, "a"),
        call("read_file", {"path": "b.py"}, "b"),
        call("edit_file", {"path": "a.py", "old_text": "old", "new_text": "new"}, "edit"),
        call("read_file", {"path": "a.py"}, "after"),
    ]
    results = list(
        execute_batch(
            executor,
            calls,
            lambda c: finished.append((c["call_id"], threading.get_ident())),
            lambda: False,
        )
    )
    assert [c["call_id"] for c, _ in results] == ["a", "b", "edit", "after"]
    assert all(tid == main for _, tid in finished)
    assert "old" in results[0][1].content and "new" in results[-1][1].content


def test_shell_capture_and_preview_limits_are_separate(tmp_path, monkeypatch):
    import shlex
    import sys

    from tero import commands

    monkeypatch.setattr(commands, "CAPTURE_BYTES", 128)
    executor, _ = tool(tmp_path)
    command = shlex.quote(sys.executable) + " -c \"print('a'*2000); print('TAIL')\""
    result = executor.execute("run_shell", {"command": command})
    assert result.data["capture_truncated"] and result.data["projection_truncated"]
    assert result.data["capture"]["stdout"]["retained_bytes"] <= 128
    page = executor.execute("read_artifact", {"artifact_id": result.data["artifact_id"]})
    assert "TAIL" in page.content
    assert page.data["capture_truncated"]
    assert page.data["id"] == result.data["artifact_id"]  # No recursive artifact of a page.


def test_preimage_failure_prevents_modification(tmp_path, monkeypatch):
    source = tmp_path / "a.py"
    source.write_text("before")
    executor, _ = tool(tmp_path)
    executor.execute("read_file", {"path": "a.py"})

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(executor.artifacts, "write_bytes", fail)
    result = executor.execute(
        "edit_file", {"path": "a.py", "old_text": "before", "new_text": "after"}
    )
    assert result.status != "success"
    assert source.read_text() == "before"


def test_net_diff_cancels_roundtrip_edit(tmp_path):
    (tmp_path / "a.py").write_text("A\n")
    executor, session = tool(tmp_path)
    executor.execute("read_file", {"path": "a.py"})
    executor.execute("edit_file", {"path": "a.py", "old_text": "A", "new_text": "B"})
    executor.execute("edit_file", {"path": "a.py", "old_text": "B", "new_text": "A"})
    diff = build_task_diff(session, executor.artifacts, Budget(10))
    assert diff["changed_paths"] == []
    assert diff["artifact"] is None


def test_old_read_preview_is_omitted_but_saved_result_is_unchanged(tmp_path):
    from tero.context import ContextManager

    session = Session.create(tmp_path)
    session.user("Keep the API")
    executor, _ = tool(tmp_path, session)
    (tmp_path / "a.py").write_text("code " * 10000)
    result = executor.execute("read_file", {"path": "a.py"})
    session.history.append(
        new_turn([call("read_file", {"path": "a.py"}, "read")], {"read": result.to_dict()})
    )
    session.history.append({"kind": "feedback", "text": "recent evidence " * 100})
    session.observed = len(session.history)
    original = json.dumps(session.history)
    context = ContextManager(Config(recent_tokens=100))
    inputs = context.items(session, [])
    output = next(item for item in inputs if item.get("type") == "function_call_output")
    assert "Older read preview omitted" in output["output"]
    assert json.dumps(session.history) == original


def test_artifact_cannot_cross_session_or_follow_symlink(tmp_path):
    first = ArtifactStore(tmp_path / "first", str)
    second = ArtifactStore(tmp_path / "second", str)
    saved = first.write_text("retained")
    with pytest.raises(FileNotFoundError):
        second.read_page(saved["id"], 0, 100)
    content = tmp_path / "first" / (saved["id"] + ".txt")
    content.unlink()
    outside = tmp_path / "outside"
    outside.write_text("private")
    content.symlink_to(outside)
    with pytest.raises(ValueError):
        first.read_page(saved["id"], 0, 100)


def test_one_read_failure_does_not_cancel_sibling(tmp_path):
    executor, _ = tool(tmp_path)
    (tmp_path / "good.py").write_text("good")
    calls = [
        call("read_file", {"path": "missing.py"}, "missing"),
        call("read_file", {"path": "good.py"}, "good"),
    ]
    results = list(execute_batch(executor, calls, lambda call: None, lambda: False))
    assert results[0][1].error == "missing_path"
    assert results[1][1].status == "success"
    assert not executor.budget.cancelled.is_set()


def test_artifact_redaction_retains_no_configured_secret(tmp_path):
    secret = "do-not-persist-this-secret"
    store = ArtifactStore(tmp_path / "results", lambda text: text.replace(secret, "<redacted>"))
    saved = store.write_text(secret * 100)
    page = store.read_page(saved["id"], 0, 8192)
    assert page["redacted"]
    assert secret not in page["content"]
