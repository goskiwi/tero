"""One completion decision: resolve effects, verify, then recheck the tested files."""

import json

from .session import new_verification
from .tool_executor import ToolResult


def check_completion(session, executor, store):
    """success -> complete, error -> model repair, rejected -> user action.

    Only the current verification snapshot is saved. Old passes are never reused.
    """
    config, budget, trace = executor.config, executor.budget, executor.trace
    budget.check()
    files_to_read = [item for item in session.unconfirmed if item["path"] is not None]
    if files_to_read:
        return ToolResult(
            "error",
            "Read these files to confirm their current state before completion: "
            + ", ".join(sorted({item["path"] for item in files_to_read})),
            error="file_observation_required",
        )
    unchecked = [item for item in session.unconfirmed if not item["observations"]]
    if unchecked:
        return ToolResult(
            "error",
            "Inspect the interrupted operation using tools: relevant files, diffs, process state "
            "or remote status as appropriate. Do not blindly replay it. "
            "Local observations cannot establish external effects; report unresolved limitations "
            "and ask the user if they prevent completing the task.",
            error="effect_inspection_required",
            data={"unconfirmed": unchecked},
        )
    if session.verification_required and (not config.verify_command or config.mode == "ask"):
        return ToolResult(
            "rejected",
            "This unfinished task requires verification. Configure --verify "
            "and use code/auto mode; an old requirement cannot be dropped on resume.",
            error="verification_required",
        )
    if not config.verify_command or config.mode == "ask":
        session.verification = new_verification()
        return ToolResult("success", "Task ended without configured verification")
    if config.mode != "auto" and (
        executor.approve is None
        or not executor.approve("verify", {"command": config.verify_command})
    ):
        return ToolResult("rejected", "Verification was not approved", error="verification_denied")

    budget.check()
    verification_id = f"verify:{session.run['id']}:{len(session.history)}"
    record = {
        "status": "running",
        "command": config.verify_command,
        "call_id": verification_id,
        "files": None,
    }
    session.verification = record
    store.save(session)
    result, files = executor.observe_command(config.verify_command, config.tool_seconds)
    passed = result.status == "success" and result.workspace_effect == "none" and files is not None
    record.update(status="passed" if passed else "failed", files=files if passed else None)
    session.history.append(
        {
            "kind": "feedback",
            "text": "Runtime verification: " + json.dumps(result.to_dict(), ensure_ascii=False),
        }
    )
    if result.workspace_effect == "unknown":
        session.add_unconfirmed(verification_id, "verify")
    elif result.workspace_effect == "changed":
        session.verification_required = True
    store.save(session)
    trace.record("verification_finished", result=result.to_dict())
    if result.workspace_effect == "unknown":
        return ToolResult(
            "error",
            "Verification effects could not be observed. Inspect the command before retrying: "
            + verification_id,
            error="unconfirmed_effects",
            data={"unconfirmed": list(session.unconfirmed)},
        )
    if not passed:
        return ToolResult(
            "error",
            "Verification did not pass without changing the workspace. "
            "Inspect the recorded output, repair, and submit again.",
            error="verification_failed",
        )

    # Recheck after result persistence and reporting, immediately before accepting completion.
    budget.check()
    try:
        current = executor.snapshot()
    except OSError as exc:
        record.update(status="stale", files=None)
        session.add_unconfirmed(verification_id, "verify")
        store.save(session)
        return ToolResult(
            "error",
            "Cannot observe the tested workspace; inspect and retry: " + str(exc),
            error="unconfirmed_effects",
            data={"unconfirmed": list(session.unconfirmed)},
        )
    budget.check()
    if current != files:
        changed = sorted(
            path for path in set(current) | set(files) if current.get(path) != files.get(path)
        )
        record.update(status="stale", files=None)
        store.save(session)
        return ToolResult(
            "error",
            "Files changed after verification; inspect and verify again: " + ", ".join(changed),
            error="verification_stale",
            data={"changed_paths": changed},
        )
    return ToolResult("success", "Verification passed for the current observed workspace")
