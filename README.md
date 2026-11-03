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
```

## Tests

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

`tests/reference_compare.py` compares the splitter with markdown-it-py over whole folders; it needs
`pip install markdown-it-py==3.0.0` and is not run by the unit tests.
