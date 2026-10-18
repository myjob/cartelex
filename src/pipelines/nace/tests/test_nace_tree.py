"""Tests for NaceTree."""

import unittest

from config.settings import settings
from pipelines.nace.nace_tree import NaceTree, NaceVo


class TestNaceTreeInit(unittest.TestCase):
    """Tests for NaceTree initialisation."""

    def test_init_loads_tree(self):
        """NaceTree initialises without error and populates root sections."""
        tree = NaceTree(settings.NACE_JSON_PATH)
        self.assertIsInstance(tree, NaceTree)
        self.assertGreater(len(tree.root_sections), 0)

    def test_root_sections_are_nace_vos(self):
        """Root sections are NaceVo instances of type 'section'."""
        tree = NaceTree(settings.NACE_JSON_PATH)
        for section in tree.root_sections:
            self.assertIsInstance(section, NaceVo)
            self.assertEqual(section.type, "section")


class TestNaceTreeClassesOfGroup(unittest.TestCase):
    """Tests for resolving NACE class titles from a group code string."""

    @classmethod
    def setUpClass(cls):
        cls.tree = NaceTree(settings.NACE_JSON_PATH)

    def _classes_for_group_string(self, group_string: str) -> list[str]:
        """Parse 'CODE:Title' group string and return child class titles.

        Args:
            group_string: NACE group in 'code:title' format, e.g. '64.1:Monetary intermediation'.

        Returns:
            List of class titles belonging to the group.
        """
        code = group_string.split(":")[0]
        group_nodes = [vo for vo in self.tree.traverse() if vo.type == "group" and vo.code == code]
        self.assertTrue(group_nodes, f"Group code '{code}' not found in NACE tree")
        return [child.title for child in group_nodes[0].childs]

    def test_monetary_intermediation_classes(self):
        """Group '64.1:Monetary intermediation' resolves to its two NACE classes."""
        titles = self._classes_for_group_string("64.1:Monetary intermediation")
        self.assertIn("Central banking", titles)
        self.assertIn("Other monetary intermediation", titles)

    def test_returns_list_of_strings(self):
        """Class titles are non-empty strings."""
        titles = self._classes_for_group_string("64.1:Monetary intermediation")
        self.assertIsInstance(titles, list)
        self.assertTrue(all(isinstance(t, str) and t for t in titles))

    def test_unknown_group_code_raises(self):
        """An unrecognised group code causes the helper assertion to fail."""
        with self.assertRaises(AssertionError):
            self._classes_for_group_string("99.9:Does not exist")


class TestFastRankClasses(unittest.TestCase):
    """Tests for NaceTree.fast_rank_classes."""

    @classmethod
    def setUpClass(cls):
        cls.tree = NaceTree(settings.NACE_JSON_PATH)
        cls.group = cls.tree.select_group_by_title(
            "Manufacture of articles of concrete, cement and plaster"
        )
        cls.classes = cls.tree.list_nacetree_classes_of_group(cls.group)

    def test_group_has_classes(self):
        """Group 'Manufacture of articles of concrete, cement and plaster' has child classes."""
        self.assertGreater(len(self.classes), 0)

    def test_mortars_top_ranked(self):
        """fast_rank_classes ranks 'Manufacture of mortars' first for query 'mortars'."""
        ranked = self.tree.fast_rank_classes(self.classes, "mortars")
        top = ranked[0]
        self.assertIsInstance(top, NaceVo)
        self.assertEqual(top.code, "23.64")
        self.assertEqual(top.title, "Manufacture of mortars")
        self.assertEqual(top.isic_code, "2395")

    def test_ranked_length_unchanged(self):
        """fast_rank_classes returns all input classes, not a filtered subset."""
        ranked = self.tree.fast_rank_classes(self.classes, "mortars")
        self.assertEqual(len(ranked), len(self.classes))


if __name__ == "__main__":
    unittest.main()
