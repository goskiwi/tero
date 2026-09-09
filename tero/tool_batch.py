"""Bounded consecutive read groups, with serial mutations and main-thread state writes."""

import json
from concurrent.futures import ThreadPoolExecutor

from .execution import ExecutionStopped
from .tool_executor import ToolResult
from .tools import READ_TOOLS


def execute_batch(executor, calls, on_start, stopped):
    index = 0
    while index < len(calls):
        if stopped():
            call = calls[index]
            yield (
                call,
                executor.finish(
                    call["name"],
                    ToolResult("rejected", "Not started: tool loop stopped.", error="loop_stopped"),
                ),
            )
            index += 1
            continue
        group = [calls[index]]
        index += 1
        if group[0]["name"] in READ_TOOLS:
            while (
                index < len(calls)
                and calls[index]["name"] in READ_TOOLS
                and len(group) < executor.config.max_parallel_tools
            ):
                group.append(calls[index])
                index += 1
        prepared = []
        for call in group:
            try:
                arguments = json.loads(call["arguments"])
            except (ValueError, TypeError):
                value = ToolResult(
                    "rejected", "Tool arguments must be JSON", error="invalid_arguments"
                )
            else:
                value = executor.admit(call["name"], arguments)
            prepared.append((call, value))
        # Single calls, including all mutations, never enter the pool.
        if len(group) == 1:
            call, value = prepared[0]
            if not isinstance(value, ToolResult):
                executor.budget.check()
                on_start(call)
                executor.trace.record("tool_started", tool=call["name"], call_id=call["call_id"])
                value = executor.run_admitted(call["name"], *value)
            yield call, executor.finish(call["name"], value)
            continue
        pool = ThreadPoolExecutor(
            max_workers=executor.config.max_parallel_tools, thread_name_prefix="tero-read"
        )
        submitted = []
        try:
            for call, value in prepared:
                if isinstance(value, ToolResult):
                    submitted.append((call, value))
                    continue
                executor.budget.check()
                on_start(call)
                executor.trace.record("tool_started", tool=call["name"], call_id=call["call_id"])
                submitted.append((call, pool.submit(executor.run_admitted, call["name"], *value)))
            for call, future in submitted:
                value = future if isinstance(future, ToolResult) else future.result()
                yield call, executor.finish(call["name"], value)
        except (KeyboardInterrupt, ExecutionStopped):
            executor.budget.cancelled.set()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
