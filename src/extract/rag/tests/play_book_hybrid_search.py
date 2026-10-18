import dspy
from pydantic import BaseModel, Field

from config.llm_thinking import thinking_kwargs
from config.settings import settings
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.embeddings.qwen3_embedding import Qwen3OllamaEmbeddings
from extract.rag.ranker.flash_ranker import FlashRanker
from extract.rag.hybrid_book_search import HybridBookSearch
from extract.rag.models.chunk_vo import BookChapterVo

CASE_ID = "COMP/M.2621"
QUESTION = f"What infringement was investigated in case {CASE_ID}?"


# ---------------------------------------------------------------------------
# DSPy output model and signature
# ---------------------------------------------------------------------------

class InfringementAnswer(BaseModel):
    """Structured answer about a competition-law infringement."""

    infringement_type: str = Field(
        description="Type of infringement investigated (e.g. cartel, merger, abuse of dominance)."
    )
    summary: str = Field(
        description="Brief factual summary of the infringement investigated in the case."
    )


class InfringementQuestion(dspy.Signature):
    """Answer a competition-law question based on the provided book chapter context."""

    context: str = dspy.InputField(
        desc="Relevant book chapter text providing background on the competition case."
    )
    question: str = dspy.InputField(
        desc="Specific question about the infringement investigated in the case."
    )
    answer: InfringementAnswer = dspy.OutputField(
        desc="Structured answer identifying the infringement type and a brief summary."
    )


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

search = HybridBookSearch(
    engine=make_engine(),
    embeddings=Qwen3OllamaEmbeddings(),
    ranker=FlashRanker(),
)
chapter: list[BookChapterVo] = search.search(
    query="cartel fine calculation", keyword=CASE_ID
)
print(f"Retrieved {len(chapter)} chapter rows.")

# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

MODEL_NAME = f"ollama/{settings.LLM_MODEL_ID}"
lm = dspy.LM(
    MODEL_NAME,
    api_base=settings.OLLAMA_HOST,
    model_type="chat",
    **thinking_kwargs(MODEL_NAME),
)
dspy.configure(lm=lm, adapter=dspy.JSONAdapter())

predictor = dspy.ChainOfThought(InfringementQuestion)

context_text = "\n\n".join(row.document for row in chapter)
prediction = predictor(context=context_text, question=QUESTION)

print("\nQuestion:", QUESTION)
print("Infringement type:", prediction.answer.infringement_type)
print("Summary:", prediction.answer.summary)
