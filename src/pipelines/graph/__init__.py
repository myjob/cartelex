"""Typed entity graph materialized from the fact store (Phase 5.1).

Projects ``celex_facts.facts`` into typed nodes (``entities``) and
lineage-grounded relations (``entity_edges``) so cross-case questions
(company overlap across cartels, fines per company over time) resolve in SQL
instead of string-matching over ``value_md``.
"""
