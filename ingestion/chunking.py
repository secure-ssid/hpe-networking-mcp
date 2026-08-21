import re

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


_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*?)\s*$", re.MULTILINE)


def heading_breadcrumbs(text: str) -> list[tuple[int, str]]:
    """Return Markdown heading positions and their active ancestor paths."""
    stack: list[tuple[int, str]] = []
    breadcrumbs: list[tuple[int, str]] = []
    for match in _HEADING_RE.finditer(text):
        level = len(match.group(1))
        title = match.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        breadcrumbs.append((match.start(), " > ".join(title for _, title in stack)))
    return breadcrumbs


def breadcrumb_at(
    breadcrumbs: list[tuple[int, str]],
    offset: int,
) -> str | None:
    """Return the heading path active at ``offset``, if one exists."""
    result: str | None = None
    for position, breadcrumb in breadcrumbs:
        if position > offset:
            break
        result = breadcrumb
    return result


def _merge_small_chunks_with_meta(
    pairs: list[tuple[str, str | None]],
) -> list[tuple[str, str | None]]:
    """Apply ``_merge_small_chunks`` while retaining first-piece metadata."""
    if len(pairs) <= 1:
        return pairs

    merged: list[tuple[str, str | None]] = []
    pending_text = ""
    pending_meta: str | None = None
    for chunk, metadata in pairs:
        candidate = f"{pending_text}\n\n{chunk}" if pending_text else chunk
        candidate_meta = pending_meta if pending_text else metadata
        if len(candidate) < MIN_CHUNK_SIZE:
            pending_text = candidate
            pending_meta = candidate_meta
            continue
        merged.append((candidate, candidate_meta))
        pending_text = ""
        pending_meta = None

    if pending_text:
        if merged:
            previous_text, previous_meta = merged[-1]
            merged[-1] = (f"{previous_text}\n\n{pending_text}", previous_meta)
        else:
            merged.append((pending_text, pending_meta))
    return merged


def chunk_text_with_breadcrumbs(text: str) -> list[tuple[str, str | None]]:
    """Chunk text like :func:`chunk_text` and attach heading breadcrumbs."""
    breadcrumbs = heading_breadcrumbs(text)
    cursor = 0
    pairs: list[tuple[str, str | None]] = []
    for chunk in _splitter.split_text(text):
        position = text.find(chunk, cursor)
        if position == -1:
            position = text.find(chunk)
        metadata = breadcrumb_at(breadcrumbs, position) if position >= 0 else None
        if metadata is None and position >= 0:
            chunk_end = position + len(chunk)
            for heading_position, _ in breadcrumbs:
                if position <= heading_position < chunk_end:
                    metadata = breadcrumb_at(breadcrumbs, heading_position)
                    break
        if position >= 0:
            cursor = position
        pairs.append((chunk, metadata))
    return _merge_small_chunks_with_meta(pairs)
