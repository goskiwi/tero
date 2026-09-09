"""Small fixtures adapted from the original Tero's pricing and compaction scenarios."""

PRICING = {
    "inventory/__init__.py": "",
    "inventory/pricing.py": "def order_total(items):\n    return sum(price for price, quantity in items)\n",
    "inventory/report.py": "from .pricing import order_total\ndef render_total(items):\n    return f'Total: {order_total(items)} cents'\n",
    "AGENTS.md": "Keep money in integer cents. Do not modify checks.py. Preserve public function signatures.\n",
    "checks.py": (
        "from inventory.pricing import order_total\n"
        "from inventory.report import render_total\n"
        "assert order_total([(150, 2), (700, 3)]) == 2400\n"
        "assert order_total([]) == 0\n"
        "assert render_total([(150, 2)]) == 'Total: 300 cents'\n"
    ),
}
PRICING_REQUEST = "Fix order totals when quantity exceeds one. Inspect pricing and report callers. Preserve checks.py."

NORMALIZER = {
    "normalizer.py": "def normalize_label(value):\n    return value.strip().lower()\n",
    "AGENTS.md": "Only modify normalizer.py. Do not modify checks.py. Add no dependencies.\n",
    "checks.py": (
        "from normalizer import normalize_label as n\n"
        "assert n('  Priority   Queue ') == 'priority-queue'\n"
        "assert n('\\tAlpha\\nBeta\\t') == 'alpha-beta'\n"
        "assert n('Already-Hyphenated') == 'already-hyphenated'\n"
        "assert n('') == ''\n"
    ),
}
NORMALIZER_REQUEST = (
    "Fix normalize_label: strip outer whitespace, lowercase, replace each internal whitespace run "
    "with one hyphen, preserve punctuation and the signature. Only change normalizer.py; no dependencies."
)
