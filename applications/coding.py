"""A code-repair workflow with readable delivery, without automatic Git writes."""

import argparse
import json
from pathlib import Path

from tero import Config, Tero
from tero.cli import approve, display
from tero.commands import run_command
from tero.config import load_env
from tero.execution import Budget, ExecutionStopped
from tero.storage import atomic_write, save_json


def run_coding(workspace, request, config, *, client_factory=None, workspace_root=None):
    if not config.verify_command or config.mode == "ask":
        raise ValueError("Coding delivery requires --verify and code/auto mode")
    options = {"client_factory": client_factory} if client_factory is not None else {}
    runtime = Tero(workspace, config, workspace_root=workspace_root, approve=approve, display=display, **options)
    budget = Budget(config.runtime_seconds)
    result = runtime.ask(request, budget=budget)
    directory = runtime.root / ".tero" / "runs" / result.run_id
    workspace_view = {}
    # This describes current repository state, not attribution to the agent.
    commands = {
        "tracked_diff": "git -c core.fsmonitor=false diff --no-ext-diff --no-textconv HEAD -- . ':(exclude).tero'",
        "untracked_files": "git -c core.fsmonitor=false ls-files --others --exclude-standard -- . ':(exclude).tero'",
    }
    for name, command in commands.items():
        try:
            details = run_command(command, runtime.root, 10, budget)
            workspace_view[name] = {
                **details,
                "stdout": runtime.redact(details["stdout"]),
                "stderr": runtime.redact(details["stderr"]),
            }
        except (OSError, ExecutionStopped) as exc:
            workspace_view[name] = {"unavailable": str(exc)}
    report = {
        "request": runtime.redact(request),
        "result": result.__dict__,
        "workspace_view": workspace_view,
        "task_diff": result.task_diff,
        "scope": "Current workspace diff may contain pre-existing/user changes. Untracked file contents are not in the diff.",
    }
    save_json(directory / "delivery.json", report)
    text = (
        "# Tero coding delivery\n\n"
        + result.answer
        + f"\n\nStatus: {result.status}\n\nVerification: {result.verification}"
        + f"\n\nTurns: {result.turns}; tools: {result.tools}"
        + "\n\n## Metrics\n\n```json\n"
        + json.dumps(result.metrics, ensure_ascii=False, indent=2)
        + "\n```\n\n## Task diff\n\n```json\n"
        + json.dumps(result.task_diff, ensure_ascii=False, indent=2)
        + "\n```\n\n## Current workspace\n\n"
        + report["scope"]
        + "\n\n```json\n"
        + json.dumps(workspace_view, ensure_ascii=False, indent=2)
        + "\n```\n"
    )
    if result.task_diff.get("artifact_path"):
        text += "\n[Read the task diff](<" + result.task_diff["artifact_path"] + ">)\n"
    atomic_write(directory / "delivery.md", text.encode())
    return result, directory / "delivery.md"


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="tero code", description="Repair code and produce a reviewable delivery report"
    )
    parser.add_argument("request")
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--verify", required=True)
    parser.add_argument("--mode", choices=("code", "auto"), default="code")
    parser.add_argument("--config-dir", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    load_env(args.config_dir)
    result, report = run_coding(
        args.cwd, args.request, Config.from_env(mode=args.mode, verify_command=args.verify),
        workspace_root=args.workspace_root
    )
    print(result.answer)
    print(f"Delivery: {report}")
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
