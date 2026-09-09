from tero.session import new_turn

"""Explicit opt-in model evaluation. Creates fresh temporary workspaces; never touches a supplied repo."""

import argparse
import json
import shlex
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from tero import Config, Tero
from tero.commands import run_command
from tero.config import load_env
from tero.execution import Budget
from tero.provider import ResponsesClient
from tero.storage import save_json

from .cases import NORMALIZER, NORMALIZER_REQUEST, PRICING, PRICING_REQUEST


def run_case(name, config, *, repo_map=True):
    root = Path(tempfile.mkdtemp(prefix=f"tero eval-{name}-")).resolve()
    files = NORMALIZER if name == "compaction" else PRICING
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    config = replace(
        config,
        mode="code" if name == "approval" else "auto",
        memory_enabled=False,
        repo_map_enabled=repo_map,
        verify_command=f"{shlex.quote(sys.executable)} checks.py",
    )
    if name == "compaction":
        config = replace(
            config,
            context_tokens=64000,
            output_tokens=16384,
            recent_tokens=1000,
            summary_tokens=2000,
        )
    drifted = False

    class DriftClient(ResponsesClient):
        def request(self, *args, **kwargs):
            nonlocal drifted
            read_pricing = any(
                result.get("status") == "success"
                and result.get("data", {}).get("path") == "inventory/pricing.py"
                and result.get("data", {}).get("revision")
                for entry in runtime.session.history
                if entry["kind"] == "turn"
                for result in entry["results"].values()
            )
            if name == "revision" and read_pricing and not drifted:
                target = root / "inventory/pricing.py"
                target.write_text(
                    target.read_text() + "\n# External user note: preserve this line.\n"
                )
                drifted = True
            return super().request(*args, **kwargs)

    runtime = Tero(root, config, workspace_root=root, approve=lambda *args: False, client_factory=DriftClient)
    if name == "resume":
        runtime.session.user("Inspect pricing")
        runtime.session.history.append(
            new_turn(
                [
                    {
                        "type": "function_call",
                        "name": "write_file",
                        "call_id": "pending",
                        "arguments": json.dumps(
                            {"path": "must-not-replay.txt", "content": "unexpected"}
                        ),
                    }
                ],
                {},
            )
        )
        runtime.store.save(runtime.session)
        runtime = Tero(root, config, workspace_root=root, session_id=runtime.session.id)
    if name == "compaction":
        # Controlled pressure on real compaction, not a claimed natural long-task benchmark.
        runtime.session.user(NORMALIZER_REQUEST)
        for index in range(100):
            runtime.session.history.append(
                {
                    "kind": "feedback",
                    "text": f"Historical inspection {index}: "
                    + "Previously inspected unrelated module. " * 150,
                }
            )
        runtime.session.observed = len(runtime.session.history)
        runtime.store.save(runtime.session)
    result = runtime.ask(
        "Continue the previously specified normalizer task using the earlier requirements."
        if name == "compaction"
        else PRICING_REQUEST
    )
    checks_unchanged = (root / "checks.py").is_file() and (root / "checks.py").read_text() == files[
        "checks.py"
    ]
    # Evaluate in a separate directory using the original acceptance checks, not model-edited checks.
    acceptance_root = Path(tempfile.mkdtemp(prefix="tero-acceptance-")).resolve()
    for path in files:
        target = acceptance_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        source = root / path
        if path == "checks.py":
            target.write_text(files[path])
        elif source.is_file():
            target.write_text(source.read_text())
    acceptance = run_command(config.verify_command, acceptance_root, 30, Budget(30))
    calls = [
        (item, entry["results"].get(item["call_id"], {}))
        for entry in runtime.session.history
        if entry["kind"] == "turn"
        for item in entry["items"]
        if item.get("type") == "function_call"
    ]
    if name == "approval":
        passed = (
            result.status != "completed"
            and all(
                (root / path).is_file() and (root / path).read_text() == content
                for path, content in files.items()
            )
            and any(value.get("error") == "approval_denied" for _, value in calls)
        )
    else:
        passed = result.status == "completed" and checks_unchanged and acceptance["exit_code"] == 0
        if name == "resume":
            passed = passed and not (root / "must-not-replay.txt").exists()
        if name == "compaction":
            passed = passed and result.metrics["compactions"] > 0
        if name == "revision":
            passed = (
                passed
                and drifted
                and "# External user note: preserve this line."
                in (root / "inventory/pricing.py").read_text()
            )
    report = {
        "case": name,
        "passed": passed,
        "workspace": str(root),
        "model": config.model,
        "repo_map": repo_map,
        "context_tokens": config.context_tokens,
        "max_turns": config.max_turns,
        "runtime_seconds": config.runtime_seconds,
        "drift_injected": drifted,
        "result": result.__dict__,
        "checks_unchanged": checks_unchanged,
        "acceptance": acceptance,
        "acceptance_workspace": str(acceptance_root),
    }
    path = root / ".tero/evaluation.json"
    save_json(path, report)
    return report, path


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="tero eval",
        description="Run one real-model evaluation (uses API credits and executes commands)",
    )
    parser.add_argument(
        "--case", choices=("pricing", "compaction", "resume", "approval", "revision"), required=True
    )
    parser.add_argument("--no-repo-map", action="store_true")
    parser.add_argument("--config-dir", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    load_env(args.config_dir)
    report, path = run_case(args.case, Config.from_env(), repo_map=not args.no_repo_map)
    print(json.dumps({"passed": report["passed"], "report": str(path)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
