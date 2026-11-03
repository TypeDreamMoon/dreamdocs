"""dreamdocs command line."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, check, markdown


def _segments(args) -> int:
    doc = markdown.parse(Path(args.file).read_text(encoding="utf-8"))
    rows = [{"n": n, "kind": s.kind, "signature": s.signature, "key": s.key, "texts": s.texts}
            for n, s in enumerate(doc.slots, 1)]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        for r in rows:
            print(f"{r['n']:4d} {r['signature']:8s} {r['key']} {' | '.join(r['texts'])[:110]}")
    return 0


def _check(args) -> int:
    failed = 0
    for target in args.paths:
        report = check.check_tree(Path(target))
        print(f"{target}: {report.files} files, {report.slots} slots, {len(report.failures)} with problems")
        for name, found in report.failures.items():
            failed += 1
            for line in found[: args.limit]:
                print(f"  {name}: {line}")
            if len(found) > args.limit:
                print(f"  {name}: ... {len(found) - args.limit} more")
    return 1 if failed else 0


def _migrate(args) -> int:
    from .migrate import migrate, summarize, write_tables
    from .plugin import Plugin
    results = []
    for root in args.plugin:
        result = migrate(Plugin.open(root))
        results.append(result)
        if args.out:
            count = write_tables(result, Path(args.out) / result.name)
            print(f"{result.name}: {count} alignment tables written to {Path(args.out) / result.name}", file=sys.stderr)
    text, data = summarize(results)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if not args.report:
        print(text)
    return 0


def _status(args) -> int:
    from .plugin import Plugin
    from .status import plugin_status
    plugin = Plugin.open(args.plugin)
    rows = plugin_status(plugin, Path(args.align_dir) if args.align_dir else None)
    if args.json:
        print(json.dumps([{"page": r.page, "state": r.state, "detail": r.detail,
                           **({k: r.comparison.count(k) for k in ("current", "stale", "missing", "skip")}
                              if r.comparison and r.comparison.states else {})} for r in rows], indent=1))
        return 0
    totals: dict[str, int] = {}
    for r in rows:
        totals[r.state] = totals.get(r.state, 0) + 1
        if r.state == "current":
            continue
        counts = ""
        if r.comparison and r.comparison.states:
            c = r.comparison
            counts = f"{c.count('stale')} stale, {c.count('missing')} missing"
            if c.edited:
                counts += f", {len(c.edited)} edited by hand"
        print(f"{r.state:9s} {r.page}  {counts}{r.detail}")
    print(", ".join(f"{v} {k}" for k, v in sorted(totals.items())) + f" ({len(rows)} pages)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="dreamdocs", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("segments", help="show how one Markdown file is split into slots")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=_segments)

    p = sub.add_parser("check", help="self-check the splitter on every Markdown file under the given folders")
    p.add_argument("paths", nargs="+")
    p.add_argument("--limit", type=int, default=3, help="problems shown per file")
    p.set_defaults(run=_check)

    p = sub.add_parser("migrate", help="line old per-page translation records up slot by slot (report only)")
    p.add_argument("--plugin", action="append", required=True, help="plugin root; repeat for several plugins")
    p.add_argument("--out", help="write the alignment tables under this folder, one subfolder per plugin")
    p.add_argument("--report", help="write the Markdown report here instead of printing it")
    p.add_argument("--json", help="write per-page numbers here")
    p.set_defaults(run=_migrate)

    p = sub.add_parser("status", help="which slots of each translated page are current, stale or missing")
    p.add_argument("--plugin", default=".")
    p.add_argument("--align-dir", help="read alignment tables from here instead of Docs/<lang>/.align")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=_status)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (ValueError, OSError) as error:
        print(f"dreamdocs: {error}", file=sys.stderr)
        return 1
