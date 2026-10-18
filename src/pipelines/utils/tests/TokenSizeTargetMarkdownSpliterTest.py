import os

from config.settings import settings
from pipelines.utils.TokenSizeTargetMarkdownSplitter import TokenTargetMarkdownSplitter

# --- Usage Example ---

markdown_text = """
# 3. LEGAL ASSESSMENT

# 3.1. The relevant sector
(53) The Commission notes at the outset that...

# 3.2. Another Sector
This is just some filler text to demonstrate the accumulation logic...
"""

headers = [("#", "Header 1"), ("##", "Header 2")]

splitter = TokenTargetMarkdownSplitter(headers_to_split_on=headers, chunk_size=50)
docs = splitter.split_text(markdown_text)

for doc in docs:
    print(f"Token Count: {doc.metadata['token_count']}")
    print(f"Content Preview: {doc.page_content[:40]}...")
    print("-" * 20)
    assert doc.metadata['token_count'] <= 50 and doc.metadata['token_count'] > 10

# ------------

# --- Test with your Specific Scenario ---

markdown_text = """
# 3. LEGAL ASSESSMENT

# 3.1. The relevant sector
(53) The Commission notes at the outset that, in accordance with the case law of the Court of Justice of the European Communities...
(Here is enough text to fill up 480 tokens) ... 
"""

# Headers definition
headers = [("#", "Header 1"), ("##", "Header 2")]

# Chunk size 500
splitter = TokenTargetMarkdownSplitter(headers_to_split_on=headers, chunk_size=500)
docs = splitter.split_text(markdown_text)

print(f"--- Created {len(docs)} chunks ---\n")
for i, doc in enumerate(docs):
    print(f"--- Chunk {i+1} ({doc.metadata.get('token_count')} tokens) ---")
    print(f"Metadata: {doc.metadata}")
    # Preview content
    preview = doc.page_content[:100].replace('\n', ' ')
    print(f"Content: {preview}...")
    print("-" * 30)
    assert doc.metadata['token_count'] <= 500 and doc.metadata['token_count'] > 50
# --------

fn = os.path.join(
    settings.CELEX_DOCS_DIR, "CELEX-31991D0055/mineru/auto/CELEX-31991D0055.md"
)
try:
        with open( fn, "r", encoding="utf-8") as f:
            markdown_text = f.read()
except FileNotFoundError:
        print("⚠️  {fn} not found, skipping real document test")
        exit(1)
splitter = TokenTargetMarkdownSplitter(headers_to_split_on=headers, chunk_size=500)     


chunks = splitter.split_text(markdown_text)
small_chunk_count = 0
print(f"Created {len(chunks)} chunks:\n")
for i, chunk in enumerate(chunks):
    print(f"--- Chunk {i+1} ({chunk.metadata['token_count']} tokens) ---")
    print(f"Metadata: {chunk.metadata}")
    print(chunk.page_content[:100].replace('\n', ' ')) # Print preview
    print("\n")
    if chunk.metadata['token_count'] < 100:
        small_chunk_count += 1
        continue
    assert chunk.metadata['token_count'] <= 500  # and chunk.metadata['token_count'] > 100
print(f"Small chunks: {small_chunk_count} out of {len(chunks)}")

# ---------------
fn = os.path.join(settings.CELEX_DOCS_DIR, "37750_87_1/mineru/auto/37750_87_1.md")
try:
        with open( fn, "r", encoding="utf-8") as f:
            markdown_text = f.read()
except FileNotFoundError:
        print("⚠️  {fn} not found, skipping real document test")
        exit(1)
splitter = TokenTargetMarkdownSplitter(headers_to_split_on=headers, chunk_size=500)     

small_chunk_count = 0
chunks = splitter.split_text(markdown_text)

print(f"Created {len(chunks)} chunks:\n")
for i, chunk in enumerate(chunks):
    print(f"--- Chunk {i+1} ({chunk.metadata['token_count']} tokens) ---")
    print(f"Metadata: {chunk.metadata}")
    print(chunk.page_content[:100].replace('\n', ' ')) # Print preview
    print("\n")
    if chunk.metadata['token_count'] < 100:
        small_chunk_count += 1
        continue
    assert chunk.metadata['token_count'] <= 500  # and chunk.metadata['token_count'] > 100
    if chunk.metadata['token_count'] < 100:
        small_chunk_count += 1
        continue
    assert chunk.metadata['token_count'] <= 500  # and chunk.metadata['token_count'] > 100
print(f"Small chunks: {small_chunk_count} out of {len(chunks)}")

