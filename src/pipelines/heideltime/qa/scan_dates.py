"""
QA repair for malformed dates in stored chunk text.

HeidelTime tokenises on whitespace, so a date whose parts are glued together
(``January152010``, ``August1995``, ``untilJanuary152010``) is invisible to it
and the chunk yields no temporal expression.  This script finds those shapes in
``celex_facts.pg_embedding_qwen.document`` and rewrites them in place.

Rewriting chunk text invalidates everything derived from it.  The 2026-04-29
run of this script did **not** do that, which left 475 chunks across 87
documents holding HeidelTime extractions computed from the pre-repair text --
rows that no re-run could correct, because the pipeline counted "chunk has at
least one row" as "chunk is done".  Every repaired chunk is therefore now
invalidated in the *same transaction* as its ``UPDATE``, via the shared
statements in
:func:`~pipelines.rag.pgvector.event_repository.heideltime_invalidation_statements`.

Two artefacts are written to ``reports/``:

* ``heideltime_qa_dates_fixed_<ts>.xlsx`` — one row per individual rewrite.
* ``heideltime_stale_celex_<ts>.txt`` — the touched CELEX ids, one per line,
  ready to feed ``Main_ingest_generic_facts.py --celex-list-file``.

Usage::

    .venv/bin/python src/pipelines/heideltime/qa/scan_dates.py --dry-run
    .venv/bin/python src/pipelines/heideltime/qa/scan_dates.py
    .venv/bin/python src/pipelines/heideltime/qa/scan_dates.py --celex_id CELEX-31965D0426
"""

import argparse
import datetime
import re
from collections import defaultdict
from pathlib import Path
from typing import Callable, NamedTuple

import polars as pl
from sqlalchemy import text

from pipelines.rag.pgvector.event_repository import heideltime_invalidation_statements
from pipelines.rag.pgvector.session import engine

# Full month names only for the rules added in 2026-08.  Every malformed shape
# measured in the corpus spells the month out, and restricting the alternation
# keeps three-letter abbreviations ("Mar", "May", "Aug") from matching ordinary
# words that happen to sit next to a number.
_MONTH_FULL = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December"
)

# The original day-first rule also accepted abbreviations; kept verbatim so its
# behaviour does not change.
_MONTH_ANY = _MONTH_FULL + "|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec"

# ``(?-i:[a-z])`` stays case-sensitive inside a case-insensitive pattern: the
# glue always shows up as a *lowercase* tail ("until13", "monthsApril") because
# it is the end of an English word, and matching capitals too would split
# legitimate identifiers such as "IV13".
_LOWER = "(?-i:[a-z])"


class _RepairRule(NamedTuple):
    """One named text repair applied to a chunk document.

    Attributes:
        name: Short identifier reported in the xlsx, so a reviewer can see
            which rule produced a given rewrite.
        pattern: Compiled regex located on the document text.
        replace: Callable mapping a match to its replacement.  Returning
            ``match.group(0)`` unchanged means "matched but not malformed".
    """

    name: str
    pattern: re.Pattern[str]
    replace: Callable[[re.Match[str]], str]


def _split_prefix(match: re.Match[str]) -> str:
    """Insert a space between a word tail and the date glued onto it."""
    return f"{match.group(1)} {match.group(2)}"


def _month_first(match: re.Match[str]) -> str:
    """Reorder a glued month-first date: ``January152010`` -> ``15 January 2010``."""
    return f"{match.group(2)} {match.group(1)} {match.group(3)}"


def _day_first(match: re.Match[str]) -> str:
    """Space out a glued day-first date, leaving well-formed ones untouched.

    A well-formed ``7 April 1965`` contains two spaces; anything with fewer is
    glued somewhere and gets rebuilt from its three parts.
    """
    if match.group(0).count(" ") < 2:
        return f"{match.group(1)} {match.group(2)} {match.group(3)}"
    return match.group(0)


def _month_year(match: re.Match[str]) -> str:
    """Separate a month from the year glued to it: ``August1995`` -> ``August 1995``."""
    return f"{match.group(1)} {match.group(2)}"


def _split_suffix(match: re.Match[str]) -> str:
    """Insert a space between a complete date and the token glued after it."""
    return f"{match.group(1)} {match.group(2)}"


# Order matters.  The two prefix rules run first so that the month regains a
# word boundary, which the reordering rules below depend on; the suffix rule
# runs last, on dates the earlier rules have already normalised.
_REPAIR_RULES: tuple[_RepairRule, ...] = (
    _RepairRule(
        # "on1 January 1999", "until13 March 2006", "From19 December 1989"
        name="word_glued_before_day",
        pattern=re.compile(
            rf"({_LOWER})(\d{{1,2}}[ ]?(?:{_MONTH_FULL})[ ]?\d{{4}})",
            re.IGNORECASE,
        ),
        replace=_split_prefix,
    ),
    _RepairRule(
        # "untilJanuary152010", "earlyNovember 2000", "NovemberDecember 2001"
        name="word_glued_before_month",
        pattern=re.compile(
            rf"({_LOWER})((?:{_MONTH_FULL})[ ]?\d{{1,4}})",
            re.IGNORECASE,
        ),
        replace=_split_prefix,
    ),
    _RepairRule(
        # "January152010" -> "15 January 2010"; the (?!\d) guard keeps the rule
        # off longer digit runs it could not split unambiguously.
        name="month_first_glued",
        pattern=re.compile(
            rf"\b({_MONTH_FULL})(\d{{1,2}})(\d{{4}})(?!\d)",
            re.IGNORECASE,
        ),
        replace=_month_first,
    ),
    _RepairRule(
        # The original 2026-04 rule.  The corpus now reports zero occurrences;
        # it stays as the regression guard for the shape it was written for.
        name="day_first_glued",
        pattern=re.compile(
            rf"(\d{{1,2}})\s*({_MONTH_ANY})\s*(\d{{4}})",
            re.IGNORECASE,
        ),
        replace=_day_first,
    ),
    _RepairRule(
        # "August1995" -> "August 1995".  Partial precision, but HeidelTime
        # still emits a DATE for it, which is a date the corpus loses today.
        name="month_year_glued",
        pattern=re.compile(
            rf"\b({_MONTH_FULL})(\d{{4}})(?!\d)",
            re.IGNORECASE,
        ),
        replace=_month_year,
    ),
    _RepairRule(
        # "16 November 2005716" -> "16 November 2005 716": a footnote marker
        # fused to the year.  Unambiguous, because a year is exactly four
        # digits -- and the (?!\d) guard means a run long enough to be
        # something else (eight digits, say) matches nothing at all.  Requires
        # the day part, so it only ever splits a date it is sure about.
        name="year_glued_footnote",
        pattern=re.compile(
            rf"(\d{{1,2}} (?:{_MONTH_FULL}) \d{{4}})(\d{{1,3}})(?!\d)",
            re.IGNORECASE,
        ),
        replace=_split_suffix,
    ),
    _RepairRule(
        # "19 December 1989until" -> "19 December 1989 until"
        name="word_glued_after_year",
        pattern=re.compile(
            rf"((?:\d{{1,2}} )?(?:{_MONTH_FULL}) \d{{4}})({_LOWER})",
            re.IGNORECASE,
        ),
        replace=_split_suffix,
    ),
)


def repair_document(document: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Apply every repair rule to *document* in order.

    Args:
        document: Chunk text as stored in ``pg_embedding_qwen.document``.

    Returns:
        A ``(repaired_text, rewrites)`` pair.  ``rewrites`` holds one
        ``(rule_name, before, after)`` triple per individual substitution, in
        the order they were applied; it is empty when the text was already
        well formed.
    """
    rewrites: list[tuple[str, str, str]] = []
    repaired = document

    for rule in _REPAIR_RULES:

        def _apply(match: re.Match[str], rule: _RepairRule = rule) -> str:
            replacement = rule.replace(match)
            if replacement != match.group(0):
                rewrites.append((rule.name, match.group(0), replacement))
            return replacement

        repaired = rule.pattern.sub(_apply, repaired)

    return repaired, rewrites


def _invalidate_chunk(conn, chunk_id: str) -> None:
    """Delete the HeidelTime rows and lineage derived from a repaired chunk.

    Runs on the caller's connection so the invalidation shares the transaction
    that rewrites the text.  Committing one without the other is what produced
    the 87 stale documents in the first place: the text became readable while
    the extractions stayed frozen at their pre-repair contents.

    Args:
        conn: Open SQLAlchemy connection inside the repair transaction.
        chunk_id: ``pg_embedding_qwen.id`` of the chunk that was rewritten.
    """
    for statement in heideltime_invalidation_statements():
        conn.execute(statement, {"chunk_ids": [chunk_id]})


def main() -> None:
    """Scan every chunk for malformed dates, repair them, and invalidate derivatives."""
    parser = argparse.ArgumentParser(
        description="Scan and fix malformed dates in documents."
    )
    parser.add_argument(
        "--celex_id", type=str, help="Specific celex_id to test with", default=None
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report malformed dates without writing changes to the database.",
    )
    args = parser.parse_args()

    # 1. Query the database
    print("Querying database...")
    if args.celex_id:
        query = text("""
            SELECT id, celex_id, document
            FROM celex_facts.pg_embedding_qwen
            WHERE celex_id = :celex_id
        """)
        with engine.connect() as conn:
            df = pl.read_database(
                query,
                conn,
                execute_options={"parameters": {"celex_id": args.celex_id}},
            )
    else:
        query = text("""
            SELECT id, celex_id, document
            FROM celex_facts.pg_embedding_qwen
        """)
        with engine.connect() as conn:
            df = pl.read_database(query, conn)

    print(f"Retrieved {len(df)} rows.")

    # 2. Process records
    results = []
    celex_counts: defaultdict[str, int] = defaultdict(int)
    rule_counts: defaultdict[str, int] = defaultdict(int)
    touched_celex_ids: list[str] = []
    seen_celex_ids: set[str] = set()

    print("Scanning for dates and updating malformed ones...")
    updates_count = 0

    conn_ctx = engine.connect() if args.dry_run else engine.begin()
    with conn_ctx as conn:
        for row in df.iter_rows(named=True):
            doc_text = row["document"]
            # document is a nullable varchar; polars surfaces SQL NULL as None
            # rather than pandas' NaN, so an identity check is the right test.
            if doc_text is None:
                continue

            original_text = str(doc_text)
            new_doc_text, rewrites = repair_document(original_text)
            if not rewrites:
                continue

            chunk_id = str(row["id"])
            for rule_name, before, after in rewrites:
                print(
                    f"Found malformed date '{before}' -> '{after}'"
                    f" [{rule_name}] in celex_id: {row['celex_id']}"
                )
                results.append(
                    {
                        "id": chunk_id,
                        "celex_id": row["celex_id"],
                        "rule": rule_name,
                        "matching_date": before,
                        "fixed_date": after,
                    }
                )
                celex_counts[row["celex_id"]] += 1
                rule_counts[rule_name] += 1

            if row["celex_id"] not in seen_celex_ids:
                seen_celex_ids.add(row["celex_id"])
                touched_celex_ids.append(row["celex_id"])

            if args.dry_run:
                print(
                    f"[DRY RUN] Would update document id {chunk_id}"
                    f" for celex_id {row['celex_id']} and drop its extractions"
                )
            else:
                conn.execute(
                    text(
                        "UPDATE celex_facts.pg_embedding_qwen"
                        " SET document = :new_doc WHERE id = :doc_id"
                    ),
                    {"new_doc": new_doc_text, "doc_id": row["id"]},
                )
                # Same transaction as the UPDATE: the text and everything
                # derived from it move together or not at all.
                _invalidate_chunk(conn, chunk_id)
                print(
                    f"Updated document id {chunk_id} for celex_id {row['celex_id']}"
                )
            updates_count += 1

    # 3. Generate output artefacts
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    output_excel = f"reports/heideltime_qa_dates_fixed_{timestamp}.xlsx"
    # Explicit schema so a clean run (results == []) still writes a sheet with
    # the headers instead of failing on an unknown-width frame.
    results_df = pl.DataFrame(
        results,
        schema={
            "id": pl.Utf8,
            "celex_id": pl.Utf8,
            "rule": pl.Utf8,
            "matching_date": pl.Utf8,
            "fixed_date": pl.Utf8,
        },
    )

    print(f"Saving {len(results_df)} occurrences to {output_excel}...")
    Path(output_excel).parent.mkdir(parents=True, exist_ok=True)
    results_df.write_excel(output_excel)

    # The CELEX list feeds the re-extraction step:
    #   Main_ingest_generic_facts.py --celex-list-file <this> \
    #       --filter-fact heideltime_raw --limit 0 --overwrite
    celex_list_path = Path(f"reports/heideltime_stale_celex_{timestamp}.txt")
    celex_list_path.write_text(
        "".join(f"{celex_id}\n" for celex_id in touched_celex_ids),
        encoding="utf-8",
    )
    print(
        f"Wrote {len(touched_celex_ids)} touched CELEX ids to {celex_list_path}."
    )

    # 4. Print summary
    update_label = "would update" if args.dry_run else "updates"
    print(f"\n--- Summary: Total database {update_label}: {updates_count} ---")

    print("\n--- Summary: Number of dates fixed per rule ---")
    for rule_name, count in sorted(
        rule_counts.items(), key=lambda item: item[1], reverse=True
    ):
        print(f"rule: {rule_name} -> {count} dates")

    print("\n--- Summary: Number of dates fixed per celex_id ---")
    for celex_id, count in sorted(
        celex_counts.items(), key=lambda item: item[1], reverse=True
    ):
        print(f"celex_id: {celex_id} -> {count} dates")

    print("\nDone.")


if __name__ == "__main__":
    main()

# .venv/bin/python src/pipelines/heideltime/qa/scan_dates.py --celex_id CELEX-31965D0426
