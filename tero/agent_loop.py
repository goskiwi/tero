"""The normal task path: recall -> context -> model -> tools/verification -> save."""

import json
import time
import uuid
from dataclasses import dataclass, field

from .changes import build_task_diff
from .completion import check_completion
from .context import ContextTooLarge
from .execution import Budget, ExecutionStopped
from .provider import ContextOverflow, ProviderError, response_text
from .session import new_loop_control, new_turn
from .storage import Trace, now, save_json
from .tool_batch import execute_batch
from .tool_executor import ToolExecutor, ToolResult
from .tools import TOOLS, tool_schemas


@dataclass
class RunResult:
    session_id: str
    status: str
    answer: str
    stop_reason: str
    verification: str
    turns: int
    tools: int
    memory_changes: list
    memory_error: str = ""
    unconfirmed: list[dict] = field(default_factory=list)
    run_id: str = ""
    metrics: dict = field(default_factory=dict)
    task_diff: dict = field(default_factory=dict)


class AgentLoop:
    def __init__(self, runtime):
        self.runtime = runtime

    def run(self, message, *, budget=None):
        runtime = self.runtime
        session, config = runtime.session, runtime.config
        budget = budget or Budget(config.runtime_seconds)
        runtime.budget = budget
        run_id = uuid.uuid4().hex[:16]
        trace = Trace(
            runtime.root / ".tero" / "runs" / run_id / "trace.jsonl",
            runtime.redact,
            runtime.display,
        )
        client = runtime.client_factory(config, trace)
        tools = tool_schemas(config.mode, child=runtime.child, allowed_tools=config.allowed_tools)
        if session.run.get("status") == "completed":
            session.loop_control = new_loop_control()
            session.mutations = []
        control = session.loop_control

        def save_mutation(receipt):
            for index, previous in enumerate(session.mutations):
                if previous["id"] == receipt["id"]:
                    session.mutations[index] = dict(receipt)
                    break
            else:
                session.mutations.append(dict(receipt))
            runtime.store.save(session)

        executor = ToolExecutor(
            runtime.root,
            config,
            budget,
            trace,
            runtime.approve,
            lambda args: self._delegate(args, budget),
            child=runtime.child,
            denied=control["denied"],
            save_denial=lambda: runtime.store.save(session),
            artifacts=runtime.artifacts,
            save_mutation=save_mutation,
        )
        started = time.monotonic()
        memories, memory_changes = [], []
        memory_error = ""
        answer, reason = "", ""
        turns = executed = 0
        status = "stopped"
        if session.run.get("status") == "completed":
            session.verification_required = False
        session.verification_required |= bool(config.verify_command and config.mode != "ask")
        recovered = session.recover()
        if recovered:
            session.history.append(
                {
                    "kind": "feedback",
                    "text": "Previous execution was interrupted. Results without durable confirmation are unknown. "
                    "Read current files before editing; no old command has been replayed.",
                }
            )
        # Retain the last observation for continuation; completion always verifies afresh.
        if session.verification["status"] == "passed":
            session.verification.update(status="stale", files=None)
        session.request_start = len(session.history)
        session.user(runtime.redact(message))
        session.run = {
            "id": run_id,
            "status": "running",
            "started_at": now(),
            "turns": 0,
            "tools": 0,
        }
        runtime.store.save(session)
        trace.record("run_started", session_id=session.id, recovered_calls=recovered)
        try:
            if config.memory_enabled and not runtime.child:
                try:
                    memories = runtime.memory.recall(message, client, runtime.context, budget)
                    trace.record("memory_recalled", ids=[item["id"] for item in memories])
                except ExecutionStopped:
                    raise
                except Exception as exc:  # noqa: BLE001 - optional model-assisted recall
                    trace.record("memory_recall_failed", error=str(exc))
            overflow_retried = False
            force_compaction = False
            while turns < config.max_turns:
                budget.check()
                instructions = runtime.instructions()
                items = runtime.context.prepare(
                    session,
                    instructions,
                    tools,
                    memories,
                    client,
                    runtime.store,
                    budget,
                    force=force_compaction,
                )
                force_compaction = False
                turns += 1
                session.run["turns"] = turns
                runtime.store.save(session)
                sent_end = len(session.history)
                try:
                    output = client.request(instructions, items, tools, budget)
                except ContextOverflow:
                    if overflow_retried:
                        raise
                    overflow_retried = force_compaction = True
                    continue
                overflow_retried = False
                session.observed = sent_end
                entry = new_turn(output)
                session.history.append(entry)
                # A call is durable BEFORE any external operation can begin.
                runtime.store.save(session)
                calls = [item for item in output if item.get("type") == "function_call"]
                if calls:
                    valid_call = False
                    entry_index = len(session.history) - 1

                    def start(call, entry=entry, entry_index=entry_index):
                        entry["phases"][call["call_id"]] = "running"
                        executor.operation = {"entry": entry_index, "call_id": call["call_id"]}
                        runtime.store.save(session)

                    for call, result in execute_batch(
                        executor,
                        calls,
                        start,
                        lambda: bool(reason),  # noqa: B023 - consumed synchronously before next loop iteration
                    ):
                        if result.error != "loop_stopped":
                            executed += 1
                            valid_call |= result.error not in {"invalid_arguments", "unknown_tool"}
                            try:
                                arguments = json.loads(call["arguments"])
                            except (ValueError, TypeError):
                                arguments = call["arguments"]
                            reason = reason or self._track_failure(
                                control, executor, call["name"], arguments, result
                            )
                        saved_result = result.to_dict()
                        entry["results"][call["call_id"]] = saved_result
                        entry["phases"][call["call_id"]] = "finished"
                        session.observe_result(entry_index, call, saved_result)
                        session.run["tools"] = executed
                        runtime.store.save(session)
                    control["invalid_outputs"] = 0 if valid_call else control["invalid_outputs"] + 1
                    if control["invalid_outputs"] >= 8:
                        reason = reason or "invalid_output_limit"
                    if reason:
                        answer = "Repeated failures require changed inputs, state or an explicit user decision. "
                        answer += "Recorded edits and tool results are preserved in this Session."
                        break
                    continue
                proposed = response_text(output)
                if not proposed:
                    control["invalid_outputs"] += 1
                    session.history.append(
                        {
                            "kind": "feedback",
                            "text": "Return a tool call or a non-empty final answer.",
                        }
                    )
                    runtime.store.save(session)
                    if control["invalid_outputs"] >= 8:
                        reason = "invalid_output_limit"
                        break
                    continue
                control["invalid_outputs"] = 0
                decision = check_completion(session, executor, runtime.store)
                if decision.status == "success":
                    answer, status = proposed, "completed"
                    break
                if control["completion_reason"] != decision.error:
                    control["completion_blocks"] = 0
                control["completion_reason"] = decision.error
                control["completion_paths"] = decision.data.get("changed_paths", []) or [
                    item["path"] for item in session.unconfirmed if item["path"] is not None
                ]
                control["completion_blocks"] += 1
                session.history.append(
                    {
                        "kind": "feedback",
                        "text": "Runtime completion check: "
                        + json.dumps(decision.to_dict(), ensure_ascii=False),
                    }
                )
                runtime.store.save(session)
                trace.record("completion_blocked", reason=decision.error, content=decision.content)
                if decision.status == "rejected":
                    reason, answer = decision.error, decision.content
                    break
                if control["completion_blocks"] >= 3:
                    reason, answer = "completion_block_limit", decision.content
                    break
            else:
                reason = "turn_limit"
        except (ExecutionStopped, KeyboardInterrupt) as exc:
            reason = str(exc) if isinstance(exc, ExecutionStopped) else "cancelled"
        except (ProviderError, ContextTooLarge) as exc:
            reason = runtime.redact(str(exc))
            trace.record("request_failed", error=reason)
        except Exception as exc:  # noqa: BLE001 - persist a failed task before returning
            reason = runtime.redact(f"runtime_error: {exc}")
            trace.record("runtime_failed", error=reason)
        finally:
            # Missing results remain unknown, including a crash during result persistence.
            session.recover()
            if status != "completed" and session.verification["status"] == "passed":
                session.verification.update(status="stale", files=None)
            session.run.update(
                status=status,
                stop_reason=reason,
                turns=turns,
                tools=executed,
                ended_at=now(),
            )
            runtime.store.save(session)
        task_diff = build_task_diff(session, runtime.artifacts, budget)
        if status == "completed" and config.memory_enabled and not runtime.child:
            try:
                budget.check()
                memory_changes = runtime.memory.extract(session, client, runtime.context, budget)
                trace.record("memory_updated", operations=memory_changes)
            except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - auxiliary memory cannot undo task completion
                memory_error = runtime.redact(str(exc) or "cancelled")
                trace.record("memory_update_failed", error=memory_error)
        if status != "completed":
            answer = "Task not completed: " + reason + ("\n" + answer if answer else "")
        if session.unconfirmed:
            answer += (
                "\n\nRemaining uncertainty: effects of interrupted operations "
                + ", ".join(item["id"] for item in session.unconfirmed)
                + " are not fully established. Subsequent observations and local verification "
                "do not prove absence of external effects."
            )
        result = RunResult(
            session.id,
            status,
            runtime.redact(answer),
            reason,
            session.verification["status"],
            turns,
            executed,
            memory_changes,
            memory_error,
            list(session.unconfirmed),
            run_id,
            {
                **trace.metrics,
                "usage_complete": trace.metrics["usage_complete"]
                and trace.metrics["model_requests"] == trace.metrics["model_responses"]
                and trace.metrics["model_responses"] > 0,
                "seconds": round(time.monotonic() - started, 3),
                "scope": "this run, including summary and memory requests; excludes child runs",
            },
            task_diff,
        )
        save_json(
            trace.path.with_name("report.json"),
            {
                **result.__dict__,
                "seconds": round(time.monotonic() - started, 3),
                "model": config.model,
                "context_budget_is_estimate": True,
            },
        )
        trace.record("run_finished", status=status, reason=reason)
        return result

    @staticmethod
    def _track_failure(control, executor, name, arguments, result):
        paths = set(result.data.get("changed_paths", []))
        if result.data.get("path"):
            paths.add(result.data["path"])
        # This invalidates an old refusal; it does not prove the task made semantic progress.
        global_verifier = (
            control["completion_reason"] == "verification_failed"
            and not control["completion_paths"]
        )
        relevant_changes = paths and (
            global_verifier or paths.intersection(control["completion_paths"])
        )
        if result.workspace_effect == "changed" and relevant_changes:
            control["completion_blocks"] = 0
        try:
            args = TOOLS[name][0].model_validate(arguments).model_dump()
        except (KeyError, ValueError):
            args = arguments
        if isinstance(args, dict) and result.data.get("path"):
            args = {**args, "path": result.data["path"]}
        history = control["recent_failures"]
        retained = []
        for previous in history:
            failed = previous["key"]
            failed_path = failed.get("arguments", {}).get("path") if isinstance(failed.get("arguments"), dict) else None
            related = failed_path and failed_path in paths
            changed = related and result.workspace_effect == "changed"
            reread = related and name == "read_file" and result.status == "success" and (
                failed["error"] in {"read_required", "revision_conflict", "missing_path"}
                or result.data.get("revision") != failed.get("actual_revision")
            )
            succeeded = result.status == "success" and failed["tool"] == name and failed["arguments"] == args
            if not (changed or reread or succeeded):
                retained.append(previous)
        control["recent_failures"] = retained
        control["last_failure"] = retained[-1] if retained else None
        if result.status == "success":
            return ""
        path = result.data.get("path")
        key = {"tool": name, "arguments": args, "error": result.error,
               "actual_revision": result.data.get("actual_revision", result.data.get("revision")),
               "read_revision": executor.read_versions.get(path) if path else None}
        previous = next((entry for entry in retained if entry["key"] == key), None)
        count = previous["count"] + 1 if previous else 1
        entry = {"key": key, "count": count}
        control["recent_failures"] = [item for item in retained if item["key"] != key][-7:] + [entry]
        control["last_failure"] = entry
        if count >= 2:
            result.content += (
                f"\nRepeated failure ({count}): change the failed arguments or satisfy "
                "the stated recovery condition; unrelated successful calls do not resolve this failure."
            )
            executor.trace.record(
                "repeated_tool_failure", tool=name, error=result.error, count=count
            )
        certain = {
            "invalid_arguments",
            "unknown_tool",
            "missing_path",
            "read_required",
            "revision_conflict",
            "ambiguous_text_match",
            "text_not_found",
            "invalid_range",
            "not_file",
            "not_directory",
            "approval_denied",
            "permission_denied",
            "write_scope_denied",
            "protected_path",
            "path_outside_workspace",
            "shell_scope_denied",
        }
        # Shell outcomes may depend on invisible process/remote state: warn, never deduplicate/replay.
        return (
            "repeated_tool_failure"
            if count >= 3 and (result.error in certain and result.workspace_effect == "none")
            else ""
        )

    def _delegate(self, args, budget):
        child = self.runtime.make_child(args["max_turns"])
        result = child.ask(
            args["task"]
            + "\nReturn relevant file locations, findings, suggestions and uncertainties.",
            budget=budget.child(min(180, self.runtime.config.runtime_seconds)),
        )
        return ToolResult(
            "success" if result.status == "completed" else "error",
            result.answer,
            error="" if result.status == "completed" else "child_stopped",
            data={"session_id": result.session_id, "turns": result.turns, "read_only": True},
        )
