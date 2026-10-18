import json
import os
import shutil
import tempfile
import unittest

from batch.utils.migrate_descriptors import migrate_descriptors

class TestMigrateDescriptors(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory structure for testing
        self.test_dir = tempfile.mkdtemp()
        self.src_docs_dir = os.path.join(self.test_dir, "src_docs")
        self.dest_docs_dir = os.path.join(self.test_dir, "dest_docs")
        os.makedirs(self.src_docs_dir)
        os.makedirs(self.dest_docs_dir)

        # Create dummy source files
        self.doc1_pdf = os.path.join(self.src_docs_dir, "CELEX-1", "doc.pdf")
        os.makedirs(os.path.dirname(self.doc1_pdf), exist_ok=True)
        with open(self.doc1_pdf, "w") as f:
            f.write("dummy pdf content")

        self.doc1_md = os.path.join(self.src_docs_dir, "CELEX-1", "doc.md")
        os.makedirs(os.path.dirname(self.doc1_md), exist_ok=True)
        with open(self.doc1_md, "w") as f:
            f.write("dummy md content")

        # Mock master sheet json
        self.src_json_path = os.path.join(self.test_dir, "master_sheet.json")
        self.dest_json_path = os.path.join(self.test_dir, "master_doc_descriptors.json")

        self.mock_data = {
            "documents": {
                "CELEX-1": {
                    "webDocDescriptor": {
                        "case": "1",
                        "url_pdf": "http://example.com/pdf"
                    },
                    "CELEX": "CELEX-1",
                    "comment": "test comment",
                    "local_file_html": "",
                    "local_file_pdf": self.doc1_pdf,
                    "language_init": "en",
                    "language_translated": "NA",
                    "gs_public_url": "http://gs/url",
                    "facts": os.path.join(self.src_docs_dir, "CELEX-1", "missing_facts.json"), # does not exist
                    "mineru_flag": True,
                    "txt_md_file": self.doc1_md,
                    "extra_field_to_be_removed": "should not be in output"
                },
                "CELEX-2": {
                    "CELEX": "CELEX-2",
                    "comment": "no files here",
                    "another_extra_field": 123
                }
            }
        }

        with open(self.src_json_path, "w", encoding="utf-8") as f:
            json.dump(self.mock_data, f)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_migrate_descriptors_filtering_and_copying(self):
        # Run migration
        # src_dir_prefix is self.src_docs_dir + "/"
        # dest_dir_prefix is self.dest_docs_dir + "/"
        src_prefix = self.src_docs_dir + "/"
        dest_prefix = self.dest_docs_dir + "/"
        
        stats = migrate_descriptors(
            self.src_json_path,
            self.dest_json_path,
            src_prefix,
            dest_prefix
        )

        # Assert correct stats
        self.assertEqual(stats["total_records"], 2)
        self.assertEqual(stats["new_records"], 2)
        # Total files referenced: local_file_pdf, facts, txt_md_file for CELEX-1 (3 files referenced)
        # Note: local_file_html is empty string, so it doesn't start with src_prefix.
        self.assertEqual(stats["total_files_referenced"], 3)
        self.assertEqual(stats["copied_files_count"], 2) # doc1_pdf and doc1_md exist and are copied
        self.assertEqual(stats["missing_files_count"], 1) # missing_facts.json does not exist

        # Assert output json exists
        self.assertTrue(os.path.exists(self.dest_json_path))

        with open(self.dest_json_path, "r", encoding="utf-8") as f:
            output_data = json.load(f)

        # Assert top level structure
        self.assertIn("documents", output_data)
        docs = output_data["documents"]
        self.assertEqual(len(docs), 2)

        # Assert fields of CELEX-1 are filtered and extra_field_to_be_removed is deleted
        c1 = docs["CELEX-1"]
        self.assertNotIn("extra_field_to_be_removed", c1)
        self.assertEqual(c1["CELEX"], "CELEX-1")
        self.assertEqual(c1["webDocDescriptor"]["case"], "1")
        self.assertEqual(c1["mineru_flag"], True)

        # Assert path updates
        expected_pdf_path = self.doc1_pdf.replace(src_prefix, dest_prefix)
        expected_md_path = self.doc1_md.replace(src_prefix, dest_prefix)
        expected_facts_path = os.path.join(self.src_docs_dir, "CELEX-1", "missing_facts.json").replace(src_prefix, dest_prefix)

        self.assertEqual(c1["local_file_pdf"], expected_pdf_path)
        self.assertEqual(c1["txt_md_file"], expected_md_path)
        self.assertEqual(c1["facts"], expected_facts_path)

        # Assert files are physically copied
        self.assertTrue(os.path.exists(expected_pdf_path))
        self.assertTrue(os.path.exists(expected_md_path))
        self.assertFalse(os.path.exists(expected_facts_path))

        # Assert CELEX-2 filtered correctly
        c2 = docs["CELEX-2"]
        self.assertNotIn("another_extra_field", c2)
        self.assertEqual(c2["CELEX"], "CELEX-2")
        self.assertEqual(c2["comment"], "no files here")

if __name__ == "__main__":
    unittest.main()
