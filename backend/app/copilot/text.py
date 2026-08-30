"""Turning stored document versions into searchable text -- honestly.

Two jobs, both deliberately small:

**Extraction.** Plain text, HTML, RTF, CSV and JSON decode directly. DOCX, PPTX
and XLSX are Open Packaging Convention files -- a ZIP of XML -- so they are read
with ``zipfile`` and ``xml.etree`` from the standard library and no third-party
parser is installed for them. That is a real extractor, not an approximation: the
text runs are the text the author typed. What is deliberately *not* attempted is
PDF, whose content stream needs font and encoding handling that cannot be done
honestly in a few lines; ``extract_text`` returns ``None`` for it and the caller
reports the document as present but not machine-readable, because a confident
answer built from bytes that were never decoded would be worse than admitting the
gap.

**Chunking.** Paragraph-first, so a chunk is a passage a person could read and
verify rather than an arbitrary window. Chunks carry their ordinal so a citation
can point at a location inside a long document.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from xml.etree import ElementTree

# Content types and extensions whose bytes are text. Anything absent from both
# lists is treated as unreadable rather than guessed at.
_TEXT_CONTENT_TYPES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/html",
    "application/json",
    "application/rtf",
    "text/rtf",
    "text/x-markdown",
}
_TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".html", ".htm", ".rtf"}

#: Office Open XML containers, read with the standard library. The value is the
#: reader used once the ZIP is open.
_OPC_EXTENSIONS = {".docx", ".pptx", ".xlsx"}
_OPC_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}

# Formats that need a parser this platform does not depend on. Named explicitly
# so the message can say *why* rather than "unsupported".
_BINARY_EXTENSIONS = {".pdf"}

# Open Packaging Convention namespaces. Only the text-bearing elements are read.
_W_TEXT = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
_W_PARAGRAPH = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
_W_BREAK = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}br"
_W_TAB = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tab"
_A_TEXT = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"
_A_PARAGRAPH = "{http://schemas.openxmlformats.org/drawingml/2006/main}p"
_X_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

#: A policy document is prose, not a data extract. Reading every cell of a large
#: workbook would drown the passages that matter, so spreadsheet extraction is
#: bounded and says so when it stops.
_XLSX_MAX_CELLS = 20_000

_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_RTF_CONTROL = re.compile(r"\\[a-z]+-?\d* ?|[{}]", re.IGNORECASE)
_BLANK_RUN = re.compile(r"\n{3,}")
_SPACE_RUN = re.compile(r"[ \t]{2,}")


@dataclass(frozen=True, slots=True)
class Chunk:
    ordinal: int
    text: str


def is_extractable(content_type: str | None, filename: str | None) -> bool:
    return _kind(content_type, filename) in {"text", "opc"}


def unreadable_reason(content_type: str | None, filename: str | None) -> str:
    """Why a document's content could not be read, in words a user can act on."""
    suffix = _suffix(filename)
    if suffix in _BINARY_EXTENSIONS:
        return (
            f"This version is a {suffix.lstrip('.').upper()} file. Extracting its text "
            "needs font and encoding handling that this deployment does not install, so "
            "its content was not read. Upload the same content as DOCX, Markdown or "
            "plain text to make it machine-readable; its metadata and version history "
            "are available either way, and it can be downloaded."
        )
    if _kind(content_type, filename) == "opc":
        return (
            "This version is an Office file, but its package could not be opened, so no "
            "text was read. The upload may be truncated or corrupt -- try re-uploading it."
        )
    label = content_type or suffix or "unknown"
    return (
        f"This version is stored as '{label}', which is not a text format this platform "
        "extracts, so its content was not searched."
    )


def extract_text(data: bytes, content_type: str | None, filename: str | None) -> str | None:
    """Decode a stored version to plain text, or ``None`` if it is not text.

    ``None`` is a real answer here, not a failure to handle: it drives the
    "present but not machine-readable" message instead of an empty passage that
    would read as an empty document.
    """
    kind = _kind(content_type, filename)
    if kind == "opc":
        return _from_opc(data, _opc_flavour(content_type, filename))
    if kind != "text":
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("utf-8", errors="ignore")
        except Exception:  # pragma: no cover - decode with errors="ignore" cannot raise
            return None

    suffix = _suffix(filename)
    lowered = (content_type or "").lower()
    if suffix in {".html", ".htm"} or "html" in lowered:
        text = _from_html(text)
    elif suffix == ".rtf" or "rtf" in lowered:
        text = _RTF_CONTROL.sub(" ", text)
    elif suffix == ".json" or "json" in lowered:
        text = _from_json(text)

    return normalise(text)


def normalise(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _SPACE_RUN.sub(" ", cleaned)
    cleaned = _BLANK_RUN.sub("\n\n", cleaned)
    return cleaned.strip()


def chunk_text(text: str, *, target_chars: int = 1200, max_chunks: int = 400) -> list[Chunk]:
    """Split into passages at paragraph boundaries where possible.

    Paragraphs are accumulated until adding another would overshoot the target,
    which keeps a definition and its explanation together far more often than a
    fixed-width window does. A single paragraph longer than the target is split
    on sentence ends, and only failing that on raw length.
    """
    if not text:
        return []

    paragraphs = [block.strip() for block in text.split("\n\n") if block.strip()]
    chunks: list[str] = []
    buffer = ""

    for paragraph in paragraphs:
        pieces = (
            [paragraph]
            if len(paragraph) <= target_chars
            else _split_long(paragraph, target_chars)
        )
        for piece in pieces:
            if not buffer:
                buffer = piece
            elif len(buffer) + len(piece) + 2 <= target_chars:
                buffer = f"{buffer}\n\n{piece}"
            else:
                chunks.append(buffer)
                buffer = piece
                if len(chunks) >= max_chunks:
                    return [Chunk(i + 1, c) for i, c in enumerate(chunks[:max_chunks])]
    if buffer:
        chunks.append(buffer)

    return [Chunk(i + 1, c) for i, c in enumerate(chunks[:max_chunks])]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _kind(content_type: str | None, filename: str | None) -> str:
    suffix = _suffix(filename)
    if suffix in _TEXT_EXTENSIONS:
        return "text"
    if suffix in _BINARY_EXTENSIONS:
        return "binary"
    lowered = (content_type or "").split(";")[0].strip().lower()
    if suffix in _OPC_EXTENSIONS or lowered in _OPC_CONTENT_TYPES:
        return "opc"
    if lowered in _TEXT_CONTENT_TYPES or lowered.startswith("text/"):
        return "text"
    # No filename and no recognised content type: inline content is stored as
    # text/plain, so treating the unknown case as text would misread a binary
    # upload. Say so instead.
    return "binary" if lowered else "text"


def _opc_flavour(content_type: str | None, filename: str | None) -> str:
    suffix = _suffix(filename)
    if suffix in _OPC_EXTENSIONS:
        return suffix
    lowered = (content_type or "").split(";")[0].strip().lower()
    return _OPC_CONTENT_TYPES.get(lowered, ".docx")


def _from_opc(data: bytes, flavour: str) -> str | None:
    """Read an Office Open XML package's text with the standard library.

    Returns ``None`` when the package cannot be opened at all, so the caller
    reports "not machine-readable" rather than an empty document -- an empty
    string would read as "this policy says nothing".
    """
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError):
        return None

    try:
        if flavour == ".pptx":
            blocks = _from_pptx(archive)
        elif flavour == ".xlsx":
            blocks = _from_xlsx(archive)
        else:
            blocks = _from_docx(archive)
    except (ElementTree.ParseError, KeyError, OSError, ValueError):
        return None
    finally:
        archive.close()

    joined = "\n\n".join(block for block in blocks if block.strip())
    return normalise(joined) if joined.strip() else None


def _opc_parts(archive: zipfile.ZipFile, prefix: str, suffix: str = ".xml") -> list[str]:
    """Package parts under ``prefix``, in the order a reader would meet them.

    Sorted numerically where the name ends in a number (``slide2`` before
    ``slide10``), because a policy read out of order is a different document.
    """

    names = [
        name
        for name in archive.namelist()
        if name.startswith(prefix) and name.endswith(suffix) and "/_rels/" not in name
    ]

    def key(name: str) -> tuple[int, str]:
        digits = re.search(r"(\d+)\D*$", name)
        return (int(digits.group(1)) if digits else 0, name)

    return sorted(names, key=key)


def _from_docx(archive: zipfile.ZipFile) -> list[str]:
    """Word: one block per paragraph, headers and footnotes included.

    Table cells are paragraphs too in the Word model, so a policy stated inside a
    table -- which is how comparison calendars are usually written -- comes
    through without table-specific handling.
    """

    blocks: list[str] = []
    parts = ["word/document.xml"]
    parts.extend(_opc_parts(archive, "word/header"))
    parts.extend(_opc_parts(archive, "word/footnotes"))
    parts.extend(_opc_parts(archive, "word/endnotes"))
    parts.extend(_opc_parts(archive, "word/footer"))

    for part in parts:
        try:
            raw = archive.read(part)
        except KeyError:
            continue
        root = ElementTree.fromstring(raw)
        for paragraph in root.iter(_W_PARAGRAPH):
            pieces: list[str] = []
            for node in paragraph.iter():
                if node.tag == _W_TEXT:
                    pieces.append(node.text or "")
                elif node.tag == _W_TAB:
                    pieces.append("\t")
                elif node.tag == _W_BREAK:
                    pieces.append("\n")
            text = "".join(pieces).strip()
            if text:
                blocks.append(text)
    return blocks


def _from_pptx(archive: zipfile.ZipFile) -> list[str]:
    """PowerPoint: one block per slide, plus its speaker notes."""

    blocks: list[str] = []
    for part in _opc_parts(archive, "ppt/slides/slide"):
        root = ElementTree.fromstring(archive.read(part))
        lines = [
            "".join(node.text or "" for node in paragraph.iter(_A_TEXT)).strip()
            for paragraph in root.iter(_A_PARAGRAPH)
        ]
        body = "\n".join(line for line in lines if line)
        if body:
            blocks.append(body)

    for part in _opc_parts(archive, "ppt/notesSlides/notesSlide"):
        root = ElementTree.fromstring(archive.read(part))
        note = " ".join(
            "".join(node.text or "" for node in paragraph.iter(_A_TEXT)).strip()
            for paragraph in root.iter(_A_PARAGRAPH)
        ).strip()
        if note:
            blocks.append(note)
    return blocks


def _from_xlsx(archive: zipfile.ZipFile) -> list[str]:
    """Excel: one line per row, bounded, with shared strings resolved.

    Cell *values* are read, not formulas: a policy workbook states "Friday" in a
    cell, and the formula that produced it is not the statement.
    """

    shared: list[str] = []
    try:
        strings_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except (KeyError, ElementTree.ParseError):
        strings_root = None
    if strings_root is not None:
        for entry in strings_root.findall(f"{_X_MAIN}si"):
            shared.append("".join(node.text or "" for node in entry.iter(f"{_X_MAIN}t")))

    blocks: list[str] = []
    cells_read = 0
    for part in _opc_parts(archive, "xl/worksheets/sheet"):
        root = ElementTree.fromstring(archive.read(part))
        lines: list[str] = []
        for row in root.iter(f"{_X_MAIN}row"):
            values: list[str] = []
            for cell in row.findall(f"{_X_MAIN}c"):
                if cells_read >= _XLSX_MAX_CELLS:
                    break
                cells_read += 1
                value_node = cell.find(f"{_X_MAIN}v")
                inline = cell.find(f"{_X_MAIN}is")
                if cell.get("t") == "s" and value_node is not None:
                    index = int(value_node.text or "-1")
                    text = shared[index] if 0 <= index < len(shared) else ""
                elif inline is not None:
                    text = "".join(node.text or "" for node in inline.iter(f"{_X_MAIN}t"))
                else:
                    text = (value_node.text or "") if value_node is not None else ""
                if text.strip():
                    values.append(text.strip())
            if values:
                lines.append(" | ".join(values))
            if cells_read >= _XLSX_MAX_CELLS:
                lines.append(
                    f"[Only the first {_XLSX_MAX_CELLS:,} cells of this workbook were read.]"
                )
                break
        if lines:
            blocks.append("\n".join(lines))
        if cells_read >= _XLSX_MAX_CELLS:
            break
    return blocks


def _suffix(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return f".{filename.rsplit('.', 1)[-1].lower()}"


def _from_html(text: str) -> str:
    without_code = _SCRIPT_OR_STYLE.sub(" ", text)
    # Block-level tags become paragraph breaks so the chunker still sees structure.
    spaced = re.sub(r"</(p|div|h[1-6]|li|tr|table|section|article)>", "\n\n", without_code, flags=re.IGNORECASE)
    spaced = re.sub(r"<br\s*/?>", "\n", spaced, flags=re.IGNORECASE)
    stripped = _HTML_TAG.sub(" ", spaced)
    return (
        stripped.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )


def _from_json(text: str) -> str:
    """Render JSON as ``key: value`` lines so keys become searchable words."""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text

    lines: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        else:
            lines.append(f"{path}: {node}")

    walk(parsed, "")
    return "\n".join(lines)


def _split_long(paragraph: str, target: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
    pieces: list[str] = []
    buffer = ""
    for sentence in sentences:
        while len(sentence) > target:
            pieces.append(sentence[:target])
            sentence = sentence[target:]
        if not buffer:
            buffer = sentence
        elif len(buffer) + len(sentence) + 1 <= target:
            buffer = f"{buffer} {sentence}"
        else:
            pieces.append(buffer)
            buffer = sentence
    if buffer:
        pieces.append(buffer)
    return pieces
