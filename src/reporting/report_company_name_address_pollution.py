"""
Report: which CELEX documents have registered addresses baked into the company
names of their ``companies_investigated`` fact.

The ``companies_investigated`` question asks for the parties "named as parties or
addressees of this decision ... Report each name verbatim", and a decision's
addressee block prints the name and the postal address on one line. The model
reports the whole line, so the fact stores::

    Praoil, Strada 2, Pal. F7, 20090 Assago, Milan, Italy

instead of ``Praoil``. Such a name matches no commercial register, and it is the
join key every dependent fact and the roster ETL work from.

This report scans the FACTS, not the roster table: the roster's ``company_name``
was already repaired in place by
``migrate_company_investigated_name_pollution.sql``, so the roster no longer shows
the problem while the facts it is rebuilt from still do. Re-running an extraction
is what fixes a fact, and this report produces the CELEX list to re-run.

Two independent detectors, because they fail in opposite directions:

``fact``
    A name is polluted when the document's ``companies_address`` fact supplies an
    address that is an exact suffix of the name. Near-zero false positives -- two
    independent extractions agree on the same string -- but blind to any document
    whose ``companies_address`` fact is missing or ``NA``.

``heuristic``
    A name is polluted when it reads like "name, street, postcode city, country":
    comma-separated, with a trailing country name and/or a postcode-shaped
    segment. Needs no second fact, so it covers the documents ``fact`` cannot see,
    at the cost of some false positives on names that genuinely contain a place.

Exports:
    has_address_suffix -- corroborated (fact-based) test for one name.
    looks_like_address_suffix -- standalone heuristic test for one name.
    scan_facts -- build the per-CELEX report frame.

Usage:
    # Human-readable summary of every affected document:
    PYTHONPATH=src .venv/bin/python \\
        src/reporting/report_company_name_address_pollution.py

    # Bare CELEX ids for --celex-list-file, both detectors:
    PYTHONPATH=src .venv/bin/python \\
        src/reporting/report_company_name_address_pollution.py \\
        --detector both --ids-only --out /tmp/polluted_celex.txt

    # Then re-extract just those documents:
    PYTHONPATH=src .venv/bin/python src/batch/Main_ingest_generic_facts.py \\
        --filter-fact companies_investigated \\
        --celex-list-file /tmp/polluted_celex.txt
"""

import argparse
import logging
import re
import sys
import unicodedata

# The shared logging config (config_main/logging_config.yaml) binds its console
# handler to `ext://sys.stdout`, which dictConfig resolves to whatever sys.stdout
# is AT CONFIGURATION TIME -- and that configuration happens while the project
# imports below run, during which third-party libraries emit ~14 DEBUG records.
# Pointing sys.stdout at stderr for the duration of those imports makes the
# handler bind to stderr permanently, so this report owns stdout outright and a
# --celex-list-file built from --ids-only cannot be corrupted by log lines.
# Redirecting after the fact is too late: the records are already written.
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr
try:
    import polars as pl  # noqa: E402
    import pycountry  # noqa: E402

    from batch.Main_ingest_company_investigated2celex import (  # noqa: E402
        ADDRESS_FACT_NAME,
        FACT_NAME,
        derive_clean_company_name,
        extract_company_names,
        extract_enrichment_map,
    )
    from pipelines.rag.pgvector.vector_config import (  # noqa: E402
        DATABASE_URL,
        SCHEMA,
    )
finally:
    sys.stdout = _REAL_STDOUT

logger = logging.getLogger(__name__)

# Minimum comma-separated segments before a name is even considered address-like.
# "Ashland Oil, Inc." (2 segments) is a legal form, not an address; "Praoil,
# Strada 2, ... Italy" is not.
_MIN_ADDRESS_SEGMENTS = 3

# A segment carrying a postal code: any run of 4-6 digits ('20090', 'NY-10036',
# 'FIN-02151'), or a UK-style outward/inward pair ('EH4 1RR', 'EC2M 7BA').
_POSTCODE_RE = re.compile(r"\d{4,6}|\b[A-Z]{1,2}\d{1,2}[A-Z]?\s+\d[A-Z]{2}\b")

# Country spellings these decisions use that ISO does not carry: historical
# states, informal short forms and pre-1990 names. Lowercased for comparison.
_COUNTRY_ALIASES: frozenset[str] = frozenset(
    {
        "usa",
        "u.s.a.",
        "united states of america",
        "uk",
        "u.k.",
        "great britain",
        "england",
        "scotland",
        "wales",
        "northern ireland",
        "holland",
        "west germany",
        "federal republic of germany",
        "east germany",
        "czechoslovakia",
        "yugoslavia",
        "ussr",
        "soviet union",
        "korea",
        "russia",
        "turkey",
        "ivory coast",
        "swaziland",
    }
)


def _country_names() -> frozenset[str]:
    """Return every country spelling recognised as an address tail.

    Combines the ISO 3166-1 names, official names and common names from
    ``pycountry`` with :data:`_COUNTRY_ALIASES`, all casefolded.

    Returns:
        The set of accepted lowercase country spellings.
    """
    names: set[str] = set(_COUNTRY_ALIASES)
    for country in pycountry.countries:
        for attribute in ("name", "official_name", "common_name"):
            value = getattr(country, attribute, None)
            if value:
                names.add(value.casefold())
    return frozenset(names)


_COUNTRY_NAMES = _country_names()


def _segments(company_name: str) -> list[str]:
    """Split a company name on commas into stripped, NFKC-normalized segments.

    Args:
        company_name: The name to split.

    Returns:
        The non-empty segments, in order.
    """
    normalized = unicodedata.normalize("NFKC", company_name)
    return [segment.strip() for segment in normalized.split(",") if segment.strip()]


def has_address_suffix(company_name: str, address: str | None) -> bool:
    """Report whether a name ends with the address the address fact gave it.

    The corroborated detector. Delegates to
    :func:`~batch.Main_ingest_company_investigated2celex.derive_clean_company_name`
    so the report and the ingest agree by construction on what counts as
    polluted -- if this says yes, the ETL will strip it.

    Args:
        company_name: The verbatim name from ``companies_investigated``.
        address: The address matched to it, or None when unknown.

    Returns:
        True when the name carries the address as a strippable suffix.
    """
    return derive_clean_company_name(company_name, address) != company_name


def looks_like_address_suffix(company_name: str) -> bool:
    """Report whether a name reads like "name, street, postcode city, country".

    The standalone detector, for documents with no usable ``companies_address``
    fact. Requires at least :data:`_MIN_ADDRESS_SEGMENTS` comma-separated
    segments plus a positional signal: a trailing country name, or a
    postcode-shaped segment anywhere after the first.

    Both conditions are positional on purpose. Requiring the country to be the
    LAST segment keeps "Bank of Ireland" and "Air France" out, and ignoring the
    first segment when looking for a postcode keeps a company whose own name
    contains digits from flagging itself.

    Args:
        company_name: The name to test.

    Returns:
        True when the name looks address-bearing.
    """
    segments = _segments(company_name)
    if len(segments) < _MIN_ADDRESS_SEGMENTS:
        return False
    if segments[-1].casefold() in _COUNTRY_NAMES:
        return True
    return any(_POSTCODE_RE.search(segment) for segment in segments[1:])


def _load_facts(db_url: str = DATABASE_URL) -> dict[str, dict[str, str]]:
    """Load the newest companies_investigated / companies_address fact per CELEX.

    Args:
        db_url: PostgreSQL connection string.

    Returns:
        Mapping of celex_id to {fact name: value_json}.
    """
    query = f"""
    WITH newest AS (
        SELECT celex_id,
               name,
               value_json,
               ROW_NUMBER() OVER (
                   PARTITION BY celex_id, name ORDER BY created_at DESC
               ) AS rn
        FROM {SCHEMA}.facts
        WHERE name IN ('{FACT_NAME}', '{ADDRESS_FACT_NAME}')
    )
    SELECT celex_id, name, value_json FROM newest WHERE rn = 1
    """
    # connector-x does not understand SQLAlchemy dialect suffixes.
    clean_url = db_url.replace("+psycopg2", "").replace("+psycopg", "")
    frame = pl.read_database_uri(query=query, uri=clean_url)

    by_celex: dict[str, dict[str, str]] = {}
    for row in frame.iter_rows(named=True):
        by_celex.setdefault(row["celex_id"], {})[row["name"]] = row["value_json"]
    return by_celex


def scan_facts(db_url: str = DATABASE_URL, detector: str = "both") -> pl.DataFrame:
    """Scan every stored ``companies_investigated`` fact for address pollution.

    Args:
        db_url: PostgreSQL connection string.
        detector: ``fact`` (corroborated only), ``heuristic`` (standalone only)
            or ``both`` (a name counts when either fires).

    Returns:
        One row per affected CELEX document, most affected first, with the
        company counts, which detectors fired, and a worked example.
    """
    rows: list[dict[str, object]] = []
    for celex_id, facts in _load_facts(db_url).items():
        names = extract_company_names(facts.get(FACT_NAME))
        if not names:
            continue
        address_map = extract_enrichment_map(facts.get(ADDRESS_FACT_NAME))

        by_fact = [
            name for name in names if has_address_suffix(name, address_map.get(name))
        ]
        by_heuristic = [name for name in names if looks_like_address_suffix(name)]
        # Dependent facts keyed by names companies_investigated no longer uses.
        # Once companies_investigated is re-extracted clean, the older
        # companies_address still echoes the OLD polluted entity, so the two
        # facts no longer join and the address fact needs re-running too.
        stale = [key for key in address_map if key not in set(names)]

        match detector:
            case "fact":
                affected = by_fact
            case "heuristic":
                affected = by_heuristic
            case "stale":
                affected = stale
            case _:
                affected = [
                    name
                    for name in names
                    if name in set(by_fact) | set(by_heuristic)
                ]
        if not affected:
            continue

        example = affected[0]
        rows.append(
            {
                "celex_id": celex_id,
                "companies": len(names),
                "affected": len(affected),
                "by_fact": len(by_fact),
                "by_heuristic": len(by_heuristic),
                "stale_address_keys": len(stale),
                # Only the corroborated detector knows the true clean name; the
                # heuristic can say "this is polluted" but not where to cut.
                "has_address_fact": bool(address_map),
                "example": example,
                "example_clean": derive_clean_company_name(
                    example, address_map.get(example)
                ),
            }
        )

    if not rows:
        return pl.DataFrame(
            schema={
                "celex_id": pl.String,
                "companies": pl.Int64,
                "affected": pl.Int64,
                "by_fact": pl.Int64,
                "by_heuristic": pl.Int64,
                "stale_address_keys": pl.Int64,
                "has_address_fact": pl.Boolean,
                "example": pl.String,
                "example_clean": pl.String,
            }
        )
    return pl.DataFrame(rows).sort(
        ["affected", "celex_id"], descending=[True, False]
    )


def main() -> None:
    """Entry point: report the CELEX documents whose company names carry addresses."""
    parser = argparse.ArgumentParser(
        description=(
            "List CELEX documents whose companies_investigated fact stores "
            "company names with the registered address appended."
        )
    )
    parser.add_argument(
        "--detector",
        choices=("fact", "heuristic", "both", "stale"),
        default="both",
        help=(
            "fact: address fact corroborates the suffix (precise, needs the "
            "address fact). heuristic: name looks like an address (standalone). "
            "both: either (default) -- these three list documents whose "
            "companies_investigated needs re-extracting. stale: "
            "companies_address is keyed by names companies_investigated no "
            "longer uses, so the dependent fact needs re-running to resync."
        ),
    )
    parser.add_argument(
        "--ids-only",
        action="store_true",
        help="Print bare celex_ids, one per line, for --celex-list-file.",
    )
    parser.add_argument(
        "--out",
        help="Write the output to this file instead of stdout.",
    )
    args = parser.parse_args()

    report = scan_facts(detector=args.detector)
    if report.is_empty():
        print(f"No affected CELEX documents (detector={args.detector}).")
        return

    if args.ids_only:
        payload = "\n".join(report["celex_id"].to_list())
    else:
        with pl.Config(
            tbl_rows=-1, tbl_cols=-1, fmt_str_lengths=60, tbl_width_chars=200
        ):
            if args.detector == "stale":
                breakdown = (
                    f"Re-run '{ADDRESS_FACT_NAME}' for these to resync them with "
                    f"'{FACT_NAME}'."
                )
            else:
                breakdown = (
                    f"Corroborated by the address fact: "
                    f"{report['by_fact'].sum()}; heuristic-only: "
                    f"{report['affected'].sum() - report['by_fact'].sum()}.\n"
                    f"Re-run '{FACT_NAME}' for these."
                )
            payload = (
                f"{report}\n\n"
                f"{report.height} CELEX document(s), "
                f"{report['affected'].sum()} name(s) affected "
                f"(detector={args.detector}).\n{breakdown}"
            )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        print(f"Wrote {report.height} CELEX id(s) to {args.out}", file=sys.stderr)
    else:
        print(payload)


if __name__ == "__main__":
    main()
