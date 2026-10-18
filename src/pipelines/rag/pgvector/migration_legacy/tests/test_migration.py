from pipelines.rag.pgvector.migration_legacy.copy_facts_from_rule_engine import (
    parse_companies,
    format_as_markdown_list,
    format_as_json_array,
)

def test_parse_companies_semicolon():
    input_str = "Company A; Company B; Company C"
    expected = ["Company A", "Company B", "Company C"]
    assert parse_companies(input_str) == expected

def test_parse_companies_comma_simple():
    input_str = "Apple, Banana, Orange"
    expected = ["Apple", "Banana", "Orange"]
    assert parse_companies(input_str) == expected

def test_parse_companies_comma_with_suffixes():
    input_str = "Tech Corp, Inc., Global Services Ltd., StartUp LLC, Normal Company"
    expected = ["Tech Corp, Inc.", "Global Services Ltd.", "StartUp LLC", "Normal Company"]
    assert parse_companies(input_str) == expected

def test_parse_companies_mixed_case_suffixes():
    input_str = "Acme S.A., Widget PLC., Foo Inc"
    expected = ["Acme S.A.", "Widget PLC.", "Foo Inc"]
    assert parse_companies(input_str) == expected

def test_parse_companies_empty():
    assert parse_companies("") == []
    assert parse_companies(None) == []

def test_format_as_markdown_list():
    companies = ["Company A, Inc.", "Company B"]
    expected = "- Company A, Inc.\n- Company B"
    assert format_as_markdown_list(companies) == expected

def test_format_as_json_array():
    companies = ["Company A, Inc.", "Company B"]
    expected = '["Company A, Inc.", "Company B"]'
    assert format_as_json_array(companies) == expected
