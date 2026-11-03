import unittest

from dreamdocs import check
from dreamdocs.markdown import escape, parse


def kinds(text):
    return [(s.signature, s.texts) for s in parse(text).slots]


class SplitTests(unittest.TestCase):
    def assertSound(self, text):
        self.assertEqual(check.problems(parse(text)), [])

    def test_frontmatter_values_are_slots(self):
        doc = parse('---\ntitle: Types\ndescription: "Say \\"hi\\""\nsidebar: 3\n---\n\nBody.\n')
        self.assertEqual([(s.name, s.texts, s.quote) for s in doc.slots[:2]],
                         [("title", ["Types"], ""), ("description", ['Say "hi"'], '"')])
        self.assertSound(doc.src)

    def test_hard_wrapped_paragraph_is_one_slot(self):
        self.assertEqual(kinds("One line\nand the next.\n\nSecond.\n"),
                         [("p", ["One line and the next."]), ("p", ["Second."])])

    def test_headings_keep_ids_and_closing_hashes_in_the_skeleton(self):
        self.assertEqual(kinds("## Types [#types]\n\n### Notes ##\n\nUnderlined\nheading\n==\n"),
                         [("h2", ["Types"]), ("h3", ["Notes"]), ("h1", ["Underlined heading"])])

    def test_numbered_atx_heading_is_not_escaped(self):
        slot = parse("## 1. Install\n").slots[0]
        self.assertEqual(escape(slot, "1. 安装"), "1. 安装")

    def test_lists_nest_and_continue_lazily(self):
        text = "- first\n  wrapped\n- second\n\n  more of second\n  - nested\nlazy line\n"
        self.assertEqual(kinds(text), [("li", ["first wrapped"]), ("li", ["second"]),
                                       ("li", ["more of second"]), ("li", ["nested lazy line"])])
        self.assertSound(text)

    def test_alert_marker_is_skeleton(self):
        self.assertEqual(kinds("> [!NOTE]\n> Mind the gap.\n"), [("q", ["Mind the gap."])])

    def test_code_html_and_anchors_are_skeleton(self):
        text = ('```cpp\nint x; // not prose\n```\n\n    indented code\n\n<!-- generated:begin X -->\n'
                '<a id="x"></a>\n\n<details>\nraw\n</details>\n\nText.\n')
        self.assertEqual(kinds(text), [("p", ["Text."])])

    def test_table_rows_and_escaped_pipes(self):
        text = "| Name | Use |\n|---|:-:|\n| `a \\| b` | either |\n| x | y | dropped |\n"
        self.assertEqual(kinds(text), [("row2", ["Name", "Use"]), ("row2", ["`a \\| b`", "either"]),
                                       ("row2", ["x", "y"])])
        self.assertSound(text)

    def test_html_block_name_interrupts_a_paragraph(self):
        self.assertEqual(kinds("A line\n<source> is html\n"), [("p", ["A line"])])

    def test_crlf_and_bom_are_normalized(self):
        self.assertEqual(parse("﻿A\r\nB\r\n").src, "A\nB\n")

    def test_render_replaces_only_slots(self):
        doc = parse("## Title\n\n- item one\n\n| a | b |\n|---|---|\n| c | d |\n")
        values = {i: ["X" * (n + 1) for n in range(len(s.spans))] for i, s in enumerate(doc.slots)}
        self.assertEqual(doc.render(values), "## X\n\n- X\n\n| X | XX |\n|---|---|\n| X | XX |\n")

    def test_escape_keeps_translations_from_starting_blocks(self):
        para = parse("Text.\n").slots[0]
        self.assertEqual(escape(para, "1. 第一"), "1\\. 第一")
        self.assertEqual(escape(para, "# 不是标题"), "\\# 不是标题")
        self.assertEqual(escape(para, "两行\n合一"), "两行 合一")
        row = parse("| a |\n|---|\n").slots[0]
        self.assertEqual(escape(row, "甲|乙"), "甲\\|乙")
        meta = parse("---\ntitle: A\n---\n").slots[0]
        self.assertEqual(escape(meta, "甲: 乙"), '"甲: 乙"')

    def test_keys_ignore_wrapping_but_not_wording(self):
        a, b, c = (parse(t).slots[0].key for t in ("Some text\nhere.\n", "Some  text here.\n", "Some text there.\n"))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


if __name__ == "__main__":
    unittest.main()
