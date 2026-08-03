"""ChromaDB-based vector store for tool discovery and chat memory.
Provides a consistent interface for semantic search across service metadata
and conversation history.
"""
import os
from typing import List, Dict, Any, Optional
from loguru import logger

import chromadb
from chromadb.config import Settings as ChromaSettings
from chromadb.utils import embedding_functions

from app.config import settings


class VectorStore:
    def __init__(self):
        os.makedirs(settings.chroma_persist_dir, exist_ok=True)
        self._client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
        self._embed_fn = None
        self._embed_fn_loaded = False
        self._embed_available = False
        self._init_collections()

    def _init_collections(self):
        self.tools = self._client.get_or_create_collection(
            name="tools",
            embedding_function=self._embed_fn,
            metadata={"hnsw:space": "cosine"},
        )
        self.memory = self._client.get_or_create_collection(
            name="chat_memory",
            embedding_function=self._embed_fn,
            metadata={"hnsw:space": "cosine"},
        )

    def _ensure_embed_fn(self):
        if self._embed_fn_loaded:
            return self._embed_fn
        self._embed_fn_loaded = True
        try:
            self._embed_fn = embedding_functions.ONNXMiniLM_L6_V2()
            self._embed_available = True
            self._init_collections()
            logger.info("ChromaDB ONNX embedding function loaded successfully.")
        except Exception as e:
            logger.warning(f"ChromaDB embedding unavailable ({e}); vector search disabled.")
            self._embed_fn = None
            self._embed_available = False
        return self._embed_fn

    def _flatten_metadata(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """ChromaDB only accepts scalar metadata values. Convert lists/dicts
        to comma-separated strings, drop Nones."""
        out: Dict[str, Any] = {}
        for k, v in metadata.items():
            if v is None:
                continue
            if isinstance(v, (str, int, float, bool)):
                out[k] = v
            elif isinstance(v, (list, tuple, set)):
                out[k] = ",".join(str(x) for x in v)
            elif isinstance(v, dict):
                out[k] = ",".join(f"{kk}={vv}" for kk, vv in v.items())
            else:
                out[k] = str(v)
        return out

    def _ensure_embed_available(self):
        """Trigger lazy load of embedding function. No-op if already tried."""
        if not self._embed_fn_loaded:
            self._ensure_embed_fn()

    def index_tool(self, tool_id: str, text: str, metadata: Dict[str, Any]):
        self._ensure_embed_available()
        if not self._embed_available:
            return
        meta = self._flatten_metadata(metadata)
        try:
            existing = self.tools.get(ids=[tool_id])
            if existing and existing.get("ids"):
                self.tools.update(ids=[tool_id], documents=[text], metadatas=[meta])
                return
        except Exception:
            pass
        self.tools.add(ids=[tool_id], documents=[text], metadatas=[meta])

    def index_tools_bulk(self, items: List[Dict[str, Any]]):
        self._ensure_embed_available()
        if not self._embed_available or not items:
            return
        ids, docs, metas = [], [], []
        for it in items:
            ids.append(it["id"])
            docs.append(it["text"])
            metas.append(self._flatten_metadata(it.get("metadata", {})))
        try:
            self.tools.upsert(ids=ids, documents=docs, metadatas=metas)
        except Exception as e:
            logger.debug(f"Bulk upsert fallback: {e}")
            try:
                self.tools.add(ids=ids, documents=docs, metadatas=metas)
            except Exception as e2:
                logger.warning(f"Bulk add fallback failed: {e2}")

    def search_tools(self, query: str, top_k: int = 8) -> List[Dict[str, Any]]:
        self._ensure_embed_available()
        if not self._embed_available:
            return []
        try:
            res = self.tools.query(query_texts=[query], n_results=top_k)
        except Exception as e:
            logger.warning(f"Tool search failed: {e}")
            return []
        out = []
        for i, doc in enumerate(res.get("documents", [[]])[0]):
            meta = res.get("metadatas", [[]])[0][i] if res.get("metadatas") else {}
            dist = res.get("distances", [[]])[0][i] if res.get("distances") else 0.0
            out.append({"text": doc, "metadata": meta, "score": 1.0 - float(dist)})
        return out

    def add_memory(self, memory_id: str, text: str, metadata: Dict[str, Any]):
        self._ensure_embed_available()
        if not self._embed_available:
            return
        meta = self._flatten_metadata(metadata)
        try:
            self.memory.upsert(ids=[memory_id], documents=[text], metadatas=[meta])
        except Exception:
            self.memory.add(ids=[memory_id], documents=[text], metadatas=[meta])

    def search_memory(self, query: str, top_k: int = 5, where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        self._ensure_embed_available()
        if not self._embed_available:
            return []
        try:
            res = self.memory.query(query_texts=[query], n_results=top_k, where=where)
        except Exception as e:
            logger.warning(f"Memory search failed: {e}")
            return []
        out = []
        for i, doc in enumerate(res.get("documents", [[]])[0]):
            meta = res.get("metadatas", [[]])[0][i] if res.get("metadatas") else {}
            out.append({"text": doc, "metadata": meta})
        return out

    def clear_memory(self):
        try:
            self._client.delete_collection("chat_memory")
            self.memory = self._client.get_or_create_collection(
                name="chat_memory",
                embedding_function=self._embed_fn,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as e:
            logger.warning(f"clear_memory failed: {e}")


vector_store = VectorStore()
