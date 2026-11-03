# dreamdocs

Shared documentation tooling for the Dream plugins (DreamShader, DreamFX, DreamGUI, DreamNarrative) and their
documentation sites.

A manual is split into a **skeleton** — headings markers, list markers, table pipes, code, HTML, anchors — and
**slots**, the runs of prose a translator may replace. A translation is the English skeleton with Chinese in
the slots, so a translator cannot break the page structure, and only the paragraphs whose English changed are
translated again.

Python 3.10+, standard library only.

```powershell
pip install -e .                             # from this checkout
dreamdocs segments Docs\language\types.md   # how one page is split
dreamdocs check Docs                         # self-check the splitter on every page under Docs
dreamdocs status --plugin .                  # which slots of each translated page are current, stale or missing
dreamdocs profiles                           # the translator profiles found
dreamdocs translate --plugin . --dry-run     # what would be sent, and roughly what it costs
dreamdocs translate --plugin . --profile ocgo
```

## Translators

Profiles are read from DreamLoc's personal file, `%APPDATA%\DreamLoc\translators.ini`
(`DREAMLOC_TRANSLATORS_INI` overrides it), so a key is configured once for both tools:

```ini
[Profile ocgo]
Type=OpenCodeGo                ; reads opencode.ai's model list to pick the address and request format
Model=deepseek-v4.1-flash
ApiKeyEnv=OPENCODE_GO_API_KEY

[Profile deepseek]
Type=OpenAI                    ; any OpenAI-compatible service, cloud or local
BaseUrl=https://api.deepseek.com/v1
Model=deepseek-chat
ApiKeyEnv=DEEPSEEK_API_KEY
JsonMode=true
ExtraBody={"thinking": {"type": "disabled"}}
```

`Type` is `OpenAI` (`Api=chat` or `responses`), `Anthropic` (messages) or `OpenCodeGo`. Optional keys:
`MaxTokens`, `TokenField`, `ExtraBody`, `Headers`, `Concurrency`, `Retry`, `ChunkChars`, `Timeout`, `Proxy`,
`Temperature`. In CI the `DOCS_AI_*` variables form a profile named `env`.

Translations already received are kept in `.dreamdocs-cache/` in this checkout (`DREAMDOCS_CACHE` overrides
it), so an interrupted run resumes without paying twice.

## Tests

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

`tests/reference_compare.py` compares the splitter with markdown-it-py over whole folders; it needs
`pip install markdown-it-py==3.0.0` and is not run by the unit tests.
