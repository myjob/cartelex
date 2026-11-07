"""On-disk checks of already stored PDFs ("already exists" rule).

Mechanical helpers of the orchestrator: a file counts as a valid stored PDF
when it exists, is non-empty and starts with the ``%PDF-`` magic; an
existing file is described as a ``FetchResult`` without any HTTP.
"""

import hashlib
import time
from pathlib import Path

from pipelines.download.fetcher import PDF_MAGIC
from pipelines.download.models import FetchResult, Outcome

_HASH_CHUNK = 1 << 20
CANDIDATES_DIR = "candidates"
PDF_SUFFIX = ".pdf"
MINERU_DIR = "mineru"
MINERU_AUTO_DIR = "auto"
MD_SUFFIX = ".md"


def target_path(
    docs_dir: Path, decision_id: str, document_id: str, *, selected: bool
) -> Path:
    """Compute the storage path of a document (design "File layout").

    Args:
        docs_dir: Document store root.
        decision_id: Final decision id (folder name).
        document_id: Final document id (stem for undecided candidates).
        selected: True for the decision's single selected document.

    Returns:
        ``<docs>/<decision_id>/<decision_id>.pdf`` when selected, else
        ``<docs>/<decision_id>/candidates/<document_id>.pdf``.
    """
    folder = docs_dir / decision_id
    if selected:
        return folder / f"{decision_id}{PDF_SUFFIX}"
    return folder / CANDIDATES_DIR / f"{document_id}{PDF_SUFFIX}"


def markdown_path_for(pdf: Path) -> Path:
    """Compute the MinerU Markdown path of a stored PDF.

    Args:
        pdf: Stored PDF path (selected or candidate).

    Returns:
        ``<pdf folder>/mineru/<pdf stem>.md``.
    """
    return pdf.parent / MINERU_DIR / f"{pdf.stem}{MD_SUFFIX}"


def conventional_markdown_path(docs_dir: Path, celex_id: str) -> Path:
    """Compute the legacy MinerU output path of a decision's Markdown.

    The fallback layout used when a decision's Markdown is recorded nowhere
    else (master descriptor, chunk ``path``).

    Args:
        docs_dir: Document store root (``settings.CELEX_DOCS_DIR``).
        celex_id: Decision key (folder name and file stem).

    Returns:
        ``<docs>/<celex_id>/mineru/auto/<celex_id>.md`` (existence not checked).
    """
    return docs_dir / celex_id / MINERU_DIR / MINERU_AUTO_DIR / f"{celex_id}{MD_SUFFIX}"


def is_valid_pdf(path: Path) -> bool:
    """Tell whether ``path`` holds a non-empty file starting with ``%PDF-``.

    Args:
        path: Candidate file path.

    Returns:
        True for a regular, non-empty file with the PDF magic, else False
        (also when it cannot be read).
    """
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        with path.open("rb") as handle:
            return handle.read(len(PDF_MAGIC)) == PDF_MAGIC
    except OSError:
        return False


def existing_file_result(
    document_id: str, url: str, path: Path, outcome: Outcome, *, with_hash: bool
) -> FetchResult:
    """Describe an existing stored file as a fetch result (no HTTP).

    Args:
        document_id: Document id.
        url: Source URL of the document.
        path: Stored file path (must exist).
        outcome: ``registered_existing`` or ``skipped_exists``.
        with_hash: Compute the SHA-256 of the file (needed when the file is
            registered; skipped files are not re-hashed).

    Returns:
        A ``FetchResult`` with local path, size and optionally the hash.

    Raises:
        OSError: If the file cannot be read.
    """
    start = time.monotonic()
    sha: str | None = None
    if with_hash:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(_HASH_CHUNK):
                digest.update(chunk)
        sha = digest.hexdigest()
    return FetchResult(
        document_id=document_id,
        source_url=url,
        outcome=outcome,
        local_path=str(path),
        sha256=sha,
        size_bytes=path.stat().st_size,
        duration_ms=int((time.monotonic() - start) * 1000),
    )


def remove_new_file(path: Path, docs_dir: Path) -> None:
    """Remove a file written in this run and the folders it leaves empty.

    Only empty parent folders below ``docs_dir`` are removed (the decision
    folder and its ``candidates/`` folder); a folder with other files stays.

    Args:
        path: The file to remove (a missing file is no error).
        docs_dir: Document store root (never removed).

    Raises:
        OSError: If the file exists but cannot be removed.
    """
    path.unlink(missing_ok=True)
    root = docs_dir.resolve()
    parent = path.parent.resolve()
    while parent != root and root in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent
