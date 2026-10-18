import tiktoken
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from langchain_core.documents import Document
from typing import List


class TokenTargetMarkdownSplitter:
    def __init__(
        self,
        headers_to_split_on: List[tuple],
        chunk_size: int = 500,
        model_name: str = "gpt-3.5-turbo",
    ):
        self.chunk_size = chunk_size
        self.tokenizer = tiktoken.encoding_for_model(model_name)

        self.md_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=headers_to_split_on, strip_headers=False
        )

        # Recursive splitter for breaking down merged blocks that exceed limit
        self.recursive_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            model_name=model_name, chunk_size=chunk_size, chunk_overlap=0
        )

    def _token_len(self, text: str) -> int:
        return len(self.tokenizer.encode(text))

    def split_text(self, text: str) -> List[Document]:
        raw_splits = self.md_splitter.split_text(text)

        final_docs = []
        current_content_parts = []
        current_token_count = 0
        current_metadata = {}

        for split in raw_splits:
            if not current_content_parts:
                current_metadata = split.metadata.copy()

            content = split.page_content
            token_count = self._token_len(content)

            # --- HEADER BOUNDARY LOGIC --- need to be tested
            headers_changed = current_metadata != split.metadata
            buffer_is_small = current_token_count < 50

            if headers_changed and current_content_parts:
                if buffer_is_small:
                    # Orphaned header merge: adopt the new section's metadata
                    current_metadata = split.metadata.copy()
                else:
                    # Flush large section before starting new header
                    self._flush_buffer(
                        final_docs, current_content_parts, current_metadata
                    )
                    current_content_parts = []
                    current_metadata = split.metadata.copy()
                    current_token_count = 0

            # Calculate hypothetical size if we merge
            combined_size = current_token_count + token_count

            # --- CASE 1: Fits in Buffer ---
            if combined_size <= self.chunk_size:
                current_content_parts.append(content)
                current_token_count += token_count
                continue

            # --- CASE 2: Overflow Detected ---
            # We must decide: Flush buffer? Or Merge & Recursively Split?

            # Heuristic: If current buffer is small (< 50 tokens), it's likely just a Header.
            # We SHOULD NOT orphan it. We must force merge.
            buffer_is_small_header = current_token_count < 50

            if buffer_is_small_header and current_content_parts:
                # MERGE STRATEGY: Combine Buffer + New Content -> Recursive Split
                # This keeps the header attached to the new content
                combined_content = "\n\n".join(current_content_parts + [content])

                # Split the Combined Block
                sub_splits = self.recursive_splitter.create_documents(
                    [combined_content], [current_metadata]
                )

                # Add all resulting parts
                for sub_doc in sub_splits:
                    sub_doc.metadata["token_count"] = self._token_len(
                        sub_doc.page_content
                    )
                    final_docs.append(sub_doc)

                # Reset Buffer
                current_content_parts = []
                current_token_count = 0
                current_metadata = {}

            else:
                # FLUSH STRATEGY: Buffer is a full chunk already. Flush it.
                if current_content_parts:
                    self._flush_buffer(
                        final_docs, current_content_parts, current_metadata
                    )

                # Now handle the new content
                # If the new content ITSELF is huge, split it recursively
                if token_count > self.chunk_size:
                    sub_splits = self.recursive_splitter.create_documents(
                        [content], [split.metadata]
                    )
                    for sub_doc in sub_splits:
                        sub_doc.metadata["token_count"] = self._token_len(
                            sub_doc.page_content
                        )
                        final_docs.append(sub_doc)

                    # Reset
                    current_content_parts = []
                    current_token_count = 0
                    current_metadata = {}
                else:
                    # Otherwise start new buffer with this content
                    current_content_parts = [content]
                    current_token_count = token_count
                    current_metadata = split.metadata.copy()

        # Final Flush
        if current_content_parts:
            self._flush_buffer(final_docs, current_content_parts, current_metadata)

        return final_docs

    def _flush_buffer(self, final_docs_list, content_parts, metadata):
        if not content_parts:
            return
        full_content = "\n\n".join(content_parts)
        metadata["token_count"] = self._token_len(full_content)
        final_docs_list.append(Document(page_content=full_content, metadata=metadata))
