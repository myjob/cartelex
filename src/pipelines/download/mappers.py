"""Loader mappers of the HTTP document downloader.

A mapper turns a loader frame (see ``reader.read_loader``) into
``DecisionRequest`` objects plus audited ``RowFailure`` objects. The shared
row rules, the ``loader_row`` convention and the provisional-id contract
(decision ids and candidate document ids are re-derived by the orchestrator
after final decision ids are assigned) live in ``mapping_rules``.
"""

import logging
import re
from pathlib import Path
from typing import ClassVar, Protocol

import polars as pl

from pipelines.download.identifiers import UnsafeIdentifier, document_id_for
from pipelines.download.mapping_rules import (
    CELEX_IN_URL,
    PIPE,
    REASON_DUP_CELEX,
    build_decision,
    case_ids,
    cell,
    collect_meta,
    duplicate_celex,
    infer_format,
    is_duplicate,
    parse_date,
    split_cell,
)
from pipelines.download.master_backfill import CaseLookup, MasterBackfillMapper
from pipelines.download.models import MapperResult, RowFailure

logger = logging.getLogger(__name__)

# Accepted header names of the case column; the first alias present wins.
CASE_COLUMN_ALIASES: tuple[str, ...] = ("case", "case.x")
_CASE_INDEX_USED = {"celex", "title", "decision_date", "firms", "url_pdf", "url_html"}
_FINAL_USED = {"celex", "case_numbers", "date", "title", "parties", "document_class"}
_FINAL_DETECT = frozenset({"manifest_row_id", "case_numbers"})
_FINAL_REQUIRED = ("manifest_row_id", "source_url", "file_format")
_DUPLICATED = re.compile(r"^(?P<base>.+)_duplicated_\d+$")
_REVIEW_STATUS = "review"
_UNKNOWN_FORMAT = "other"


class LoaderMapper(Protocol):
    """Maps a loader frame to decisions."""

    needs_file_manifest: ClassVar[bool]
    needs_master: ClassVar[bool]

    def map(self, df: pl.DataFrame, loader_file: Path) -> MapperResult:
        """Map all rows of a loader frame.

        Args:
            df: Frame from ``read_loader`` (all columns strings).
            loader_file: Path of the loader file (recorded on every row).

        Returns:
            Mapped decisions and row failures; ``loader_row`` is the 1-based
            position among the data rows as read.
        """
        ...


class UrlListMapper:
    """One decision per URL (``url`` column), no case ids, no meta."""

    needs_file_manifest: ClassVar[bool] = False
    needs_master: ClassVar[bool] = False

    def map(self, df: pl.DataFrame, loader_file: Path) -> MapperResult:
        """Map a URL list.

        The decision id is the CELEX key for eur-lex URLs carrying
        ``uri=CELEX:...``, otherwise the URL's file stem (as master key).

        Args:
            df: Frame with a ``url`` column.
            loader_file: Loader path.

        Returns:
            Decisions and failures; ``loader_row`` is the 1-based position
            among the URLs.
        """
        result = MapperResult()
        for row_no, row in enumerate(df.iter_rows(named=True), start=1):
            url = cell(row, "url")
            match = CELEX_IN_URL.search(url)
            stem: str | None = None
            if match is None:
                try:
                    stem = document_id_for(url, "")
                except UnsafeIdentifier as exc:
                    result.failures.append(RowFailure(
                        loader_file=loader_file.name, loader_row=row_no,
                        reason=str(exc)))
                    continue
            build_decision(
                loader_file=loader_file, row_no=row_no,
                celex=match.group(1) if match else "", cases=[], date=None,
                fields={}, meta={}, reasons=[], urls=[(url, "", None)],
                result=result, existing_key=stem)
        return result


def _drop_duplicated_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Drop polars ``<name>_duplicated_<n>`` columns whose base column exists.

    Args:
        df: Loader frame.

    Returns:
        Frame without the shadow duplicates.
    """
    drop = [c for c in df.columns
            if (m := _DUPLICATED.match(c)) and m["base"] in df.columns]
    return df.drop(drop)


def missing_case_index_columns(columns: list[str]) -> list[str]:
    """List the headers a case index frame lacks.

    Args:
        columns: Frame column names.

    Returns:
        Missing required names (``celex`` and one case alias); empty when
        the frame can be mapped by :class:`CaseIndexMapper`.
    """
    missing = [] if "celex" in columns else ["celex"]
    if not any(a in columns for a in CASE_COLUMN_ALIASES):
        missing.append(" or ".join(CASE_COLUMN_ALIASES))
    return missing


class CaseIndexMapper:
    """Legacy case index: ``celex, case, url_pdf, url_html, ...`` by header."""

    needs_file_manifest: ClassVar[bool] = False
    needs_master: ClassVar[bool] = False

    def map(self, df: pl.DataFrame, loader_file: Path) -> MapperResult:
        """Map a legacy case index frame by header name.

        The ``celex`` column is the decision key. A value that is not a
        usable CELEX number (master keys such as ``33585_2_4``) becomes the
        decision id with ``celex_number`` None and a review reason.

        Args:
            df: Frame with (some of) celex, case or case.x, url_pdf,
                url_html, year, comment, title, decision_date, firms, nace.
                Polars ``*_duplicated_*`` columns are ignored when their
                base column exists.
            loader_file: Loader path.

        Returns:
            Decisions and failures; the URL columns become candidates (format
            inferred from the URL), the other columns go to meta.
            ``loader_row`` is the 1-based position among the data rows.
        """
        result = MapperResult()
        df = _drop_duplicated_columns(df)
        case_col = next((a for a in CASE_COLUMN_ALIASES if a in df.columns), None)
        used = _CASE_INDEX_USED | ({case_col} if case_col else set())
        rows = list(df.iter_rows(named=True))
        dups = duplicate_celex([cell(r, "celex") for r in rows])
        for row_no, row in enumerate(rows, start=1):
            celex = cell(row, "celex")
            reasons: list[str] = []
            if celex and is_duplicate(celex, dups):
                reasons.append(REASON_DUP_CELEX)
            raw_date = cell(row, "decision_date")
            date = parse_date(raw_date)
            if raw_date and date is None:
                reasons.append(f"unparseable decision date: {raw_date}")
            build_decision(
                loader_file=loader_file, row_no=row_no, celex=celex,
                cases=case_ids(cell(row, case_col) if case_col else ""),
                date=date,
                fields={"title": cell(row, "title"),
                        "parties": cell(row, "firms")},
                meta=collect_meta(row, used), reasons=reasons,
                urls=[(cell(row, "url_pdf"), "", None),
                      (cell(row, "url_html"), "", None)],
                result=result, celex_is_master_key=True)
        return result


class FinalManifestMapper:
    """``filtered_final_manifest.xlsx`` joined with the per-file manifest."""

    needs_file_manifest: ClassVar[bool] = True
    needs_master: ClassVar[bool] = False

    def __init__(self, file_manifest: pl.DataFrame) -> None:
        """Store the per-file manifest.

        Args:
            file_manifest: Frame of ``final_commission_file_manifest.xlsx``
                with at least manifest_row_id, source_url, file_format.

        Raises:
            ValueError: If it is missing or lacks a required column. Pipe
                cells are never split positionally as a fallback.
        """
        if not isinstance(file_manifest, pl.DataFrame):
            raise ValueError("FinalManifestMapper requires the per-file manifest")
        missing = [c for c in _FINAL_REQUIRED if c not in file_manifest.columns]
        if missing:
            raise ValueError(f"per-file manifest lacks columns: {missing}")
        self._files: dict[str, tuple[str, str]] = {}
        duplicates = 0
        for rec in file_manifest.iter_rows(named=True):
            rid = cell(rec, "manifest_row_id")
            duplicates += rid in self._files
            self._files[rid] = (cell(rec, "source_url"), cell(rec, "file_format"))
        if duplicates:
            logger.warning(
                "per-file manifest: %d duplicate manifest_row_id (last wins)",
                duplicates)

    def map(self, df: pl.DataFrame, loader_file: Path) -> MapperResult:
        """Map the filtered manifest.

        Args:
            df: Filtered manifest frame.
            loader_file: Loader path.

        Returns:
            Decisions and failures. Candidates come only from the per-file
            records of ``manifest_row_id``; the pipe cells ``source_url``,
            ``raw_path`` and ``file_format`` of the row are kept verbatim in
            meta and never interpreted. ``loader_row`` is the 1-based
            position among the data rows as read.

        Raises:
            ValueError: If none of the exploded manifest_row_ids matches a
                per-file record (wrong file or wrong columns).
        """
        result = MapperResult()
        rows = list(df.iter_rows(named=True))
        ids_per_row = [split_cell(cell(r, "manifest_row_id"), PIPE) for r in rows]
        total = sum(len(ids) for ids in ids_per_row)
        matched = sum(1 for ids in ids_per_row for i in ids if i in self._files)
        if total and not matched:
            raise ValueError(
                "none of the manifest_row_ids matches the per-file manifest")
        blank_urls = 0
        dups = duplicate_celex([cell(r, "celex") for r in rows])
        for row_no, (row, ids) in enumerate(zip(rows, ids_per_row), start=1):
            celex = cell(row, "celex")
            reasons: list[str] = []
            if celex and is_duplicate(celex, dups):
                reasons.append(REASON_DUP_CELEX)
            raw_date = cell(row, "date")
            dates = split_cell(raw_date, PIPE)
            if len(dates) > 1:
                reasons.append(f"two decision dates: {raw_date}")
            date = parse_date(dates[0]) if dates else None
            if dates and date is None:
                reasons.append(f"unparseable decision date: {dates[0]}")
            if cell(row, "processing_status").lower() == _REVIEW_STATUS:
                reasons.append("upstream processing_status review")
            urls: list[tuple[str, str, str | None]] = []
            unmatched: list[str] = []
            for rid in ids:
                rec = self._files.get(rid)
                if rec is None:
                    unmatched.append(rid)
                elif rec[0]:
                    fmt = rec[1].lower() or infer_format(rec[0], _UNKNOWN_FORMAT)
                    urls.append((rec[0], fmt, rid))
                else:
                    blank_urls += 1
            if unmatched:
                reasons.append("unmatched manifest_row_id: " + ", ".join(unmatched))
            build_decision(
                loader_file=loader_file, row_no=row_no, celex=celex,
                cases=case_ids(cell(row, "case_numbers")), date=date,
                fields={"title": cell(row, "title"),
                        "parties": cell(row, "parties"),
                        "document_class": cell(row, "document_class")},
                meta=collect_meta(row, _FINAL_USED), reasons=reasons, urls=urls,
                result=result)
        if total != matched or blank_urls:
            logger.warning(
                "final manifest: %d unmatched manifest_row_ids, %d per-file "
                "records with blank URL", total - matched, blank_urls)
        return result


MAPPERS: dict[str, type[LoaderMapper]] = {
    "url_list": UrlListMapper,
    "case_index": CaseIndexMapper,
    "final_manifest": FinalManifestMapper,
    "master_backfill": MasterBackfillMapper,
}


def mapper_for(
    path: Path, name: str | None, columns: list[str] | None = None
) -> type[LoaderMapper]:
    """Select the mapper class by name, else by headers, else by extension.

    Args:
        path: Loader file path.
        name: Registry name or None for auto-detection.
        columns: Loader frame columns; when given and no name is chosen, a
            frame with ``manifest_row_id`` and ``case_numbers`` selects
            :class:`FinalManifestMapper`.

    Returns:
        The mapper class (``.txt`` -> UrlListMapper; final manifest headers ->
        FinalManifestMapper; others -> CaseIndexMapper when no name is given).

    Raises:
        ValueError: If the name is not in ``MAPPERS``.
    """
    if name is None:
        if columns is not None and _FINAL_DETECT.issubset(columns):
            logger.info("loader %s has final manifest headers: using final_manifest",
                        path.name)
            return FinalManifestMapper
        return UrlListMapper if path.suffix.lower() == ".txt" else CaseIndexMapper
    try:
        return MAPPERS[name]
    except KeyError:
        raise ValueError(f"unknown mapper {name!r}; known: {sorted(MAPPERS)}") from None


def build_mapper(
    path: Path,
    name: str | None,
    file_manifest: pl.DataFrame | None,
    columns: list[str] | None = None,
    *,
    master: dict[str, dict] | None = None,
    initial_loader: pl.DataFrame | None = None,
    cases: CaseLookup | None = None,
) -> LoaderMapper:
    """Instantiate the selected mapper, passing the per-file manifest if needed.

    Args:
        path: Loader file path.
        name: Registry name or None.
        file_manifest: Per-file manifest frame (used only by mappers with
            ``needs_file_manifest``).
        columns: Loader frame columns; when given and no mapper name was
            chosen, the default case index mapper is checked against them.
        master: Master ``documents`` dict (only mappers with ``needs_master``).
        initial_loader: Initial loader frame (only mappers with ``needs_master``).
        cases: Case id lookup (only mappers with ``needs_master``).

    Returns:
        A ready mapper instance.

    Raises:
        ValueError: Unknown name, a mapper needing the per-file manifest or
            the master got none, or the default mapper's headers are missing
            (use --mapper).
    """
    cls = mapper_for(path, name, columns)
    if name is None and columns is not None and cls is CaseIndexMapper:
        missing = missing_case_index_columns(columns)
        if missing:
            raise ValueError(
                f"default case index mapper needs columns {missing}; "
                f"choose one with --mapper {sorted(MAPPERS)}")
    if cls.needs_master:
        if master is None or cases is None:
            raise ValueError(f"mapper {name!r} requires the master and a case lookup")
        return cls(master, initial_loader, cases)  # type: ignore[call-arg]
    if cls.needs_file_manifest:
        if file_manifest is None:
            raise ValueError(f"mapper {name!r} requires the per-file manifest")
        return cls(file_manifest)  # type: ignore[call-arg]
    return cls()
