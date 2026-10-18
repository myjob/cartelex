import json
import os
import shutil
import sys

def migrate_descriptors(
    src_json_path: str,
    dest_json_path: str,
    src_dir_prefix: str,
    dest_dir_prefix: str
) -> dict:
    """Migrates document descriptors from src_json_path to dest_json_path.
    
    Keeps only specified fields, copies local files to dest_dir_prefix,
    and updates the path references in the output JSON.
    """
    fields_to_keep = {
        "webDocDescriptor",
        "CELEX",
        "comment",
        "local_file_html",
        "local_file_pdf",
        "language_init",
        "language_translated",
        "gs_public_url",
        "facts",
        "mineru_flag",
        "txt_md_file"
    }

    with open(src_json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    documents = data.get("documents", {})
    new_documents = {}

    copied_files = []
    missing_files = []
    total_files_referenced = 0

    for doc_id, doc_info in documents.items():
        filtered_doc = {}
        for key in fields_to_keep:
            if key in doc_info:
                val = doc_info[key]
                if isinstance(val, str) and val.startswith(src_dir_prefix):
                    total_files_referenced += 1
                    src_file_path = val
                    dest_file_path = val.replace(src_dir_prefix, dest_dir_prefix)

                    if os.path.exists(src_file_path):
                        os.makedirs(os.path.dirname(dest_file_path), exist_ok=True)
                        shutil.copy2(src_file_path, dest_file_path)
                        copied_files.append((src_file_path, dest_file_path))
                    else:
                        missing_files.append(src_file_path)

                    filtered_doc[key] = dest_file_path
                else:
                    filtered_doc[key] = val
        new_documents[doc_id] = filtered_doc

    new_data = {"documents": new_documents}

    os.makedirs(os.path.dirname(dest_json_path), exist_ok=True)
    with open(dest_json_path, 'w', encoding='utf-8') as f:
        json.dump(new_data, f, indent=4, ensure_ascii=False)

    return {
        "total_records": len(documents),
        "new_records": len(new_documents),
        "total_files_referenced": total_files_referenced,
        "copied_files_count": len(copied_files),
        "missing_files_count": len(missing_files),
        "copied_files": copied_files,
        "missing_files": missing_files
    }

if __name__ == "__main__":
    from config.settings import settings

    src_json = settings.MASTER_SHEET_PATH
    dest_json = settings.MASTER_DOC_DESCRIPTORS_PATH
    # The OCR staging area this sheet was originally migrated from. Not derived
    # from settings: it is an external, now-defunct tree, not project config.
    src_prefix = "/usr/job/a/pdf-pytesseract-ocr/docs/"
    dest_prefix = os.path.join(settings.CELEX_DOCS_DIR, "")

    print("Starting descriptor migration...")
    try:
        stats = migrate_descriptors(src_json, dest_json, src_prefix, dest_prefix)
        print("Migration completed successfully!")
        print(f"Original records count: {stats['total_records']}")
        print(f"Migrated records count: {stats['new_records']}")
        print(f"Total files referenced: {stats['total_files_referenced']}")
        print(f"Successfully copied: {stats['copied_files_count']}")
        print(f"Missing (not copied): {stats['missing_files_count']}")
        
        if stats['missing_files_count'] > 0:
            print(f"Warning: {stats['missing_files_count']} files were not found on disk.")
            for mf in stats['missing_files'][:10]:
                print(f"  Missing: {mf}")
            if len(stats['missing_files']) > 10:
                print("  ...")
    except Exception as e:
        print(f"Error during migration: {e}", file=sys.stderr)
        sys.exit(1)
