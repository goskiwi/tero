"""Net file-tool changes from the first preimage, with explicit attribution limits."""

import difflib
from itertools import pairwise
from pathlib import Path

from .execution import Budget, ExecutionStopped
from .tool_executor import content_revision, file_revision


def build_task_diff(session, artifacts, budget):
    paths = {}
    for receipt in session.mutations:
        if receipt["status"] != "not_applied":
            paths.setdefault(receipt["path"], []).append(receipt)
    chunks, changed, external, unavailable = [], [], [], []
    root = Path(session.workspace)
    for logical, receipts in paths.items():
        try:
            budget.check()
            first, last = receipts[0], receipts[-1]
            before = (
                artifacts.read_bytes(first["preimage_id"], kind="preimage")
                if first["preimage_id"]
                else b""
            )
            target = root / logical
            if target.resolve() != target or not target.is_relative_to(root):
                raise ValueError("Changed target cannot be followed")
            after = target.read_bytes() if target.exists() else b""
            actual = content_revision(after) if target.exists() else "absent"
            if actual != last["after_revision"] or any(
                left["after_revision"] != right["before_revision"]
                for left, right in pairwise(receipts)
            ):
                external.append(logical)
            if actual == first["before_revision"]:
                continue
            changed.append(logical)
            chunks.append(
                "".join(
                    difflib.unified_diff(
                        before.decode().splitlines(True),
                        after.decode().splitlines(True),
                        fromfile=logical if first["preimage_id"] else "/dev/null",
                        tofile=logical if target.exists() else "/dev/null",
                    )
                )
            )
        except (OSError, ValueError, ExecutionStopped) as exc:
            unavailable.append({"path": logical, "reason": str(exc)})
    shell_paths = sorted(
        {
            path
            for entry in session.history[session.request_start :]
            for result in entry.get("results", {}).values()
            if result.get("data", {}).get("command")
            for path in result["data"].get("changed_paths", [])
        }
    )
    note = "Net diff from the first file-tool preimage to current disk. It may include external changes.\n"
    artifact = artifacts.write_text(note + "\n".join(chunks), kind="diff") if chunks else None
    return {
        "artifact": artifact,
        "artifact_path": str(artifacts.root / (artifact["id"] + ".txt")) if artifact else None,
        "changed_paths": changed,
        "external_or_uncertain_paths": external,
        "unavailable": unavailable,
        "shell_observed_paths": shell_paths,
        "scope": "File tools only; Shell changes have no automatic preimage or rollback. Listed Shell paths may be truncated; consult the saved result. External attribution is not proven.",
    }


def observe_interrupted_file(session, entry_index, call):
    records = [
        receipt
        for receipt in session.mutations
        if receipt["operation"] == {"entry": entry_index, "call_id": call["call_id"]}
    ]
    if not records:
        return "unknown", {}
    receipt = records[-1]
    data = {"path": receipt["path"], "receipt": dict(receipt)}
    try:
        target = Path(session.workspace) / receipt["path"]
        if target.resolve() != target or not target.is_relative_to(Path(session.workspace)):
            return "unknown", data
        actual = file_revision(target, Budget(5))
        data["observed_revision"] = actual
        if receipt["status"] == "not_applied" or actual == receipt["before_revision"]:
            return "none", data
        if actual == receipt["after_revision"]:
            data["observation"] = (
                "Current bytes match the planned after-state; this does not prove authorship."
            )
            return "changed", data
    except (OSError, ExecutionStopped):
        pass
    return "unknown", data
