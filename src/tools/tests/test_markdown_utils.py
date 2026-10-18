import unittest
from tools.markdown_utils import format_fact_to_markdown

class TestMarkdownUtils(unittest.TestCase):
    def test_single_string(self):
        result = format_fact_to_markdown("A single string fact")
        self.assertEqual(result, "A single string fact")

    def test_single_dict_single_key(self):
        # The commissioner case
        data = [{'commissioner': 'François-Xavier ORTOLI'}]
        result = format_fact_to_markdown(data)
        self.assertEqual(result, "François-Xavier ORTOLI")

    def test_multi_dict_or_multi_key_to_table(self):
        # The fines case
        data = [
            {'company': 'Stokvis', 'amount': 'Fl 3700'},
            {'company': 'Kriek', 'amount': 'Fl 5000'}
        ]
        result = format_fact_to_markdown(data)
        # Check that it produces a table
        self.assertIn("| company", result)
        self.assertIn("| amount", result)
        self.assertIn("| Stokvis", result)
        self.assertIn("| Fl 3700", result)

    def test_bulleted_list(self):
        # A simple string list case
        data = ["Fact 1", "Fact 2", "Fact 3"]
        result = format_fact_to_markdown(data)
        self.assertEqual(result, "- Fact 1\n- Fact 2\n- Fact 3")

    def test_empty_list(self):
        result = format_fact_to_markdown([])
        self.assertEqual(result, "")

if __name__ == "__main__":
    unittest.main()
