"""dreamdocs command line."""
from __future__ import annotations

import argparse
import json
import os
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
    if args.plugin:
        report = check.check_plugin(Path(args.plugin), strict=args.strict)
        for line in report.warnings:
            print(f"warning: {line}")
        for line in report.errors:
            print(f"error: {line}")
        print(f"{report.pages} pages; {len(report.errors)} errors, {len(report.warnings)} warnings")
        return 1 if report.errors else 0
    if not args.paths:
        raise ValueError("pass --plugin, or folders to self-check")
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
        plugin = Plugin.open(root)
        result = migrate(plugin)
        results.append(result)
        if args.write:
            count = write_tables(result, plugin.mirror / ".align")
            print(f"{result.name}: {count} alignment tables written to {plugin.mirror / '.align'}", file=sys.stderr)
        elif args.out:
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


def _profiles(args) -> int:
    from . import profiles
    found, warnings = profiles.load()
    print(f"{profiles.user_file()}{'' if profiles.user_file().exists() else ' (not found)'}")
    for line in warnings:
        print(f"  warning: {line}")
    for profile in found.values():
        key = "key set" if profile.key() else "no key"
        usable = "" if profile.type in profiles.TYPES.values() else " (machine translator: DreamLoc only)"
        print(f"  {profile.describe()}, {key}{usable}")
    return 0


def _translate(args) -> int:
    from . import translate
    from .cache import Cache
    from .plugin import Plugin
    from .profiles import choose
    from .providers import Client
    plugin = Plugin.open(args.plugin)
    offline = args.offline or bool(args.export) or bool(args.answers)
    profile = None if offline else choose(args.profile, plugin.config.get("profile"))
    if profile is not None and args.model:
        profile.model = args.model
    if profile is not None and args.concurrency:
        profile.concurrency = args.concurrency
    model = profile.model if profile else (args.model or "")
    cache = Cache(enabled=not args.no_cache and bool(model))
    pages = plugin.pages()
    if args.page:
        unknown = sorted(set(args.page) - set(pages))
        if unknown:
            raise ValueError(f"not an English manual of this plugin: {', '.join(unknown)} (paths are relative to Docs/)")
        pages = [p for p in pages if p in args.page]
    plans, jobs = translate.plan(plugin, pages, cache, model, reuse=not args.no_reuse, rebuild=args.rebuild)
    for p in plans:
        if p.note:
            print(f"  skipped {p.page}: {p.note}")
    if args.export:
        count = translate.export_jobs(jobs, Path(args.export))
        print(f"{count} slots written to {args.export}; fill in the answers, then run with --answers {args.export}")
        return 0
    if args.answers:
        accepted, problems = translate.apply_answers(jobs, Path(args.answers))
        print(f"{accepted} answers accepted")
        for line in problems:
            print(f"  refused {line}")
        if problems:
            return 1
    client = Client(profile, cache) if profile else None
    if profile is not None:
        est = translate.estimate(plans, jobs, profile, client if profile.type == "OpenCodeGo" else None,
                                 translate.system_prompt(plugin))
        todo = est.jobs - est.cached
        if args.limit is not None:
            todo = min(todo, args.limit)
        print(f"{profile.describe()}")
        print(f"{est.pages} pages to write; {est.jobs} slots to translate ({est.cached} already cached), "
              f"{est.reused} copied from identical English elsewhere")
        if todo:
            cost = f", about ${est.cost:.2f}" if est.cost is not None else ""
            print(f"to send: {todo} slots, {est.chars:,} characters in about {est.requests} requests "
                  f"(~{est.input_tokens:,} input / ~{est.output_tokens:,} output tokens{cost})")
        if args.dry_run:
            return 0
        if todo and not args.yes and not translate.confirm(f"send {todo} slots to {profile.name}?"):
            return 1
    elif args.dry_run:
        print(f"{sum(1 for j in jobs.values() if j.result is None)} slots to translate; offline: nothing is sent")
        return 0
    summary = translate.run(plugin, profile, plans, jobs, client, cache, args.limit,
                            log=lambda line: print(line, file=sys.stderr))
    sent = f"; {client.requests} requests, {client.input_tokens:,} input / {client.output_tokens:,} output tokens" \
        if client else " (offline)"
    print(f"wrote {len(summary.written)} pages; {summary.sent} slots translated, {summary.cached} from the cache, "
          f"{summary.reused} copied{sent}")
    for page, left in summary.unfinished:
        print(f"  not written {page}: {left} slots still untranslated")
    if offline:
        return 0  # Pages still waiting for a translation are expected without a translator.
    for line in summary.failures[:20]:
        print(f"  failed {line}")
    if summary.stopped:
        print(f"stopped: {summary.stopped}")
    return 1 if summary.unfinished or summary.stopped else 0


def _site(args) -> int:
    from . import site
    root = Path(args.site).resolve()
    if args.action == "ci":
        from . import site_ci
        return site_ci.main(args.ci_command, root)
    if args.action == "sync":
        plugin = args.plugin or os.environ.get("DOCS_PLUGIN_PATH")
        if not plugin:
            raise ValueError("pass --plugin (CI checks the plugin out explicitly)")
        changes = site.sync(root, plugin, args.revision, args.check, args.adopt_existing)
        print(f"{len(changes)} source file changes" + (" (check only)" if args.check else ""))
        if args.check and changes:
            return 1
    elif args.action == "validate":
        print(f"{site.validate(root)} generated pages verified")
    if args.action == "status" or args.report:
        summary = site.report(root)
        if args.report:
            site.write(args.report, summary)
        else:
            print(summary)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="dreamdocs", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("segments", help="show how one Markdown file is split into slots")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=_segments)

    p = sub.add_parser("check", help="check a plugin's manuals and translations (--plugin), or self-check folders")
    p.add_argument("paths", nargs="*", help="folders whose Markdown the splitter self-checks")
    p.add_argument("--plugin", help="check this plugin: tables, structure, links, and (with --strict) staleness")
    p.add_argument("--strict", action="store_true", help="outdated translations and broken links fail too")
    p.add_argument("--limit", type=int, default=3, help="problems shown per file")
    p.set_defaults(run=_check)

    p = sub.add_parser("migrate", help="line old per-page translation records up slot by slot (report only)")
    p.add_argument("--plugin", action="append", required=True, help="plugin root; repeat for several plugins")
    p.add_argument("--write", action="store_true", help="write the tables into each plugin's Docs/<lang>/.align")
    p.add_argument("--out", help="write the alignment tables under this folder, one subfolder per plugin")
    p.add_argument("--report", help="write the Markdown report here instead of printing it")
    p.add_argument("--json", help="write per-page numbers here")
    p.set_defaults(run=_migrate)

    p = sub.add_parser("status", help="which slots of each translated page are current, stale or missing")
    p.add_argument("--plugin", default=".")
    p.add_argument("--align-dir", help="read alignment tables from here instead of Docs/<lang>/.align")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=_status)

    p = sub.add_parser("translate", help="translate stale and missing slots and rebuild the translated pages")
    p.add_argument("--plugin", default=".")
    p.add_argument("--page", action="append", help="English page relative to Docs/ (repeatable); default: all")
    p.add_argument("--profile", help="translator profile name (see dreamdocs profiles)")
    p.add_argument("--model", help="use this model instead of the profile's")
    p.add_argument("--limit", type=int, help="send at most this many slots")
    p.add_argument("--concurrency", type=int)
    p.add_argument("--dry-run", action="store_true", help="only show what would be sent")
    p.add_argument("--yes", action="store_true", help="send without asking")
    p.add_argument("--no-reuse", action="store_true", help="translate identical English again instead of copying")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--rebuild", action="store_true", help="rewrite every translated page from today's English layout")
    p.add_argument("--offline", action="store_true",
                   help="send nothing: only write pages whose every slot is known (needs no profile)")
    p.add_argument("--export", metavar="FILE", help="write the slots still to translate to a JSON file and stop")
    p.add_argument("--answers", metavar="FILE", help="take translations filled into an exported file (implies --offline)")
    p.set_defaults(run=_translate)

    p = sub.add_parser("profiles", help="list the translator profiles dreamdocs can use")
    p.set_defaults(run=_profiles)

    p = sub.add_parser("site", help="documentation-site commands: generate pages from a plugin checkout")
    p.add_argument("action", choices=["sync", "status", "validate", "ci"])
    p.add_argument("ci_command", nargs="?", choices=["event", "commit-source"], help="for ci: what to do")
    p.add_argument("--site", default=".", help="the documentation site checkout")
    p.add_argument("--plugin", help="the plugin checkout (sync)")
    p.add_argument("--revision", help="the exact plugin commit (default: its HEAD)")
    p.add_argument("--check", action="store_true", help="sync: report changes, write nothing")
    p.add_argument("--adopt-existing", action="store_true", help="sync: first migration of a site's own Chinese pages")
    p.add_argument("--report", help="also write the translation status here")
    p.set_defaults(run=_site)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (ValueError, OSError) as error:
        print(f"dreamdocs: {error}", file=sys.stderr)
        return 1
