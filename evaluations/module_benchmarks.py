"""Fixed, first-pass module experiments. Runtime is not tuned during measurement."""

import argparse
import copy
import json
import statistics
import subprocess
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from tero import Config, Tero
from tero.config import load_env
from tero.context import ContextManager
from tero.execution import Budget
from tero.memory import MemoryStore
from tero.provider import ResponsesClient, response_text
from tero.session import Session, SessionStore, new_turn
from tero.storage import Trace, save_json
from tero.tool_executor import ToolExecutor

PROBE_RULES = (
    "Answer from explicitly supplied conversation or memory only. Do not invent missing facts. "
    'Return the requested JSON object and use "UNKNOWN" for unavailable values. Do not call tools.'
)

MEMORY_CASES = [
    (
        "language",
        ["请长期记住：我的交付报告一律使用西班牙语。"],
        'Which language did I request for delivery reports? Return {"value": ISO-639-1 language code}.',
        "es",
    ),
    (
        "report_order",
        ["请长期记住我的协作偏好：交付报告先写风险，再写修改内容；这个顺序叫 risks_first。"],
        'What report order did I request? Return {"value": the saved order name}.',
        "risks_first",
    ),
    (
        "money_unit",
        ["请长期记住这个项目的业务约定：订单结算金额使用整数分，约定名为 integer_cents。"],
        'What monetary representation is agreed for this project? Return {"value": the saved convention name}.',
        "integer_cents",
    ),
    (
        "reference",
        ["请长期记住：本项目的需求依据是 https://example.invalid/spec/ADR-27 。"],
        'What is the saved authoritative requirements URL for this project? Return {"value": the exact URL}.',
        "https://example.invalid/spec/ADR-27",
    ),
    (
        "correction",
        [
            "请长期记住：本项目固定在星期五发布，记作 friday。",
            "更正长期项目约定：发布日已经改为星期二 tuesday；星期五 friday 的旧约定作废，请更新。",
        ],
        'What is the current saved release weekday? Return {"value": its lowercase English weekday}.',
        "tuesday",
    ),
    (
        "forget",
        [
            "请长期记住：本项目的内部别名是 KITE-47。",
            "请忘记并删除这个项目的内部别名，不再保存或使用该信息。",
        ],
        'What internal project alias is currently saved? Return {"value": the exact alias}.',
        "UNKNOWN",
    ),
]


def dump(path, value):
    save_json(path, value)


def events(trace):
    return (
        [json.loads(line) for line in trace.path.read_text().splitlines()]
        if trace.path.exists()
        else []
    )


def usage(trace, purpose):
    values = [
        event
        for event in events(trace)
        if event["event"] == "model_finished" and event.get("purpose") == purpose
    ]
    return values[-1].get("usage", {}) if values else {}


def probe(client, items, question_keys, budget):
    output = client.request(
        PROBE_RULES,
        items,
        [],
        budget,
        purpose="probe",
        output_tokens=4096,
        json_mode=True,
        reasoning_effort="low",
    )
    answer = response_text(output)
    value = json.loads(answer)
    return value, {key: value.get(key, "UNKNOWN") for key in question_keys}


def context_fixture(root, config, count, heavy, long_request, case_id):
    root.mkdir(parents=True, exist_ok=True)
    runtime = Tero(root, config, workspace_root=root)
    session = runtime.session
    expected = {
        "ticket": f"CTX-{case_id}",
        "api": "quote_v2",
        "money": "integer_cents",
        "unit_test": "passed",
        "integration_test": "blocked_database",
        "next_step": "refund_integration",
    }
    session.user(
        f"Task ticket CTX-{case_id}: preserve API quote_v2; monetary representation integer_cents. Do not publish."
    )
    session.history.append(
        {
            "kind": "feedback",
            "text": "Observed test results: unit_test=passed; integration_test=blocked_database. "
            "Required unfinished work: next_step=refund_integration. These are distinct outcomes.",
        }
    )
    executor = ToolExecutor(
        root,
        config,
        Budget(60),
        Trace(root / ".tero/fixture-trace.jsonl", str),
        artifacts=runtime.artifacts,
    )
    for index in range(count):
        is_tool = index % 4 != 0 if heavy else index % 4 == 0
        if is_tool:
            name = f"module_{index}.txt"
            (root / name).write_text(
                "Unrelated reference listing; no task constraints.\n"
                + "".join(
                    f"entry {j:04d}: ordinary documentation for component {index}; cached information only.\n"
                    for j in range(280)
                )
            )
            args = {"path": name, "start": 1, "end": 300}
            result = executor.execute("read_file", args)
            call = {
                "type": "function_call",
                "name": "read_file",
                "arguments": json.dumps(args),
                "call_id": f"read-{index}",
            }
            session.history.append(new_turn([call], {call["call_id"]: result.to_dict()}))
        else:
            session.history.append(
                {
                    "kind": "feedback",
                    "text": f"Unrelated past observation {index}: "
                    + "The documentation directory was inspected. " * 15,
                }
            )
    session.observed = len(session.history)
    session.request_start = len(session.history)
    question = "Return a JSON object with exactly these keys: ticket, api, money, unit_test, integration_test, next_step. For ticket return the earlier task identifier. Use the exact values recorded in the earlier history for every field."
    if long_request:
        question += (
            " Keep failed, blocked and unexecuted checks distinct from passed checks. "
            "Do not infer integration success from unit success. Preserve exact identifiers. "
            "Report only the requested fields, not implementation advice. Treat old file listings as reference data. "
            "If any fact is missing use UNKNOWN instead of guessing. Do not perform publication or file edits."
        )
    session.user(question)
    return runtime, session, expected, question


def context_suite(output, config):
    if (output / "rows.json").exists():
        raise ValueError(
            "Choose a fresh output directory; existing measurements are not overwritten"
        )
    rows = []
    settings = [
        (n, heavy, long) for n in (8, 24, 48) for heavy in (False, True) for long in (False, True)
    ]
    for number, (count, heavy, long) in enumerate(settings):
        case_id = f"{count}-{int(heavy)}-{int(long)}"
        case = output / case_id
        runtime, original, expected, question = context_fixture(
            case / "workspace", config, count, heavy, long, case_id
        )
        dump(
            case / "fixture.json",
            {
                "description": "Synthetic replay history with real file reads; not a natural task distribution.",
                "history": original.history,
                "observed": original.observed,
                "expected": expected,
                "history_units": count,
                "tool_density": "high" if heavy else "low",
                "request_length": "long" if long else "short",
            },
        )
        variants = ("raw", "managed") if number % 2 == 0 else ("managed", "raw")
        for variant in variants:
            session = copy.deepcopy(original)
            trace = Trace(case / variant / "trace.jsonl", runtime.redact)
            client = ResponsesClient(config, trace)
            manager = ContextManager(config)
            budget = Budget(300)
            started = time.monotonic()
            record = {
                "case": case_id,
                "variant": variant,
                "error": None,
                "answer": None,
                "correct": False,
            }
            try:
                if variant == "raw":
                    items = [item for entry in session.history for item in manager.unit(entry)]
                else:
                    items = manager.prepare(
                        session,
                        PROBE_RULES,
                        [],
                        [],
                        client,
                        SessionStore(case / variant / "sessions"),
                        budget,
                    )
                record["prepare_seconds"] = time.monotonic() - started
                record["estimated_input_tokens"] = manager.count(
                    {"instructions": PROBE_RULES, "input": items, "tools": []}
                )
                record["request_preserved"] = any(item.get("content") == question for item in items)
                calls = [item["call_id"] for item in items if item.get("type") == "function_call"]
                results = [
                    item["call_id"] for item in items if item.get("type") == "function_call_output"
                ]
                record["tool_pairs_valid"] = sorted(calls) == sorted(results) and len(calls) == len(
                    set(calls)
                )
                dump(case / variant / "input.json", {"instructions": PROBE_RULES, "input": items})
                answer, fields = probe(client, items, expected, budget)
                record.update(
                    answer=answer,
                    correct=fields == expected,
                    correct_fields=sum(fields[k] == v for k, v in expected.items()),
                    total_fields=len(expected),
                )
            except Exception as exc:  # noqa: BLE001 - every failure remains in experiment results
                record["error"] = runtime.redact(str(exc))
            record.update(
                seconds=time.monotonic() - started,
                probe_usage=usage(trace, "probe"),
                metrics=trace.metrics,
            )
            record["summary_calls"] = sum(
                e["event"] == "model_requested" and e.get("purpose") == "compaction"
                for e in events(trace)
            )
            dump(case / variant / "result.json", record)
            rows.append(record)
            dump(output / "rows.json", rows)
            print(
                json.dumps(
                    {
                        k: record.get(k)
                        for k in ("case", "variant", "correct", "error", "summary_calls")
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return rows


def extract_steps(root, config, statements, label):
    root.mkdir(parents=True, exist_ok=True)
    session = Session.create(root)
    store = MemoryStore(root / "memory.json", str)
    trace = Trace(root / "trace.jsonl", str)
    client = ResponsesClient(config, trace)
    context = ContextManager(config)
    stages = []
    for text in statements:
        session.request_start = len(session.history)
        session.user(text)
        try:
            applied = store.extract(session, client, context, Budget(120))
            stage = {"applied": applied, "error": None, "stored": store.load()}
        except Exception as exc:  # noqa: BLE001 - do not hide extraction failures
            stage = {"applied": [], "error": str(exc), "stored": store.load()}
        stages.append(stage)
    dump(
        root / "setup.json",
        {
            "label": label,
            "user_history": session.history,
            "stages": stages,
            "metrics": trace.metrics,
        },
    )
    return store, stages


def memory_suite(output, config):
    if (output / "rows.json").exists():
        raise ValueError(
            "Choose a fresh output directory; existing measurements are not overwritten"
        )
    rows = []
    prepared = {
        name: extract_steps(output / name / "setup", config, statements, name)
        for name, statements, _question, _expected in MEMORY_CASES
    }
    noise_sources = {
        "language": "money_unit",
        "report_order": "money_unit",
        "money_unit": "language",
        "reference": "report_order",
        "correction": "reference",
        "forget": "language",
    }
    for name, _statements, question, expected in MEMORY_CASES:
        case = output / name
        correct, stages = prepared[name]
        unrelated, unrelated_stages = prepared[noise_sources[name]]
        noise_ok = bool(unrelated.load()) and all(not step["error"] for step in unrelated_stages)
        setup_ok = bool(stages[0]["stored"]) and all(not step["error"] for step in stages)
        if name == "forget":
            setup_ok = setup_ok and not correct.load()
        for variant in ("on", "off", "irrelevant"):
            trace = Trace(case / variant / "trace.jsonl", str)
            client = ResponsesClient(config, trace)
            context = ContextManager(config)
            # No old transcript crosses into this new Session.
            session = Session.create(case / "query-workspace")
            session.user(question)
            record = {
                "case": name,
                "variant": variant,
                "setup_ok": setup_ok
                if variant == "on"
                else noise_ok
                if variant == "irrelevant"
                else True,
                "noise_source": noise_sources[name] if variant == "irrelevant" else None,
                "expected": expected,
                "answer": None,
                "correct": False,
                "error": None,
                "recall_error": None,
            }
            start = time.monotonic()
            budget = Budget(120)
            selected = []
            try:
                if variant == "irrelevant" and not noise_ok:
                    raise ValueError(
                        "Unrelated memory was not prepared; this is not a valid noise contrast"
                    )
                if variant != "off":
                    store = correct if variant == "on" else unrelated
                    try:
                        selected = store.recall(question, client, context, budget)
                    except Exception as exc:  # noqa: BLE001 - retain recall failure
                        record["recall_error"] = str(exc)
                record["recall_seconds"] = time.monotonic() - start
                items = context.items(session, selected)
                dump(
                    case / variant / "input.json",
                    {
                        "instructions": PROBE_RULES,
                        "input": items,
                        "selected_ids": [x["id"] for x in selected],
                    },
                )
                answer, fields = probe(client, items, ["value"], budget)
                record.update(answer=answer, correct=fields["value"] == expected)
            except Exception as exc:  # noqa: BLE001 - retain every query failure
                record["error"] = str(exc)
            record.update(
                seconds=time.monotonic() - start,
                selected_ids=[x["id"] for x in selected],
                probe_usage=usage(trace, "probe"),
                metrics=trace.metrics,
            )
            dump(case / variant / "result.json", record)
            rows.append(record)
            dump(output / "rows.json", rows)
            print(
                json.dumps(
                    {
                        k: record[k]
                        for k in ("case", "variant", "correct", "setup_ok", "recall_error", "error")
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return rows


def summarize(root):
    summary = {}
    path = root / "context/rows.json"
    if path.exists():
        rows = json.loads(path.read_text())
        pairs = []
        for case in sorted({row["case"] for row in rows}):
            matched = {r["variant"]: r for r in rows if r["case"] == case}
            if all(
                v in matched and isinstance(matched[v]["probe_usage"].get("input_tokens"), int)
                for v in ["raw", "managed"]
            ):
                pairs.append(
                    (
                        matched["raw"]["probe_usage"]["input_tokens"],
                        matched["managed"]["probe_usage"]["input_tokens"],
                    )
                )
        summary["context"] = {
            "cases_with_paired_usage": len(pairs),
            "mean_raw_input_tokens": statistics.mean(a for a, b in pairs) if pairs else None,
            "mean_managed_input_tokens": statistics.mean(b for a, b in pairs) if pairs else None,
            "micro_input_reduction": 1 - sum(b for a, b in pairs) / sum(a for a, b in pairs)
            if pairs
            else None,
            "variants": {
                v: {
                    "runs": sum(r["variant"] == v for r in rows),
                    "correct": sum(r["correct"] for r in rows if r["variant"] == v),
                    "errors": sum(bool(r["error"]) for r in rows if r["variant"] == v),
                    "request_preserved": sum(
                        bool(r.get("request_preserved")) for r in rows if r["variant"] == v
                    ),
                    "tool_pairs_valid": sum(
                        bool(r.get("tool_pairs_valid")) for r in rows if r["variant"] == v
                    ),
                    "summary_calls": sum(r["summary_calls"] for r in rows if r["variant"] == v),
                    "reported_all_input_tokens": sum(
                        r["metrics"]["input_tokens"] or 0 for r in rows if r["variant"] == v
                    ),
                    "reported_all_output_tokens": sum(
                        r["metrics"]["output_tokens"] or 0 for r in rows if r["variant"] == v
                    ),
                }
                for v in ("raw", "managed")
            },
        }
    path = root / "memory/rows.json"
    if path.exists():
        rows = json.loads(path.read_text())
        summary["memory"] = {
            v: {
                "runs": sum(r["variant"] == v for r in rows),
                "correct": sum(r["correct"] for r in rows if r["variant"] == v),
                "setup_failures": sum(not r["setup_ok"] for r in rows if r["variant"] == v),
                "recall_errors": sum(bool(r["recall_error"]) for r in rows if r["variant"] == v),
                "query_errors": sum(bool(r["error"]) for r in rows if r["variant"] == v),
            }
            for v in ("on", "off", "irrelevant")
        }
    path = root / "recovery/rows.json"
    if path.exists():
        rows = json.loads(path.read_text())
        resumable = [
            row
            for row in rows
            if row["case"]
            in {
                "pending_before_execution",
                "running_before_mutation",
                "replace_before_result",
                "external_edit_on_recovery",
            }
        ]
        summary["recovery"] = {
            "scenarios": len(rows),
            "met_expected_conditions": sum(row["passed"] for row in rows),
            "resume_scenarios": len(resumable),
            "automatic_completions": sum(
                row.get("automatic_completion", False) for row in resumable
            ),
            "expected_safe_stops": sum(row.get("expected_safe_stop", False) for row in resumable),
        }
    dump(root / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fixed module experiments; context/memory call a real model."
    )
    parser.add_argument("--suite", choices=["context", "memory", "recovery", "all"], required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    project = Path(__file__).resolve().parents[1]
    load_env(project)
    config = Config.from_env(
        mode="ask", repo_map_enabled=False, compaction_trigger_tokens=12000, output_tokens=4096
    )
    root = (args.output or project / ".tero/experiments" / uuid.uuid4().hex).resolve()
    root.mkdir(parents=True, exist_ok=True)
    metadata = asdict(config)
    metadata.pop("api_key")
    dump(
        root / "protocol.json",
        {
            "format": "tero-module-experiments-2",
            "runtime_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=project, text=True
            ).strip(),
            "config": metadata,
            "repetitions": 1,
            "context_cases": 12,
            "memory_cases": 6,
            "recovery_cases": 10,
            "method": "First pass. All failures retained. No retries of experiment rows. Provider transient retries remain enabled.",
            "context_metric": "1-sum(managed probe input tokens)/sum(raw probe input tokens), paired available usage only; correctness includes all rows.",
            "memory_metric": "Exact answer accuracy on fresh sessions; no old transcript. UNKNOWN required for unavailable facts.",
        },
    )
    print("OUTPUT " + str(root), flush=True)
    if args.suite in ("context", "all"):
        context_suite(root / "context", config)
    if args.suite in ("memory", "all"):
        memory_suite(root / "memory", config)
    if args.suite in ("recovery", "all"):
        from .module_recovery import run_suite

        run_suite(root / "recovery")
    print(json.dumps(summarize(root), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
