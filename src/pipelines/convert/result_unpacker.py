"""Atomic placement of a MinerU result ZIP plus metrics for the audit table.

Result ZIP layout (live probe against server 4.1.3, tier ``flash``)::

    markdown.md
    middle_json.json
    structured_content.json
    images/            (empty directory entry when the PDF has no pictures)

``middle_json.json`` is a ``docvortex.middle`` 2.0 envelope, not the legacy
``pdf_info`` layout::

    {"metadata": {"document": {"page_count": 2, ...}, ...},
     "pages": [{"page_idx": 0, "blocks": [{"type": "text", ...}, ...]}, ...],
     "schema": "docvortex.middle", "schema_version": "2.0", ...}

Chosen keys:

* pages: ``len(middle_json["pages"])``;
* tables: blocks with ``type == "table"`` anywhere under ``pages``;
* formulas: blocks with ``type == "equation"`` (display equations; inline
  ones are ``equation_inline`` and are not counted) anywhere under ``pages``.

The probe PDF held neither tables nor equations, so the ``table`` and
``equation`` names come from the MinerU docs (``docs/next/middle-json``),
not from a live sample. ``structured_content.json`` carries the same page
list with the same block types, so it adds nothing and is not parsed. A count
that cannot be derived (missing or malformed JSON) is ``None``.

Placement is crash-safe: everything is extracted and validated in a
``.tmp-<uuid>`` directory beside the target, the stem-named JSON sidecars
and image files are swapped in with the old ones kept aside, and the Markdown
file is replaced last. Many Markdowns share one folder, so the sidecars are
named ``<stem>_middle.json`` / ``<stem>_structured_content.json`` and
``images/`` is merged, never replaced. Any failure restores the old files and
removes the temp directory.
"""

import io
import json
import logging
import os
import shutil
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_MARKDOWN_MEMBER = "markdown.md"
_IMAGES_DIR = "images"
_MIDDLE_JSON = "middle_json.json"
_STRUCTURED_JSON = "structured_content.json"
_MIDDLE_SUFFIX = "_middle.json"
_STRUCTURED_SUFFIX = "_structured_content.json"
_ASIDE_SEP = "__"
_TMP_PREFIX = ".tmp-"
_ASIDE_DIR = "aside"
_STAGE_DIR = "stage"
_TABLE_TYPE = "table"
_FORMULA_TYPE = "equation"
_SYMLINK_MODE = 0o120000


class ResultInvalid(Exception):
    """The result ZIP is corrupt, incomplete or unsafe to extract."""


class ResultMetrics(BaseModel):
    """Counts describing one placed result, stored in the audit table."""

    md_chars: int = Field(description="Number of characters in the Markdown text.")
    image_count: int = Field(description="Number of image files under images/.")
    table_count: int | None = Field(
        default=None, description="Table blocks in middle_json, None if not derivable.")
    formula_count: int | None = Field(
        default=None, description="Display equation blocks in middle_json, None if not derivable.")
    pdf_pages: int | None = Field(
        default=None, description="Pages in middle_json, None if not derivable.")


def _safe_extract(archive: zipfile.ZipFile, dest: Path) -> None:
    """Extract all members into ``dest`` after rejecting unsafe names.

    Args:
        archive: Open ZIP file.
        dest: Existing empty directory to extract into.

    Raises:
        ResultInvalid: On an absolute path, ``..`` component, symlink member or
            a member that would land outside ``dest``.
    """
    root = dest.resolve()
    for info in archive.infolist():
        parts = PurePosixPath(info.filename.replace("\\", "/")).parts
        if info.filename.startswith(("/", "\\")) or ".." in parts or (
                parts and ":" in parts[0]):
            raise ResultInvalid(f"Unsafe member name {info.filename!r}")
        if (info.external_attr >> 16) & 0o170000 == _SYMLINK_MODE:
            raise ResultInvalid(f"Symlink member {info.filename!r}")
        if not (root / Path(*parts)).resolve().is_relative_to(root):
            raise ResultInvalid(f"Member escapes target {info.filename!r}")
    archive.extractall(dest)


def _count_blocks(node: object, block_type: str) -> int:
    """Count dicts whose ``type`` equals ``block_type`` anywhere below ``node``.

    Args:
        node: Parsed JSON value.
        block_type: Block type name to count.

    Returns:
        Number of matching blocks.
    """
    if isinstance(node, dict):
        own = 1 if node.get("type") == block_type else 0
        return own + sum(_count_blocks(v, block_type) for v in node.values())
    if isinstance(node, list):
        return sum(_count_blocks(v, block_type) for v in node)
    return 0


def _middle_counts(path: Path) -> tuple[int | None, int | None, int | None]:
    """Derive ``(pages, tables, formulas)`` from ``middle_json.json``.

    Args:
        path: Location of the middle JSON file.

    Returns:
        The three counts, each None when not derivable.
    """
    if not path.is_file():
        logger.debug("no middle_json to derive metrics from: %s", path)
        return None, None, None
    try:
        pages = json.loads(path.read_text(encoding="utf-8"))["pages"]
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("middle_json not usable for metrics: %s", path)
        return None, None, None
    if not isinstance(pages, list):
        return None, None, None
    return len(pages), _count_blocks(pages, _TABLE_TYPE), _count_blocks(pages, _FORMULA_TYPE)


def _swap_in(stage: Path, target_md: Path, aside: Path) -> None:
    """Swap staged artifacts into place, restoring the old ones on failure.

    The Markdown file and the two stem-named JSON sidecars are replaced as a
    unit. ``images/`` is shared by every Markdown in the folder: staged image
    files are merged in (same name overwrites, other files are untouched).

    Args:
        stage: Directory holding the validated new artifacts and markdown.
        target_md: Final Markdown path; its parent receives the other artifacts.
        aside: Empty directory that temporarily holds the old artifacts.

    Raises:
        OSError: When a move fails; old artifacts are restored first and only
            image files this call newly added are removed.
    """
    parent = target_md.parent
    stem = target_md.stem
    sidecars = ((_MIDDLE_JSON, parent / f"{stem}{_MIDDLE_SUFFIX}"),
                (_STRUCTURED_JSON, parent / f"{stem}{_STRUCTURED_SUFFIX}"))
    staged_images = sorted(
        p.relative_to(stage / _IMAGES_DIR)
        for p in (stage / _IMAGES_DIR).rglob("*") if p.is_file())
    moved_aside: list[tuple[Path, Path]] = []
    placed: list[Path] = []
    aside.mkdir()
    try:
        for member, final in sidecars:
            if final.exists():
                os.replace(final, aside / final.name)
                moved_aside.append((aside / final.name, final))
        for n, rel in enumerate(staged_images):
            dest = parent / _IMAGES_DIR / rel
            if dest.exists():
                kept = aside / _IMAGES_DIR / f"{n}{_ASIDE_SEP}{rel.name}"
                kept.parent.mkdir(parents=True, exist_ok=True)
                os.replace(dest, kept)
                moved_aside.append((kept, dest))
        for member, final in sidecars:
            if (stage / member).exists():
                os.replace(stage / member, final)
                placed.append(final)
        for rel in staged_images:
            dest = parent / _IMAGES_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / _IMAGES_DIR / rel, dest)
            placed.append(dest)
        os.replace(stage / _MARKDOWN_MEMBER, target_md)
    except BaseException:
        for path in placed:
            _remove(path)
        for kept, original in moved_aside:
            original.parent.mkdir(parents=True, exist_ok=True)
            os.replace(kept, original)
        raise


def _remove(path: Path) -> None:
    """Delete a file or directory tree, ignoring a missing path.

    Args:
        path: Path to delete.
    """
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def place_result(zip_bytes: bytes, target_md: Path) -> ResultMetrics:
    """Place a MinerU result ZIP at ``target_md`` and measure it.

    ``markdown.md`` becomes ``target_md``; ``middle_json.json`` and
    ``structured_content.json`` become ``<stem>_middle.json`` and
    ``<stem>_structured_content.json`` beside it, and the image files are
    merged into the shared ``images/`` folder of ``target_md.parent`` (other
    Markdowns in the folder keep their files). Old copies of the Markdown and
    sidecars are replaced as a unit and survive any failure; on failure only
    image files newly added by this call are removed.

    Args:
        zip_bytes: Raw bytes of the downloaded result ZIP.
        target_md: Final Markdown path; the parent directory is created.

    Returns:
        Metrics of the placed result.

    Raises:
        ResultInvalid: Corrupt ZIP, no ``markdown.md`` or an unsafe member.
        OSError: When the filesystem refuses a move (old artifacts restored).
    """
    parent = target_md.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = parent / f"{_TMP_PREFIX}{uuid.uuid4().hex}"
    stage = tmp / _STAGE_DIR
    stage.mkdir(parents=True)
    try:
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
                _safe_extract(archive, stage)
        except (zipfile.BadZipFile, ValueError, NotImplementedError) as exc:
            raise ResultInvalid(f"Corrupt result ZIP: {exc}") from exc
        markdown = stage / _MARKDOWN_MEMBER
        if not markdown.is_file():
            raise ResultInvalid(f"Result ZIP has no {_MARKDOWN_MEMBER}")
        md_chars = len(markdown.read_text(encoding="utf-8"))
        images = stage / _IMAGES_DIR
        images.mkdir(exist_ok=True)
        image_count = sum(1 for p in images.rglob("*") if p.is_file())
        pages, tables, formulas = _middle_counts(stage / _MIDDLE_JSON)
        _swap_in(stage, target_md, tmp / _ASIDE_DIR)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return ResultMetrics(
        md_chars=md_chars, image_count=image_count, table_count=tables,
        formula_count=formulas, pdf_pages=pages)
