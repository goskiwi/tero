import pytest

from tero.session import Session, SessionStore
from tero.tool_executor import ToolResult


def test_round_trip_and_pending_recovery(tmp_path):
    session = Session.create(tmp_path)
    session.user("Fix a file")
    session.history.append(
        {
            "kind": "turn",
            "items": [
                {
                    "type": "function_call",
                    "name": "write_file",
                    "arguments": "{}",
                    "call_id": "call1",
                },
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": "{}",
                    "call_id": "call2",
                },
            ],
            "results": {"call1": ToolResult("success", "done").to_dict()},
        }
    )
    store = SessionStore(tmp_path / ".tero/sessions")
    store.save(session)
    loaded = store.load(session.id, tmp_path)
    assert loaded.recover() == 1
    assert loaded.history[-1]["results"]["call1"]["status"] == "success"
    assert loaded.history[-1]["results"]["call2"]["workspace_effect"] == "none"
    assert loaded.recover() == 0


def test_resume_rejects_different_workspace(tmp_path):
    session = Session.create(tmp_path)
    store = SessionStore(tmp_path / ".tero/sessions")
    store.save(session)
    with pytest.raises(ValueError, match="another workspace"):
        store.load(session.id, tmp_path / "other")


def test_session_path_rejects_traversal(tmp_path):
    with pytest.raises(ValueError):
        SessionStore(tmp_path).path("../other")
