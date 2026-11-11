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


def _adopt(args) -> int:
    from .migrate import migrate_page
    from .plugin import Plugin
    from .status import page_status
    plugin = Plugin.open(args.plugin)
    directions = {page: d for d in plugin.directions() for page in d.pages()}
    pages = args.page or [p for p, d in directions.items()
                          if page_status(plugin, p, direction=d).state in ("untracked", "mismatch")]
    for page in pages:
        if page not in directions:
            raise ValueError(f"not a page of this plugin: {page}")
        result = migrate_page(plugin, page, adopt=True, direction=directions[page])
        if result.table is None:
            print(f"  {page}: no translation to adopt")
            continue
        result.table.write(directions[page].alignment(page))
        c = result.comparison
        print(f"  {page}: {c.count('current')} current, {c.count('stale')} stale, {c.count('missing')} missing"
              + (f"; {result.unaligned} translated slots line up with no English" if result.unaligned else ""))
    print(f"{len(pages)} pages adopted")
    return 0


def _status(args) -> int:
    from .plugin import Plugin
    from .status import plugin_status
    plugin = Plugin.open(args.plugin)
    rows = plugin_status(plugin, Path(args.align_dir) if args.align_dir else None)
    if args.json:
        print(json.dumps([{"page": r.page, "state": r.state, "detail": r.detail, "reverse": r.reverse,
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
        print(f"{r.state:9s} {r.page}{' (written in Chinese first)' if r.reverse else ''}  {counts}{r.detail}")
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
    work = translate.plan_all(plugin, cache, model, args.page, reuse=not args.no_reuse, rebuild=args.rebuild)
    all_jobs = {key: job for w in work for key, job in w.jobs.items()}
    for w in work:
        for p in w.plans:
            if p.note:
                print(f"  skipped {p.page}: {p.note}")
    if args.export:
        count = translate.export_jobs(all_jobs, Path(args.export))
        print(f"{count} slots written to {args.export}; fill in the answers, then run with --answers {args.export}")
        return 0
    if args.answers:
        accepted, problems = translate.apply_answers(all_jobs, Path(args.answers))
        print(f"{accepted} answers accepted")
        for line in problems:
            print(f"  refused {line}")
        if problems:
            return 1
    client = Client(profile, cache) if profile else None
    if profile is not None:
        print(f"{profile.describe()}")
        todo = 0
        for w in work:
            est = translate.estimate(w.plans, w.jobs, profile, client if profile.type == "OpenCodeGo" else None,
                                     translate.system_prompt(plugin, w.direction.target_lang))
            todo += est.jobs - est.cached
            print(f"{w.direction.label}: {est.pages} pages to write; {est.jobs} slots to translate "
                  f"({est.cached} already cached), {est.reused} copied from identical text elsewhere")
            if est.jobs - est.cached:
                cost = f", about ${est.cost:.2f}" if est.cost is not None else ""
                print(f"  to send: {est.jobs - est.cached} slots, {est.chars:,} characters in about {est.requests} requests "
                      f"(~{est.input_tokens:,} input / ~{est.output_tokens:,} output tokens{cost})")
        if args.limit is not None:
            todo = min(todo, args.limit)
        if args.dry_run:
            return 0
        if todo and not args.yes and not translate.confirm(f"send {todo} slots to {profile.name}?"):
            return 1
    elif args.dry_run:
        print(f"{sum(1 for j in all_jobs.values() if j.result is None)} slots to translate; offline: nothing is sent")
        return 0
    failed = False
    for w in work:
        summary = translate.run(plugin, profile, w.plans, w.jobs, client, cache, args.limit,
                                log=lambda line: print(line, file=sys.stderr))
        print(f"{w.direction.label}: wrote {len(summary.written)} pages; {summary.sent} slots translated, "
              f"{summary.cached} from the cache, {summary.reused} copied")
        for page, left in summary.unfinished:
            print(f"  not written {page}: {left} slots still untranslated")
        if not offline:
            for line in summary.failures[:20]:
                print(f"  failed {line}")
            if summary.stopped:
                print(f"stopped: {summary.stopped}")
                failed = True
                break
            failed = failed or bool(summary.unfinished)
    if client:
        print(f"{client.requests} requests, {client.input_tokens:,} input / {client.output_tokens:,} output tokens")
    return 1 if failed and not offline else 0


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


def _ci(args) -> int:
    from . import ci
    return ci.main(args)


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

    p = sub.add_parser("adopt", help="take a hand-written translation of today's English under alignment")
    p.add_argument("--plugin", default=".")
    p.add_argument("--page", action="append",
                   help="English page relative to Docs/ (repeatable); default: every translation without a usable table")
    p.set_defaults(run=_adopt)

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

    p = sub.add_parser("ci", help="plugin GitHub Actions steps")
    p.add_argument("action", choices=["translate"],
                   help="translate: translate what is stale with the DOCS_AI_* profile and open or update a pull request")
    p.add_argument("--plugin", default=".")
    p.add_argument("--branch", default="dreamdocs/translate", help="the branch the translations go to")
    p.add_argument("--base", default="main", help="the branch the pull request targets")
    p.add_argument("--no-push", action="store_true", help="translate and check, but leave the result in the checkout")
    p.set_defaults(run=_ci)

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
