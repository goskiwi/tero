"""Task-aware Python repository map built from a tree-sitter symbol graph."""

from __future__ import annotations

import os
import re
import stat
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

import tiktoken
import tree_sitter_python
from tree_sitter import Language, Parser

IGNORED_PATH_NAMES = {
    ".git",
    ".tero",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
}

REPO_MAP_MAX_FILES = 2000
REPO_MAP_MAX_FILE_BYTES = 512_000
REPO_MAP_SCAN_MAX_ENTRIES = 20_000
REPO_MAP_SCAN_TIMEOUT_SECONDS = 2.0
REPO_MAP_PAGE_RANK_ITERATIONS = 32
REPO_MAP_DAMPING = 0.85


def count_tokens(text):
    return len(tiktoken.get_encoding("o200k_base").encode(str(text or ""), disallowed_special=()))


def _token_clip(text, token_budget, *, token_counter=count_tokens):
    text = str(text or "")
    budget = max(0, int(token_budget))
    if token_counter(text) <= budget:
        return text
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if token_counter(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip()


PYTHON_LANGUAGE = Language(tree_sitter_python.language())
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUERY_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]*|[\u4e00-\u9fff]{2,}")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WHITESPACE_RE = re.compile(r"\s+")
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "code",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "please",
        "project",
        "repository",
        "repo",
        "the",
        "this",
        "to",
        "with",
        "修改",
        "代码",
        "仓库",
        "实现",
        "工程",
        "项目",
        "文件",
        "检查",
        "请",
    }
)
_REFERENCE_WEIGHTS = {
    "call": 1.0,
    "import": 0.8,
    "import_module": 0.65,
    "inherit": 1.35,
    "contains": 0.18,
    "test": 1.15,
}
_REPO_MAP_IGNORED_PARTS = frozenset(
    {
        *IGNORED_PATH_NAMES,
        "artifacts",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "vendor",
    }
)
_BUILTIN_CALLS = frozenset(
    {
        "all",
        "any",
        "append",
        "bool",
        "bytes",
        "dict",
        "enumerate",
        "extend",
        "filter",
        "float",
        "format",
        "get",
        "getattr",
        "hasattr",
        "hash",
        "id",
        "int",
        "isinstance",
        "issubclass",
        "items",
        "iter",
        "keys",
        "len",
        "list",
        "lower",
        "map",
        "max",
        "min",
        "next",
        "object",
        "open",
        "read_text",
        "print",
        "property",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "setattr",
        "slice",
        "split",
        "sorted",
        "str",
        "strip",
        "sum",
        "super",
        "tuple",
        "type",
        "update",
        "upper",
        "values",
        "vars",
        "write_text",
        "zip",
    }
)


@dataclass(frozen=True)
class Symbol:
    """One definition that can be ranked and rendered into model context."""

    symbol_id: str
    path: str
    name: str
    qualified_name: str
    kind: str
    line: int
    end_line: int
    signature: str

    @property
    def is_renderable(self):
        return self.kind != "module"


@dataclass(frozen=True)
class Reference:
    """Unresolved relation emitted while parsing one file."""

    source_id: str
    target: str
    kind: str
    target_is_id: bool = False


@dataclass(frozen=True)
class ParsedFile:
    fingerprint: tuple[int, int, int]
    symbols: tuple[Symbol, ...]
    references: tuple[Reference, ...]
    has_parse_error: bool


@dataclass(frozen=True)
class RepoSnapshot:
    symbols: dict[str, Symbol]
    edges: dict[str, dict[str, float]]
    scan_truncated: bool
    parsed_files: int
    skipped_files: int
    parse_error_files: int
    cache_hits: int
    cache_misses: int

    @property
    def edge_count(self):
        return sum(len(targets) for targets in self.edges.values())


@dataclass(frozen=True)
class RankedSymbol:
    symbol: Symbol
    score: float
    lexical_score: float
    graph_score: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderedRepoMap:
    text: str
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RepoMapQuery:
    query: str
    ranked: tuple[RankedSymbol, ...]
    snapshot: RepoSnapshot

    def render(self, budget_tokens=1600, max_results=24, *, token_counter=None):
        """Render a diverse, deterministic symbol selection within a token budget."""
        budget_tokens = max(0, int(budget_tokens))
        max_results = max(1, int(max_results))
        token_counter = token_counter or count_tokens
        if budget_tokens == 0:
            return RenderedRepoMap(
                text="",
                details=self._details((), truncated=bool(self.ranked)),
            )

        header = "Repository map (task-ranked Python signatures; use read_file for details):"
        if not self.ranked:
            text = _token_clip(
                header + "\n- no Python symbols found",
                budget_tokens,
                token_counter=token_counter,
            )
            return RenderedRepoMap(
                text=text,
                details=self._details((), truncated=False),
            )

        selected = self._select_diverse(max_results)
        accepted = []
        lines = [header]
        current_path = None
        for item in selected:
            symbol = item.symbol
            additions = []
            if symbol.path != current_path:
                additions.append(symbol.path)
            additions.append(
                f"  L{symbol.line} {symbol.kind} {symbol.qualified_name} — {symbol.signature}"
            )
            candidate = "\n".join([*lines, *additions])
            if token_counter(candidate) > budget_tokens:
                continue
            lines.extend(additions)
            accepted.append(item)
            current_path = symbol.path

        if not accepted:
            text = _token_clip(
                header + "\n- omitted: repo-map budget is too small",
                budget_tokens,
                token_counter=token_counter,
            )
        else:
            text = "\n".join(lines)
        truncated = len(accepted) < len(self.ranked)
        accepted_tuple = tuple(accepted)
        return RenderedRepoMap(
            text=text,
            details=self._details(accepted_tuple, truncated=truncated),
        )

    def _select_diverse(self, max_results):
        remaining = list(self.ranked)
        selected = []
        file_counts = defaultdict(int)
        while remaining and len(selected) < max_results:
            best = max(
                remaining,
                key=lambda item: (
                    item.score / (1.0 + 0.35 * file_counts[item.symbol.path]),
                    item.score,
                    -item.symbol.line,
                    item.symbol.path,
                    item.symbol.qualified_name,
                ),
            )
            remaining.remove(best)
            selected.append(best)
            file_counts[best.symbol.path] += 1
        return selected

    def _details(self, selected, *, truncated):
        return {
            "query": self.query,
            "graph_nodes": len(self.snapshot.symbols),
            "graph_edges": self.snapshot.edge_count,
            "scan_truncated": self.snapshot.scan_truncated,
            "parsed_files": self.snapshot.parsed_files,
            "skipped_files": self.snapshot.skipped_files,
            "parse_error_files": self.snapshot.parse_error_files,
            "cache_hits": self.snapshot.cache_hits,
            "cache_misses": self.snapshot.cache_misses,
            "selected_count": len(selected),
            "selected_files": list(dict.fromkeys(item.symbol.path for item in selected)),
            "selected_symbols": [
                {
                    "path": item.symbol.path,
                    "line": item.symbol.line,
                    "end_line": item.symbol.end_line,
                    "kind": item.symbol.kind,
                    "qualified_name": item.symbol.qualified_name,
                    "score": round(item.score, 6),
                    "lexical_score": round(item.lexical_score, 6),
                    "graph_score": round(item.graph_score, 6),
                    "reasons": list(item.reasons),
                }
                for item in selected
            ],
            "truncated": bool(truncated),
        }


class RepoMap:
    """Incremental Python symbol graph with task-personalized ranking."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self._parser = Parser(PYTHON_LANGUAGE)
        self._file_cache: dict[str, ParsedFile] = {}
        self._snapshot_cache: RepoSnapshot | None = None

    def query(self, query, *, check_active=lambda: None):
        check_active()
        snapshot = self.refresh(check_active=check_active)
        lexical_scores, reasons = _lexical_scores(
            snapshot.symbols, query, check_active=check_active
        )
        personalization = _personalization(snapshot.symbols, lexical_scores)
        graph_scores = _personalized_page_rank(snapshot, personalization, check_active=check_active)
        max_graph = max(graph_scores.values(), default=1.0) or 1.0
        max_lexical = max(lexical_scores.values(), default=1.0) or 1.0

        ranked = []
        for symbol_id, symbol in snapshot.symbols.items():
            check_active()
            if not symbol.is_renderable:
                continue
            lexical = lexical_scores.get(symbol_id, 0.0) / max_lexical
            graph = graph_scores.get(symbol_id, 0.0) / max_graph
            kind_boost = (
                0.02 if (lexical > 0 or graph > 0) and symbol.kind in {"class", "function"} else 0.0
            )
            score = 0.62 * lexical + 0.36 * graph + kind_boost
            if score <= 0:
                continue
            ranked.append(
                RankedSymbol(
                    symbol=symbol,
                    score=score,
                    lexical_score=lexical,
                    graph_score=graph,
                    reasons=tuple(reasons.get(symbol_id, ())),
                )
            )
        ranked.sort(
            key=lambda item: (
                -item.score,
                item.symbol.path,
                item.symbol.line,
                item.symbol.qualified_name,
            )
        )
        return RepoMapQuery(query=str(query), ranked=tuple(ranked), snapshot=snapshot)

    def render(
        self,
        query,
        *,
        budget_tokens=1600,
        max_results=24,
        token_counter=None,
    ):
        return self.query(query).render(
            budget_tokens=budget_tokens,
            max_results=max_results,
            token_counter=token_counter,
        )

    def refresh(self, *, check_active=lambda: None):
        paths, skipped_files, scan_truncated = self._python_paths(check_active=check_active)
        active_paths = {path.as_posix() for path in paths}
        cache_changed = False
        for cached_path in tuple(self._file_cache):
            if cached_path not in active_paths:
                del self._file_cache[cached_path]
                self._snapshot_cache = None
                cache_changed = True

        parsed_files = []
        cache_hits = 0
        cache_misses = 0
        for relative_path in paths:
            check_active()
            absolute_path = self.root / relative_path
            try:
                stat = absolute_path.stat()
            except OSError:
                self._file_cache.pop(relative_path.as_posix(), None)
                self._snapshot_cache = None
                skipped_files += 1
                cache_changed = True
                continue
            fingerprint = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
            cached = self._file_cache.get(relative_path.as_posix())
            if cached is not None and cached.fingerprint == fingerprint:
                parsed = cached
                cache_hits += 1
            else:
                try:
                    parsed = self._parse_file(relative_path, fingerprint, check_active=check_active)
                except (OSError, RecursionError):
                    self._file_cache.pop(relative_path.as_posix(), None)
                    self._snapshot_cache = None
                    skipped_files += 1
                    cache_changed = True
                    continue
                self._file_cache[relative_path.as_posix()] = parsed
                # Cancellation may interrupt graph rebuilding after parsing succeeded.
                self._snapshot_cache = None
                cache_misses += 1
                cache_changed = True
            parsed_files.append(parsed)

        if not cache_changed and self._snapshot_cache is not None:
            cached_snapshot = self._snapshot_cache
            return RepoSnapshot(
                symbols=cached_snapshot.symbols,
                edges=cached_snapshot.edges,
                scan_truncated=scan_truncated,
                parsed_files=len(parsed_files),
                skipped_files=skipped_files,
                parse_error_files=cached_snapshot.parse_error_files,
                cache_hits=cache_hits,
                cache_misses=cache_misses,
            )

        symbols = {symbol.symbol_id: symbol for parsed in parsed_files for symbol in parsed.symbols}
        references = [reference for parsed in parsed_files for reference in parsed.references]
        edges = _resolve_graph(symbols, references, check_active=check_active)
        snapshot = RepoSnapshot(
            symbols=symbols,
            edges=edges,
            scan_truncated=scan_truncated,
            parsed_files=len(parsed_files),
            skipped_files=skipped_files,
            parse_error_files=sum(parsed.has_parse_error for parsed in parsed_files),
            cache_hits=cache_hits,
            cache_misses=cache_misses,
        )
        self._snapshot_cache = snapshot
        return snapshot

    def _python_paths(self, *, check_active=lambda: None):
        candidates = []
        skipped = 0
        scanned = 0
        truncated = False
        started = time.monotonic()
        queue = deque([(self.root, Path("."))])
        while queue:
            check_active()
            if (
                scanned >= REPO_MAP_SCAN_MAX_ENTRIES
                or time.monotonic() - started >= REPO_MAP_SCAN_TIMEOUT_SECONDS
            ):
                truncated = True
                break
            absolute, relative = queue.popleft()
            try:
                with os.scandir(absolute) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name.lower())
            except OSError:
                skipped += 1
                continue
            for entry in entries:
                check_active()
                if (
                    scanned >= REPO_MAP_SCAN_MAX_ENTRIES
                    or time.monotonic() - started >= REPO_MAP_SCAN_TIMEOUT_SECONDS
                ):
                    truncated = True
                    break
                scanned += 1
                if entry.name in _REPO_MAP_IGNORED_PARTS:
                    skipped += 1
                    continue
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    skipped += 1
                    continue
                child = Path(entry.name) if relative == Path(".") else relative / entry.name
                if stat.S_ISLNK(metadata.st_mode):
                    skipped += 1
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    queue.append((Path(entry.path), child))
                    continue
                if not stat.S_ISREG(metadata.st_mode) or child.suffix != ".py":
                    continue
                if metadata.st_size > REPO_MAP_MAX_FILE_BYTES:
                    skipped += 1
                    continue
                if len(candidates) >= REPO_MAP_MAX_FILES:
                    skipped += 1
                    truncated = True
                    continue
                candidates.append(child)
        return sorted(candidates), skipped, truncated

    def _parse_file(self, relative_path, fingerprint, *, check_active=lambda: None):
        check_active()
        source = (self.root / relative_path).read_bytes()
        tree = self._parser.parse(source)
        path_text = relative_path.as_posix()
        module_name = _module_name(relative_path)
        module_id = f"{path_text}::<module>"
        symbols = [
            Symbol(
                symbol_id=module_id,
                path=path_text,
                name=module_name.rsplit(".", 1)[-1] or relative_path.stem,
                qualified_name=module_name,
                kind="module",
                line=1,
                end_line=max(1, source.count(b"\n") + 1),
                signature=f"module {module_name}",
            )
        ]
        references = []

        pending = [(tree.root_node, module_id, (), "module")]
        while pending:
            check_active()
            node, current_id, scope, parent_kind = pending.pop()
            if node.type in {"class_definition", "function_definition"}:
                name_node = node.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(source, name_node)
                qualified_name = ".".join((*scope, name)) if scope else name
                kind = (
                    "class"
                    if node.type == "class_definition"
                    else "method"
                    if parent_kind == "class"
                    else "function"
                )
                symbol_id = f"{path_text}::{qualified_name}"
                body_node = node.child_by_field_name("body")
                signature = _definition_signature(source, node, body_node)
                symbol = Symbol(
                    symbol_id=symbol_id,
                    path=path_text,
                    name=name,
                    qualified_name=qualified_name,
                    kind=kind,
                    line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    signature=signature,
                )
                symbols.append(symbol)
                references.append(
                    Reference(
                        source_id=current_id,
                        target=symbol_id,
                        kind="contains",
                        target_is_id=True,
                    )
                )
                if node.type == "class_definition":
                    superclasses = node.child_by_field_name("superclasses")
                    if superclasses is not None:
                        for target in _identifier_names(_node_text(source, superclasses)):
                            if target not in {"metaclass"}:
                                references.append(
                                    Reference(
                                        source_id=symbol_id,
                                        target=target,
                                        kind="inherit",
                                    )
                                )
                pending.extend(
                    (child, symbol_id, (*scope, name), kind)
                    for child in reversed(node.children)
                    if child != name_node
                )
                continue

            if node.type == "call":
                function_node = node.child_by_field_name("function")
                if function_node is not None:
                    target = _call_target(_node_text(source, function_node))
                    if target and target.rsplit(".", 1)[-1] not in _BUILTIN_CALLS:
                        references.append(
                            Reference(
                                source_id=current_id,
                                target=target,
                                kind="call",
                            )
                        )
            elif node.type in {"import_statement", "import_from_statement"}:
                references.extend(
                    _import_references(
                        current_id,
                        _node_text(source, node),
                    )
                )

            pending.extend(
                (child, current_id, scope, parent_kind) for child in reversed(node.children)
            )
        return ParsedFile(
            fingerprint=fingerprint,
            symbols=tuple(symbols),
            references=tuple(references),
            has_parse_error=bool(tree.root_node.has_error),
        )


def _node_text(source, node):
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _definition_signature(source, node, body_node):
    end_byte = body_node.start_byte if body_node is not None else node.end_byte
    header = (
        _node_text(source, node)
        if end_byte <= node.start_byte
        else source[node.start_byte : end_byte].decode("utf-8", errors="replace")
    )
    header = _WHITESPACE_RE.sub(" ", header).strip().rstrip(":").strip()
    return header[:220] + ("…" if len(header) > 220 else "")


def _module_name(path):
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or path.stem


def _identifier_names(text):
    return tuple(dict.fromkeys(_IDENTIFIER_RE.findall(text)))


def _call_target(text):
    identifiers = _identifier_names(text)
    if not identifiers:
        return ""
    return ".".join(identifiers[-2:]) if len(identifiers) > 1 else identifiers[-1]


def _import_references(source_id, text):
    compact = _WHITESPACE_RE.sub(" ", text).strip()
    references = []
    if compact.startswith("from ") and " import " in compact:
        module_text, imported_text = compact[5:].split(" import ", 1)
        module_text = module_text.strip()
        if module_text:
            references.append(
                Reference(
                    source_id,
                    module_text.lstrip("."),
                    "import_module",
                )
            )
        imported_text = imported_text.strip("() ")
        for item in imported_text.split(","):
            name = item.strip().split(" as ", 1)[0].strip()
            if name and name != "*":
                references.append(
                    Reference(
                        source_id,
                        name,
                        "import",
                    )
                )
        return references
    if compact.startswith("import "):
        for item in compact[7:].split(","):
            name = item.strip().split(" as ", 1)[0].strip()
            if name:
                references.append(
                    Reference(
                        source_id,
                        name,
                        "import_module",
                    )
                )
    return references


def _resolve_graph(symbols, references, *, check_active=lambda: None):
    edges: dict[str, dict[str, float]] = {symbol_id: {} for symbol_id in symbols}
    by_name = defaultdict(list)
    by_qualified = defaultdict(list)
    modules = defaultdict(list)
    for symbol in symbols.values():
        check_active()
        by_name[symbol.name.lower()].append(symbol)
        by_qualified[symbol.qualified_name.lower()].append(symbol)
        if symbol.kind == "module":
            modules[symbol.qualified_name.lower()].append(symbol)

    for reference in references:
        check_active()
        if reference.source_id not in symbols:
            continue
        if reference.target_is_id:
            candidates = [symbols[reference.target]] if reference.target in symbols else []
        else:
            candidates = _reference_candidates(
                symbols[reference.source_id],
                reference,
                by_name,
                by_qualified,
                modules,
            )
        if not candidates:
            continue
        weight = _REFERENCE_WEIGHTS.get(reference.kind, 0.5) / len(candidates)
        for candidate in candidates:
            _add_edge(edges, reference.source_id, candidate.symbol_id, weight)
            _add_edge(edges, candidate.symbol_id, reference.source_id, weight * 0.28)

    renderable = [symbol for symbol in symbols.values() if symbol.is_renderable]
    production_by_name = defaultdict(list)
    for symbol in renderable:
        check_active()
        if not _is_test_symbol(symbol):
            production_by_name[symbol.name.lower()].append(symbol)
    for symbol in renderable:
        check_active()
        if not _is_test_symbol(symbol):
            continue
        test_name = symbol.name.lower().removeprefix("test_")
        matches = [
            candidate
            for name, candidates in production_by_name.items()
            if len(name) >= 4 and name in test_name
            for candidate in candidates
        ][:4]
        for candidate in matches:
            _add_edge(edges, symbol.symbol_id, candidate.symbol_id, _REFERENCE_WEIGHTS["test"])
            _add_edge(
                edges, candidate.symbol_id, symbol.symbol_id, _REFERENCE_WEIGHTS["test"] * 0.28
            )
    return edges


def _reference_candidates(source, reference, by_name, by_qualified, modules):
    target = reference.target.strip(".").lower()
    if not target:
        return []
    if reference.kind == "import_module":
        exact_modules = modules.get(target, [])
        if exact_modules:
            return exact_modules[:2]
        suffix_modules = [
            symbol
            for module_name, candidates in modules.items()
            if module_name.endswith("." + target) or target.endswith("." + module_name)
            for symbol in candidates
        ]
        return suffix_modules[:2]

    exact = list(by_qualified.get(target, []))
    last_name = target.rsplit(".", 1)[-1]
    candidates = exact or list(by_name.get(last_name, []))
    if not candidates:
        return []
    same_file = [candidate for candidate in candidates if candidate.path == source.path]
    if same_file:
        candidates = same_file
    elif len(candidates) > 4 and reference.kind == "call":
        return []
    return sorted(
        candidates,
        key=lambda symbol: (
            symbol.path != source.path,
            not symbol.qualified_name.lower().endswith(target),
            symbol.path,
            symbol.line,
        ),
    )[:4]


def _add_edge(edges, source_id, target_id, weight):
    if source_id == target_id or source_id not in edges or target_id not in edges:
        return
    edges[source_id][target_id] = edges[source_id].get(target_id, 0.0) + float(weight)


def _is_test_symbol(symbol):
    path_parts = Path(symbol.path).parts
    return (
        symbol.name.startswith("test_")
        or Path(symbol.path).name.startswith("test_")
        or "tests" in path_parts
    )


def _query_tokens(query):
    tokens = []
    for raw in _QUERY_TOKEN_RE.findall(str(query)):
        stripped = raw.strip("./-")
        normalized = stripped.lower()
        if not normalized:
            continue
        pieces = re.split(r"[_./-]+", stripped)
        expanded = [normalized]
        for piece in pieces:
            expanded.extend(part.lower() for part in _CAMEL_BOUNDARY_RE.split(piece))
        for token in expanded:
            if len(token) >= 2 and token not in _STOP_WORDS and token not in tokens:
                tokens.append(token)
    return tuple(tokens)


def _lexical_scores(symbols, query, *, check_active=lambda: None):
    tokens = _query_tokens(query)
    scores = {}
    reasons = {}
    query_lower = str(query).lower()
    for symbol_id, symbol in symbols.items():
        check_active()
        name = symbol.name.lower()
        qualified = symbol.qualified_name.lower()
        path = symbol.path.lower()
        signature = symbol.signature.lower()
        score = 0.0
        matched = []
        for token in tokens:
            if token == name or token in qualified.split("."):
                score += 4.0
                matched.append(f"name:{token}")
            elif token in name or token in qualified:
                score += 2.0
                matched.append(f"symbol:{token}")
            if token in path:
                score += 2.2
                matched.append(f"path:{token}")
            if token in signature and token not in name:
                score += 0.6
                matched.append(f"signature:{token}")
        if symbol.path.lower() in query_lower:
            score += 5.0
            matched.append("exact_path")
        if _is_test_symbol(symbol) and any(
            token in {"test", "tests", "pytest", "failing", "failure", "测试", "失败"}
            for token in tokens
        ):
            score += 1.5
            matched.append("test_intent")
        scores[symbol_id] = score
        reasons[symbol_id] = tuple(dict.fromkeys(matched))
    return scores, reasons


def _personalization(symbols, lexical_scores):
    total = sum(lexical_scores.values())
    if total > 0:
        return {symbol_id: lexical_scores.get(symbol_id, 0.0) / total for symbol_id in symbols}
    uniform = 1.0 / max(1, len(symbols))
    return {symbol_id: uniform for symbol_id in symbols}


def _personalized_page_rank(snapshot, personalization, *, check_active=lambda: None):
    if not snapshot.symbols:
        return {}
    ranks = dict(personalization)
    damping = float(REPO_MAP_DAMPING)
    for _ in range(REPO_MAP_PAGE_RANK_ITERATIONS):
        check_active()
        next_ranks = {
            symbol_id: (1.0 - damping) * personalization[symbol_id]
            for symbol_id in snapshot.symbols
        }
        dangling = 0.0
        for source_id, rank in ranks.items():
            targets = snapshot.edges.get(source_id, {})
            total_weight = sum(targets.values())
            if total_weight <= 0:
                dangling += rank
                continue
            for target_id, weight in targets.items():
                next_ranks[target_id] += damping * rank * (weight / total_weight)
        if dangling:
            for symbol_id, probability in personalization.items():
                next_ranks[symbol_id] += damping * dangling * probability
        delta = sum(
            abs(next_ranks[symbol_id] - ranks.get(symbol_id, 0.0)) for symbol_id in next_ranks
        )
        ranks = next_ranks
        if delta < 1e-10:
            break
    return ranks
