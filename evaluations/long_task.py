"""A real multi-file feature task, using the normal context window and an independent acceptance suite."""

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from applications.coding import run_coding
from tero.commands import run_command
from tero.config import Config, load_env
from tero.execution import Budget
from tero.provider import ResponsesClient
from tero.storage import save_json

REQUEST = """Complete the Pocket Orders application, implementing checkout, cancellation, JSON persistence,
and a usable CLI. Inspect the existing modules and preserve the public function names and signatures.
Work directly without delegation. Do not modify tests or AGENTS.md. Use only Python's standard library.
Requirements:
1. Money uses integer cents. Catalog entries contain name and price; stock is a separate sku->quantity map.
   Validate unknown SKU, empty cart, zero/negative/non-integer quantity (bool is invalid), and insufficient stock.
   Repeated SKU lines must be merged before checking available stock or calculating totals.
2. quote(catalog, lines, coupon=None) returns subtotal, discount, shipping, total (all integer cents).
   Coupons are {'kind':'fixed'|'percent','value':int}; fixed nonnegative discount capped at subtotal;
   percent is 0..100 with floor integer rounding. Invalid coupons raise ValueError.
   Shipping is 500 cents, free if the amount after discount is at least 5000 cents.
3. checkout(state, lines, coupon=None, request_id=None) returns a dict order with id, items, status='paid',
   and all quote fields. Deduct stock only after full validation. Failed operations leave state unchanged.
   state has catalog, stock, orders, requests, next_id. IDs are ORD-0001, ORD-0002, etc.
   A successful request_id retry with the same canonical cart and coupon returns the same order without
   charging stock again; a changed cart/coupon with that request_id raises ValueError without mutation.
   Reordered or split duplicate cart lines represent the same cart. Returned dicts must not alias stored orders.
4. cancel(state, order_id) returns a cancelled order, restores stock exactly once, and is safe to repeat.
   Unknown orders raise ValueError. A request_id retry after cancellation returns that cancelled order without stock changes.
5. load_state(path) / save_state(path, state) round-trip all data. Missing file raises FileNotFoundError;
   malformed JSON and malformed state raise ValueError. Validate required fields and basic field types.
   Save with a same-directory temporary file and atomic replace; invalid states must not overwrite existing files.
6. render_order(order) returns a readable string containing order id, status and total as decimal currency
   with exactly two fractional digits. This is presentation only; calculations remain integer cents.
7. python -m shop.cli --state PATH checkout --items '[{"sku":"pen","quantity":2}]'
   supports optional --coupon JSON and --request-id TEXT. Also support 'cancel ORDER_ID' and 'show ORDER_ID'.
   Successful commands print JSON and exit 0. Invalid commands or domain errors exit nonzero with a concise
   error on stderr; do not print a traceback. Only successful mutations save state; show must not rewrite it.
8. Update README with command examples and describe the module responsibilities. Do not add services,
   frameworks, databases or dependencies. Use existing Runtime verification feedback to finish the task.
"""

FILES = {
    ".gitignore": ".tero/\n__pycache__/\n*.pyc\n",
    "AGENTS.md": "Use integer cents. Standard library only. Preserve function signatures. Do not modify tests or this file.\n",
    "README.md": "# Pocket Orders\n\nA small unfinished local order application.\n",
    "shop/__init__.py": '"""Pocket Orders."""\n',
    "shop/catalog.py": "def validate_lines(catalog, lines):\n    return lines\n",
    "shop/pricing.py": "def quote(catalog, lines, coupon=None):\n    raise NotImplementedError\n",
    "shop/inventory.py": "def reserve(stock, items):\n    raise NotImplementedError\n\ndef restore(stock, items):\n    raise NotImplementedError\n",
    "shop/orders.py": "def checkout(state, lines, coupon=None, request_id=None):\n    raise NotImplementedError\n\ndef cancel(state, order_id):\n    raise NotImplementedError\n",
    "shop/storage.py": "def load_state(path):\n    raise NotImplementedError\n\ndef save_state(path, state):\n    raise NotImplementedError\n",
    "shop/report.py": "def render_order(order):\n    return str(order)\n",
    "shop/cli.py": 'def main():\n    raise NotImplementedError\n\nif __name__ == "__main__":\n    main()\n',
}

CHECKS = r"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from shop.pricing import quote
from shop.orders import checkout, cancel
from shop.storage import save_state, load_state
from shop.report import render_order


def state():
    return {'catalog': {'pen': {'name': 'Pen', 'price': 150}, 'book': {'name': 'Book', 'price': 2700}},
            'stock': {'pen': 10, 'book': 5}, 'orders': {}, 'requests': {}, 'next_id': 1}


def cart(sku='pen', quantity=2):
    return [{'sku': sku, 'quantity': quantity}]


class Pricing(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(quote(state()['catalog'], cart()), {'subtotal':300,'discount':0,'shipping':500,'total':800})
    def test_duplicates(self):
        self.assertEqual(quote(state()['catalog'], cart()+cart())['subtotal'],600)
    def test_free_shipping(self):
        self.assertEqual(quote(state()['catalog'],cart('book',2))['shipping'],0)
    def test_discount_shipping_threshold(self):
        self.assertEqual(quote(state()['catalog'],cart('book',2),{'kind':'fixed','value':500}),
                         {'subtotal':5400,'discount':500,'shipping':500,'total':5400})
    def test_percent_floor(self):
        self.assertEqual(quote(state()['catalog'],cart('pen',1),{'kind':'percent','value':33})['discount'],49)
    def test_fixed_cap(self):
        self.assertEqual(quote(state()['catalog'],cart(),{'kind':'fixed','value':9999})['total'],500)
    def test_invalid_lines(self):
        for lines in [[], cart('missing'), cart(quantity=0), cart(quantity=-1),cart(quantity=1.5),cart(quantity=True)]:
            with self.subTest(lines=lines), self.assertRaises(ValueError): quote(state()['catalog'],lines)
    def test_invalid_coupons(self):
        for coupon in [{'kind':'other','value':1},{'kind':'percent','value':101},{'kind':'fixed','value':-1},
                       {'kind':'percent','value':True},{'kind':'fixed','value':1.2}]:
            with self.subTest(coupon=coupon),self.assertRaises(ValueError):quote(state()['catalog'],cart(),coupon)


class Orders(unittest.TestCase):
    def test_checkout(self):
        s=state();o=checkout(s,cart())
        self.assertEqual((o['id'],o['status'],o['total'],s['stock']['pen']),('ORD-0001','paid',800,8))
        self.assertEqual(checkout(s,cart())['id'],'ORD-0002')
    def test_aggregate_stock_atomic(self):
        s=state(); before=copy.deepcopy(s)
        with self.assertRaises(ValueError):checkout(s,cart(quantity=6)+cart(quantity=6))
        self.assertEqual(s,before)
    def test_invalid_second_line_atomic(self):
        s=state();before=copy.deepcopy(s)
        with self.assertRaises(ValueError):checkout(s,cart()+cart('missing'))
        self.assertEqual(s,before)
    def test_invalid_coupon_atomic(self):
        s=state();before=copy.deepcopy(s)
        with self.assertRaises(ValueError):checkout(s,cart(),{'kind':'percent','value':101})
        self.assertEqual(s,before)
    def test_retry(self):
        s=state();o=checkout(s,cart(),request_id='r');before=copy.deepcopy(s)
        self.assertEqual(checkout(s,cart(),request_id='r'),o);self.assertEqual(s,before)
    def test_canonical_retry(self):
        s=state();o=checkout(s,cart('pen',1)+cart('book',1)+cart('pen',1),request_id='r')
        before=copy.deepcopy(s)
        self.assertEqual(checkout(s,cart('book',1)+cart('pen',2),request_id='r'),o)
        self.assertEqual(s,before)
    def test_changed_request_rejected(self):
        for lines,coupon in [(cart(quantity=3),None),(cart(),{'kind':'fixed','value':1})]:
            s=state();checkout(s,cart(),request_id='r');before=copy.deepcopy(s)
            with self.assertRaises(ValueError):checkout(s,lines,coupon,request_id='r')
            self.assertEqual(s,before)
    def test_result_no_alias(self):
        s=state();o=checkout(s,cart(),request_id='r');o['status']='corrupted';o['items'].clear()
        current=checkout(s,cart(),request_id='r')
        self.assertEqual(current['status'],'paid');self.assertTrue(current['items'])
    def test_cancel_once(self):
        s=state();o=checkout(s,cart());self.assertEqual(cancel(s,o['id'])['status'],'cancelled')
        before=copy.deepcopy(s);cancel(s,o['id']);self.assertEqual(s,before);self.assertEqual(s['stock']['pen'],10)
    def test_missing_cancel(self):
        s=state();before=copy.deepcopy(s)
        with self.assertRaises(ValueError):cancel(s,'missing')
        self.assertEqual(s,before)
    def test_retry_cancelled(self):
        s=state();o=checkout(s,cart(),request_id='r');cancel(s,o['id']);before=copy.deepcopy(s)
        self.assertEqual(checkout(s,cart(),request_id='r')['status'],'cancelled');self.assertEqual(s,before)
    def test_display(self):
        o=checkout(state(),cart());text=render_order(o)
        for value in ['ORD-0001','paid','8.00']:self.assertIn(value,text)


class Storage(unittest.TestCase):
    def test_roundtrip_and_retry(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json';s=state();o=checkout(s,cart(),request_id='r');save_state(p,s)
            loaded=load_state(p);self.assertEqual(loaded,s)
            self.assertEqual(checkout(loaded,cart(),request_id='r'),o);self.assertEqual(loaded,s)
    def test_invalid_save_preserves_file(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json';save_state(p,state());before=p.read_bytes()
            for bad in [{},dict(state(),stock=[]),dict(state(),next_id='bad')]:
                with self.assertRaises(ValueError):save_state(p,bad)
                self.assertEqual(p.read_bytes(),before)
    def test_missing_and_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json'
            with self.assertRaises(FileNotFoundError):load_state(p)
            for content in ['{','[]','{}']:
                p.write_text(content)
                with self.assertRaises(ValueError):load_state(p)


class CLI(unittest.TestCase):
    def invoke(self,p,*args):
        return subprocess.run([sys.executable,'-m','shop.cli','--state',str(p),*args],capture_output=True,text=True,timeout=10)
    def test_checkout_show_cancel(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json';save_state(p,state())
            result=self.invoke(p,'checkout','--items',json.dumps(cart()),'--request-id','r')
            self.assertEqual(result.returncode,0,result.stderr);o=json.loads(result.stdout)
            before=p.read_bytes();stamp=p.stat().st_mtime_ns
            result=self.invoke(p,'show',o['id']);self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['id'],o['id'])
            self.assertEqual((p.read_bytes(),p.stat().st_mtime_ns),(before,stamp))
            result=self.invoke(p,'cancel',o['id']);self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(load_state(p)['stock']['pen'],10)
    def test_error_does_not_save(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json';save_state(p,state());before=p.read_bytes()
            for args in [('checkout','--items','bad'),('checkout','--items',json.dumps(cart('missing'))),('show','missing')]:
                result=self.invoke(p,*args);self.assertNotEqual(result.returncode,0)
                self.assertTrue(result.stderr);self.assertNotIn('Traceback',result.stderr)
                self.assertEqual(p.read_bytes(),before)
"""


def main():
    parser = argparse.ArgumentParser(description="Real long-task / context-management comparison")
    parser.add_argument("--compaction-trigger-tokens", type=int, default=0)
    args = parser.parse_args()
    context_checks = []

    class AuditedClient(ResponsesClient):
        def request(self, instructions, items, tools, budget, **options):
            if options.get("purpose", "main") == "main":
                calls = [item["call_id"] for item in items if item.get("type") == "function_call"]
                outputs = [
                    item["call_id"] for item in items if item.get("type") == "function_call_output"
                ]
                context_checks.append(
                    {
                        "tool_pairs_complete": sorted(calls) == sorted(outputs)
                        and len(calls) == len(set(calls)),
                        "original_request_present": any(
                            item.get("role") == "user" and item.get("content") == REQUEST
                            for item in items
                        ),
                    }
                )
            return super().request(instructions, items, tools, budget, **options)

    project = Path(__file__).resolve().parents[1]
    load_env(project)
    root = Path(tempfile.mkdtemp(prefix="tero-long-orders-")).resolve()
    files = {**FILES, "tests/test_orders.py": CHECKS}
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for git_args in [
        ("init", "-q"),
        ("add", "."),
        (
            "-c",
            "user.name=Tero Evaluation",
            "-c",
            "user.email=evaluation@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "Initial exercise",
        ),
    ]:
        subprocess.run(["git", *git_args], cwd=root, check=True, capture_output=True)
    command = f"{shlex.quote(sys.executable)} -m unittest discover -s tests -v"
    initial = run_command(command, root, 60, Budget(60))
    print(
        json.dumps({"workspace": str(root), "initial_exit_code": initial["exit_code"]}), flush=True
    )
    config = Config.from_env(
        mode="auto",
        max_turns=64,
        runtime_seconds=900,
        verify_command=command,
        memory_enabled=False,
        compaction_trigger_tokens=args.compaction_trigger_tokens,
    )
    result, delivery = run_coding(root, REQUEST, config, client_factory=AuditedClient)
    acceptance = Path(tempfile.mkdtemp(prefix="tero-long-acceptance-")).resolve()
    shutil.copytree(
        root / "shop", acceptance / "shop", ignore=shutil.ignore_patterns("__pycache__")
    )
    (acceptance / "tests").mkdir()
    (acceptance / "tests/test_orders.py").write_text(CHECKS)
    accepted = run_command(command, acceptance, 60, Budget(60))
    unchanged = (root / "tests/test_orders.py").read_text() == CHECKS and (
        root / "AGENTS.md"
    ).read_text() == FILES["AGENTS.md"]
    passed = (
        result.status == "completed"
        and accepted["exit_code"] == 0
        and not accepted["stop_reason"]
        and unchanged
    )
    trace_path = delivery.parent / "trace.jsonl"
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    compactions = [event for event in events if event["event"] == "compacted"]
    context_correct = (
        bool(context_checks)
        and all(
            check["tool_pairs_complete"] and check["original_request_present"]
            for check in context_checks
        )
        and all(event["after_tokens"] < event["before_tokens"] for event in compactions)
    )
    if args.compaction_trigger_tokens:
        context_correct = context_correct and bool(compactions)
    passed = passed and context_correct
    report = {
        "passed": passed,
        "model": config.model,
        "context_tokens": config.context_tokens,
        "compaction_trigger_tokens": config.compaction_trigger_tokens,
        "context_checks": context_checks,
        "compactions": compactions,
        "context_correct": context_correct,
        "max_turns": config.max_turns,
        "runtime_seconds": config.runtime_seconds,
        "memory_enabled": False,
        "workspace": str(root),
        "delivery": str(delivery),
        "request": REQUEST,
        "result": result.__dict__,
        "initial_checks": initial,
        "independent_acceptance": accepted,
        "protected_files_unchanged": unchanged,
    }
    destination = (
        project
        / ".tero/evaluations"
        / ("long-orders-compressed" if args.compaction_trigger_tokens else "long-orders-normal")
    )
    shutil.copytree(root / ".tero", destination / "runtime", dirs_exist_ok=True)
    save_json(destination / "evaluation.json", report)
    print(
        json.dumps(
            {
                "passed": passed,
                "report": str(destination / "evaluation.json"),
                "result": result.__dict__,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
