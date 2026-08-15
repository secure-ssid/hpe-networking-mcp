from langchain_text_splitters import RecursiveCharacterTextSplitter

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100

#: Chunks shorter than this are folded into a neighboring chunk instead of
#: being embedded/indexed standalone (see ``_merge_small_chunks``).
MIN_CHUNK_SIZE = 200

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
    return _merge_small_chunks(_splitter.split_text(text))


def _merge_small_chunks(chunks: list[str]) -> list[str]:
    """Fold chunks under ``MIN_CHUNK_SIZE`` into an adjacent chunk.

    ``RecursiveCharacterTextSplitter`` splits on its highest-priority
    separator ("\\n\\n") first. A short heading (optionally preceded by an
    HTML source comment) immediately followed by a blank line and a large
    body paragraph -- an extremely common Markdown shape -- is carved into
    its own tiny "orphan" chunk, because the splitter's merge step won't
    combine it with the next paragraph once doing so would exceed
    ``CHUNK_SIZE``. That orphan chunk still gets embedded and indexed, and
    because it carries none of the diluting body text, it can outrank its
    own (far more useful) neighbor for title-keyword queries -- confirmed
    for real ingested content, e.g. a lone
    ``"# EX4400 -- specifications"`` chunk winning top-1 retrieval for
    "EX4400 switch power specifications" ahead of the chunk with the actual
    spec values.

    This performs a single accumulating pass: small chunks are folded
    forward into however many of their immediate successors are needed to
    clear ``MIN_CHUNK_SIZE`` (so a run of several tiny paragraphs merges
    into one usable chunk, not several still-tiny ones). A small chunk with
    no successor (i.e. the text ends on a small trailing chunk) is instead
    folded into the previous, already-emitted chunk. A document that never
    reaches ``MIN_CHUNK_SIZE`` in total is returned as its own single
    chunk -- there is nothing else to merge it with.
    """
    if len(chunks) <= 1:
        return chunks

    merged: list[str] = []
    pending = ""
    for chunk in chunks:
        candidate = f"{pending}\n\n{chunk}" if pending else chunk
        if len(candidate) < MIN_CHUNK_SIZE:
            pending = candidate
            continue
        merged.append(candidate)
        pending = ""

    if pending:
        if merged:
            merged[-1] = f"{merged[-1]}\n\n{pending}"
        else:
            merged.append(pending)

    return merged
