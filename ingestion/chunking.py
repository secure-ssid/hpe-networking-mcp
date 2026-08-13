from langchain_text_splitters import RecursiveCharacterTextSplitter

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=CHUNK_OVERLAP,
    # Trailing "" enables a character-level fallback split: without it, text
    # with none of the other separators (e.g. a long unbroken token/base64
    # blob) comes back as a single unbounded chunk instead of being cut to
    # CHUNK_SIZE.
    separators=["\n\n", "\n", ". ", " ", ""],
)


def chunk_text(text: str) -> list[str]:
    return _splitter.split_text(text)
