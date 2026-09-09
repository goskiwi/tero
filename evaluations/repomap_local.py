"""Local lexical-only versus full RepoMap ranking; no model calls."""
import argparse
import json
import statistics
import time
from pathlib import Path

from tero.repo_map import RepoMap, RepoMapQuery, RankedSymbol, _lexical_scores, count_tokens
from tero.storage import save_json


def lexical_query(engine, text):
    snapshot = engine.refresh()
    scores, reasons = _lexical_scores(snapshot.symbols, text)
    maximum = max(scores.values(), default=1) or 1
    ranked = []
    for key, symbol in snapshot.symbols.items():
        lexical = scores[key] / maximum
        if not symbol.is_renderable or lexical <= 0:
            continue
        boost = .02 if symbol.kind in {'class', 'function'} else 0
        ranked.append(RankedSymbol(symbol, .62 * lexical + boost, lexical, 0,
                                   tuple(reasons.get(key, ()))))
    ranked.sort(key=lambda r: (-r.score, r.symbol.path, r.symbol.line, r.symbol.qualified_name))
    return RepoMapQuery(text, tuple(ranked), snapshot)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    protocol = json.loads((args.source_run / 'protocol.json').read_text())
    source = args.source_run / 'source'
    # Initialize tokenizer once, outside rank/parse timing for both variants.
    count_tokens('initialize')
    rows = []
    for name, task, checks in protocol['cases']:
        text = task + '\n' + '\n'.join(description for _, description, _, _ in checks)
        expected_files = {path for _, _, path, _ in checks}
        expected_symbols = {(path, symbol) for _, _, path, symbol in checks}
        for variant in ['lexical', 'repomap']:
            engine = RepoMap(source)
            started = time.perf_counter()
            snapshot = engine.refresh()
            build_ms = (time.perf_counter() - started) * 1000
            timings = []
            for _ in range(7):
                started = time.perf_counter()
                query = lexical_query(engine, text) if variant == 'lexical' else engine.query(text)
                rendered = query.render(budget_tokens=1200)
                timings.append((time.perf_counter() - started) * 1000)
            top_files = rendered.details['selected_files'][:5]
            visible = {(s['path'], s['qualified_name'].rsplit('.', 1)[-1])
                       for s in rendered.details['selected_symbols']}
            row = dict(case=name, variant=variant, query=text,
                       expected_files=sorted(expected_files), top5_files=top_files,
                       file_hits=len(expected_files & set(top_files)), file_total=len(expected_files),
                       expected_symbols=sorted(expected_symbols),
                       symbol_hits=len(expected_symbols & visible), symbol_total=len(expected_symbols),
                       missing_symbols=sorted(expected_symbols - visible),
                       build_ms=build_ms, median_query_render_ms=statistics.median(timings),
                       tokens=count_tokens(rendered.text), details=rendered.details)
            rows.append(row)
            (args.output / f'{name}-{variant}.txt').write_text(rendered.text)
    summary = {}
    for variant in ['lexical', 'repomap']:
        selected = [r for r in rows if r['variant'] == variant]
        summary[variant] = {key: sum(r[key] for r in selected)
                           for key in ['file_hits', 'file_total', 'symbol_hits', 'symbol_total']}
        summary[variant].update(
            median_build_ms=statistics.median(r['build_ms'] for r in selected),
            median_query_render_ms=statistics.median(r['median_query_render_ms'] for r in selected))
    save_json(args.output / 'rows.json', rows)
    save_json(args.output / 'summary.json', summary)
    save_json(args.output / 'protocol.json', {
        'source': str(source.resolve()), 'cases': protocol['cases'], 'budget_tokens': 1200,
        'method': 'Same Tree-sitter symbol pool, lexical scorer, kind boost, diversity selection and rendering; lexical arm removes graph score. Top5 files are first five distinct files in rendered output, symbols must occur in actual output. No model calls. Seven warm query+render timings per case; build uses fresh object but uncontrolled OS cache. No tuning after results.',
        'limitation': 'One small Python repository; this compares lexical-only with full RepoMap, not grep or end-to-end Agent performance.'})
    print(json.dumps(summary, indent=2))
    for r in rows:
        print(r['case'], r['variant'], f"files {r['file_hits']}/{r['file_total']}",
              f"symbols {r['symbol_hits']}/{r['symbol_total']}")

if __name__ == '__main__':
    main()
