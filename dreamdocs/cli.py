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

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (ValueError, OSError) as error:
        print(f"dreamdocs: {error}", file=sys.stderr)
        return 1
