import json
from types import SimpleNamespace

import pytest

from tero.config import Config
from tero.context import ContextManager
from tero.execution import Budget, ExecutionStopped
from tero.repo_map import RepoMap
from tero.session import Session, SessionStore
from tero.storage import Trace


def repository(root):
    (root / "helpers.py").write_text(
        "class Base:\n    pass\n\ndef line_total(value):\n    return value * 2\n"
    )
    (root / "pricing.py").write_text(
        "from helpers import Base, line_total\n\nclass Price(Base):\n    pass\n\n"
        "def calculate_invoice_total(value):\n    return line_total(value)\n"
    )
    return RepoMap(root)


def test_graph_keeps_static_call_import_and_inheritance_relations(tmp_path):
    index = repository(tmp_path)
    snapshot = index.refresh()
    assert snapshot.edges["pricing.py::calculate_invoice_total"]["helpers.py::line_total"] > 0
    assert snapshot.edges["pricing.py::Price"]["helpers.py::Base"] > 0
    assert snapshot.edges["pricing.py::<module>"]["helpers.py::<module>"] > 0
    rendered = index.render("calculate_invoice_total")
    assert "calculate_invoice_total" in rendered.text
    assert rendered.details["selected_count"] > 0
    assert "graph_score" in rendered.details["selected_symbols"][0]


def test_render_counts_the_context_envelope(tmp_path):
    index = repository(tmp_path)
    context = ContextManager(Config())
    count = lambda text: context.count({"role": "user", "content": "Repository map:\n" + text})
    result = index.render("invoice", budget_tokens=120, token_counter=count)
    assert count(result.text) <= 120


def test_cached_graph_refreshes_after_file_change(tmp_path):
    index = repository(tmp_path)
    first = index.refresh()
    assert first.cache_misses == 2
    assert index.refresh().cache_hits == 2
    (tmp_path / "pricing.py").write_text(
        "def replacement_invoice_total(value):\n    return value\n"
    )
    updated = index.query("replacement_invoice_total")
    assert "pricing.py::calculate_invoice_total" not in updated.snapshot.symbols
    assert "pricing.py::replacement_invoice_total" in updated.snapshot.symbols


def test_index_skips_internal_agent_files(tmp_path):
    index = repository(tmp_path)
    private = tmp_path / ".tero"
    private.mkdir()
    (private / "private.py").write_text("def private_symbol():\n    pass\n")
    assert all(not key.startswith(".tero/") for key in index.refresh().symbols)


def test_navigation_is_ephemeral_and_does_not_call_summary_model(tmp_path):
    index = repository(tmp_path)
    context = ContextManager(Config(), repo_map=index)
    session = Session.create(tmp_path)
    session.user("Fix calculate_invoice_total")
    before = json.dumps(session.history)
    client = SimpleNamespace(trace=Trace(tmp_path / ".tero/trace", str))
    items = context.prepare(
        session, "Rules", [], [], client, SessionStore(tmp_path / ".tero/sessions"), Budget(10)
    )
    assert "calculate_invoice_total" in items[0]["content"]
    assert "Repository navigation" in items[0]["content"]
    assert json.dumps(session.history) == before
    assert session.covered == 0


def test_no_room_for_navigation_does_not_compact_history(tmp_path):
    index = repository(tmp_path)
    session = Session.create(tmp_path)
    session.user("Inspect calculate_invoice_total")
    base = ContextManager(Config())
    original = base.items(session, [])
    config = Config(
        context_tokens=base.count({"instructions": "Rules", "input": original, "tools": []}) + 261,
        output_tokens=256,
    )
    context = ContextManager(config, repo_map=index)
    client = SimpleNamespace(trace=Trace(tmp_path / ".tero/trace", str))
    items = context.prepare(
        session, "Rules", [], [], client, SessionStore(tmp_path / ".tero/sessions"), Budget(10)
    )
    assert items == original
    assert session.covered == 0


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_or_failed_index_keeps_file_tool_context(tmp_path, enabled):
    class Unavailable:
        def query(self, *args, **kwargs):
            if not enabled:
                raise AssertionError("Disabled index must not be queried")
            raise ValueError("Parser unavailable")

    session = Session.create(tmp_path)
    session.user("Inspect source")
    context = ContextManager(Config(repo_map_enabled=enabled), repo_map=Unavailable())
    trace = Trace(tmp_path / ".tero/trace", str)
    items = context.prepare(
        session,
        "Rules",
        [],
        [],
        SimpleNamespace(trace=trace),
        SessionStore(tmp_path / ".tero/sessions"),
        Budget(10),
    )
    assert items == context.items(session, [])
    assert trace.path.exists() == enabled


def test_navigation_does_not_swallow_cancellation(tmp_path):
    index = repository(tmp_path)
    session = Session.create(tmp_path)
    session.user("Inspect invoice")
    context = ContextManager(Config(), repo_map=index)
    budget = Budget(10)
    budget.cancelled.set()
    with pytest.raises(ExecutionStopped):
        context.prepare(
            session,
            "Rules",
            [],
            [],
            SimpleNamespace(trace=Trace(tmp_path / ".tero/trace", str)),
            SessionStore(tmp_path / ".tero/sessions"),
            budget,
        )


def test_interrupted_graph_rebuild_cannot_reuse_old_snapshot(tmp_path, monkeypatch):
    import tero.repo_map as module

    index = repository(tmp_path)
    index.refresh()
    (tmp_path / "pricing.py").write_text("def new_total():\n    return 1\n")
    original = module._resolve_graph

    def interrupted(*args, **kwargs):
        raise ExecutionStopped("cancelled")

    monkeypatch.setattr(module, "_resolve_graph", interrupted)
    with pytest.raises(ExecutionStopped):
        index.refresh()
    monkeypatch.setattr(module, "_resolve_graph", original)
    refreshed = index.refresh()
    assert "pricing.py::new_total" in refreshed.symbols
    assert "pricing.py::calculate_invoice_total" not in refreshed.symbols
