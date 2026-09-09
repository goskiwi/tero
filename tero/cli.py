"""Thin CLI: configuration, approval, session selection, and presentation."""

import argparse
import json
import sys
from pathlib import Path

from .config import Config, load_env
from .runtime import Tero
from .session import SessionStore
from .workspace import Workspace
from .tools import TOOLS


def parser():
    result = argparse.ArgumentParser(
        prog="tero",
        description="Tero: native tools, bounded context, resumable sessions",
        epilog="Subcommands: tero code --help (code repair); tero eval --help (evaluation). "
        "Use -- before a literal prompt named code or eval.",
    )
    result.add_argument("prompt", nargs="?")
    result.add_argument("--cwd", type=Path, default=Path.cwd())
    result.add_argument("--config-dir", type=Path, default=Path(__file__).resolve().parent.parent)
    result.add_argument("--workspace-root", type=Path)
    result.add_argument("--allow-tool", action="append", choices=tuple(TOOLS))
    result.add_argument("--mode", choices=("ask", "code", "auto"), default="code")
    result.add_argument("--resume", metavar="SESSION_ID_OR_LATEST")
    result.add_argument(
        "--compact", action="store_true", help="Explicitly retry compaction of a resumed session"
    )
    result.add_argument(
        "--retry-denied",
        action="store_true",
        help="Allow approval to be requested again in a resumed task",
    )
    result.add_argument("--model")
    result.add_argument("--base-url")
    result.add_argument("--verify", default="", metavar="COMMAND")
    result.add_argument("--max-turns", type=int, default=32)
    result.add_argument("--max-parallel-tools", type=int, default=4)
    result.add_argument("--max-seconds", type=float, default=600)
    result.add_argument("--context-tokens", type=int)
    result.add_argument("--output-tokens", type=int)
    result.add_argument("--compaction-trigger-tokens", type=int, default=0)
    result.add_argument("--allow-write", action="append", metavar="RELATIVE_PATH")
    result.add_argument("--no-memory", action="store_true")
    result.add_argument(
        "--no-repo-map", action="store_true", help="Disable task-ranked Python navigation"
    )
    result.add_argument("--repo-map-tokens", type=int, default=1200)
    result.add_argument("--trace", action="store_true")
    return result


def approve(name, args):
    print(f"\nApprove {name}: {json.dumps(args, ensure_ascii=False)}")
    try:
        return input("[y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def display(event, data):
    if event == "model_requested":
        print(f"[model] {data['purpose']}")
    elif event == "tool_started":
        print(f"[tool] {data['tool']}")
    elif event == "tool_finished":
        print(
            f"[result] {data['result']['status']} / workspace: {data['result']['workspace_effect']}"
        )
    elif event in {"memory_recall_failed", "memory_update_failed", "request_failed"}:
        print(f"[{event}] {data['error']}")
    elif event == "compacted":
        print(f"[context] {data['before_tokens']} -> {data['after_tokens']} estimated tokens")
    elif event == "repo_map_built":
        print(
            f"[repo-map] {data['details']['selected_count']} symbols; included={data['included']}"
        )
    elif event in {"compaction_failed", "compaction_suppressed"}:
        print(f"[context] {event}: {data['reason']}")
    elif event == "repo_map_failed":
        print(f"[repo-map] unavailable; use read/search: {data['error']}")


def show_result(result, memory_enabled):
    print(result.answer)
    print(
        f"Session: {result.session_id} | status: {result.status} | verification: {result.verification}"
    )
    for item in result.unconfirmed:
        print(
            f"Unconfirmed: {item['id']} | {item['tool']} | {item['path'] or 'external effects not established'}"
        )
    if memory_enabled:
        if result.memory_error:
            print(f"Long-term memory was not updated: {result.memory_error}")
        else:
            print(f"Long-term memory changes saved: {len(result.memory_changes)}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "code":
        from applications.coding import main as code_main

        return code_main(argv[1:])
    if argv and argv[0] == "eval":
        from evaluations.run import main as eval_main

        return eval_main(argv[1:])
    arguments = parser()
    args = arguments.parse_args(argv)
    if args.retry_denied and not args.resume:
        arguments.error("--retry-denied requires --resume")
    if args.compact and not args.resume:
        arguments.error("--compact requires --resume")
    load_env(args.config_dir.resolve())
    options = {
        "allowed_tools": tuple(args.allow_tool) if args.allow_tool is not None else None,
        "mode": args.mode,
        "max_turns": args.max_turns,
        "max_parallel_tools": args.max_parallel_tools,
        "runtime_seconds": args.max_seconds,
        "context_tokens": args.context_tokens,
        "output_tokens": args.output_tokens,
        "compaction_trigger_tokens": args.compaction_trigger_tokens,
        "verify_command": args.verify,
        "memory_enabled": not args.no_memory,
        "repo_map_enabled": not args.no_repo_map,
        "repo_map_tokens": args.repo_map_tokens,
        "allowed_write_paths": tuple(args.allow_write) if args.allow_write is not None else None,
    }
    config = Config.from_env(**options)
    if args.model or args.base_url:
        from dataclasses import replace

        config = replace(
            config, model=args.model or config.model, base_url=args.base_url or config.base_url
        )
    session_id = args.resume
    if session_id == "latest":
        session_id = SessionStore(Workspace(args.cwd, root=args.workspace_root).root / ".tero" / "sessions").latest()
        if session_id is None:
            raise SystemExit("No saved session in this workspace")
    runtime = Tero(
        args.cwd,
        config,
        workspace_root=args.workspace_root,
        session_id=session_id,
        approve=approve,
        display=display if args.trace else None,
    )
    if args.retry_denied:
        runtime.retry_denied()
    if args.compact:
        print(json.dumps(runtime.compact(), ensure_ascii=False, indent=2))
        if not args.prompt:
            return 0
    if args.prompt:
        result = runtime.ask(args.prompt)
        show_result(result, config.memory_enabled)
        return 0 if result.status == "completed" else 1
    print("Tero — /session /memory /forget ID /effects /compact /retry-denied /reset /exit")
    while True:
        try:
            prompt = input("tero> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if prompt == "/exit":
            return 0
        if prompt == "/session":
            print(runtime.store.path(runtime.session.id))
        elif prompt == "/memory":
            print(json.dumps(runtime.memory.load(), ensure_ascii=False, indent=2))
        elif prompt == "/retry-denied":
            runtime.retry_denied()
            print("Approval may be requested again; no operation has been approved or executed.")
        elif prompt == "/compact":
            print(json.dumps(runtime.compact(), ensure_ascii=False, indent=2))
        elif prompt == "/effects":
            print(json.dumps(runtime.session.unconfirmed, ensure_ascii=False, indent=2))
        elif prompt.startswith("/forget "):
            runtime.memory.forget(prompt.split(maxsplit=1)[1])
            print("Memory deleted. Conversation history is unchanged.")
        elif prompt == "/reset":
            runtime.reset()
            print("New session. Long-term memory retained.")
        elif prompt:
            result = runtime.ask(prompt)
            show_result(result, config.memory_enabled)


if __name__ == "__main__":
    raise SystemExit(main())
