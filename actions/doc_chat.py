"""
doc_chat.py — "chat with a document" (RAG) using the local embedding model +
Chroma the app already ships, answered by the user's own LLM. Replaces ChatPDF /
Humata. Fully local except the final LLM call.

    ingest(path)   → extract text, chunk, embed into a fresh Chroma collection
    ask(question)  → retrieve the most relevant chunks and answer with the LLM

One document at a time: ingest() resets the collection.
"""

from __future__ import annotations

import logging
from pathlib import Path

import config

log = logging.getLogger(__name__)

_DOC_DB = config.DATA_DIR / "doc_db"
_COLLECTION = "doc_chat"
_ef = None  # cached embedding function (loading the model is slow)


def _embedder():
    global _ef
    if _ef is None:
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        _ef = SentenceTransformerEmbeddingFunction(model_name=config.EMBEDDER_MODEL)
    return _ef


def _client():
    import chromadb
    return chromadb.PersistentClient(path=str(_DOC_DB))


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".csv", ".log"):
        return path.read_text(encoding="utf-8", errors="replace")
    # PDF / EPUB / etc. via PyMuPDF
    try:
        import fitz
        doc = fitz.open(str(path))
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        return text
    except Exception as exc:
        log.warning("doc_chat extract failed (%s); trying plain read", exc)
        return path.read_text(encoding="utf-8", errors="replace")


def _chunk(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        i += size - overlap
    return out


def ingest(path: str) -> dict:
    """Index a document for Q&A (resets any previously loaded document)."""
    src = Path(path)
    if not src.exists():
        return {"ok": False, "error": "File not found."}
    try:
        text = _extract_text(src)
        chunks = _chunk(text)
        if not chunks:
            return {"ok": False, "error": "No readable text in that file."}
        client = _client()
        try:
            client.delete_collection(_COLLECTION)
        except Exception:
            pass
        col = client.create_collection(_COLLECTION, embedding_function=_embedder())
        # Batch the adds so large docs don't blow a single call.
        for start in range(0, len(chunks), 128):
            batch = chunks[start:start + 128]
            col.add(documents=batch,
                    ids=[f"c{start + j}" for j in range(len(batch))],
                    metadatas=[{"src": src.name} for _ in batch])
        return {"ok": True, "name": src.name, "chunks": len(chunks)}
    except Exception as exc:
        log.error("doc_chat ingest failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def ask(question: str) -> dict:
    """Answer a question about the last ingested document."""
    if not (question or "").strip():
        return {"ok": False, "error": "Type a question."}
    try:
        client = _client()
        try:
            col = client.get_collection(_COLLECTION, embedding_function=_embedder())
        except Exception:
            return {"ok": False, "error": "Load a document first."}
        res = col.query(query_texts=[question], n_results=5)
        docs = (res.get("documents") or [[]])[0]
        if not docs:
            return {"ok": False, "error": "Nothing indexed — load a document first."}
        context = "\n\n---\n\n".join(docs)
        from actions.text_tools import _chat
        return _chat(
            "Answer the user's question using ONLY the document excerpts provided. "
            "If the answer isn't in them, say so. Be concise and cite nothing else.",
            f"Document excerpts:\n{context}\n\nQuestion: {question}",
            max_tokens=600,
        )
    except Exception as exc:
        log.error("doc_chat ask failed: %s", exc)
        return {"ok": False, "error": str(exc)}
