"""Plugin-owned English and Chinese -> generated documentation-site pages.

Moved here from each site's scripts/docs_sync.py, which the four sites used to
copy by hand. The site keeps its page mapping (docs-sync/config.json), its own
pages and notes; this module generates the rest from a plugin checkout.

What a Chinese page's state is comes from the plugin's alignment tables
(Docs/zh-CN/.align, see align.py), not from per-page records. No command here
commits, pushes or deploys; site_ci.py does that inside GitHub Actions only.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import urllib.parse

from .align import Alignment

NOTES = "<!-- docs-sync:notes -->"


def read(path):
    return Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n")


def load(path, default=None):
    return json.loads(read(path)) if Path(path).exists() else default


def digest(text):
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def write(path, text):
    path = Path(path)
    if path.exists() and read(path) == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def within(root, name):
    """Reject traversal, drive paths and symlinks escaping the declared root."""
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError(f"Invalid relative path: {name!r}")
    root = Path(root).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f"Path escapes root: {name}")
    return path


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                            encoding="utf-8", check=False)
    if result.returncode:
        raise ValueError(f"git {args[0]} failed (exit {result.returncode})")
    return result.stdout.rstrip('\r\n')


def source_file(root, name):
    """Validate source spelling on Windows too, before a Linux CI checkout reads it."""
    path = within(root, name)
    current = Path(root).resolve()
    for part in Path(name).parts:
        if part in (".", ".."):
            raise ValueError(f"Source path must be normalized: {name}")
        names = {entry.name for entry in current.iterdir()}
        if part not in names:
            similar = next((entry for entry in names if entry.casefold() == part.casefold()), None)
            if similar:
                raise ValueError(f"Source path case mismatch: {name}; use {similar}, not {part}")
            raise FileNotFoundError(f"Missing source: {name}")
        current = current / part
    if not path.is_file():
        raise FileNotFoundError(f"Source is not a file: {name}")
    return path


def split_frontmatter(text):
    match = re.match(r"\A---\n(.*?)\n---(?:\n|$)", text, re.S)
    return (match.group(1), text[match.end():].lstrip("\n")) if match else ("", text)


def field(front, name, default=""):
    match = re.search(rf"^{re.escape(name)}:\s*(.+)$", front, re.M)
    if not match:
        return default
    value = match.group(1).strip()
    if value.startswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value.strip("'\"")


def document(title, description, body):
    return f"---\ntitle: {json.dumps(title, ensure_ascii=False)}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n{body.rstrip()}\n"


def headings(text):
    """ATX headings outside fenced code; offsets let a page select stable sections."""
    result, offset, fence = [], 0, None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        elif fence is None:
            match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if match:
                result.append((len(match.group(1)), match.group(2), offset))
        offset += len(line)
    return result


def excerpt(text, selection=None):
    if not selection:
        return text
    entries = headings(text)
    matches = [i for i, (_, title, _) in enumerate(entries) if title == selection]
    if len(matches) != 1:
        raise ValueError(f"Section must occur exactly once: {selection}")
    index = matches[0]
    level, _, start = entries[index]
    end = next((offset for depth, _, offset in entries[index + 1:] if depth <= level), len(text))
    return text[start:end].strip() + "\n"


def select_source(text, entry):
    if entry.get("headingPrefix"):
        selected = [title for _, title, _ in headings(text) if title.startswith(entry["headingPrefix"])]
        if not selected:
            raise ValueError(f"No headings matching {entry['headingPrefix']}")
        return "\n".join(excerpt(text, title) for title in selected)
    selected = excerpt(text, entry.get("heading"))
    if entry.get("diagnosticPrefix"):
        rows = []
        for line in selected.splitlines():
            if re.match(r"^\| DUI\d{4} \|", line):
                cells = line.split(" | ", 2)
                code = cells[0].strip("| ")
                if code.startswith(entry["diagnosticPrefix"]):
                    rows.append(f"## {code}\n\n`{cells[1]}`\n\n{cells[2].rstrip(' |')}\n")
        if not rows:
            raise ValueError("No diagnostics for " + entry["diagnosticPrefix"])
        return "\n".join(rows)
    return selected


def outside_code(text, transform):
    """Leave fenced blocks and inline code byte-for-byte intact."""
    lines, fence = [], None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            lines.append(line)
        elif fence:
            lines.append(line)
        else:
            parts = re.split(r"(`+[^`\n]+`+)", line)
            lines.append("".join(part if part.startswith("`") else transform(part) for part in parts))
    return "".join(lines)


def split_notes(text):
    """Only a standalone marker outside code starts website-owned notes."""
    offset, fence = 0, None
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        elif fence is None and line.rstrip('\r\n') == NOTES:
            return text[:offset], NOTES, text[offset + len(NOTES):]
        offset += len(line)
    return text, '', ''


def strip_leading_title(body):
    """Drop a leading level-1 heading: the page template already renders the title.

    A plugin keeps `# Title` at the top of a topic so the file reads well on GitHub;
    a site whose template renders DocsTitle from the same title would show two <h1>.
    Sites opt in with "stripLeadingTitle": true in docs-sync/config.json.
    """
    return re.sub(r"\A# [^\n]*\n+(?=\S)", "", body, count=1)


def route(page_id, locale="en"):
    slug = page_id[:-6] if page_id.endswith("/index") else "" if page_id == "index" else page_id
    return ("/en/docs/" if locale == "en" else "/docs/") + slug


def rewrite_links(body, source_path, repository, revision, source_routes, section=False, anchors=None):
    github = f"https://github.com/{repository}/blob/{revision}/"
    raw = f"https://raw.githubusercontent.com/{repository}/{revision}/"
    anchors = anchors or {}

    def fragment(path, value):
        return anchors.get(path, {}).get(urllib.parse.unquote(value), value)

    def target(url):
        if url.startswith('/en/docs/'):
            parsed = urllib.parse.urlsplit(url)
            source = next((p for p, r in source_routes.items() if r.rstrip('/') == parsed.path.rstrip('/')), None)
            return urllib.parse.urlunsplit(parsed._replace(fragment=fragment(source, parsed.fragment)))
        if re.match(r"^(?:[a-zA-Z][\w+.-]*:|//|/)", url):
            return url
        parsed = urllib.parse.urlsplit(url)
        if not parsed.path:
            if section and parsed.fragment:
                return github + source_path + url
            return urllib.parse.urlunsplit(parsed._replace(fragment=fragment(source_path, parsed.fragment)))
        path = str(Path(source_path).parent.joinpath(urllib.parse.unquote(parsed.path))).replace("\\", "/")
        import posixpath
        path = posixpath.normpath(path)
        suffix = ("?" + parsed.query if parsed.query else "") + ("#" + parsed.fragment if parsed.fragment else "")
        if path in source_routes:
            return source_routes[path] + ("?" + parsed.query if parsed.query else "") + ("#" + fragment(path, parsed.fragment) if parsed.fragment else "")
        if path.startswith("../"):
            raise ValueError(f"Link leaves plugin: {source_path}: {url}")
        image = Path(path).suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif")
        return (raw if image else github) + urllib.parse.quote(path, safe="/") + suffix

    def convert(line):
        # Markdown links, reference definitions and quoted HTML/JSX links.
        line = re.sub(r"(\]\(<?)([^\s)>]+)(>?)(?=[\s)])", lambda m: m[1] + target(m[2]) + m[3], line)
        line = re.sub(r"(^\s*\[[^\]]+\]:\s*<?)([^\s>]+)", lambda m: m[1] + target(m[2]), line)
        return re.sub(r'(\b(?:href|src)=")([^"]+)(")', lambda m: m[1] + target(m[2]) + m[3], line)
    return outside_code(body, convert)


def source_anchors(body):
    # Native Markdown discards HTML, so sync carries an <a id> onto the heading after it
    # (html_to_explicit): the anchor stays the heading's ID on the site too.
    offsets = {offset for _, _, offset in headings(body)}
    return {m[1]: m[1] for m in re.finditer(r'<a (?:id|name)="([^"<>]+)"></a>\s*\n(#{1,6} [^\n]+)', body)
            if m.start(2) in offsets}


# The production server answers any path with a segment named like a framework's
# private directory with its own 404 (seen with runtime), so no route may use one.
BLOCKED_SEGMENTS = {"runtime", "vendor"}


def check_route(page_id):
    blocked = BLOCKED_SEGMENTS & set(page_id.lower().split("/"))
    if blocked:
        raise ValueError(f"Route {page_id} uses a path segment the web server blocks: {', '.join(sorted(blocked))}")


def config(root):
    value = load(root / "docs-sync/config.json")
    if not value or value.get("version") != 1:
        raise ValueError("Missing or unsupported docs-sync/config.json")
    ids = [page["id"] for page in value["pages"]]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate page IDs")
    for page_id in ids:
        within(root / "content/docs", page_id)
        check_route(page_id)
    return value


def translation_files(plugin, source):
    """(the Chinese copy of a Docs/ source, its alignment table)."""
    relative = Path(source).relative_to('Docs').as_posix()
    mirror = plugin / 'Docs/zh-CN'
    if mirror.is_symlink():
        raise ValueError('Chinese documentation root cannot be a symlink')
    return (within(mirror, relative), within(mirror, '.align/' + relative + '.tsv'))


def plugin_statuses(plugin):
    """One translation unit per English file, independent of website routing.

    current / outdated / missing, plus unreviewed for a Chinese page nobody has
    checked against its English: no alignment table yet (a plugin not moved to
    dreamdocs), a table that no longer matches the page, or one whose every row
    was imported without the English it was made from.
    """
    from .plugin import Plugin
    from .status import page_status
    owner = Plugin.open(plugin)
    plugin = owner.root
    revision = git(plugin, 'rev-parse', 'HEAD')
    rows = []
    for page in owner.pages():
        name = 'Docs/' + page
        result = page_status(owner, page)
        states = result.comparison.states if result.comparison else []
        pending = sum(1 for s in states if s.state in ('stale', 'missing'))
        status = {'current': 'current', 'missing': 'missing'}.get(result.state)
        legacy = legacy_record(plugin, name)
        if result.state == 'untracked' and legacy is not None:
            # A plugin not moved to dreamdocs yet: its per-page record decides, as it always did.
            fingerprint = digest(read(source_file(plugin, name)))
            status = ('unreviewed' if not legacy.get('sourceHash') else
                      'current' if legacy['sourceHash'] == fingerprint else 'outdated')
        if status is None:
            status = 'unreviewed' if result.state in ('untracked', 'mismatch') else 'outdated'
            table_path = owner.alignment(page)
            if status == 'outdated' and table_path.exists() and \
                    not any(row.verified and row.known for row in Alignment.read(table_path).rows):
                status = 'unreviewed'
        rows.append({'id': page, 'path': name, 'status': status, 'pending': pending,
                     'hash': digest(read(source_file(plugin, name))), 'revision': revision})
    return rows


def legacy_record(plugin, source):
    """The per-page record of a plugin translated before dreamdocs (Docs/zh-CN/.translations), if any."""
    relative = Path(source).relative_to('Docs').as_posix()
    return load(Path(plugin) / 'Docs/zh-CN/.translations' / (relative + '.json'))


def aligned_english(plugin, source, table):
    """The English a Chinese page was laid out from, else today's English."""
    if table is None:
        legacy = legacy_record(plugin, source)
        if legacy and legacy.get('source'):
            return legacy['source']
    if table is not None and table.rev:
        result = subprocess.run(['git', '-C', str(plugin), 'show', f'{table.rev}:{source}'],
                                capture_output=True, check=False)
        if result.returncode == 0:
            try:
                return result.stdout.decode('utf-8-sig').replace('\r\n', '\n')
            except UnicodeDecodeError:
                pass
    return read(source_file(plugin, source))


def explicit_to_html(body):
    """Keep source IDs usable in GitHub and packaged ordinary Markdown."""
    for level, title, offset in reversed(headings(body)):
        match = re.search(r'\s+\[#([^\]]+)\]$', title)
        if not match:
            continue
        end = body.find('\n', offset)
        end = len(body) if end == -1 else end
        replacement = f'<a id="{match[1]}"></a>\n\n' + '#' * level + ' ' + title[:match.start()]
        body = body[:offset] + replacement + body[end:]
    return body


def html_to_explicit(body):
    """Fumadocs drops raw HTML in .md; carry each anchor onto its heading."""
    heading_offsets = {offset for _, _, offset in headings(body)}
    return re.sub(r'<a (?:id|name)="([^"<>]+)"></a>\s*\n(#{1,6} [^\n]+)',
                  lambda m: (m[2] if m[2].endswith('[#' + m[1] + ']') else m[2] + ' [#' + m[1] + ']') if m.start(2) in heading_offsets
                  else m[0], body)


def select_translation(body, entry, source):
    body = html_to_explicit(body)
    if entry.get('diagnosticPrefix'):
        prefix = entry['diagnosticPrefix']
        groups = [title for _, title, _ in headings(body)
                  if re.match(re.escape(prefix) + r'xxx\b', title)]
        if groups:
            return '\n'.join(excerpt(body, title) for title in groups)
        selected = [title for _, title, _ in headings(body)
                    if re.match(re.escape(prefix) + r'\d\b', title)]
        if selected:
            return '\n'.join(excerpt(body, title) for title in selected)
        return select_source(body, {'diagnosticPrefix': prefix})
    if not entry.get('heading') and not entry.get('headingPrefix'):
        return body
    # Select against the version this Chinese file actually translated.
    source_headings = headings(heading_ids(html_to_explicit(split_frontmatter(source)[1])))
    wanted = []
    for _, title, _ in source_headings:
        match = re.search(r'\s+\[#([^\]]+)\]$', title)
        original = title[:match.start()] if match else title
        if (entry.get('heading') == original or
                entry.get('headingPrefix') and original.startswith(entry['headingPrefix'])):
            wanted.append(match[1])
    titles = [title for _, title, _ in headings(body)
              if any(title.endswith('[#' + anchor + ']') for anchor in wanted)]
    if not titles:
        raise ValueError('Translated section is missing; update the plugin translation: ' + entry['path'])
    return '\n'.join(excerpt(body, title) for title in titles)


def plugin_translation_snapshot(plugin):
    directory = plugin / 'Docs/zh-CN'
    return digest(json_text({path.relative_to(directory).as_posix(): digest(read(path))
                             for path in sorted(directory.rglob('*'))
                             if path.is_file() and path.suffix in ('.md', '.json', '.tsv')}))


def sync(root, plugin, revision=None, check=False, adopt_existing=False):
    cfg = config(root)
    plugin = Path(plugin).resolve()
    revision = revision or git(plugin, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        raise ValueError("An exact plugin commit SHA is required")
    if git(plugin, "rev-parse", "HEAD") != revision:
        raise ValueError("Plugin checkout does not match the requested commit")
    old = load(root / "docs-sync/source-lock.json", {"pages": {}})
    dirty = git(plugin, "status", "--porcelain", "--untracked-files=all", "--", "Docs", "README.md", "CHANGELOG.md")
    dirty_sources = bool(dirty)
    preview = os.environ.get('DOCS_TRANSLATION_PREVIEW') == 'true'
    if preview and any(not line[3:].strip('"').startswith('Docs/zh-CN/') for line in dirty.splitlines()):
        raise ValueError('Translation preview may change Chinese plugin files only')
    if dirty_sources and os.environ.get("GITHUB_ACTIONS") and not preview:
        raise ValueError("CI requires committed plugin documentation")
    prior = old.get("revision")
    different_history = False
    if prior and prior != revision:
        result = subprocess.run(["git", "-C", str(plugin), "merge-base", "--is-ancestor", prior, revision],
                                capture_output=True)
        different_history = result.returncode != 0
        if different_history:
            backwards = subprocess.run(['git', '-C', str(plugin), 'merge-base', '--is-ancestor', revision, prior],
                                       capture_output=True)
            if backwards.returncode == 0:
                raise ValueError('Refusing an older plugin revision; Chinese must not roll back')
    definitions = list(cfg["pages"])
    known = {s["path"] for page in definitions for s in page["sources"]}
    for path in sorted((plugin / "Docs").rglob("*.md")):
        name = path.relative_to(plugin).as_posix()
        if name in known or name.startswith(("Docs/Website/", "Docs/zh-CN/")) or name in cfg.get("exclude", []):
            continue
        page_id = "plugin/" + path.relative_to(plugin / "Docs").with_suffix("").as_posix().lower()
        if any(p["id"] == page_id for p in definitions):
            raise ValueError(f"New source collides with route: {page_id}")
        check_route(page_id)
        definitions.append({"id": page_id, "sources": [{"path": name}], "format": "md", "locale": cfg.get("sourceLocale", "en")})
    source_routes = {}
    anchors = {entry['path']: source_anchors(split_frontmatter(read(source_file(plugin, entry['path'])))[1])
               for page in definitions for entry in page['sources']}
    for page in definitions:
        if page.get("locale", "en") == "en":
            for entry in page["sources"]:
                if not any(entry.get(key) for key in ("heading", "headingPrefix", "diagnosticPrefix")):
                    source_routes.setdefault(entry["path"], route(page["id"]))
    planned, removals, pages = {}, [], {}
    for page in definitions:
        page_id = page["id"]
        chunks, fingerprints = [], []
        source_front = ""
        for entry in page["sources"]:
            source_path = source_file(plugin, entry["path"])
            front, body = split_frontmatter(read(source_path))
            source_front = source_front or front
            selected = select_source(body, entry)
            fingerprints.append({**entry, "hash": digest(selected)})
            # GitHub shows a trailing [#id] literally, so sources write <a id> before a plain heading.
            chunks.append(rewrite_links(html_to_explicit(selected), entry["path"], cfg["repository"], revision,
                                        source_routes, bool(entry.get("heading")), anchors))
        body = "\n\n".join(chunks)
        if cfg.get("stripLeadingTitle"):
            body = strip_leading_title(body)
        if cfg.get("titleSource") == "plugin":
            # The plugin's frontmatter wins over the site's mapping (the narrative site's choice).
            title = field(source_front, "title") or page.get("title") or next((h[1] for h in headings(body)), page_id)
            description = field(source_front, "description") or page.get("description") or title
        else:
            title = page.get("title") or field(source_front, "title") or next((h[1] for h in headings(body)), page_id)
            description = page.get("description", field(source_front, "description", title))
        semantic_body = body.replace(f"/blob/{revision}/", "/blob/SOURCE/").replace(
            f"raw.githubusercontent.com/{cfg['repository']}/{revision}/", f"raw.githubusercontent.com/{cfg['repository']}/SOURCE/")
        source_hash = digest(json_text({"sources": fingerprints, "title": title, "description": description,
                                       "rendered": semantic_body, "format": page.get("format", "md")}))
        # Stable per-page revision: unrelated plugin commits must not rewrite links/mark translations stale.
        previous = old["pages"].get(page_id, {})
        page_revision = previous.get("revision", revision) if previous.get("hash") == source_hash and not old.get("workingTree") and not different_history else revision
        if page_revision != revision:
            body = body.replace(f"/blob/{revision}/", f"/blob/{page_revision}/")
            body = body.replace(f"raw.githubusercontent.com/{cfg['repository']}/{revision}/",
                                f"raw.githubusercontent.com/{cfg['repository']}/{page_revision}/")
        fmt = page.get("format", "md")
        if fmt not in ("md", "mdx"):
            raise ValueError(f"Unsupported source format: {fmt}")
        english = page.get("locale", "en") == "en"
        output = f"content/docs/{page_id}.en.{fmt}" if english else f"docs-sync/sources/{page_id}.{fmt}"
        generated = document(title, description, body)
        destination = within(root, output)
        previous_output = previous.get("output")
        if previous_output and (root / previous_output).exists():
            if digest(read(root / previous_output)) != previous.get("outputHash"):
                raise ValueError(f"Generated source was edited locally: {previous_output}; edit the plugin instead")
            if previous_output != output and previous_output.startswith("docs-sync/sources/"):
                removals.append(within(root, previous_output))
        if english:
            other = within(root, f"content/docs/{page_id}.en.{'mdx' if fmt == 'md' else 'md'}")
            if other.exists():
                removals.append(other)
        planned[destination] = generated
        pages[page_id] = {"hash": source_hash, "revision": page_revision, "output": output,
                          "outputHash": digest(generated), "locale": page.get("locale", "en"),
                          "format": fmt, "title": title, "sources": fingerprints}
    removed = set(old["pages"]) - set(pages)
    if removed:
        raise ValueError("Pages removed from mapping; migrate routes/translations explicitly: " + ", ".join(sorted(removed)))
    if different_history and {key: page["hash"] for key, page in pages.items()} != {key: page["hash"] for key, page in old["pages"].items()}:
        raise ValueError("Refusing an older or unrelated plugin revision with different documentation; fetch full plugin history")
    snapshot = plugin_translation_snapshot(plugin)
    if different_history and old.get('translationSnapshotHash', snapshot) != snapshot:
        raise ValueError('Refusing unrelated history with different Chinese documentation')
    plan_chinese(root, plugin, cfg, definitions, pages, old, planned, removals, revision, adopt_existing)
    lock = {"version": 1, "repository": cfg["repository"], "revision": revision, "workingTree": dirty_sources,
            "translationSnapshotHash": snapshot, "pages": pages}
    planned[root / "docs-sync/source-lock.json"] = json_text(lock)
    changes = [str(p.relative_to(root)) for p, value in planned.items() if not p.exists() or read(p) != value]
    changes += [str(p.relative_to(root)) for p in removals]
    if not check:
        # All source, path and overwrite checks have completed before the first write.
        for path, value in planned.items():
            write(path, value)
        for path in removals:
            path.unlink()
    return changes


def plan_chinese(root, plugin, cfg, definitions, pages, old, planned, removals, revision, adopt_existing):
    units = {row['path']: row for row in plugin_statuses(plugin)}
    source_routes = {}
    for page in definitions:
        for entry in page['sources']:
            if not any(entry.get(key) for key in ('heading', 'headingPrefix', 'diagnosticPrefix')):
                source_routes.setdefault(entry['path'], route(page['id']))
                translated, _ = translation_files(plugin, entry['path'])
                if translated.exists():
                    source_routes.setdefault(translated.relative_to(plugin).as_posix(), route(page['id'], 'zh'))
    imported = []
    for definition in definitions:
        page_id = definition['id']
        page = pages[page_id]
        previous = old['pages'].get(page_id, {}).get('translation', {})
        unit_rows = [units.get(entry['path'], {'status': 'missing'}) for entry in definition['sources']]
        state = next((name for name in ('missing', 'unreviewed', 'outdated')
                      if any(row['status'] == name for row in unit_rows)), 'current')
        if state == 'missing':
            # An incomplete source set must never replace a complete existing page.
            page['translation'] = {**previous, 'status': state}
            continue
        bodies, source_records, source_front = [], [], ''
        for entry in definition['sources']:
            translated, table_path = translation_files(plugin, entry['path'])
            table = Alignment.read(table_path) if table_path.exists() else None
            front, body = split_frontmatter(read(source_file(plugin, translated.relative_to(plugin).as_posix())))
            source_front = source_front or front
            # Sections are selected against the English this Chinese page was laid out from.
            selected = select_translation(body, entry, aligned_english(plugin, entry['path'], table))
            bodies.append(rewrite_links(selected, translated.relative_to(plugin).as_posix(), cfg['repository'],
                                        revision, source_routes, bool(entry.get('heading'))))
            if table is None and legacy_record(plugin, entry['path']) is not None:
                legacy = legacy_record(plugin, entry['path'])
                source_records.append({'path': entry['path'], 'sourceHash': legacy.get('sourceHash'),
                                       'sourceRevision': legacy.get('sourceRevision'),
                                       'translationHash': digest(read(translated))})
            else:
                source_records.append({'path': entry['path'], 'alignedRevision': table.rev if table else None,
                                       'alignedPage': table.extra.get('page') if table else None,
                                       'translationHash': digest(read(translated))})
        old_path = translation_path(root, page_id)
        old_text = read(old_path) if old_path.exists() else ''
        old_prose, marker, notes = split_notes(old_text)
        if previous.get('output'):
            owned = within(root, previous['output'])
            if not owned.exists() or digest(split_notes(read(owned))[0].rstrip()) != previous.get('bodyHash'):
                raise ValueError('Generated Chinese was edited locally: ' + str(owned) + '; edit the plugin or notes instead')
        elif old_text and not adopt_existing:
            raise ValueError('Existing Chinese needs explicit migration: ' + page_id + '; use --adopt-existing locally after importing it into the plugin')
        if adopt_existing and os.environ.get('GITHUB_ACTIONS'):
            raise ValueError('CI cannot adopt existing website Chinese')
        body = '\n\n'.join(bodies)
        old_front = split_frontmatter(old_prose)[0]
        scoped = len(definition['sources']) > 1 or any(len(entry) > 1 for entry in definition['sources'])
        title = (definition.get('titleZh') or (field(old_front, 'title') if scoped else '') or
                 field(source_front, 'title') or next((h[1] for h in headings(body)), page_id))
        title = re.sub(r'\s+\[#[^\]]+\]$', '', title)
        description = (definition.get('descriptionZh') or (field(old_front, 'description') if scoped else '') or
                       field(source_front, 'description', title))
        output = document(title, description, body)
        if marker:
            output += '\n' + marker + notes
        destination = within(root, f'content/docs/{page_id}.md')
        if old_path != destination and old_path.exists():
            removals.append(old_path)
        planned[destination] = output
        record_path = within(root, f'docs-sync/translations/{page_id}.json')
        page['translation'] = {'status': state, 'output': destination.relative_to(root).as_posix(),
                               'record': record_path.relative_to(root).as_posix()}
        pending = sum(row.get('pending', 0) for row in unit_rows)
        if pending and state != 'current':
            page['translation']['pending'] = pending  # Paragraphs the Chinese has yet to catch up with.
        imported.append((page_id, destination, record_path, source_records))
    localize_translation_links(root, planned, removals)
    for page_id, destination, record_path, source_records in imported:
        page = pages[page_id]
        text = planned[destination]
        page['translation'].update(outputHash=digest(text), bodyHash=digest(split_notes(text)[0].rstrip()))
        planned[record_path] = json_text({'version': 2, 'status': page['translation']['status'],
            'sourceHash': page['hash'] if page['translation']['status'] == 'current' else None,
            # The revision this page's source is pinned to, not the sync's: an unrelated
            # English edit elsewhere must not rewrite every record.
            'sourceRevision': page['revision'], 'sources': source_records,
            'translationFile': page['translation']['output'], 'translationHash': digest(text)})


def translation_path(root, page_id, preferred="mdx"):
    for ext in (preferred, "md" if preferred == "mdx" else "mdx"):
        path = within(root, f"content/docs/{page_id}.{ext}")
        if path.exists():
            return path
    return within(root, f"content/docs/{page_id}.{preferred}")


def statuses(root):
    lock = load(root / "docs-sync/source-lock.json", {"pages": {}})
    rows = []
    for page_id, page in lock["pages"].items():
        translated = load(within(root, f"docs-sync/translations/{page_id}.json"), {})
        exists = translation_path(root, page_id).exists()
        status = page.get('translation', {}).get('status') or ("missing" if not exists else "unreviewed" if not translated else "current" if translated.get("sourceHash") == page["hash"] else "outdated")
        rows.append({"id": page_id, "status": status, "hash": page["hash"], "revision": page["revision"]})
    return rows


def report(root):
    rows = statuses(root)
    lines = ["# Translation status", "", "English and Chinese are imported from the plugin; translate there with `dreamdocs translate`.", "",
             "| Page | Status | Source revision |", "| --- | --- | --- |"]
    lines += [f"| `{r['id']}` | {r['status']} | `{r['revision'][:12]}` |" for r in rows if r["status"] != "current"]
    lines += ["", f"{sum(r['status'] != 'current' for r in rows)} pending / {len(rows)} managed pages.", ""]
    return "\n".join(lines)



def heading_ids(body):
    """Keep source anchors stable when translated heading text changes."""
    replacements, counts = [], {}
    for _, title, offset in headings(body):
        if re.search(r"\[#[^\]]+\]$", title):
            continue
        slug = re.sub(r"[^\w\s-]", "", title.lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        anchor = slug if count == 0 else f"{slug}-{count}"
        end = body.find("\n", offset)
        end = len(body) if end == -1 else end
        replacements.append((end, f" [#{anchor}]"))
    for offset, suffix in reversed(replacements):
        body = body[:offset] + suffix + body[offset:]
    return body



def translation_anchors(text, mdx=False):
    """Confirm explicit IDs and plain heading slugs without guessing MDX rendering."""
    body = split_frontmatter(text)[1]
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    anchors, used, plain_headings = set(), set(), True
    for _, title, _ in headings(body):
        explicit = re.search(r"\s+\[#([^\]]+)\]$", title)
        if explicit:
            anchors.add(explicit[1])
        elif plain_headings and re.fullmatch(r"[\w -]+", title):
            slug = title.lower().replace(" ", "-")
            anchor, count = slug, 0
            while anchor in used:
                count += 1
                anchor = f"{slug}-{count}"
            used.add(anchor)
            anchors.add(anchor)
        else:
            # Unknown inline markup can also affect subsequent duplicate slugs.
            # New translations have explicit IDs from heading_ids(), so this
            # conservative fallback only affects older hand-authored pages.
            plain_headings = False
    if mdx:
        def collect(line):
            if line.startswith(("    ", "\t")):
                return line
            for match in re.finditer(r'<(?:a|span|div|section|h[1-6])\b[^>]*\bid\s*=\s*([\'"])(.*?)\1', line):
                anchors.add(html.unescape(match[2]))
            return line
        outside_code(body, collect)
    return anchors


def localize_translation_links(root, planned, removals):
    """Prefer Chinese routes only when the final batch contains a valid target."""
    content = (root / "content/docs").resolve()
    pages = {path.resolve(): read(path) for path in content.rglob("*")
             if path.suffix in (".md", ".mdx") and not path.stem.endswith(".en")
             and path.resolve() not in removals}
    before = dict(pages)
    pages.update({path: value for path, value in planned.items()
                  if path.is_relative_to(content) and path.suffix in (".md", ".mdx")
                  and not path.stem.endswith('.en')})
    targets = {route(path.relative_to(content).with_suffix("").as_posix(), "zh").rstrip("/"):
               translation_anchors(value, path.suffix == ".mdx") for path, value in pages.items()}

    def slug(title):
        return re.sub(r"[^\w\s-]", "", title.lower()).replace(" ", "-")

    # A translated heading carries the English ID ("## 预处理器 [#preprocessor]"); older Chinese
    # pages still link to its Chinese slug (#预处理器), which GitHub gives it but the site does not.
    # When the retranslation reworded the heading, the old page's heading at the same position
    # (same number of headings) names it.
    aliases = {}
    for path, value in pages.items():
        found, new = {}, headings(split_frontmatter(value)[1])
        ids = [re.search(r"\s+\[#([^\]]+)\]$", title) for _, title, _ in new]
        for (_, title, _), explicit in zip(new, ids):
            if explicit:
                found.setdefault(slug(title[:explicit.start()]), explicit[1])
        old = headings(split_frontmatter(before.get(path, ""))[1])
        if before.get(path) != value and len(old) == len(new):
            for (_, title, _), explicit in zip(old, ids):
                if explicit:
                    found.setdefault(slug(re.sub(r"\s+\[#[^\]]+\]$", "", title)), explicit[1])
        aliases[route(path.relative_to(content).with_suffix("").as_posix(), "zh").rstrip("/")] = found
    english = {}
    for path in content.rglob("*.en.*"):
        if path.suffix in (".md", ".mdx"):
            text = planned.get(path.resolve(), planned.get(path)) or read(path)
            english[route(path.relative_to(content).with_suffix("").with_suffix("").as_posix(), "zh").rstrip("/")] = \
                translation_anchors(text, path.suffix == ".mdx")

    def chinese_target(url, page):
        # /docs/... or #... inside a Chinese page whose fragment the Chinese target lacks.
        parsed = urllib.parse.urlsplit(url)
        chinese = urllib.parse.unquote(parsed.path).rstrip("/") if parsed.path else page
        fragment = urllib.parse.unquote(html.unescape(parsed.fragment))
        if not fragment or chinese not in targets or fragment in targets[chinese]:
            return url
        if fragment in aliases.get(chinese, {}):
            fixed = aliases[chinese][fragment]
            return urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, urllib.parse.quote(fixed, safe="-_")))
        if fragment in english.get(chinese, set()):
            return urllib.parse.urlunsplit(("", "", "/en" + chinese, parsed.query, parsed.fragment))
        # Nothing names the section any more: the page itself, not a dead anchor.
        return urllib.parse.urlunsplit(("", "", parsed.path or chinese, parsed.query, ""))

    def target(url, page=None):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme or parsed.netloc:
            return url
        if page is not None and (parsed.path == "/docs" or parsed.path.startswith("/docs/")
                                 or (not parsed.path and parsed.fragment)):
            return chinese_target(url, page)
        if not (parsed.path == "/en/docs" or parsed.path.startswith("/en/docs/")):
            return url
        chinese = urllib.parse.unquote(parsed.path[3:]).rstrip("/")
        fragment = urllib.parse.unquote(html.unescape(parsed.fragment))
        if chinese in targets and (not fragment or fragment in targets[chinese]):
            return url[3:]  # Keep the original query, escaping and fragment bytes.
        if chinese in targets and fragment in aliases.get(chinese, {}):
            return chinese_target(url[3:], None)
        return url

    def converter(page):
        def convert(line):
            # Indented code is left alone as well as outside_code's fenced/inline code.
            if line.startswith(("    ", "\t")):
                return line
            line = re.sub(r"(\]\(<?)([^\s)>]+)(>?)(?=[\s)])", lambda m: m[1] + target(m[2], page) + m[3], line)
            line = re.sub(r"(^\s*\[[^\]]+\]:\s*<?)([^\s>]+)", lambda m: m[1] + target(m[2], page), line)
            return re.sub(r'(\bhref\s*=\s*(?:\{\s*)?)([\'"])([^\'"]+)(\2)',
                          lambda m: m[1] + m[2] + target(m[3], page) + m[4], line)
        return convert

    for path in pages.keys() & planned.keys():
        text = planned[path]
        body = split_frontmatter(text)[1]
        prose, marker, notes = split_notes(body)
        page = route(path.relative_to(content).with_suffix("").as_posix(), "zh").rstrip("/")
        planned[path] = text[:len(text) - len(body)] + outside_code(prose, converter(page)) + marker + notes



def validate(root):
    cfg = config(root)
    lock = load(root / "docs-sync/source-lock.json")
    if lock["repository"] != cfg["repository"]:
        raise ValueError("Source repository mismatch")
    if lock.get("workingTree") and os.environ.get("GITHUB_ACTIONS") and os.environ.get('DOCS_TRANSLATION_PREVIEW') != 'true':
        raise ValueError("Source snapshot includes local plugin edits; commit/push the plugin and run Sync plugin documentation first")
    for page_id, page in lock["pages"].items():
        if digest(read(within(root, page["output"]))) != page["outputHash"]:
            raise ValueError(f"Generated source differs from lock: {page_id}")
        record = load(within(root, f"docs-sync/translations/{page_id}.json"))
        if record:
            within(root, record["translationFile"])
            if record.get('sourceHash') is not None and not re.fullmatch(r"[a-f0-9]{64}", record["sourceHash"]):
                raise ValueError(f"Invalid translation baseline: {page_id}")
        translation = page.get('translation', {})
        if translation.get('output') and digest(split_notes(read(within(root, translation['output'])))[0].rstrip()) != translation['bodyHash']:
            raise ValueError('Generated Chinese differs from plugin snapshot: ' + page_id)
    return len(lock["pages"])


