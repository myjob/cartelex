import logging

import ollama
from langchain_core.embeddings import Embeddings
from config.settings import settings
import tiktoken

logger = logging.getLogger(__name__)

_TIKTOKEN_ENCODING = "cl100k_base"


class EmbeddingInputTooLongError(ValueError):
    """Raised when input exceeds the embedding model's context window.

    Subclasses ``ValueError`` so existing ``except ValueError`` handlers keep
    working, while callers that can react to the size specifically (for example
    by splitting the input and pooling the results) can catch this type.

    Attributes:
        tokens_num: Token count of the offending input.
        token_limit: Configured limit that was exceeded.
    """

    def __init__(self, tokens_num: int, token_limit: int) -> None:
        """Store the measured token count and the limit that was exceeded."""
        self.tokens_num = tokens_num
        self.token_limit = token_limit
        super().__init__(
            f"Input text is too long for embeddings: {tokens_num} tokens "
            f"exceeds the limit of {token_limit}"
        )


def count_tokens(text: str) -> int:
    """Count tokens the same way the embedding size guard does.

    Uses the tiktoken ``cl100k_base`` encoding. This is an approximation of the
    Qwen3 tokenizer, so callers sizing input near the limit should keep a margin.

    Args:
        text: Text to measure.

    Returns:
        Number of tokens in ``text``.
    """
    return len(tiktoken.get_encoding(_TIKTOKEN_ENCODING).encode(text))


def embedding_token_limit() -> int:
    """Return the configured embedding context limit in tokens.

    Returns:
        ``settings.OLLAMA_EMBEDDING_QWEN3_TOKEN_LIMIT`` as an int.
    """
    return int(settings.OLLAMA_EMBEDDING_QWEN3_TOKEN_LIMIT)


def retrieve_qwen3_embedding(
    text: str | list[str], model: str = None, host: str = None
) -> list[float] | list[list[float]]:
    """
    Generate an embedding for the input text using the local Ollama server
    and the configured qwen3 embedding model.

    The size guard is deliberately strict: for chunk ingestion pipelines an
    oversized input means the chunker misbehaved and must fail loudly. Callers
    with legitimately long input (such as the fact loader) split it themselves
    and pool the resulting vectors.

    Args:
        text: The input text or sequence of texts to embed.
        model: The embedding model to use. Defaults to settings.EMBEDDING_MODEL_qwen3.
        host: The Ollama server host. Defaults to settings.OLLAMA_HOST.

    Returns:
        A single embedding vector for str input, or a list of vectors for list input.

    Raises:
        EmbeddingInputTooLongError: If the input exceeds
            ``settings.OLLAMA_EMBEDDING_QWEN3_TOKEN_LIMIT`` tokens.
    """
    model = model or settings.EMBEDDING_MODEL_qwen3
    host = host or settings.OLLAMA_HOST

    if isinstance(text, str):
        text_str = text
    else:
        text_str = "\n".join(text)

    tokens_num = count_tokens(text_str)
    token_limit = embedding_token_limit()
    logger.debug("Tokens for embeddings: %d (limit %d)", tokens_num, token_limit)
    if tokens_num > token_limit:
        raise EmbeddingInputTooLongError(tokens_num, token_limit)

    client = ollama.Client(host=host)
    response = client.embed(model=model, input=text)

    # Safely handle the response whether it's an EmbedResponse object or a dict
    embeddings = (
        response.embeddings
        if hasattr(response, "embeddings")
        else response.get("embeddings", [])
    )

    if isinstance(text, str):
        return embeddings[0] if embeddings else []
    return embeddings


class Qwen3OllamaEmbeddings(Embeddings):
    """
    LangChain compatible embeddings class for using the qwen3-embedding model
    with a local Ollama server. Useful for injecting into Vector stores
    following the Provisioner Pattern.
    """

    def __init__(self, model: str = None, host: str = None):
        """Initialize the embedding model client."""
        self.model = model or settings.EMBEDDING_MODEL_qwen3
        self.host = host or settings.OLLAMA_HOST
        self.client = ollama.Client(host=self.host)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Compute doc embeddings using the local Ollama model."""
        response = self.client.embed(model=self.model, input=texts)
        embeddings = (
            response.embeddings
            if hasattr(response, "embeddings")
            else response.get("embeddings", [])
        )
        return embeddings

    def embed_query(self, text: str) -> list[float]:
        """Compute query embedding using the local Ollama model."""
        response = self.client.embed(model=self.model, input=text)
        embeddings = (
            response.embeddings
            if hasattr(response, "embeddings")
            else response.get("embeddings", [])
        )
        return embeddings[0] if embeddings else []
