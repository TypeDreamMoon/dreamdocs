"""llms.txt, llms-full.txt and published plugin files for a documentation site.

A site opts in with an "llms" block in docs-sync/config.json:

    "llms": {
      "title": "DreamShader",
      "summary": "One sentence: what the plugin is.",
      "details": ["Paragraphs a model should read before any link."],
      "sections": [
        {"title": "Start here", "links": [
          {"page": "cheatsheet", "note": "the language on one page"},
          {"file": "diagnostics.json", "title": "Diagnostics as JSON", "note": "..."}
        ]},
        {"title": "Optional", "links": [{"page": "changelog/dreamshader"}]}
      ],
      "full": ["cheatsheet", {"source": ".skill/reference/dss.md", "title": "..."}],
      "publish": {"diagnostics.json": "Docs/diagnostics/codes.json"}
    }

sync then writes public/llms.txt (the index, in the llmstxt.org shape: an H1, a blockquote summary,
paragraphs, and H2 sections of links, "Optional" being the one a reader may skip), public/llms-full.txt
(the English text of every "full" entry, one after another, with links made absolute) and each
"publish" file copied byte for byte from the plugin into public/. A static export serves public/ at the
site root, so they appear as /llms.txt, /llms-full.txt and /<name>.

A published JSON file whose entries carry "doc" links into this site is checked: every such link has to
name a page the site has, so a diagnostics file can never point at a route the site renamed.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from pathlib import Path

PUBLIC = "public"


def _absolute(body, site_url):
    """Root-relative links (/en/docs/...) become absolute; code is left alone."""
    from .site import outside_code

    def convert(line):
        line = re.sub(r"(\]\(<?)(/[^\s)>]*)", lambda m: m[1] + site_url + m[2], line)
        line = re.sub(r"(^\s*\[[^\]]+\]:\s*<?)(/[^\s>]*)", lambda m: m[1] + site_url + m[2], line)
        return re.sub(r'(\b(?:href|src)=")(/[^"]*)(")', lambda m: m[1] + site_url + m[2] + m[3], line)
    return outside_code(body, convert)


def _page_url(site_url, page_route):
    return site_url + page_route.rstrip("/") + "/"


def _walk_docs(value):
    if isinstance(value, dict):
        if isinstance(value.get("doc"), str):
            yield value["doc"]
        for item in value.values():
            yield from _walk_docs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_docs(item)


def plan(root, plugin, cfg, pages, english_bodies, source_routes, revision):
    """{output path: text or bytes} for the llms files; {} when the site does not opt in."""
    from .site import (html_to_explicit, read, rewrite_links, route, source_file, split_frontmatter, strip_leading_title,
                       within)

    spec = cfg.get("llms")
    if not spec:
        return {}
    site_url = (cfg.get("siteUrl") or "").rstrip("/")
    if not re.match(r"^https://[^/\s]+$", site_url):
        raise ValueError('llms needs "siteUrl": "https://<host>" in docs-sync/config.json')
    for key in ("title", "summary", "sections"):
        if not spec.get(key):
            raise ValueError(f"llms.{key} is required")

    routes = {page_id: route(page_id) for page_id in pages}
    known_paths = {r.rstrip("/") for r in routes.values()}
    published = spec.get("publish", {})
    outputs = {}

    for name, source in published.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or name.startswith("llms"):
            raise ValueError(f"llms.publish: invalid public file name {name!r}")
        data = source_file(plugin, source).read_bytes()
        if name.endswith(".json"):
            try:
                value = json.loads(data.decode("utf-8-sig"))
            except ValueError as error:
                raise ValueError(f"llms.publish: {source} is not JSON: {error}") from None
            for doc in _walk_docs(value):
                parsed = urllib.parse.urlsplit(doc)
                if f"{parsed.scheme}://{parsed.netloc}" != site_url:
                    continue
                if parsed.path.rstrip("/") not in known_paths:
                    raise ValueError(f"llms.publish: {source} links to {parsed.path}, which is not a page of this site")
        outputs[within(root, f"{PUBLIC}/{name}")] = data

    def link_line(link):
        note = f": {link['note']}" if link.get("note") else ""
        if "page" in link:
            page_id = link["page"]
            if page_id not in pages:
                raise ValueError(f"llms: no page {page_id!r}")
            title = link.get("title") or pages[page_id]["title"]
            return f"- [{title}]({_page_url(site_url, routes[page_id])}){note}"
        if "file" in link:
            if link["file"] not in published and link["file"] not in ("llms-full.txt",):
                raise ValueError(f"llms: {link['file']!r} is not a published file")
            return f"- [{link.get('title') or link['file']}]({site_url}/{link['file']}){note}"
        if "url" in link:
            return f"- [{link['title']}]({link['url']}){note}"
        raise ValueError("llms: a link needs page, file or url")

    index = [f"# {spec['title']}", "", f"> {spec['summary']}", ""]
    for paragraph in spec.get("details", []):
        index += [paragraph, ""]
    for section in spec["sections"]:
        index += [f"## {section['title']}", ""]
        index += [link_line(link) for link in section.get("links", [])]
        index.append("")
    outputs[within(root, f"{PUBLIC}/llms.txt")] = "\n".join(index).rstrip() + "\n"

    full_entries = spec.get("full")
    if full_entries is None:
        full_entries = [link["page"] for section in spec["sections"] if section["title"].lower() != "optional"
                        for link in section.get("links", []) if "page" in link]
    full = [f"# {spec['title']}", "", f"> {spec['summary']}", "",
            f"The full text of the pages listed in {site_url}/llms.txt, English, generated from the plugin "
            f"repository {cfg['repository']} at {revision[:12]}.", ""]
    for entry in full_entries:
        if isinstance(entry, str):
            if entry not in pages:
                raise ValueError(f"llms.full: no page {entry!r}")
            title, url, body = pages[entry]["title"], _page_url(site_url, routes[entry]), english_bodies[entry]
        else:
            path = entry["source"]
            _, body = split_frontmatter(read(source_file(plugin, path)))
            body = rewrite_links(html_to_explicit(body), path, cfg["repository"], revision, source_routes)
            if cfg.get("stripLeadingTitle"):
                body = strip_leading_title(body)
            title = entry.get("title") or path
            url = f"https://github.com/{cfg['repository']}/blob/{revision}/{urllib.parse.quote(path)}"
        full += ["", "---", "", f"# {title}", "", f"Source: {url}", "", _absolute(body.strip(), site_url), ""]
    outputs[within(root, f"{PUBLIC}/llms-full.txt")] = "\n".join(full).rstrip() + "\n"
    return outputs


def _hash(data):
    # Line endings do not count: a checkout may turn LF into CRLF and the file is still the one sync wrote.
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def file_hash(path):
    return _hash(Path(path).read_bytes())


def lock_entry(root, outputs):
    """What source-lock.json records: each file and the hash of what sync wrote, for validate."""
    return {Path(path).relative_to(root).as_posix(): _hash(value.encode("utf-8") if isinstance(value, str) else value)
            for path, value in sorted(outputs.items())}
