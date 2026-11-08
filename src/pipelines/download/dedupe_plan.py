"""Plan of the registry de-duplication against the master (read-only).

A registry decision D that is not a master key is acted on only on hard
evidence (design: ``docs/claude/plans/2026-10-30-master-identity-dedupe.md``):

* identity rule ``celex``, ``url`` or ``url+case`` of
  :mod:`pipelines.download.master_identity` (index built without the
  ``--include-master-key`` entries, so a key never matches itself), or
* a sha256 match of D's OWN selected/source PDF(s) with a selected/source
  document of master key K, where every selected/source PDF of D has K's sha.

``case+date`` alone, a URL match the date guard dropped (``url-date``), or
a sha shared only by a non-selected copy of D's document (``sha-copy``) is a
``review`` (report only): master dates can be wrong. When sha and an identity
rule agree the signal is ``sha+identity:<rule>``: the sha is the hard
evidence, the identity rule (possibly only ``case+date``) corroborates it.
Signals pointing to different keys are a ``conflict``; an ambiguous identity
match is ``ambiguous``; neither is acted on. A delete is ``refused`` when D's
id is not a safe path component, when facts or embeddings reference D, when
D's folder holds K's PDF, or when K has no registry row to re-point to.

A ``delete`` merges D into K (see ``RegistryDedupeRepository.merge_decision``):
D's documents are re-pointed to K, its case links move to K, its registry row
is deleted. The plan reports the planned ``documents_repointed`` and
``case_links_moved``.
"""

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from pipelines.download.descriptor_store import MasterDescriptorStore
from pipelines.download.identifiers import celex_key
from pipelines.download.master_identity import MasterIdentityIndex
from pipelines.rag.pgvector.registry_dedupe_repository import DecisionRow, repointed_id

logger = logging.getLogger(__name__)

Action = Literal["delete", "conflict", "ambiguous", "review", "refused"]
Outcome = Literal["planned", "none", "done", "db_failed", "move_failed",
                  "master_failed", "refused"]
SOFT_RULE = "case+date"
SIGNAL_SHA = "sha"
SIGNAL_URL_DATE = "url-date"


class PlanItem(BaseModel):
    """One report row: what to do about one candidate decision."""

    decision_id: str = Field(description="Candidate decision D.")
    duplicate_of: str = Field(
        default="", description="Master key K; for conflict/ambiguous/review the "
        "candidate keys joined by '|'.")
    signal: str = Field(description="identity:<rule>, sha, sha+identity:<rule>, "
                                    "url-date, sha-copy or a comma list of the "
                                    "conflicting signals.")
    action: Action = Field(description="delete, conflict, ambiguous, review or refused.")
    documents_repointed: int = Field(
        default=0, description="D's document rows re-pointed to K (planned in a "
        "dry run); 0 unless delete.")
    case_links_moved: int = Field(
        default=0, description="(case, K) links added from D's links (planned in "
        "a dry run); 0 unless delete.")
    files_moved: int = Field(default=0, description="Files moved (planned in a dry run).")
    master_entry_removed: bool = Field(
        default=False, description="Master entry removed (planned in a dry run).")
    outcome: Outcome = Field(
        default="planned", description="planned (dry run), none (no action), "
        "done, db_failed, move_failed, master_failed or refused.")


def pdf_urls(decision: DecisionRow, selected_only: bool) -> list[str]:
    """Return a decision's PDF URLs.

    Args:
        decision: Registry decision.
        selected_only: Keep only selected documents; otherwise all PDF
            documents plus the audited candidate URLs.

    Returns:
        The source URLs.
    """
    urls = [d.source_url for d in decision.documents
            if d.is_pdf and (d.is_selected or not selected_only)]
    return urls if selected_only else [*urls, *decision.audit_urls]


def sha_owners(decisions: Sequence[DecisionRow], master_keys: set[str]) -> dict[str, set[str]]:
    """Map sha256 to the master keys holding it as a selected/source document.

    Args:
        decisions: Registry decisions.
        master_keys: Master keys.

    Returns:
        Mapping of sha256 to master keys.
    """
    owners: dict[str, set[str]] = {}
    for dec in decisions:
        if dec.decision_id not in master_keys:
            continue
        for doc in dec.documents:
            if doc.sha256 and (doc.is_source or doc.is_selected):
                owners.setdefault(doc.sha256, set()).add(dec.decision_id)
    return owners


def classify(
    dec: DecisionRow, index: MasterIdentityIndex, owners: dict[str, set[str]],
) -> PlanItem | None:
    """Decide what the evidence says about D.

    Args:
        dec: Candidate decision.
        index: Identity index without the included master keys.
        owners: sha256 to master keys.

    Returns:
        The plan item, or None when D matches nothing.
    """
    own = celex_key(dec.celex_number) if dec.celex_number else dec.decision_id
    match, conflicts = index.match_detail(own, pdf_urls(dec, True), pdf_urls(dec, False),
                                          dec.case_ids, dec.decision_date)
    own_shas = {d.sha256 for d in dec.documents
                if d.sha256 and d.is_pdf and (d.is_source or d.is_selected)}
    all_shas = {d.sha256 for d in dec.documents if d.sha256}

    def holders(shas: set[str]) -> set[str]:
        return {k for s in shas for k in owners.get(s, set())} - {dec.decision_id}

    hard_sha = {k for k in holders(own_shas)
                if all(k in owners.get(s, set()) for s in own_shas)}
    soft_sha = holders(all_shas) - hard_sha
    base = {"decision_id": dec.decision_id}
    sig = f"identity:{match.rule}" if match else ""
    if match is not None and match.key is None:
        if hard_sha and not hard_sha <= set(match.ambiguous):
            return PlanItem(**base, duplicate_of="|".join(sorted(hard_sha | set(match.ambiguous))),
                            signal=f"{sig},sha", action="conflict")
        return PlanItem(**base, duplicate_of="|".join(match.ambiguous),
                        signal=sig, action="ambiguous")
    hard_ident = match.key if match and match.rule != SOFT_RULE else None
    soft_ident = match.key if match and match.rule == SOFT_RULE else None
    sha_key = next(iter(hard_sha)) if len(hard_sha) == 1 else None
    other = hard_ident or soft_ident
    if len(hard_sha) > 1 or (sha_key and other and sha_key != other):
        keys = sorted(hard_sha | ({other} if other else set()))
        return PlanItem(**base, duplicate_of="|".join(keys),
                        signal=f"{sig},sha" if sig else SIGNAL_SHA, action="conflict")
    key = hard_ident or sha_key
    if key is not None:
        signal = (f"{SIGNAL_SHA}+{sig}" if sha_key and other
                  else (sig if hard_ident else SIGNAL_SHA))
        return PlanItem(**base, duplicate_of=key, signal=signal, action="delete")
    if soft_ident is not None:
        return PlanItem(**base, duplicate_of=soft_ident, signal=sig, action="review")
    if conflicts:
        return PlanItem(**base, duplicate_of="|".join(c.key for c in conflicts),
                        signal=SIGNAL_URL_DATE, action="review")
    if soft_sha:
        return PlanItem(**base, duplicate_of="|".join(sorted(soft_sha)),
                        signal="sha-copy", action="review")
    return None


def _folder_files(folder: Path) -> list[Path]:
    """List the files below a folder.

    Args:
        folder: Folder (may be absent).

    Returns:
        Resolved file paths; empty when the folder does not exist.
    """
    if not folder.is_dir():
        return []
    return [p.resolve() for p in folder.rglob("*") if p.is_file()]


def _protected_paths(
    key: str, by_id: dict[str, DecisionRow], store: MasterDescriptorStore,
) -> set[Path]:
    """Collect the stored PDF paths of master key K.

    Args:
        key: Master key K.
        by_id: Registry decisions by id.
        store: Master store.

    Returns:
        Resolved paths of K's registry and master PDFs.
    """
    paths = [d.local_file_pdf for d in by_id[key].documents] if key in by_id else []
    paths.append(store.local_file_pdf(key))
    return {Path(p).resolve() for p in paths if p}


def planned_merge(
    dec: DecisionRow, key_row: DecisionRow, document_ids: set[str],
) -> tuple[int, int]:
    """Count what merging D into K would change (mirrors ``merge_decision``).

    Args:
        dec: Duplicate decision D.
        key_row: K's registry decision.
        document_ids: Every registered document id; updated with the ids
            this merge takes, so later merges of the plan see them.

    Returns:
        (documents re-pointed, case links added to K).
    """
    taken = document_ids
    repointed = 0
    for doc in dec.documents:
        new = repointed_id(doc.document_id, doc.source_url, doc.file_format,
                           key_row.decision_id)
        if new != doc.document_id and new in taken:
            continue
        taken.add(new)
        repointed += 1
    return repointed, len(set(dec.case_ids) - set(key_row.case_ids))


def is_safe_id(decision_id: str) -> bool:
    """Report whether a decision id is a single, plain path component.

    Args:
        decision_id: Decision id.

    Returns:
        False for an empty id, a separator or a ``.``/``..`` component.
    """
    return bool(decision_id) and Path(decision_id).name == decision_id \
        and decision_id not in (".", "..") and "\\" not in decision_id


def build_plan(
    decisions: Sequence[DecisionRow], store: MasterDescriptorStore,
    docs_dir: Path, include_master: Sequence[str],
    referenced: Callable[[list[DecisionRow]], set[str]] = lambda _rows: set(),
) -> list[PlanItem]:
    """Plan the clean-up (read-only).

    Args:
        decisions: Registry decisions (plus id-only rows of included master
            keys without a registry row).
        store: Master store (only read).
        docs_dir: Document store root (folders only listed).
        include_master: Downloader-created master keys to treat as
            candidates; they are left out of the identity index and the sha
            owners, which also keeps each from matching itself.
        referenced: Returns the ids among the given decisions that facts or
            embeddings reference.

    Returns:
        One item per candidate with evidence, ordered by id.
    """
    included = set(include_master)
    all_keys = {k for k, _ in store.entries()}
    entries = [(k, e) for k, e in store.entries() if k not in included]
    master_keys = {k for k, _ in entries}
    index = MasterIdentityIndex(entries)
    owners = sha_owners(decisions, master_keys)
    by_id = {d.decision_id: d for d in decisions}
    document_ids = {doc.document_id for d in decisions for doc in d.documents}
    plan: list[PlanItem] = []
    for dec in decisions:
        if dec.decision_id in master_keys:
            continue
        item = classify(dec, index, owners)
        if item is not None:
            plan.append(item)
    used = {i.decision_id for i in plan if i.action == "delete"}
    used_refs = referenced([by_id[i] for i in sorted(used) if is_safe_id(i)])
    out: list[PlanItem] = []
    for item in plan:
        if item.action == "delete":
            d_id = item.decision_id
            files = _folder_files(docs_dir / d_id) if is_safe_id(d_id) else []
            if not is_safe_id(d_id):
                reason = "unsafe id"
            elif d_id in used_refs:
                reason = "facts or embeddings reference it"
            elif _protected_paths(item.duplicate_of, by_id, store) & set(files):
                reason = f"folder holds a PDF of {item.duplicate_of}"
            elif item.duplicate_of not in by_id:
                reason = f"{item.duplicate_of} has no registry row to re-point to"
            else:
                reason = ""
            if reason:
                logger.error("%s: refused (%s)", d_id, reason)
                item = item.model_copy(update={"action": "refused", "outcome": "refused"})
            else:
                repointed, links = planned_merge(by_id[d_id], by_id[item.duplicate_of],
                                                 document_ids)
                item = item.model_copy(update={
                    "documents_repointed": repointed, "case_links_moved": links,
                    "files_moved": len(files), "master_entry_removed": d_id in included & all_keys})
        out.append(item)
    return out
