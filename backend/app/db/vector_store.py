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
        try:
            from chromadb.utils import embedding_functions as _ef
            if hasattr(_ef, "DefaultEmbeddingFunction"):
                self._embed_fn = _ef.DefaultEmbeddingFunction()
                logger.info("Using ChromaDB DefaultEmbeddingFunction (no model download required).")
        except Exception as e:
            logger.warning(f"Could not initialize default embedding function: {e}")
        if self._embed_fn is None:
            try:
                self._embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name=settings.embedding_model
                )
            except Exception as e:
                logger.warning(f"SentenceTransformer unavailable ({e}); embeddings disabled.")
                self._embed_fn = None
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

    def index_tool(self, tool_id: str, text: str, metadata: Dict[str, Any]):
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
        if not items:
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
        meta = self._flatten_metadata(metadata)
        try:
            self.memory.upsert(ids=[memory_id], documents=[text], metadatas=[meta])
        except Exception:
            self.memory.add(ids=[memory_id], documents=[text], metadatas=[meta])

    def search_memory(self, query: str, top_k: int = 5, where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
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
