"""Unit tests for Filter_MasterJsnOrg_CELEX module."""

import json
import os
import shutil
import tempfile
import unittest

from batch.utils.Filter_MasterJsnOrg_CELEX import (
    loadCelexFilterFrom,
    list_CELEX_keysIn,
    filterMasterJsonListFor,
    selectCelexKeysOutOffilterMasterJsonListFor,
)


class TestFilterMasterJsnOrgCelex(unittest.TestCase):
    """Test suite for Filter_MasterJsnOrg_CELEX functions."""

    def setUp(self) -> None:
        """Create a temporary directory and prepare mock files for testing."""
        self.test_dir = tempfile.mkdtemp()

        # Paths for temporary test files
        self.master_json_path = os.path.join(self.test_dir, "mock_master_doc_descriptors.json")
        self.filter_list_path = os.path.join(self.test_dir, "mock_CELEX-FilterList.txt")
        self.empty_filter_path = os.path.join(self.test_dir, "mock_celexFilterEmpty.txt")

        # Prepare mock master JSON data
        self.mock_master_data = {
            "documents": {
                "CELEX-31982D0742": {
                    "CELEX": "CELEX-31982D0742",
                    "webDocDescriptor": {"title": "Amersham Buchler"}
                },
                "CELEX-31997D0039": {
                    "CELEX": "CELEX-31997D0039",
                    "webDocDescriptor": {"title": "Iridium"}
                },
                "CELEX-31987D0123": {
                    "CELEX": "CELEX-31987D0123",
                    "webDocDescriptor": {"title": "Boussois/Interpane"}
                },
                "CELEX-31975D0075": {
                    "CELEX": "CELEX-31975D0075",
                    "webDocDescriptor": {"title": "General Motors Continental"}
                }
            }
        }

        with open(self.master_json_path, "w", encoding="utf-8") as f:
            json.dump(self.mock_master_data, f)

        # Prepare mock filter list text file
        self.mock_filter_items = [
            "CELEX-31982D0742",
            "CELEX-31997D0039",
            "CELEX-31987D0123",
            "CELEX-31975D0075"
        ]
        with open(self.filter_list_path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.mock_filter_items) + "\n")

        # Prepare mock empty/whitespace filter list
        with open(self.empty_filter_path, "w", encoding="utf-8") as f:
            f.write("")

    def tearDown(self) -> None:
        """Clean up the temporary test directory."""
        shutil.rmtree(self.test_dir)

    def test_load_celex_filter_from(self) -> None:
        """Test loading filter items from a text file."""
        filter_list = loadCelexFilterFrom(self.filter_list_path)
        # loadCelexFilterFrom splits by "\n", so a trailing newline results in an empty string in the split list.
        self.assertEqual(filter_list, ["CELEX-31982D0742", "CELEX-31997D0039", "CELEX-31987D0123", "CELEX-31975D0075", ""])

    def test_list_celex_keys_in(self) -> None:
        """Test listing CELEX keys present in the master json."""
        keys = list_CELEX_keysIn(self.mock_master_data)
        self.assertEqual(len(keys), 4)
        self.assertIn("CELEX-31982D0742", keys)
        self.assertIn("CELEX-31997D0039", keys)

    def test_filter_master_json_list_for_with_valid_filter(self) -> None:
        """Test filtering master json list using a valid filter list file."""
        result = filterMasterJsonListFor(self.master_json_path, self.filter_list_path)
        self.assertEqual(len(result), 4)
        self.assertIsInstance(result[0], tuple)
        self.assertEqual(result[0][0], "CELEX-31982D0742")
        self.assertEqual(result[0][1]["webDocDescriptor"]["title"], "Amersham Buchler")

    def test_filter_master_json_list_for_with_missing_keys(self) -> None:
        """Test filtering master json list with keys that do not exist in master data."""
        temp_filter_path = os.path.join(self.test_dir, "temp_filter.txt")
        # "CELEX-MISSING" does not exist in mock_master_data
        with open(temp_filter_path, "w", encoding="utf-8") as f:
            f.write("CELEX-31982D0742\nCELEX-MISSING\n")

        result = filterMasterJsonListFor(self.master_json_path, temp_filter_path)
        # Should only contain the match (1 element)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "CELEX-31982D0742")

    def test_filter_master_json_list_for_with_empty_filter(self) -> None:
        """Test filtering master json list when the filter file has only empty lines or whitespace."""
        result = filterMasterJsonListFor(self.master_json_path, self.empty_filter_path)
        # When filter is empty/whitespace, it should return all elements from master json
        self.assertEqual(len(result), 4)
        all_keys = [item[0] for item in result]
        self.assertIn("CELEX-31982D0742", all_keys)
        self.assertIn("CELEX-31997D0039", all_keys)

    def test_filter_master_json_list_for_missing_filter_file(self) -> None:
        """Test that FileNotFoundError is raised if the filter file does not exist."""
        non_existent_path = os.path.join(self.test_dir, "non_existent_filter.txt")
        with self.assertRaises(FileNotFoundError):
            filterMasterJsonListFor(self.master_json_path, non_existent_path)

    def test_select_celex_keys_out_of_filter_master_json_list_for(self) -> None:
        """Test selecting only CELEX keys from the filtered master list."""
        keys = selectCelexKeysOutOffilterMasterJsonListFor(self.master_json_path, self.filter_list_path)
        expected_keys = [
            "CELEX-31982D0742",
            "CELEX-31997D0039",
            "CELEX-31987D0123",
            "CELEX-31975D0075"
        ]
        self.assertEqual(keys, expected_keys)


if __name__ == "__main__":
    unittest.main()
