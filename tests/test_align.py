import json
import tempfile
import unittest
from pathlib import Path

from dreamdocs.align import Alignment, Row, compare
from dreamdocs.markdown import parse
from dreamdocs.migrate import line_up, migrate_page
from dreamdocs.plugin import Plugin
from dreamdocs.status import page_status

EN_OLD = "# Types\n\nUse `float3` for colour.\n\n- one\n- two\n\n| Name | Use |\n|---|---|\n| `a` | first |\n"
ZH_OLD = "# 类型\n\n颜色用 `float3`。\n\n- 一\n- 二\n\n| 名称 | 用途 |\n|---|---|\n| `a` | 第一 |\n"


def table_for(en, zh, origin="m"):
    source, translated = parse(en), parse(zh)
    pairs, _ = line_up(source, translated)
    table = Alignment("Docs/types.md", "abc")
    for j, slot in enumerate(translated.slots):
        i = pairs.get(j)
        table.rows.append(Row(slot.signature, source.slots[i].key if i is not None else "-", slot.text_key, origin))
    return table


class AlignTests(unittest.TestCase):
    def test_round_trip_of_the_table_format(self):
        table = table_for(EN_OLD, ZH_OLD)
        self.assertEqual(Alignment.loads(table.dumps()).dumps(), table.dumps())
        self.assertEqual(Alignment.loads(table.dumps().replace("\n", "\r\n")).dumps(), table.dumps())

    def test_unchanged_english_is_current(self):
        result = compare(table_for(EN_OLD, ZH_OLD), parse(EN_OLD), parse(ZH_OLD))
        self.assertEqual([s.state for s in result.states], ["current"] * 6)

    def test_changed_inserted_and_removed_slots(self):
        en_new = EN_OLD.replace("- two\n", "- two, changed\n- three\n").replace("Use `float3` for colour.\n\n", "")
        result = compare(table_for(EN_OLD, ZH_OLD), parse(en_new), parse(ZH_OLD))
        self.assertEqual([s.state for s in result.states], ["current", "current", "stale", "missing", "current", "current"])
        self.assertEqual(result.states[2].old, 3)  # The old "二" is handed over as the translation to revise.

    def test_hand_edit_is_noticed_and_structure_change_is_refused(self):
        table = table_for(EN_OLD, ZH_OLD)
        edited = compare(table, parse(EN_OLD), parse(ZH_OLD.replace("第一", "头一个")))
        self.assertEqual(edited.edited, [5])
        broken = compare(table, parse(EN_OLD), parse(ZH_OLD.replace("- 二\n", "- 二\n- 多出来的\n")))
        self.assertTrue(broken.mismatch)

    def test_line_up_survives_a_merged_paragraph(self):
        en = "# Alpha\n\nFirst `x`.\n\nSecond `y`.\n\nThird `z`.\n"
        zh = "# 甲\n\n第一 `x`，第二 `y`。\n\n第三 `z`。\n"
        pairs, _ = line_up(parse(en), parse(zh))
        self.assertEqual(pairs, {0: 0, 2: 3})

    def test_code_only_slots_must_match_verbatim(self):
        pairs, rejected = line_up(parse("| `a` |\n|---|\n| `b` |\n"), parse("| `a` |\n|---|\n| `c` |\n"))
        self.assertEqual((pairs, rejected), ({0: 0}, 0))  # Different code: not even a candidate pair.


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "Docs/zh-CN/.translations").mkdir(parents=True)
        (root / "Docs/types.md").write_text(EN_OLD.replace("- two\n", "- two, changed\n"), encoding="utf-8")
        (root / "Docs/zh-CN/types.md").write_text('---\ntitle: "类型"\n---\n\n' + ZH_OLD, encoding="utf-8")
        (root / "Docs/zh-CN/.translations/types.md.json").write_text(json.dumps(
            {"origin": "ai", "source": EN_OLD, "sourceHash": "0" * 64, "sourceRevision": "1234567890abcdef"}),
            encoding="utf-8")
        (root / "Docs/new.md").write_text("# New\n\nNot translated yet.\n", encoding="utf-8")
        self.plugin = Plugin.open(root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_becomes_a_table_and_status_reads_it_back(self):
        result = migrate_page(self.plugin, "types.md")
        self.assertEqual((result.lined, result.unaligned, result.added_meta), ("exact", 0, 1))
        self.assertEqual([s.state for s in result.comparison.states],
                         ["current", "current", "current", "stale", "current", "current"])
        result.table.write(self.plugin.alignment("types.md"))
        status = page_status(self.plugin, "types.md")
        self.assertEqual((status.state, status.comparison.count("stale")), ("outdated", 1))
        self.assertEqual(page_status(self.plugin, "new.md").state, "missing")


if __name__ == "__main__":
    unittest.main()
