
"""Atomic, merge-only writer of the master document descriptor JSON.

The master file is ``{"documents": {<decision_id>: {...}}}``. This store
loads it once, merges downloader results in memory and writes it back with
temp file + ``os.replace`` after a one-off backup. Existing entries are only
touched in ``case_ids`` and the standard ``webDocDescriptor`` keys, and only
where the stored value is blank; MinerU/fact fields, unknown keys and every
non-blank stored value are preserved. Loader meta keys beyond the standard
set are written into new entries only (deviation from the design spec's
"webDocDescriptor keys the loader supplies"; ``decision_registry.meta`` keeps
all of them).
"""

import copy
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from pipelines.download.identifiers import pdf_url_key
from pipelines.download.local_pdf import markdown_path_for
from pipelines.download.models import CandidateDocument, DecisionRequest

logger = logging.getLogger(__name__)

_DOCUMENTS_KEY = "documents"
_NA = "NA"
_WEB_KEYS: tuple[str, ...] = (
    "case", "url_pdf", "url_html", "year", "text", "comment", "title",
    "decision_date", "firms", "nace",
)
_JSON_INDENT = 4
_ORIGINAL_FILE_MD = "original_file_md"


class TranslationEntry(BaseModel):
    """Markdown and language keys of a master entry switched by translation."""

    txt_md_file: str = Field(description="Working Markdown of the decision (English once translated).")
    original_file_md: str | None = Field(
        description="Foreign-language Markdown the English one was translated from; None removes the key.")
    language_init: str = Field(description="Lower-case language of the original document.")
    language_translated: str = Field(description="Lower-case target language, or NA.")


class MasterDescriptorStore:
    """In-memory view of the master descriptor file with an atomic flush."""

    def __init__(self, path: Path) -> None:
        """Load the master file (an absent file means an empty store).

        Args:
            path: Master descriptor JSON path.

        Raises:
            ValueError: If the file exists but is not a JSON object with a
                ``documents`` object.
            json.JSONDecodeError: If the file is not valid JSON.
        """
        self._path = Path(path)
        self._lock = threading.RLock()
        self._dirty = False
        self._backed_up = False
        self._removed: list[str] = []
        self._upserts: list[
            tuple[DecisionRequest, CandidateDocument, Path, str]
        ] = []
        self._markdown_updates: list[tuple[str, str, str | None]] = []
        self._translation_updates: list[tuple[str, TranslationEntry]] = []
        self._source_switches: list[tuple[str, str, str, str]] = []
        self._payload: dict[str, Any] = {_DOCUMENTS_KEY: {}}
        self._signature: tuple[int, int] | None = None
        self._load()

    def _file_signature(self) -> tuple[int, int] | None:
        """Return the (mtime_ns, size) of the master file.

        Returns:
            The signature, or None if the file does not exist.
        """
        try:
            st = self._path.stat()
        except FileNotFoundError:
            return None
        return st.st_mtime_ns, st.st_size

    def _load(self) -> None:
        """(Re)read the master file into a fresh payload.

        Raises:
            ValueError: If the file exists but has no ``documents`` object.
            json.JSONDecodeError: If the file is not valid JSON.
        """
        payload: dict[str, Any] = {_DOCUMENTS_KEY: {}}
        signature = self._file_signature()
        if signature is not None:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or not isinstance(
                loaded.get(_DOCUMENTS_KEY), dict
            ):
                raise ValueError(f"{self._path}: no 'documents' object")
            payload = loaded
        self._payload = payload
        self._signature = signature

    @property
    def _documents(self) -> dict[str, dict[str, Any]]:
        """Return the live documents mapping.

        Returns:
            Mapping of decision id to entry.
        """
        return self._payload[_DOCUMENTS_KEY]

    def has_key(self, key: str) -> bool:
        """Report whether a decision id has an entry.

        Args:
            key: Decision id.

        Returns:
            True if the master file holds the key.
        """
        with self._lock:
            return key in self._documents

    def local_file_pdf(self, key: str) -> str | None:
        """Return the stored PDF path of an entry (read-only).

        Args:
            key: Decision id.

        Returns:
            The entry's non-empty ``local_file_pdf``, or None when the key is
            absent or has no stored path.
        """
        with self._lock:
            value = self._documents.get(key, {}).get("local_file_pdf")
        return value if isinstance(value, str) and value else None

    def language_init(self, key: str) -> str | None:
        """Return the stored language of an entry's PDF (read-only).

        Args:
            key: Decision id.

        Returns:
            The entry's non-empty ``language_init`` (as stored), or None when
            the key is absent or has none.
        """
        with self._lock:
            value = self._documents.get(key, {}).get("language_init")
        return value if isinstance(value, str) and value else None

    def url_pdf(self, key: str) -> str | None:
        """Return the stored ``webDocDescriptor.url_pdf`` of an entry (read-only).

        Args:
            key: Decision id.

        Returns:
            The stored PDF URL, or None when the key is absent or has none.
        """
        with self._lock:
            web = self._documents.get(key, {}).get("webDocDescriptor")
        value = web.get("url_pdf") if isinstance(web, dict) else None
        return value if isinstance(value, str) and value else None

    def entries(self) -> list[tuple[str, dict[str, Any]]]:
        """Return a deep-copied snapshot of every entry (read-only).

        Returns:
            (decision id, entry) pairs in file order; changing them does not
            change the store.
        """
        with self._lock:
            return copy.deepcopy(list(self._documents.items()))

    def remove(self, key: str) -> bool:
        """Remove one entry; written by the next ``flush`` (thread-safe).

        Args:
            key: Decision id.

        Returns:
            True if the entry existed.
        """
        with self._lock:
            if self._documents.pop(key, None) is None:
                return False
            self._removed.append(key)
            self._dirty = True
            return True

    @staticmethod
    def _txt_md_file(local_file_pdf: Path) -> str:
        """Compute the expected MinerU markdown path of a PDF.

        Args:
            local_file_pdf: Stored PDF path.

        Returns:
            ``<pdf folder>/mineru/<pdf stem>.md``.
        """
        return str(markdown_path_for(local_file_pdf))

    def markdown_entry(self, key: str) -> tuple[str | None, str | None]:
        """Return the stored PDF and markdown paths of an entry (read-only).

        Args:
            key: Decision id.

        Returns:
            ``(local_file_pdf, txt_md_file)``; each is None when the key is
            absent or the value is missing or empty.
        """
        with self._lock:
            entry = self._documents.get(key, {})
            pdf, md = entry.get("local_file_pdf"), entry.get("txt_md_file")
        return (
            pdf if isinstance(pdf, str) and pdf else None,
            md if isinstance(md, str) and md else None,
        )

    def mineru_flag(self, key: str) -> bool | None:
        """Return the ``mineru_flag`` of an entry (read-only).

        Args:
            key: Decision id.

        Returns:
            True or False as stored, or None when the key is absent or the
            entry has no boolean flag.
        """
        with self._lock:
            value = self._documents.get(key, {}).get("mineru_flag")
        return value if isinstance(value, bool) else None

    def set_markdown(
        self, key: str, txt_md_file: str, language_init: str | None
    ) -> bool:
        """Record a converted markdown file on an entry (thread-safe).

        Sets ``mineru_flag`` and ``txt_md_file``; ``language_init`` is filled
        only when it is absent or "NA" and ``language_init`` is not None. The
        update is replayed by ``flush`` if the file changed on disk.

        Args:
            key: Decision id.
            txt_md_file: Markdown file path.
            language_init: Detected language (lower-cased on storage) or None.

        Returns:
            True if the entry exists and was updated, False otherwise.
        """
        with self._lock:
            if not self._apply_markdown(key, txt_md_file, language_init):
                return False
            self._markdown_updates.append((key, txt_md_file, language_init))
            self._dirty = True
            return True

    def _apply_markdown(
        self, key: str, txt_md_file: str, language_init: str | None
    ) -> bool:
        """Apply one markdown update to the in-memory payload (idempotent).

        Args:
            key: Decision id.
            txt_md_file: Markdown file path.
            language_init: Detected language or None.

        Returns:
            True if the entry exists.
        """
        entry = self._documents.get(key)
        if entry is None:
            return False
        entry["mineru_flag"] = True
        entry["txt_md_file"] = txt_md_file
        if language_init is not None and entry.get("language_init") in (None, "", _NA):
            entry["language_init"] = language_init.lower()
        return True

    def translation_entry(self, key: str) -> TranslationEntry | None:
        """Snapshot the translation-relevant keys of an entry (read-only).

        Args:
            key: Decision id.

        Returns:
            The four keys as stored (missing values read as "" / None / "NA"),
            or None when the key is absent.
        """
        with self._lock:
            entry = self._documents.get(key)
            if entry is None:
                return None
            return TranslationEntry(
                txt_md_file=entry.get("txt_md_file") or "",
                original_file_md=entry.get(_ORIGINAL_FILE_MD) or None,
                language_init=entry.get("language_init") or _NA,
                language_translated=entry.get("language_translated") or _NA,
            )

    def set_translation(self, key: str, translation: TranslationEntry) -> None:
        """Switch an entry to a translated Markdown, or restore a snapshot.

        Writes ``txt_md_file``, ``original_file_md`` (removed when None),
        ``language_init`` and ``language_translated``. The update is replayed
        by ``flush`` if the file changed on disk.

        Args:
            key: Decision id.
            translation: Values to store.

        Raises:
            KeyError: If the key is not in the master.
        """
        with self._lock:
            if key not in self._documents:
                raise KeyError(key)
            self._apply_translation(key, translation)
            self._translation_updates.append((key, translation))
            self._dirty = True

    def _apply_translation(self, key: str, translation: TranslationEntry) -> None:
        """Apply one translation update to the in-memory payload (idempotent).

        Args:
            key: Decision id; an entry removed on disk meanwhile is skipped.
            translation: Values to store.
        """
        entry = self._documents.get(key)
        if entry is None:
            logger.warning("master entry %s vanished; translation not replayed", key)
            return
        entry["txt_md_file"] = translation.txt_md_file
        if translation.original_file_md is None:
            entry.pop(_ORIGINAL_FILE_MD, None)
        else:
            entry[_ORIGINAL_FILE_MD] = translation.original_file_md
        entry["language_init"] = translation.language_init
        entry["language_translated"] = translation.language_translated

    def switch_source(self, key: str, *, local_file_pdf: str, txt_md_file: str, language_init: str) -> None:
        """Make another document the entry's source (a better copy of the same decision).

        Sets ``local_file_pdf``, ``txt_md_file``, ``mineru_flag`` and
        ``language_init``, resets ``language_translated`` to NA and removes
        ``original_file_md`` (the new source is not a translation). Replayed
        by ``flush`` if the file changed on disk.

        Args:
            key: Decision id.
            local_file_pdf: PDF of the new source.
            txt_md_file: Markdown of the new source.
            language_init: Lower-case language of the new source.

        Raises:
            KeyError: If the key is not in the master.
        """
        with self._lock:
            if key not in self._documents:
                raise KeyError(key)
            self._apply_source_switch(key, local_file_pdf, txt_md_file, language_init)
            self._source_switches.append((key, local_file_pdf, txt_md_file, language_init))
            self._dirty = True

    def _apply_source_switch(self, key: str, local_file_pdf: str, txt_md_file: str, language_init: str) -> None:
        """Apply one source switch to the in-memory payload (idempotent)."""
        entry = self._documents.get(key)
        if entry is None:
            logger.warning("master entry %s vanished; source switch not replayed", key)
            return
        entry["local_file_pdf"] = local_file_pdf
        entry["txt_md_file"] = txt_md_file
        entry["mineru_flag"] = True
        entry["language_init"] = language_init
        entry["language_translated"] = _NA
        entry.pop(_ORIGINAL_FILE_MD, None)

    @staticmethod
    def _supplied_web(
        decision: DecisionRequest, document: CandidateDocument
    ) -> dict[str, str]:
        """Collect the webDocDescriptor values the loader supplies.

        Args:
            decision: Decision with loader meta.
            document: Selected document (source of ``url_pdf``).

        Returns:
            Non-empty values only; unknown meta keys are included.
        """
        fixed: dict[str, str] = {
            "url_pdf": document.source_url,
            "title": decision.title,
            "firms": decision.parties,
            "decision_date": (
                decision.decision_date.isoformat() if decision.decision_date else ""
            ),
        }
        supplied: dict[str, str] = {
            k: str(v) for k, v in decision.meta.items() if v not in (None, "")
        }
        supplied.update({k: v for k, v in fixed.items() if v})
        return supplied

    def upsert(
        self,
        decision: DecisionRequest,
        document: CandidateDocument,
        local_file_pdf: Path,
        language_init: str,
    ) -> list[str]:
        """Create or merge the entry of a decision (thread-safe).

        A decision has exactly one entry, whatever the language version. A
        new entry gets every supplied ``webDocDescriptor`` value, including
        loader meta keys beyond the standard set. For an existing entry only
        ``case_ids`` (union) and the standard ``webDocDescriptor`` keys
        (``_WEB_KEYS``) change, and a supplied value only fills a stored value
        that is missing or blank: a non-blank stored value is never rewritten
        (no date reformatting, no URL respelling) and extra meta keys are not
        added. Deviation from the literal spec, which updates every supplied
        key: the master file is the legacy source being phased out and the
        extras live in ``decision_registry.meta``. A supplied ``url_pdf`` that
        differs from a stored one is logged and returned as a review reason,
        because the entry's preserved ``local_file_pdf`` belongs to the stored
        URL. URLs are compared with
        :func:`~pipelines.download.identifiers.pdf_url_key` (scheme and the
        ``from`` query parameter ignored), so spelling variants are no conflict.

        Args:
            decision: Decision (its id is the master key).
            document: The selected document.
            local_file_pdf: Stored PDF path (used for new entries only).
            language_init: Language of the stored PDF ("en", original, "NA").

        Returns:
            Review reasons, empty normally; ``url_pdf conflict: stored <u1>
            supplied <u2>`` when the URL differs from the stored one.
        """
        args = (decision, document, local_file_pdf, language_init)
        with self._lock:
            reasons = self._apply(*args)
            self._upserts.append(args)
            self._dirty = True
        return reasons

    def _apply(
        self,
        decision: DecisionRequest,
        document: CandidateDocument,
        local_file_pdf: Path,
        language_init: str,
    ) -> list[str]:
        """Merge one upsert into the in-memory payload (idempotent).

        Args:
            decision: Decision.
            document: Selected document.
            local_file_pdf: Stored PDF path (new entries only).
            language_init: Language of the stored PDF.

        Returns:
            Review reasons (see ``upsert``).
        """
        supplied = self._supplied_web(decision, document)
        key = decision.decision_id
        reasons: list[str] = []
        entry = self._documents.get(key)
        if entry is None:
            web = {k: "" for k in _WEB_KEYS}
            web.update(supplied)
            entry = {
                "CELEX": key,
                "local_file_pdf": str(local_file_pdf),
                "txt_md_file": self._txt_md_file(local_file_pdf),
                "mineru_flag": False,
                "language_init": (
                    language_init if language_init == _NA else language_init.lower()
                ),
                "language_translated": _NA,
                "case_ids": list(dict.fromkeys(decision.case_ids)),
                "webDocDescriptor": web,
            }
            self._documents[key] = entry
        else:
            web = entry.setdefault("webDocDescriptor", {})
            stored = web.get("url_pdf")
            new_url = supplied.get("url_pdf")
            if stored and new_url and pdf_url_key(new_url) != pdf_url_key(stored):
                reason = f"url_pdf conflict: stored {stored} supplied {new_url}"
                logger.warning("%s: %s", key, reason)
                reasons.append(reason)
            web.update({
                k: supplied[k] for k in _WEB_KEYS
                if k in supplied and web.get(k) in (None, "")
            })
            merged = list(entry.get("case_ids", [])) + decision.case_ids
            entry["case_ids"] = list(dict.fromkeys(merged))
        return reasons

    def _backup_once(self) -> None:
        """Copy the current file aside once per store, never overwriting.

        Raises:
            OSError: If the copy fails.
        """
        if self._backed_up or not self._path.exists():
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        n = 0
        while True:
            suffix = f".bak-{stamp}" + (f"-{n}" if n else "")
            target = self._path.with_name(self._path.name + suffix)
            if not target.exists():
                break
            n += 1
        shutil.copy2(self._path, target)
        self._backed_up = True
        logger.info("master descriptor backup written: %s", target)

    def flush(self) -> None:
        """Write the master file atomically (no-op when nothing changed).

        Makes the run's single backup first, then writes a temp file in the
        same directory and swaps it in with ``os.replace``. On any failure the
        original file is untouched and the temp file is removed.

        Raises:
            OSError: If backup, write or replace fails.
        """
        with self._lock:
            if not self._dirty:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._file_signature() != self._signature:
                logger.warning(
                    "%s changed since load; re-reading and re-applying %d upserts",
                    self._path,
                    len(self._upserts),
                )
                self._load()
                for args in self._upserts:
                    self._apply(*args)
                for update in self._markdown_updates:
                    self._apply_markdown(*update)
                for key, translation in self._translation_updates:
                    self._apply_translation(key, translation)
                for switch in self._source_switches:
                    self._apply_source_switch(*switch)
                for key in self._removed:
                    self._documents.pop(key, None)
            self._backup_once()
            text = json.dumps(
                copy.deepcopy(self._payload),
                indent=_JSON_INDENT,
                ensure_ascii=False,
            )
            fd, tmp_name = tempfile.mkstemp(
                dir=self._path.parent, prefix=self._path.name + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                if self._path.exists():
                    shutil.copymode(self._path, tmp_name)
                os.replace(tmp_name, self._path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
            self._dirty = False
            self._signature = self._file_signature()
