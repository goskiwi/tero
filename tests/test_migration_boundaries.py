"""Migration checks; local Git/process fixtures only, no LLM access."""
import json
import subprocess
from pathlib import Path

import pytest

from tero import Config, Tero
from tero.agent_loop import AgentLoop
from tero.execution import Budget
from tero.session import Session, SessionStore, new_loop_control
from tero.storage import Trace
from tero.tool_executor import ToolExecutor, content_revision
from tero.tools import tool_schemas
from tero.workspace import Workspace


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)


def executor(root, config):
    return ToolExecutor(root, config, Budget(20), Trace(root / '.tero/trace', str))


def test_root_discovery_and_explicit_subdirectory(tmp_path):
    git(tmp_path, 'init')
    sub = tmp_path / 'src'
    sub.mkdir()
    found = Workspace(sub)
    assert found.root == tmp_path.resolve()
    assert found.startup == sub.resolve()
    bounded = Tero(sub, Config(memory_enabled=False), workspace_root=sub)
    assert bounded.root == sub.resolve()
    assert bounded.workspace.repository == tmp_path.resolve()
    (sub / 'a.txt').write_text('changed')
    assert bounded.workspace.observe()['status'] == 'dirty'


def test_whitelist_filters_schema_and_executor(tmp_path):
    config = Config(mode='auto', allowed_tools=('read_file',))
    assert [t['name'] for t in tool_schemas(config.mode, allowed_tools=config.allowed_tools)] == ['read_file']
    tool = executor(tmp_path, config)
    assert tool.execute('write_file', {'path': 'a.txt', 'content': 'x'}).status == 'rejected'
    assert not (tmp_path / 'a.txt').exists()
    assert tool_schemas('ask', allowed_tools=('write_file',)) == []
    with pytest.raises(ValueError, match='Unknown tool'):
        Config(allowed_tools=('unknown',))


def test_normalized_scope_and_resolved_approval(tmp_path):
    config = Config(allowed_write_paths=('./a.txt',))
    assert config.allowed_write_paths == ('a.txt',)
    tool = executor(tmp_path, config)
    seen = []
    tool.approve = lambda name, args: seen.append(args) or True
    assert tool.execute('write_file', {'path': 'a.txt', 'content': 'x'}).status == 'success'
    assert seen[0]['resolved_target'] == str(tmp_path / 'a.txt')


def test_read_uses_bounded_stream_and_full_revision(tmp_path, monkeypatch):
    data = b'x' * (1024 * 1024 + 12) + b'\nlast\n'
    (tmp_path / 'large.txt').write_bytes(data)
    tool = executor(tmp_path, Config(mode='ask'))
    monkeypatch.setattr(tool, '_bytes', lambda target: pytest.fail('whole-file read used'))
    result = tool.execute('read_file', {'path': 'large.txt', 'start': 2, 'end': 2})
    assert result.status == 'success'
    assert result.data['revision'] == content_revision(data)
    assert result.content == '2: last'


def test_git_index_change_without_content_change_is_observed(tmp_path):
    git(tmp_path, 'init')
    (tmp_path / 'a.txt').write_text('x')
    tool = executor(tmp_path, Config(mode='auto'))
    before = tool.snapshot()
    git(tmp_path, 'add', 'a.txt')
    after = tool.snapshot()
    assert before['a.txt'] == after['a.txt']
    assert before['.git/observed-index'] != after['.git/observed-index']


def test_alternating_failure_stops_and_survives_resume(tmp_path):
    tool = executor(tmp_path, Config(mode='ask'))
    session = Session.create(tmp_path)
    session.user('Find files')
    control = session.loop_control
    for name in ['a.txt', 'b.txt', 'a.txt', 'b.txt']:
        args = {'path': name}
        result = tool.execute('read_file', args)
        assert not AgentLoop._track_failure(control, tool, 'read_file', args, result)
    store = SessionStore(tmp_path / '.tero/sessions')
    store.save(session)
    control = store.load(session.id, tmp_path).loop_control
    args = {'path': 'a.txt'}
    result = tool.execute('read_file', args)
    assert AgentLoop._track_failure(control, tool, 'read_file', args, result) == 'repeated_tool_failure'


@pytest.mark.parametrize('field,value', [('id', 'f' * 16), ('request_start', 10)])
def test_session_rejects_wrong_identity_or_request(tmp_path, field, value):
    session = Session.create(tmp_path)
    session.user('task')
    store = SessionStore(tmp_path / '.tero/sessions')
    store.save(session)
    saved = json.loads(store.path(session.id).read_text())
    saved[field] = value
    store.path(session.id).write_text(json.dumps(saved))
    with pytest.raises(ValueError):
        store.load(session.id, tmp_path)


def test_search_honors_gitignore_and_reports_matches(tmp_path):
    git(tmp_path, 'init')
    (tmp_path / '.gitignore').write_text('ignored.txt\n')
    (tmp_path / 'ignored.txt').write_text('needle')
    (tmp_path / 'visible.txt').write_text('needle')
    result = executor(tmp_path, Config(mode='ask')).execute('search', {'path': '.', 'pattern': 'needle'})
    assert result.status == 'success'
    assert 'visible.txt' in result.content and 'ignored.txt' not in result.content
