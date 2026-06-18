"""
actions/documents.py — Summarise a local document with one LLM call.

Supported: .pdf (pypdf), .docx (python-docx), .txt / .md (plain read).

Usage (via LLM actions):
    summarize_document | C:\\Users\\me\\Downloads\\report.pdf
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_MAX_CHARS = 8000  # how much document text goes to the LLM

_SUMMARY_SYSTEM = (
    "You are a precise document analyst. Given document text, reply with:\n"
    "1. Summary — 2-4 sentences.\n"
    "2. Key points — short bullet list.\n"
    "3. Action items — bullet list of anything the reader must do, with "
    "deadlines if mentioned (write 'None' if there are none).\n"
    "Be factual; don't invent content that isn't in the text."
)


def _clean_path(param: str) -> Path:
    return Path(param.strip().strip('"').strip("'")).expanduser()


# ─── Extractors (blocking — run in executor) ─────────────────────────────────

def _extract_pdf(path: Path) -> str:
    from pypdf import PdfReader  # lazy optional dep
    reader = PdfReader(str(path))
    chunks: list[str] = []
    total = 0
    for page in reader.pages:
        text = page.extract_text() or ""
        chunks.append(text)
        total += len(text)
        if total >= _MAX_CHARS:
            break
    return "\n".join(chunks)


def _extract_docx(path: Path) -> str:
    import docx  # lazy optional dep (python-docx)
    document = docx.Document(str(path))
    chunks: list[str] = []
    total = 0
    for para in document.paragraphs:
        if para.text.strip():
            chunks.append(para.text)
            total += len(para.text)
            if total >= _MAX_CHARS:
                break
    return "\n".join(chunks)


def _extract_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[:_MAX_CHARS + 1000]


# ─── Public action ───────────────────────────────────────────────────────────

async def summarize_document(param: str) -> str:
    """Summarise a .pdf / .docx / .txt / .md file. param = file path."""
    try:
        if not param.strip():
            return "Give me a file path, e.g. summarize_document | C:\\path\\to\\file.pdf"

        path = _clean_path(param)
        if not path.exists():
            return (f"Can't find '{path}'. Check the path — quotes are fine, "
                    "but the file has to exist.")
        if path.is_dir():
            return f"'{path}' is a folder — point me at a single document."

        suffix = path.suffix.lower()
        loop = asyncio.get_running_loop()
        try:
            if suffix == ".pdf":
                text = await loop.run_in_executor(None, _extract_pdf, path)
            elif suffix == ".docx":
                text = await loop.run_in_executor(None, _extract_docx, path)
            elif suffix in (".txt", ".md", ".markdown", ".log", ".rst"):
                text = await loop.run_in_executor(None, _extract_text, path)
            else:
                return (f"I can't read {suffix or 'extension-less'} files yet — "
                        "supported: .pdf, .docx, .txt, .md")
        except ImportError:
            dep = "pypdf" if suffix == ".pdf" else "python-docx"
            return f"pip install {dep} to enable {suffix} summaries."

        text = (text or "").strip()
        if not text:
            return (f"'{path.name}' contains no extractable text "
                    "(maybe a scanned/image-only document?).")
        truncated = len(text) > _MAX_CHARS
        text = text[:_MAX_CHARS]

        from main import get_llm_response  # lazy — avoids circular import
        note = ("\n\n[Note: document truncated to the first "
                f"{_MAX_CHARS} characters.]" if truncated else "")
        summary = await get_llm_response(
            f"Document: {path.name}\n\n{text}{note}", _SUMMARY_SYSTEM)
        if not summary:
            return "The summariser came back empty — try again in a moment."
        return f"📄 {path.name}\n\n{summary}"
    except Exception as exc:
        log.error("summarize_document failed: %s", exc, exc_info=True)
        return f"Couldn't summarise that document: {exc}"
