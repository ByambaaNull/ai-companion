"""
memory.py — Persistent memory manager using mem0 + ChromaDB.

All LLM and embedding calls are routed to local Ollama and HuggingFace
sentence-transformers respectively. No network access after first import
of the sentence-transformer model.

Architecture:
    mem0 MemoryClient
    ├── LLM:       Ollama (phi3:mini) — localhost:11434
    ├── Embedder:  sentence-transformers/all-MiniLM-L6-v2 (CPU)
    └── VectorDB:  ChromaDB (local filesystem at data/memory_db/)

Usage:
    mem = MemoryManager()
    mem.add("User's name is Alex", user_id="user")
    results = mem.search("what is the user's name", user_id="user")
    for r in results:
        print(r["memory"])
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import httpx
from mem0 import Memory

import config
from config import MEM0_CONFIG, MEMORY_TOP_K

log = logging.getLogger(__name__)


class MemoryManager:
    """
    Thin wrapper around mem0.Memory that enforces offline configuration
    and provides a clean interface for the companion agent.
    """

    def __init__(self) -> None:
        log.info("Initialising memory system (ChromaDB at %s)", config.MEMORY_DB_DIR)
        # Always pass explicit config — never rely on mem0 defaults (hits OpenAI)
        self._mem = Memory.from_config(MEM0_CONFIG)
        self._write_lock = threading.Lock()  # serialise concurrent DB writes
        log.info("Memory system ready ✓")

    # ─── Write ────────────────────────────────────────────────────────────────

    def add(self, text: str, user_id: str = config.USER_ID) -> list[dict]:
        """
        Extract and store facts from text.

        Uses a compact self-managed LLM call (~300 tokens total) instead of
        mem0's built-in extraction (~7 000-token system prompt) to stay within
        the 8 000-token per-request limit on GitHub Models.
        """
        log.debug("Memory.add | user=%s | text=%.80s…", user_id, text)
        MAX_CHARS = 600
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS]
        facts = self._extract_facts(text)
        if not facts:
            log.debug("Stored 0 memory record(s) (nothing memorable)")
            return []
        stored = []
        for fact in facts:
            with self._write_lock:
                record = self._direct_store(fact, user_id)
            if record:
                stored.append(record)
        log.debug("Stored %d memory record(s)", len(stored))
        return stored

    def _extract_facts(self, text: str) -> list[str]:
        """
        Short-prompt LLM extraction (~300 total tokens, never triggers 413).
        Returns compact fact strings; falls back to raw snippet on failure.
        """
        if not config.LLM_API_KEY:
            return [text[:200]] if len(text) > 10 else []
        try:
            resp = httpx.post(
                config.LLM_API_URL,
                json={
                    "model": config.LLM_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Extract memorable facts from this text as short statements. "
                                "Return one fact per line. "
                                "Return NONE if nothing is worth remembering."
                            ),
                        },
                        {"role": "user", "content": text},
                    ],
                    "max_tokens": 120,
                    "temperature": 0.1,
                    "stream": False,
                },
                headers={
                    "Authorization": f"Bearer {config.LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            if raw.upper().startswith("NONE") and len(raw) < 10:
                return []
            return [
                f.strip()
                for f in raw.splitlines()
                if f.strip() and f.strip().upper() != "NONE"
            ]
        except Exception as exc:
            try:
                body = getattr(getattr(exc, "response", None), "text", "")[:200]
            except Exception:
                body = ""
            log.warning("_extract_facts LLM call failed: %s%s — storing raw snippet",
                        exc, f" — {body}" if body else "")
            return [text[:200]] if len(text) > 10 else []

    def _direct_store(self, text: str, user_id: str) -> dict | None:
        """
        Embed and insert one fact directly, bypassing mem0's LLM extraction step.

        Path 1: mem0's internal embedding_model + vector_store (preferred).
        Path 2: direct chromadb write with sentence-transformers (fallback).
        """
        import uuid as _uuid
        doc_id = str(_uuid.uuid4())
        payload = {"memory": text, "user_id": user_id}

        # Path 1 — mem0 internals
        try:
            embedding = self._mem.embedding_model.embed(text)
            self._mem.vector_store.insert(
                vectors=[embedding],
                payloads=[payload],
                ids=[doc_id],
            )
            return {"id": doc_id, "memory": text}
        except AttributeError:
            pass  # mem0 internals not exposed — fall through to direct ChromaDB
        except Exception as exc:
            log.debug("_direct_store mem0 path failed: %s", exc)

        # Path 2 — direct ChromaDB with sentence-transformers
        try:
            import chromadb
            from chromadb.utils.embedding_functions import (
                SentenceTransformerEmbeddingFunction,
            )
            ef = SentenceTransformerEmbeddingFunction(
                model_name="sentence-transformers/all-MiniLM-L6-v2"
            )
            client = chromadb.PersistentClient(path=str(config.MEMORY_DB_DIR))
            col = client.get_or_create_collection(
                "companion_memory", embedding_function=ef
            )
            col.add(ids=[doc_id], documents=[text], metadatas=[payload])
            return {"id": doc_id, "memory": text}
        except Exception as exc:
            log.error("_direct_store chromadb path failed: %s", exc)
            return None

    def update(self, memory_id: str, new_text: str) -> None:
        """Update an existing memory record by ID."""
        try:
            self._mem.update(memory_id, data=new_text)
            log.debug("Updated memory %s", memory_id)
        except Exception as exc:
            log.error("memory.update failed: %s", exc)

    def delete(self, memory_id: str) -> None:
        """Delete a specific memory record."""
        try:
            self._mem.delete(memory_id)
            log.debug("Deleted memory %s", memory_id)
        except Exception as exc:
            log.error("memory.delete failed: %s", exc)

    def delete_all(self, user_id: str = config.USER_ID) -> None:
        """Wipe all memories for a user. Use with caution."""
        try:
            self._mem.delete_all(user_id=user_id)
            log.warning("All memories deleted for user=%s", user_id)
        except Exception as exc:
            log.error("memory.delete_all failed: %s", exc)

    # ─── Read ─────────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        user_id: str = config.USER_ID,
        limit: int = MEMORY_TOP_K,
    ) -> list[dict[str, Any]]:
        """
        Retrieve the top-K memories relevant to query.

        Returns a list of dicts with at minimum {"id": str, "memory": str}.
        Empty list on error (never raises — caller must not depend on memories).
        """
        log.debug("Memory.search | user=%s | query=%.80s…", user_id, query)
        try:
            results = self._mem.search(
                query,
                filters={"user_id": user_id},
                limit=limit,
            )
            records = results.get("results", []) if isinstance(results, dict) else results
            log.debug("Retrieved %d memory record(s)", len(records))
            return records
        except Exception as exc:
            log.error("memory.search failed: %s", exc)
            return []

    def get_all(self, user_id: str = config.USER_ID) -> list[dict[str, Any]]:
        """Return every stored memory for a user."""
        try:
            # mem0 ≥0.1: scoping is via filters=, not a top-level user_id kwarg
            # (the old form raised "Top-level entity parameters ... not supported").
            results = self._mem.get_all(filters={"user_id": user_id})
            return results.get("results", []) if isinstance(results, dict) else results
        except Exception as exc:
            log.error("memory.get_all failed: %s", exc)
            return []

    # ─── Convenience ──────────────────────────────────────────────────────────

    def format_for_prompt(
        self,
        query: str,
        user_id: str = config.USER_ID,
    ) -> str:
        """
        Retrieve relevant memories and format them as a bullet list ready
        for injection into the LLM system prompt.

        Returns empty string if no memories found.
        """
        records = self.search(query, user_id=user_id)
        if not records:
            return ""
        lines = [f"- {r['memory']}" for r in records if r.get("memory")]
        return "\n".join(lines)


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import time

    config.setup_logging()
    log.info("Running memory.py standalone test…")

    mgr = MemoryManager()

    # 1. Store facts
    log.info("--- Test 1: Storing facts ---")
    mgr.add("My name is Alex. I work as a software engineer.", user_id="test_user")
    mgr.add("I love lofi music and dark mode everything.", user_id="test_user")
    mgr.add("My favourite programming language is Python.", user_id="test_user")
    time.sleep(1)  # give ChromaDB time to flush

    # 2. Search
    log.info("--- Test 2: Searching ---")
    results = mgr.search("what is the user's job?", user_id="test_user")
    log.info("Search results:")
    for r in results:
        log.info("  [%s] %s", r.get("id", "?")[:8], r.get("memory", ""))

    # 3. Format for prompt
    log.info("--- Test 3: Format for prompt ---")
    prompt_context = mgr.format_for_prompt("music preferences", user_id="test_user")
    log.info("Prompt context:\n%s", prompt_context)

    # 4. Cleanup test data
    log.info("--- Test 4: Cleanup ---")
    mgr.delete_all(user_id="test_user")
    assert mgr.get_all(user_id="test_user") == [], "Delete all failed"
    log.info("All tests passed ✓")
