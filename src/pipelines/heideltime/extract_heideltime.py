"""HeidelTime wrapper — safe, pool-free, single-subprocess invocation.

Why we bypass ``py_heideltime.heideltime()``
--------------------------------------------
``py_heideltime.heideltime()`` has three critical defects that cause the
"Server[50]" crash observed in production:

1. **Multiprocessing pool inside a threaded process.**
   It calls ``multiprocessing.Pool(processes=cpu_count())`` — 12 fresh
   Python+Java processes — for *every single chunk*.  When this pool is
   created inside a ``ThreadPoolExecutor`` (or inside the debugpy multi-
   threaded environment), ``os.fork()`` is unsafe and worker initialisation
   fails with UIMA's "Server[50]" error.

2. **``config.props`` race condition.**
   It writes ``config.props`` to the *current working directory* at the
   start of each call and **deletes it** at the end (``Path("config.props")
   .unlink()``).  When the pool's 12 workers start concurrently one worker
   deletes the file while others are still reading it, crashing the Java
   process.

3. **Silent stderr.**
   ``execute_command()`` uses ``os.popen()`` on Linux, which discards
   stderr entirely, so Java errors are silently swallowed.

Our replacement
---------------
* Writes ``config.props`` **once** at singleton construction (via
  ``model_post_init``).  The file lives in the py_heideltime package
  directory — a stable path that all JAR invocations can find regardless
  of the Python process's cwd.
* Calls the HeidelTime JAR **directly** with ``subprocess.run()``,
  capturing both stdout and stderr so errors are visible in logs.
* Processes each text file sequentially (one JVM per text, no pool).
  For typical chunk sizes (< 30 kB) this is faster than spawning 12 JVMs.
"""

import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel
from py_heideltime.py_heideltime import (
    HEIDELTIME_JAR_PATH,
    LIBRARY_PATH,
    _get_timexs,
    _validate_inputs,
)
from py_heideltime.utils import process_text

from pipelines.rag.pgvector.models import HeidelTimeVO

import warnings

# Suppress unclosed file / running-subprocess warnings from py_heideltime
warnings.filterwarnings("ignore", category=ResourceWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning, module="subprocess")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="pydev_monkey")

logger = logging.getLogger(__name__)

# Stable path for config.props — written once into the py_heideltime package
# directory so all JAR calls can always find it, regardless of cwd.
_CONFIG_PROPS_PATH: Path = LIBRARY_PATH / "config.props"

# py_heideltime uses singular lowercase names for document types (e.g. "narrative")
# but the HeidelTime JAR's -t flag requires plural lowercase (e.g. "narratives").
# This map bridges the two conventions.
_DOC_TYPE_JAR_MAP: dict[str, str] = {
    "news": "news",
    "narrative": "narratives",
    "colloquial": "colloquial",
    "scientific": "scientific",
}


def _write_stable_config_props() -> None:
    """Write ``config.props`` to the py_heideltime package directory.

    py_heideltime's ``_write_config_props()`` writes to ``Path("config.props")``
    (the *current working directory*).  We write to the *package directory*
    instead, which is a stable, cwd-independent location.
    """
    import platform

    if platform.system() == "Windows":
        pass
    else:
        tagger_path = LIBRARY_PATH / "Heideltime" / "TreeTaggerLinux"

    with (LIBRARY_PATH / "resources" / "config_props_template").open() as fh:
        conf_template = fh.read()
    conf_content = conf_template.replace("{path}", str(tagger_path.absolute()))
    _CONFIG_PROPS_PATH.write_text(conf_content)


def _run_heideltime_jar(
    text: str,
    language: str,
    document_type: str,
    dct: Optional[str],
) -> list:
    """Invoke the HeidelTime standalone JAR for a single text string.

    This replaces ``py_heideltime.heideltime()`` with a safe, pool-free,
    single-subprocess call that:

    * does **not** spawn a ``multiprocessing.Pool``
    * does **not** touch ``config.props`` (written once at singleton init)
    * captures stderr so Java errors surface in the Python log

    Returns a list of timex dicts (same shape as ``py_heideltime._get_timexs()``).
    """
    processed = process_text(text)

    # Normalise and translate the document_type for the JAR's -t flag.
    # py_heideltime stores singular forms ("narrative"); the JAR needs plural ("narratives").
    doc_type_norm = document_type.lower()
    jar_doc_type = _DOC_TYPE_JAR_MAP.get(doc_type_norm, doc_type_norm)

    # Write the text into a temp file in the library directory so the JAR
    # (which sets its cwd to LIBRARY_PATH) can find both the temp file and
    # config.props reliably.
    with tempfile.NamedTemporaryFile(
        dir=LIBRARY_PATH,
        suffix=".txt",
        delete=False,
        mode="w",
        encoding="utf-8",
    ) as tf:
        tf.write(processed)
        tmp_path = Path(tf.name)

    try:
        if dct is not None:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", dct):
                raise ValueError(f"dct must be YYYY-MM-DD, got: {dct!r}")
            cmd = [
                "java", "-jar", str(HEIDELTIME_JAR_PATH),
                str(tmp_path),
                "-dct", dct,
                "-t", jar_doc_type,
                "-l", language.lower(),
                "-c", str(_CONFIG_PROPS_PATH),
            ]
        else:
            cmd = [
                "java", "-jar", str(HEIDELTIME_JAR_PATH),
                str(tmp_path),
                "-t", jar_doc_type,
                "-l", language.lower(),
                "-c", str(_CONFIG_PROPS_PATH),
            ]

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(LIBRARY_PATH),  # JAR expects CWD = its own directory
            timeout=120,
        )

        if result.returncode != 0:
            stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
            logger.warning(
                "HeidelTime JAR exited %d. stderr: %s",
                result.returncode,
                stderr_text[:500],
            )
            return []

        time_ml = result.stdout.decode("utf-8", errors="replace")
        tml_match = re.findall(r"<TimeML>(.*)</TimeML>", time_ml, re.DOTALL)
        if not tml_match:
            return []

        tml_content = tml_match[0].strip("\n")
        return _get_timexs(tml_content)

    except subprocess.TimeoutExpired:
        logger.error("HeidelTime JAR timed out after 120 s for text of %d chars.", len(text))
        return []
    finally:
        tmp_path.unlink(missing_ok=True)


class HeidelTime(BaseModel):
    """Pool-free HeidelTime wrapper.

    Invokes the HeidelTime standalone JAR directly via ``subprocess.run()``
    — one JVM per text, no ``multiprocessing.Pool``, no ``config.props``
    race conditions.

    :param str language: {\"English\", \"Portuguese\", \"Spanish\", \"Germany\",
        \"Dutch\", \"Italian\", \"French\"}
    :param str document_type: {\"News\", \"Narrative\", \"Colloquial\", \"Scientific\"}

    .. seealso::
        `Strötgen, Gertz: HeidelTime: High Quality Rule-based Extraction and
        Normalization of Temporal Expressions. SemEval'10.
        <https://aclanthology.org/S10-1071/>`_
    """

    language: str = "english"
    document_type: str = "news"

    def model_post_init(self, __context: object) -> None:  # noqa: ANN001
        """Normalise fields, validate config, and write ``config.props`` exactly once.

        Accepts any-case strings for language and document_type (e.g. ``"Narrative"``
        is normalised to ``"narrative"`` before validation) so that settings values
        are not required to be exact-case.

        ``config.props`` is written to the py_heideltime package directory
        (a stable, cwd-independent path) so every subsequent JAR call can
        find it without any per-call I/O.
        """
        # Pydantic frozen models require object.__setattr__ for post-init mutations.
        object.__setattr__(self, "language", self.language.lower())
        object.__setattr__(self, "document_type", self.document_type.lower())
        _validate_inputs(self.language, self.document_type)
        _write_stable_config_props()
        logger.info(
            "HeidelTime singleton ready (language=%s, document_type=%s → JAR: %s); "
            "config.props written to %s.",
            self.language,
            self.document_type,
            _DOC_TYPE_JAR_MAP.get(self.document_type, self.document_type),
            _CONFIG_PROPS_PATH,
        )

    def predict(
        self,
        celex_id: str,
        fk_chunk_id: str,
        texts: List[str],
        dcts: List[Optional[str]] = None,
    ) -> List[List[HeidelTimeVO]]:
        """Extract temporal expressions from *texts* and return HeidelTimeVOs.

        :param celex_id: Foreign-key linking results to the documents table.
        :param fk_chunk_id: Foreign-key linking results to the chunks table.
        :param texts: Sequence of text snippets to process.
        :param dcts: Optional ISO-8601 document-creation dates (one per text).
        :returns: One inner list per input text, each containing
            :class:`HeidelTimeVO` objects found in that text.
        """
        if dcts is None:
            dcts = [None] * len(texts)

        predictions: List[List[HeidelTimeVO]] = []
        for text, dct in zip(texts, dcts):
            # raw element shape:
            # {'text': '30 June 2013', 'tid': 't3', 'type': 'DATE',
            #  'value': '2013-06-30', 'span': [117, 129]}
            raw: list = _run_heideltime_jar(
                text=text,
                language=self.language,
                document_type=self.document_type,
                dct=dct,
            )
            vos: List[HeidelTimeVO] = [
                HeidelTimeVO.from_dict(
                    {**pred, "celex_id": celex_id, "fk_chunk_id": fk_chunk_id}
                )
                for pred in raw
            ]
            predictions.append(vos)

        return predictions
