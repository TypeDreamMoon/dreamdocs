import unittest

from dreamdocs import inline


class InlineTests(unittest.TestCase):
    def test_mask_freezes_code_links_and_codes_but_not_link_text(self):
        masked, values = inline.mask("See [the guide](../a.md#x) and `UE.Lerp`, DSH1001 or <https://x.cn>.")
        self.assertEqual(masked, "See [the guide]⟦3⟧ and ⟦1⟧, ⟦4⟧ or ⟦2⟧.")
        self.assertEqual(inline.unmask(masked, values),
                         "See [the guide](../a.md#x) and `UE.Lerp`, DSH1001 or <https://x.cn>.")

    def test_placeholder_check_names_the_damage(self):
        self.assertIsNone(inline.check_placeholders("a ⟦1⟧ ⟦2⟧", "甲 ⟦2⟧ ⟦1⟧"))
        self.assertEqual(inline.check_placeholders("a ⟦1⟧ ⟦2⟧", "甲 ⟦1⟧ ⟦1⟧"), "missing ⟦2⟧; unexpected ⟦1⟧")

    def test_only_words_outside_frozen_spans_need_translation(self):
        self.assertFalse(inline.translatable("`float3`"))
        self.assertFalse(inline.translatable("—"))
        self.assertFalse(inline.translatable("x"))
        self.assertTrue(inline.translatable("`float3` vector"))

    def test_source_with_a_placeholder_is_refused(self):
        with self.assertRaises(ValueError):
            inline.mask("odd ⟦1⟧ text")

    def test_anchor_tokens_survive_translation(self):
        self.assertEqual(inline.anchor_tokens("Use `A` then `B`."), inline.anchor_tokens("先用 `B` 再用 `A`。"))
        self.assertEqual(inline.anchor_tokens("No code."), "")


if __name__ == "__main__":
    unittest.main()
