"""Paired real-model navigation experiment. Answers stay outside agent workspaces."""
import argparse
import ast
import json
import shutil
import statistics
import time
from dataclasses import asdict, replace
from pathlib import Path

from tero import Config, Tero
from tero.config import load_env
from tero.storage import save_json

# Each checkpoint describes one implementation responsibility, not a symbol name.
CASES = [
    ('compaction', 'Trace how older conversation is compressed while preserving current work.', [
        ('select', 'Select the eligible history boundary and commit the new summary.', 'tero/context.py', 'prepare'),
        ('request', 'Send the dedicated summary request to the provider.', 'tero/provider.py', 'summarize'),
        ('loop', 'Record which history the main model has actually observed.', 'tero/agent_loop.py', 'run')]),
    ('recovery', 'Trace recovery after a file replacement succeeds but the tool result was not saved.', [
        ('close', 'Close pending interrupted calls without automatically replaying them.', 'tero/session.py', 'recover'),
        ('observe', 'Compare current file contents with the saved mutation receipt.', 'tero/changes.py', 'observe_interrupted_file')]),
    ('verification', 'Trace how a premature final answer is checked and failed verification returns to repair.', [
        ('verify', 'Execute configured verification and recheck the tested file state.', 'tero/completion.py', 'check_completion'),
        ('repair', 'Feed a blocked completion back into the main model loop.', 'tero/agent_loop.py', 'run'),
        ('observe', 'Observe workspace changes around command execution.', 'tero/tool_executor.py', 'observe_command')]),
    ('parallel_reads', 'Trace how a model response containing multiple independent reads is executed.', [
        ('schedule', 'Group consecutive reads and dispatch bounded parallel execution.', 'tero/tool_batch.py', 'execute_batch'),
        ('admit', 'Validate each call and enforce permissions before dispatch.', 'tero/tool_executor.py', 'admit'),
        ('save', 'Persist each call entering execution and save its results in the parent loop.', 'tero/agent_loop.py', 'run')]),
    ('file_edit', 'Trace an exact text edit from original-file preservation to a final task diff.', [
        ('publish', 'Save the preimage and mutation receipt before replacing the file.', 'tero/tool_executor.py', '_publish'),
        ('diff', 'Build a task diff using the original preimage and current disk contents.', 'tero/changes.py', 'build_task_diff')]),
    ('timeout', 'Trace cancellation and timeout of a running shell command.', [
        ('deadline', 'Manage the shared deadline and cancellation flag.', 'tero/execution.py', 'check'),
        ('command', 'Observe deadline expiry while collecting process output.', 'tero/commands.py', 'run_command'),
        ('kill', 'Terminate the process group and reap the process.', 'tero/commands.py', 'stop_process')]),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    load_env(project)
    config = Config.from_env(mode='ask', memory_enabled=False, verify_command='', max_turns=12,
                             runtime_seconds=180, output_tokens=4096, compaction_trigger_tokens=0)
    # Freeze actual source bytes for both arms, excluding credentials, tests and answer keys.
    source = output / 'source'
    for path in sorted((project / 'tero').rglob('*.py')):
        target = source / path.relative_to(project)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    for _, _, checks in CASES:
        for _, _, path, symbol in checks:
            tree = ast.parse((source / path).read_text())
            assert any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == symbol
                       for n in ast.walk(tree)), (path, symbol)
    settings = asdict(config)
    settings.pop('api_key')
    save_json(output / 'protocol.json', {'config': settings, 'cases': CASES, 'repetitions': 1,
              'method': 'Same frozen Tero source; fresh sessions; alternate on/off order; cold RepoMap object per run; OS cache uncontrolled; no memory or writes; exact checkpoint path/symbol scoring. One repository, not code implementation success.'})
    rows = []
    for index, (name, task, checks) in enumerate(CASES):
        request = task + '\nLocate the implementation for each checkpoint:\n' + '\n'.join(
            f'{key}: {description}' for key, description, _, _ in checks)
        request += '\nReturn only a JSON object mapping each checkpoint key to {"path": "workspace-relative file", "symbol": "bare function or method name, no class prefix"}. Locate existing code only; do not propose changes. Use tools as needed.'
        for enabled in ([False, True] if index % 2 == 0 else [True, False]):
            variant = 'on' if enabled else 'off'
            workspace = output / name / variant / 'workspace'
            shutil.copytree(source, workspace)
            started = time.monotonic()
            runtime = Tero(workspace, replace(config, repo_map_enabled=enabled), workspace_root=workspace)
            row = {'case': name, 'variant': variant, 'request': request}
            try:
                result = runtime.ask(request)
                row['result'] = asdict(result)
                text = result.answer.strip()
                if text.startswith('```'):
                    text = '\n'.join(text.splitlines()[1:-1])
                answer = json.loads(text)
                row['checks'] = {key: answer.get(key) == {'path': path, 'symbol': symbol}
                                 for key, _, path, symbol in checks}
                row['correct'] = result.status == 'completed' and all(row['checks'].values())
            except Exception as exc:
                row.update(error=runtime.redact(str(exc)), correct=False,
                           checks={key: False for key, *_ in checks})
            row['seconds'] = time.monotonic() - started
            traces = list((workspace / '.tero/runs').glob('*/trace.jsonl'))
            events = [json.loads(line) for p in traces for line in p.read_text().splitlines()]
            row['map_injections'] = sum(e['event'] == 'repo_map_built' and e.get('included', False) for e in events)
            row['compactions'] = sum(e['event'] == 'compacted' for e in events)
            save_json(output / name / variant / 'result.json', row)
            rows.append(row)
            save_json(output / 'rows.json', rows)
            print(json.dumps({k:row[k] for k in ('case','variant','correct','seconds','map_injections') }), flush=True)
    summary = {}
    for variant in ['off', 'on']:
        group = [r for r in rows if r['variant'] == variant]
        summary[variant] = {'correct': sum(r['correct'] for r in group), 'runs': len(group),
            'correct_checkpoints': sum(sum(r['checks'].values()) for r in group),
            'mean_seconds': statistics.mean(r['seconds'] for r in group),
            'tools': sum(r.get('result', {}).get('tools', 0) for r in group),
            'input_tokens': sum(r.get('result', {}).get('metrics', {}).get('input_tokens', 0) for r in group),
            'output_tokens': sum(r.get('result', {}).get('metrics', {}).get('output_tokens', 0) for r in group),
            'usage_complete': all(r.get('result', {}).get('metrics', {}).get('usage_complete', False) for r in group),
            'compactions': sum(r['compactions'] for r in group)}
    save_json(output / 'summary.json', summary)
    print(json.dumps(summary), flush=True)

if __name__ == '__main__':
    main()
