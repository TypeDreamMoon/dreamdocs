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
dreamdocs check --plugin .                   # what CI checks; --strict also fails outdated translations
```

Without a translator: `dreamdocs translate --plugin . --export pending.json` writes the paragraphs still to
translate; fill in each `answer` (keeping every `⟦n⟧` placeholder) and run `--answers pending.json`. The answers pass
the same checks a model's do. `--offline` only rebuilds the pages whose every paragraph is known.

## Plugins

A plugin keeps its English manuals in `Docs/`, their Chinese copies in `Docs/zh-CN/` and, next to them,
`Docs/zh-CN/.align/<page>.tsv`: which English each Chinese paragraph was made from. `Docs/dreamdocs.json` names the
tool version, excluded pages, identifiers never to translate (`keep`), diagnostic-code patterns (`codes`) and the
pages translated by hand (`manual`). `Docs/zh-CN/.dreamdocs/glossary.json` and `prompt.md` add to the built-in
glossary and instructions. Edit a Chinese page directly to correct it; the next run notices and keeps the edit.

A page written in Chinese first goes into `chineseOriginal` (paths relative to `Docs/zh-CN/`). Its Chinese is then the
original: `translate` writes the English `Docs/<page>` from it, with the table in `Docs/.align/<page>.tsv`, and the
documentation sites publish both. `prompt.en.md` next to `prompt.md` adds instructions for that direction.

### Translating in CI

Copy [`templates/plugin/docs-translate.yml`](templates/plugin/docs-translate.yml) to the plugin's
`.github/workflows/`. After the manuals change on `main` it runs `dreamdocs ci translate`: translates what is stale in
both directions with the profile from the `DOCS_AI_*` variables, runs `check --plugin`, and pushes the result to the
`dreamdocs/translate` branch with one pull request, updated by later runs. Without a key it only warns. When someone
else pushes to that branch, the workflow leaves it alone until it is merged or deleted. Merge the pull request with a
merge commit, not a squash: the alignment tables record the commit they were made from.

| Setting | Where | Value |
|---|---|---|
| `DOCS_AI_API_KEY` | secret | the translator's key |
| `DOCS_AI_TYPE` | variable | `OpenAI` (any OpenAI-compatible service), `Anthropic` or `OpenCodeGo` |
| `DOCS_AI_MODEL` | variable | for example `deepseek-v4.1-flash` (OpenCodeGo) or `deepseek-chat` |
| `DOCS_AI_BASE_URL` | variable | OpenAI-compatible services only, for example `https://api.deepseek.com/v1` |
| `DOCS_AI_MAX_SLOTS` | variable | at most this many paragraphs per run (default 3000); the next run continues |
| `DOCS_BOT_TOKEN` | secret, optional | a token that may push and open pull requests, so the pull request runs the other checks too |

Without `DOCS_BOT_TOKEN`, turn on *Allow GitHub Actions to create and approve pull requests* (Settings → Actions →
General). `DOCS_AI_EXTRA_BODY` and `DOCS_AI_JSON_MODE` are read as well.

## Documentation sites

A site keeps its page mapping in `docs-sync/config.json`, its own pages and the notes after
`<!-- docs-sync:notes -->`. `dreamdocs site sync --site . --plugin <plugin checkout>` generates both locales from the
plugin; `site validate` checks the result; `site ci event|commit-source` are its GitHub Actions steps.

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
