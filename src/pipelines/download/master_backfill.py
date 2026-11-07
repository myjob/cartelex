"""Master backfill mapper of the HTTP document downloader.

Turns every entry of ``master_doc_descriptors.json`` into one
``DecisionRequest`` so the entries already on disk are registered in the
document registry (with ``is_source`` and the case links) without any
download; run it with ``--offline``. The initial loader spreadsheet enriches
an entry (title, parties, date, all columns as meta) when its ``url_pdf``
matches the entry's ``url_pdf`` under ``identifiers.pdf_url_key``.

Case ids come from ``celex_case_id``: first the rows of the master key, else
the loader's raw ``case.x``, else the entry's ``webDocDescriptor.case``, each
looked up as ``case_x`` in the same table. Several distinct case ids for one
raw case are never guessed (review reason).
"""

import datetime as dt
import logging
from pathlib import Path
from typing import ClassVar, Protocol

import polars as pl

from pipelines.download.identifiers import (
    UnsafeIdentifier,
    celex_key,
    pdf_url_key,
)
from pipelines.download.mapping_rules import build_decision, cell, clean, parse_date
from pipelines.download.models import MapperResult, RowFailure

logger = logging.getLogger(__name__)

REASON_CASE_UNRESOLVED = "case id unresolved"
REASON_AMBIGUOUS_CASE_X = "ambiguous case_x"
CASE_X_COLUMN = "case.x"
URL_COLUMN = "url_pdf"
_DESCRIPTOR = "webDocDescriptor"
_DEC_PARTS = ("dec_year", "dec_month", "dec_day")


class CaseLookup(Protocol):
    """Read-only case id lookup over ``celex_case_id``."""

    def case_ids_for(self, key: str) -> list[str]:
        """Return the normalised case ids of rows with ``celex_id == key``.

        Args:
            key: Master key (``CELEX-...`` or a master style key).

        Returns:
            Distinct case ids, sorted; empty when the key has no row.
        """
        ...

    def normalise_case_x(self, raw: str) -> list[str]:
        """Return the normalised case ids of rows with ``case_x == raw``.

        Args:
            raw: Raw case number as in the initial loader (e.g. ``"61"``).

        Returns:
            Distinct case ids, sorted; empty when unknown.
        """
        ...


class BookCaseSource(Protocol):
    """Source of the ``celex_case_id`` rows (``CelexIdentityRepository``)."""

    def select_distinct_book_cases(self) -> list[dict]:
        """Return rows with keys ``celex_id``, ``case_x`` and ``case_id``."""
        ...


class CelexCaseLookup:
    """``CaseLookup`` over one read-only snapshot of ``celex_case_id``."""

    def __init__(self, source: BookCaseSource) -> None:
        """Read the snapshot once (Provisioner pattern: the source is injected).

        Args:
            source: Repository offering ``select_distinct_book_cases``.
        """
        by_key: dict[str, set[str]] = {}
        by_x: dict[str, set[str]] = {}
        for row in source.select_distinct_book_cases():
            case_id = clean(row.get("case_id"))
            if not case_id:
                continue
            for index, name in ((by_key, "celex_id"), (by_x, "case_x")):
                value = clean(row.get(name))
                if value:
                    index.setdefault(value, set()).add(case_id)
        self._by_key = {k: sorted(v) for k, v in by_key.items()}
        self._by_x = {k: sorted(v) for k, v in by_x.items()}

    def case_ids_for(self, key: str) -> list[str]:
        """Return the case ids of a master key.

        Args:
            key: Master key.

        Returns:
            Distinct case ids, sorted.
        """
        return list(self._by_key.get(key, []))

    def normalise_case_x(self, raw: str) -> list[str]:
        """Return the case ids of a raw ``case.x``.

        Args:
            raw: Raw case number (surrounding blanks are ignored).

        Returns:
            Distinct case ids, sorted.
        """
        return list(self._by_x.get(raw.strip(), []))


def _loader_date(row: dict[str, object]) -> dt.date | None:
    """Read the decision date of an initial loader row.

    Args:
        row: Loader row.

    Returns:
        ``decision_date`` when parseable, else the date built from
        ``dec_year``/``dec_month``/``dec_day``, else None.
    """
    parsed = parse_date(cell(row, "decision_date"))
    if parsed is not None:
        return parsed
    try:
        year, month, day = (int(float(cell(row, p))) for p in _DEC_PARTS)
        return dt.date(year, month, day)
    except ValueError:
        return None


class MasterBackfillMapper:
    """One decision per master entry, enriched from the initial loader."""

    needs_file_manifest: ClassVar[bool] = False
    needs_master: ClassVar[bool] = True

    def __init__(
        self,
        master: dict[str, dict],
        initial_loader: pl.DataFrame | None,
        cases: CaseLookup,
    ) -> None:
        """Index the initial loader by ``pdf_url_key`` (first row wins).

        Args:
            master: The ``documents`` object of the master JSON, in order.
            initial_loader: Initial loader frame (all strings) or None.
            cases: Case id lookup over ``celex_case_id``.
        """
        self._master = master
        self._cases = cases
        self._rows: list[dict[str, object]] = (
            list(initial_loader.iter_rows(named=True))
            if initial_loader is not None else [])
        self._by_url: dict[str, list[int]] = {}
        for pos, row in enumerate(self._rows, start=1):
            url = cell(row, URL_COLUMN)
            if url:
                self._by_url.setdefault(pdf_url_key(url), []).append(pos)

    def map(self, df: pl.DataFrame, loader_file: Path) -> MapperResult:
        """Map every master entry; ``df`` is ignored (the master is injected).

        Args:
            df: Unused; the CLI does not read the master JSON as a frame.
            loader_file: Master JSON path (its name is recorded on every row).

        Returns:
            One decision per usable entry (decision id and existing key are the
            master key, ``loader_row`` is the 1-based position in the master
            ``documents`` order) and a row failure per unusable entry.
        """
        result = MapperResult()
        for position, (key, entry) in enumerate(self._master.items(), start=1):
            web = entry.get(_DESCRIPTOR) or {}
            url = clean(web.get(URL_COLUMN))
            if not url:
                result.failures.append(RowFailure(
                    loader_file=loader_file.name, loader_row=position,
                    reason=f"master entry {key} has no url_pdf"))
                continue
            self._map_entry(key, entry, web, url, position, loader_file, result)
        return result

    def _map_entry(
        self, key: str, entry: dict, web: dict, url: str, position: int,
        loader_file: Path, result: MapperResult,
    ) -> None:
        """Build the decision of one master entry.

        Args:
            key: Master key.
            entry: Master entry.
            web: Its ``webDocDescriptor``.
            url: Its ``url_pdf``.
            position: 1-based position in the master order.
            loader_file: Master JSON path.
            result: Result to append to.
        """
        reasons: list[str] = []
        row = self._loader_row(url, reasons)
        loader = row or {}
        meta = {k: clean(v) for k, v in loader.items()}
        for name, value in web.items():
            if not meta.get(name):
                meta[name] = clean(value)
        case_ids, case_reasons = self._resolve_cases(
            key, [cell(loader, CASE_X_COLUMN), clean(web.get("case"))])
        reasons.extend(case_reasons)
        before = len(result.decisions)
        build_decision(
            loader_file=loader_file, row_no=position, celex=_celex_of(key),
            cases=case_ids,
            date=_loader_date(loader) or parse_date(clean(web.get("decision_date"))),
            fields={"title": cell(loader, "title") or clean(web.get("title")),
                    "parties": (cell(loader, "firms") or cell(loader, "Parties")
                                or clean(web.get("firms")))},
            meta=meta, reasons=reasons, urls=[(url, "pdf", None)], result=result,
            existing_key=key)
        if len(result.decisions) == before:
            return
        language = clean(entry.get("language_init")).upper()
        for candidate in result.decisions[-1].candidates:
            if candidate.language is None and language and language != "NA":
                candidate.language = language

    def _loader_row(self, url: str, reasons: list[str]) -> dict[str, object] | None:
        """Find the initial loader row of a master URL.

        Args:
            url: The entry's ``url_pdf``.
            reasons: Review reasons (a duplicate-URL reason is appended).

        Returns:
            The first matching loader row, or None.
        """
        positions = self._by_url.get(pdf_url_key(url))
        if not positions:
            return None
        if len(positions) > 1:
            others = ", ".join(str(p) for p in positions[1:])
            reasons.append(f"duplicate url_pdf in initial loader: using row "
                           f"{positions[0]}, ignored rows {others}")
        return self._rows[positions[0] - 1]

    def _resolve_cases(
        self, key: str, raw_cases: list[str]
    ) -> tuple[list[str], list[str]]:
        """Resolve case ids: master key rows, loader case.x, descriptor case.

        Args:
            key: Master key.
            raw_cases: Raw case numbers in priority order: the matched loader
                row's ``case.x``, then the entry's ``webDocDescriptor.case``
                (blank ones are skipped).

        Returns:
            Case ids and review reasons (never a guess among several ids).
        """
        found = self._cases.case_ids_for(key)
        if found:
            return found, []
        for case_x in filter(None, raw_cases):
            found = self._cases.normalise_case_x(case_x)
            if len(found) == 1:
                return found, []
            if found:
                logger.info("master %s: case_x %s is ambiguous: %s", key, case_x,
                            ", ".join(found))
                return [], [REASON_AMBIGUOUS_CASE_X]
        return [], [REASON_CASE_UNRESOLVED]


def _celex_of(key: str) -> str:
    """Return the real CELEX key of a master key, else blank.

    Args:
        key: Master key.

    Returns:
        ``celex_key(key)`` for CELEX syntax, otherwise ``""``.
    """
    try:
        return celex_key(key)
    except UnsafeIdentifier:
        return ""
