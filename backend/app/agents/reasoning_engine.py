"""LLM Reasoning Engine.

The engine is responsible for turning a natural-language query into a
structured orchestration plan:

{
  "intent": "fetch" | "aggregate" | "navigate" | "summarize" | "unknown",
  "target_services": ["crm"],
  "steps": [
    {
      "service_id": "crm",
      "entity_set": "Customers",
      "select": ["CustomerID", "Name", "Country"],
      "filter": "Country eq 'USA'",
      "expand": ["Orders"],
      "top": 10,
      "skip": 0,
      "orderby": "Name asc"
    }
  ],
  "summary": "Show top 10 customers in the USA with their orders"
}

It supports three providers:
  - "mock": heuristic intent/entity extraction (always available)
  - "openai": uses the OpenAI chat completions API (requires OPENAI_API_KEY)
  - "gemini": uses Google Gemini via google-genai (requires GEMINI_API_KEY)

plan() returns a tuple: (plan_dict, metadata_dict) where metadata_dict
contains provider, latency_ms, and tokens_used.
"""
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger

from app.config import settings
from app.services.query_optimizer import QueryOptimizer, QueryIntent, query_optimizer
from app.services.query_rag import query_plan_rag


class LLMReasoningEngine:
    def __init__(self):
        self.provider = settings.llm_provider
        self.model = settings.llm_model
        self._lock = None
        self._key_index = 0
        self.optimizer = query_optimizer

    def set_config(self, provider: Optional[str] = None, model: Optional[str] = None) -> None:
        """Update the active LLM provider/model at runtime.

        Both arguments are optional; pass only the one(s) you want to change.
        """
        if provider is not None:
            self.provider = provider
        if model is not None:
            self.model = model
        logger.info(f"LLM config updated: provider={self.provider}, model={self.model}")

    def _get_next_api_key(self) -> str:
        """Get the next API key from the rotation list."""
        keys = settings.openai_api_keys_list
        if not keys:
            return settings.openai_api_key
        key = keys[self._key_index % len(keys)]
        return key

    def _rotate_api_key(self) -> str:
        """Rotate to the next API key after a rate limit error."""
        keys = settings.openai_api_keys_list
        if len(keys) <= 1:
            return keys[0] if keys else settings.openai_api_key
        self._key_index = (self._key_index + 1) % len(keys)
        rotated = keys[self._key_index]
        logger.info(f"Rotated to API key index {self._key_index}: {rotated[:10]}...")
        return rotated

    def get_config(self) -> Dict[str, Any]:
        return {"provider": self.provider, "model": self.model}

    def _normalize_query_typos(self, query: str) -> str:
        """Normalize common business-term typos before planning."""
        normalized = re.sub(r"\bchat\s+of\s+accounts\b", "chart of accounts", query, flags=re.IGNORECASE)
        return normalized

    def _detect_explicit_service(self, services: List[Dict[str, Any]], query: str) -> Optional[str]:
        """Detect if user explicitly names a service via 'from X' or just mentions the service name.
        Returns service_id if matched, None otherwise."""
        import re
        stop_words = {"where", "and", "with", "show", "get", "list", "filter", "that", "which", "who", "the", "first", "top", "last", "all", "some", "how", "many", "much", "count", "sum", "average", "total", "min", "max", "please", "give", "find"}
        match = re.search(r'\bfrom\s+(.+?)(?:\s+(?:where|and|with|show|get|list|filter|that|which|who|please|give|find)\b|\s*$)', query, re.IGNORECASE)
        if match:
            phrase = match.group(1).strip().lower()
            words = [w for w in phrase.split() if w not in stop_words and len(w) >= 2]
            phrase_clean = " ".join(words)
            for svc in services:
                if len(phrase_clean) < 2:
                    continue
                svc_id = svc["id"].lower()
                svc_name = svc.get("name", "").lower()
                if phrase_clean in svc_id or phrase_clean in svc_name:
                    return svc["id"]
                if phrase in svc_id or phrase in svc_name:
                    return svc["id"]
                svc_name_words = set(re.findall(r'[a-z]{3,}', svc_name))
                phrase_words = set(words)
                if len(svc_name_words & phrase_words) >= 2:
                    return svc["id"]

        for svc in services:
            svc_id = svc["id"].lower()
            svc_name_words = set(re.findall(r'[a-z]{3,}', svc.get("name", "").lower()))
            query_words = set(re.findall(r'[a-z]{3,}', query))
            if len(svc_name_words & query_words) >= 2:
                return svc["id"]
            if svc_id in query:
                return svc["id"]

        return None

    def _detect_exact_entity(self, services: List[Dict[str, Any]], query: str) -> Optional[Tuple[str, str]]:
        """Return (service_id, entity_set) when the query contains an exact entity set name."""
        q = query.lower()
        q_compact = re.sub(r"[^a-z0-9]", "", q)
        matches: List[Tuple[int, str, str]] = []

        def entity_search_forms(entity_name: str) -> List[str]:
            forms = {entity_name.lower()}
            no_prefix = re.sub(r"^[aci]_", "", entity_name, flags=re.IGNORECASE)
            spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", no_prefix)
            spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
            spaced = spaced.replace("_", " ").replace("-", " ").replace(".", " ")
            spaced = re.sub(r"\s+", " ", spaced).strip().lower()
            if spaced:
                forms.add(spaced)
                forms.add(spaced.replace(" ", ""))
            forms.add(no_prefix.lower())
            forms.add(no_prefix.lower().replace("_", " "))
            return [f for f in forms if f]

        for svc in services:
            for entity in svc.get("entity_sets", []):
                entity_lower = entity.lower()
                entity_compact = re.sub(r"[^a-z0-9]", "", entity_lower)
                if not entity_compact:
                    continue
                forms = entity_search_forms(entity)
                compact_forms = [re.sub(r"[^a-z0-9]", "", f) for f in forms]
                if any(f in q for f in forms) or any(cf and cf in q_compact for cf in compact_forms):
                    matches.append((len(entity_compact), svc["id"], entity))
        if not matches:
            return None
        matches.sort(reverse=True)
        _, service_id, entity_set = matches[0]
        return service_id, entity_set

    def find_entity_candidates(
        self,
        services: List[Dict[str, Any]],
        query: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Rank entity candidates from user words without hardcoded domain routes."""
        q = self._normalize_query_typos(query).lower()
        stop_words = {
            "show", "get", "list", "display", "fetch", "find", "all", "the", "a", "an",
            "of", "for", "from", "with", "by", "top", "first", "please", "me",
        }
        q_words = set(re.findall(r"[a-z0-9]{2,}", q)) - stop_words
        q_phrase = " ".join(w for w in re.findall(r"[a-z0-9]{2,}", q) if w not in stop_words)

        def split_entity(name: str) -> List[str]:
            no_prefix = re.sub(r"^[aci]_", "", name, flags=re.IGNORECASE)
            spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", no_prefix)
            spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
            spaced = spaced.replace("_", " ").replace("-", " ").replace(".", " ")
            return re.findall(r"[a-z0-9]{2,}", spaced.lower())

        candidates: List[Dict[str, Any]] = []
        for svc in services:
            labels = svc.get("entity_labels", {})
            props_by_entity = svc.get("entity_properties", {})
            for entity in svc.get("entity_sets", []):
                words = split_entity(entity)
                if not words:
                    continue
                word_set = set(words)
                overlap = q_words & word_set
                entity_phrase = " ".join(words)
                compact_entity = "".join(words)
                compact_query = re.sub(r"[^a-z0-9]", "", q)
                score = 0.0
                if overlap:
                    score += len(overlap) / max(len(q_words), 1)
                    score += len(overlap) / len(word_set)
                if q_phrase and (q_phrase in entity_phrase or entity_phrase in q_phrase):
                    score += 1.5
                if compact_entity and compact_entity in compact_query:
                    score += 2.0
                label = labels.get(entity, {}).get("entity_label", "")
                if label:
                    label_words = set(re.findall(r"[a-z0-9]{2,}", label.lower()))
                    label_overlap = q_words & label_words
                    if label_overlap:
                        score += len(label_overlap) / max(len(q_words), 1)
                if score <= 0:
                    continue
                candidates.append({
                    "service_id": svc["id"],
                    "service_name": svc.get("name", svc["id"]),
                    "entity_set": entity,
                    "entity_label": label,
                    "score": round(score, 4),
                    "properties": props_by_entity.get(entity, [])[:12],
                })

        candidates.sort(key=lambda c: (-c["score"], c["service_id"], c["entity_set"]))
        return candidates[:limit]

    def _build_candidate_plan(self, query: str, candidate: Dict[str, Any], memory_context: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        entity_set = candidate["entity_set"]
        service_id = candidate["service_id"]
        select, expand, filter_expr, orderby, top = self._build_query_parts(
            query.lower(),
            entity_set,
            [],
            service_id=service_id,
            metadata_xml="",
        )
        if top is None:
            top = 20
        return {
            "intent": self._infer_intent(query.lower()),
            "target_services": [service_id],
            "steps": [{
                "service_id": service_id,
                "entity_set": entity_set,
                "select": select,
                "filter": filter_expr,
                "expand": expand,
                "top": top,
                "skip": 0,
                "orderby": orderby,
            }],
            "summary": f"Showing data from {entity_set}.",
            "memory_used": memory_context or [],
        }

    def _truncate_service_for_llm(self, svc: Dict[str, Any], max_entities: int = 15, max_props_per_entity: int = 8) -> Dict[str, Any]:
        """Truncate service data to fit within LLM token limits.
        For large services, send only suggested entity names + limited properties."""
        entity_props = svc.get("entity_properties", {})
        entity_sets = svc.get("entity_sets", [])
        entity_labels = svc.get("entity_labels", {})

        if len(entity_sets) <= max_entities:
            return {
                "id": svc["id"],
                "name": svc["name"],
                "entity_sets": entity_sets,
                "entity_properties": entity_props,
                "entity_labels": entity_labels,
            }

        truncated_props = {}
        truncated_labels = {}
        for es_name in entity_sets[:max_entities]:
            props = entity_props.get(es_name, [])
            truncated_props[es_name] = props[:max_props_per_entity]
            if es_name in entity_labels:
                truncated_labels[es_name] = entity_labels[es_name]

        return {
            "id": svc["id"],
            "name": svc["name"],
            "entity_sets": entity_sets,
            "entity_properties": truncated_props,
            "entity_labels": truncated_labels,
        }

    async def plan(
        self,
        query: str,
        available_services: List[Dict[str, Any]],
        memory_context: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        query = self._normalize_query_typos(query)
        exact_entity = self._detect_exact_entity(available_services, query)
        if exact_entity:
            service_id, entity_set = exact_entity
            svc = next((s for s in available_services if s["id"] == service_id), None)
            metadata_xml = svc.get("metadata_xml", "") if svc else ""
            select, expand, filter_expr, orderby, top = self._build_query_parts(
                query.lower(),
                entity_set,
                [],
                service_id=service_id,
                metadata_xml=metadata_xml,
            )
            if top is None:
                top = 20
            plan = {
                "intent": self._infer_intent(query.lower()),
                "target_services": [service_id],
                "steps": [{
                    "service_id": service_id,
                    "entity_set": entity_set,
                    "select": select,
                    "filter": filter_expr,
                    "expand": expand,
                    "top": top,
                    "skip": 0,
                    "orderby": orderby,
                }],
                "summary": f"Showing data from {entity_set}.",
                "memory_used": memory_context or [],
            }
            plan = self.optimizer.optimize_plan(plan, query)
            self.optimizer.cache_plan(query, [service_id], plan)
            self.optimizer._stats["llm_skipped"] += 1
            logger.info(f"Exact entity detected: {service_id}/{entity_set}; skipping LLM")
            return plan, {"provider": "entity-match", "latency_ms": 0, "tokens": 0, "intent": plan["intent"]}

        explicit_service = self._detect_explicit_service(available_services, query.lower())
        if explicit_service:
            filtered = [s for s in available_services if s["id"] == explicit_service]
            logger.info(f"Explicit service detected: {explicit_service} — calling LLM with filtered services")
        else:
            filtered = available_services

        # ── Query Optimizer: intent classification ────────────────────────
        intent = self.optimizer.classify_intent(query)
        self.optimizer._stats["intent_classified"] += 1
        is_complex = self.optimizer._is_complex_query(query.lower())
        logger.info(f"Query intent: {intent} | complex: {is_complex}")

        # Check cache first
        service_ids = [s["id"] for s in filtered]
        cached_plan = self.optimizer.get_cached_plan(query, service_ids)
        if cached_plan:
            logger.info("Using cached query plan")
            return cached_plan, {"provider": "cached", "latency_ms": 0, "tokens": 0}

        # Skip LLM for certain intents with explicit service
        if explicit_service and self.optimizer.can_skip_llm(intent, has_explicit_service=True, is_complex=is_complex):
            logger.info(f"Skipping LLM for intent={intent} with explicit service={explicit_service}")
            self.optimizer._stats["llm_skipped"] += 1
            t0 = time.perf_counter()
            plan = self._plan_mock(query, filtered, memory_context)
            plan = self.optimizer.optimize_plan(plan, query)
            self.optimizer.cache_plan(query, service_ids, plan)
            return plan, {"provider": "mock", "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens": 0, "intent": intent}

        if self.provider == "openai" and settings.openai_api_key:
            t0 = time.perf_counter()
            try:
                plan, tokens = await self._plan_openai(query, filtered, memory_context)
                plan = self.optimizer.optimize_plan(plan, query)
                self.optimizer.cache_plan(query, service_ids, plan)
                return plan, {"provider": "openai", "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens": tokens, "intent": intent}
            except Exception as e:
                logger.warning(f"OpenAI planning failed, falling back to mock: {e}")
        elif self.provider == "openrouter" and settings.openrouter_api_key:
            t0 = time.perf_counter()
            try:
                plan, tokens = await self._plan_openrouter(query, filtered, memory_context)
                plan = self.optimizer.optimize_plan(plan, query)
                self.optimizer.cache_plan(query, service_ids, plan)
                return plan, {"provider": "openrouter", "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens": tokens, "intent": intent}
            except Exception as e:
                logger.warning(f"OpenRouter planning failed, falling back to mock: {e}")
        elif self.provider == "gemini" and settings.gemini_api_key:
            t0 = time.perf_counter()
            try:
                plan, tokens = await self._plan_gemini(query, filtered, memory_context)
                plan = self.optimizer.optimize_plan(plan, query)
                self.optimizer.cache_plan(query, service_ids, plan)
                return plan, {"provider": "gemini", "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens": tokens, "intent": intent}
            except Exception as e:
                logger.warning(f"Gemini planning failed, falling back to mock: {e}")
        elif self.provider == "nvidia" and settings.nvidia_api_key:
            t0 = time.perf_counter()
            try:
                plan, tokens = await self._plan_nvidia(query, filtered, memory_context)
                plan = self.optimizer.optimize_plan(plan, query)
                self.optimizer.cache_plan(query, service_ids, plan)
                return plan, {"provider": "nvidia", "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens": tokens, "intent": intent}
            except Exception as e:
                logger.warning(f"NVIDIA planning failed, falling back to mock: {e}")
        t0 = time.perf_counter()
        plan = self._plan_mock(query, available_services, memory_context)
        plan = self.optimizer.optimize_plan(plan, query)
        self.optimizer.cache_plan(query, service_ids, plan)
        return plan, {"provider": "mock", "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens": 0, "intent": intent}

    async def correct_plan(
        self,
        original_query: str,
        failed_plan: Dict[str, Any],
        error_message: str,
        available_services: List[Dict[str, Any]],
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """Ask the LLM to fix a plan that failed at the OData layer.
        Returns (corrected_plan, metadata). Falls back to None on any failure.
        """
        if self.provider == "openai" and settings.openai_api_key:
            t0 = time.perf_counter()
            try:
                plan, tokens = await self._correct_openai(original_query, failed_plan, error_message, available_services)
                return plan, {"provider": "openai", "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens": tokens}
            except Exception as e:
                logger.warning(f"OpenAI self-correction failed: {e}")
        elif self.provider == "openrouter" and settings.openrouter_api_key:
            t0 = time.perf_counter()
            try:
                plan, tokens = await self._correct_openrouter(original_query, failed_plan, error_message, available_services)
                return plan, {"provider": "openrouter", "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens": tokens}
            except Exception as e:
                logger.warning(f"OpenRouter self-correction failed: {e}")
        elif self.provider == "gemini" and settings.gemini_api_key:
            t0 = time.perf_counter()
            try:
                plan, tokens = await self._correct_gemini(original_query, failed_plan, error_message, available_services)
                return plan, {"provider": "gemini", "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens": tokens}
            except Exception as e:
                logger.warning(f"Gemini self-correction failed: {e}")
        elif self.provider == "nvidia" and settings.nvidia_api_key:
            t0 = time.perf_counter()
            try:
                plan, tokens = await self._correct_nvidia(original_query, failed_plan, error_message, available_services)
                return plan, {"provider": "nvidia", "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens": tokens}
            except Exception as e:
                logger.warning(f"NVIDIA self-correction failed: {e}")
        return None, {"provider": "none", "latency_ms": 0, "tokens": 0}

    def _plan_mock(
        self,
        query: str,
        services: List[Dict[str, Any]],
        memory_context: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        q = query.lower()
        intent = self._infer_intent(q)
        chosen_service = self._pick_service(services, q)
        entity_set, candidate_properties = self._pick_entity_set(services, chosen_service, q)

        # Extract metadata XML for column priority parsing
        metadata_xml = ""
        svc_data = next((s for s in services if s["id"] == chosen_service), None)
        if svc_data:
            metadata_xml = svc_data.get("metadata_xml", "")

        select, expand, filter_expr, orderby, top = self._build_query_parts(
            q, entity_set, candidate_properties,
            service_id=chosen_service or "", metadata_xml=metadata_xml,
        )
        steps = []
        if chosen_service and entity_set:
            steps.append({
                "service_id": chosen_service,
                "entity_set": entity_set,
                "select": select,
                "filter": filter_expr,
                "expand": expand,
                "top": top,
                "skip": 0,
                "orderby": orderby,
            })
        summary = self._summarize(query, steps)
        result = {
            "intent": intent,
            "target_services": [chosen_service] if chosen_service else [],
            "steps": steps,
            "summary": summary,
            "memory_used": memory_context or [],
        }

        # For write intents, add write_operation
        if intent in ("create", "update", "delete"):
            result["write_operation"] = {
                "operation": intent,
                "entity_set": entity_set or "",
                "service_id": chosen_service or "",
                "fields": {},
                "entity_id": None,
                "confirmed": False,
            }
        return result

    def _infer_intent(self, q: str) -> str:
        if any(w in q for w in ["create", "add", "new", "insert", "submit"]):
            return "create"
        if any(w in q for w in ["update", "modify", "change", "set", "edit", "replace"]):
            return "update"
        if any(w in q for w in ["delete", "remove", "destroy", "drop"]):
            return "delete"
        if any(w in q for w in ["how many", "count", "total", "which", "least", "fewest", "most", "highest", "lowest"]):
            return "aggregate"
        if any(w in q for w in ["with", "including", "and their", "along with"]):
            return "navigate"
        if any(w in q for w in ["show", "list", "get", "find", "fetch", "display", "give me"]):
            return "fetch"
        if any(w in q for w in ["summarize", "summary", "overview"]):
            return "summarize"
        return "unknown"

    def _pick_service(self, services: List[Dict[str, Any]], q: str) -> Optional[str]:
        if not services:
            return None
        q_lower = q.lower()
        # Generic tokens that appear in many service names/descriptions — skip for matching
        generic_tokens = {"odata", "service", "api", "data", "v4", "v2", "v3", "rest", "the", "and", "for", "srv", "local", "order", "manage", "test", "http", "https", "com", "ondemand", "eu10", "cfapps", "it", "cpi001", "rt", "soprasteriagroup"}
        # First pass: match by service name tokens (explicit mention)
        for svc in services:
            name_tokens = re.findall(r"[a-zA-Z]+", svc.get("name", "").lower())
            if any(t and len(t) > 2 and t not in generic_tokens and t in q_lower for t in name_tokens):
                return svc["id"]
        # Second pass: match by entity set name
        for svc in services:
            for es in svc.get("entity_sets", []):
                es_lower = es.lower().replace("_", " ")
                if es_lower in q_lower or es.lower() in q_lower:
                    return svc["id"]
        return services[0]["id"]

    def _pick_entity_set(self, services: List[Dict[str, Any]], service_id: Optional[str], q: str):
        svc = next((s for s in services if s["id"] == service_id), None)
        if not svc:
            return None, []
        qn = q.lower()
        available_entities = svc.get("entity_sets", [])
        if not available_entities:
            return None, []

        stop_words = {
            "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
            "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
            "has", "have", "had", "do", "does", "did", "will", "would", "could",
            "should", "may", "might", "can", "show", "me", "get", "list", "find",
            "all", "some", "first", "top", "last", "that", "this", "it", "its",
            "what", "which", "who", "whom", "how", "many", "much", "count",
            "total", "sum", "average", "max", "min", "where", "when", "if",
            "not", "no", "than", "then", "so", "very", "just", "also", "too",
        }
        # Also filter out words that appear in the service name (they identify the service, not the entity)
        svc = next((s for s in services if s["id"] == service_id), None)
        svc_name_words = set()
        if svc:
            svc_name_words = {w.lower() for w in re.findall(r'[a-zA-Z]+', svc.get("name", "")) if len(w) > 2}
        qn_words = set(re.findall(r'[a-z]+', qn)) - stop_words - svc_name_words

        def stem(word: str) -> str:
            """Light stemmer: strip common English suffixes."""
            w = word
            if w.endswith("ies") and len(w) >= 5:
                w = w[:-3] + "y"
            elif w.endswith("es") and len(w) >= 5:
                w = w[:-2]
            elif w.endswith("s") and len(w) >= 4:
                w = w[:-1]
            for suffix in ("ation", "tion", "ment", "ness", "ible", "able", "ous", "ive", "ing", "ful"):
                if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                    w = w[:-len(suffix)]
                    break
            return w

        def stem_set(words: set) -> set:
            """Stem a set of words."""
            return {stem(w) for w in words}

        def split_entity_words(name: str) -> set:
            """Split camelCase/PascalCase/underscore entity names into stemmed words."""
            s = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
            s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', s)
            s = s.replace("_", " ").replace(".", " ").replace("-", " ")
            return {stem(w) for w in re.findall(r'[a-z]{2,}', s.lower())}

        def score_entity(es_name: str, q_words: set) -> float:
            es_words = split_entity_words(es_name)
            if not es_words or not q_words:
                return 0.0
            q_stems = {stem(w) for w in q_words}
            overlap = q_stems & es_words
            if not overlap:
                return 0.0
            # Jaccard-like: prefer entities with more specific overlap
            union_size = len(q_stems | es_words)
            jaccard = len(overlap) / union_size if union_size else 0
            # Specificity: what fraction of entity words matched
            specificity = len(overlap) / len(es_words) if es_words else 0
            # Query stem length bonus: prefer matches on user's longest/most-specific word
            max_q_stem_len = max(len(s) for s in overlap)
            q_len_bonus = max_q_stem_len / 10.0
            # Penalty for overly-simple entity names (e.g. "OperationSet" with 1 word)
            complexity_penalty = 0.0
            if len(es_words) < 2:
                complexity_penalty = 0.6
            elif len(es_words) < 3:
                complexity_penalty = 0.2
            # Bonus for I_* entities (SAP CDS views — typically the queryable data entities)
            view_bonus = 0.1 if es_name.startswith("I_") else 0.0
            # Penalty for SAP Value Help entities (dropdown/metadata, not real data)
            vh_penalty = 0.0
            if re.search(r'(VH|StdVH|ValueHelp|Value_Help)$', es_name):
                vh_penalty = 0.4
            return jaccard + specificity * 0.5 + q_len_bonus - complexity_penalty + view_bonus - vh_penalty

        # Direct name match: if entity name appears in the query, prefer it immediately
        qn_lower = qn.lower()
        for es in available_entities:
            es_lower = es.lower().replace("_", " ")
            if es_lower in qn_lower or es.lower() in qn_lower:
                return es, []

        scored = [(es, score_entity(es, qn_words)) for es in available_entities]
        scored.sort(key=lambda x: -x[1])
        logger.info(f"Entity scoring for query '{qn[:60]}': top 5 = {[(es, round(sc, 3)) for es, sc in scored[:5]]}")

        if scored and scored[0][1] > 0:
            best_score = scored[0][1]
            # If tie, prefer entity with fewer words (more specific)
            tied = [es for es, sc in scored if abs(sc - best_score) < 0.01]
            if len(tied) > 1:
                best = min(tied, key=lambda es: len(split_entity_words(es)))
                return best, []
            return scored[0][0], []

        # Fallback: match entity name mentioned in query
        for es in available_entities:
            es_spaced = es.lower().replace("_", " ").replace(".", " ")
            if es_spaced in qn or es.lower() in qn:
                return es, []

        return available_entities[0], []

    def _pick_analytics_entity(self, svc: Dict[str, Any], qn: str):
        """Generic entity picker for analytics/sales queries.
        Searches entity set names and properties for sales-related keywords."""
        entity_sets = svc.get("entity_sets", [])
        entity_props = svc.get("entity_properties", {})

        sales_name_kws = {"sale", "sales", "invoice", "revenue", "order", "transaction", "deal", "payment", "financial"}
        amount_col_kws = {"amount", "price", "total", "revenue", "sales", "cost", "value", "sum", "extended", "sub"}
        location_col_kws = {"country", "region", "city", "state", "territory", "area", "zone", "location"}

        best_entity = None
        best_score = -1

        for es_name in entity_sets:
            score = 0
            es_lower = es_name.lower().replace("_", " ")

            for kw in sales_name_kws:
                if kw in es_lower:
                    score += 3
                    break

            props = entity_props.get(es_name, [])
            props_lower = [p.lower() for p in props]

            has_amount = any(any(ak in p for ak in amount_col_kws) for p in props_lower)
            has_location = any(any(lk in p for lk in location_col_kws) for p in props_lower)

            if has_amount:
                score += 2
            if has_location:
                score += 1

            if score > best_score:
                best_score = score
                best_entity = es_name

        if best_entity and best_score >= 3:
            return best_entity
        return None

    def _build_query_parts(self, q: str, entity_set: Optional[str], candidate_properties: List[str],
                           service_id: str = "", metadata_xml: str = ""):
        select: List[str] = []
        expand: List[str] = []
        filter_expr: Optional[str] = None
        orderby: Optional[str] = None
        top: Optional[int] = None

        m = re.search(r"\btop\s+(\d+)\b", q)
        if m:
            top = int(m.group(1))
        m = re.search(r"\bfirst\s+(\d+)\b", q)
        if m and top is None:
            top = int(m.group(1))
        if top is None and any(w in q.split() for w in ["all", "every"]):
            top = 100

        # Two-pass column selection using priority map
        if candidate_properties:
            try:
                from app.services.column_priority import get_top_columns, log_field_selection
                top_columns = get_top_columns(
                    entity_set_name=entity_set or "",
                    service_id=service_id,
                    all_fields=candidate_properties,
                    metadata_xml=metadata_xml,
                    max_columns=20,
                )
                if top_columns:
                    select = top_columns
                    log_field_selection(
                        entity_set=entity_set or "",
                        service_id=service_id,
                        query=q,
                        selected_fields=select,
                        total_fields=len(candidate_properties),
                    )
                else:
                    select = self._pick_smart_columns(q, candidate_properties)
            except Exception as e:
                logger.warning(f"Column priority failed, falling back to smart columns: {e}")
                select = self._pick_smart_columns(q, candidate_properties)

        explicit_filters: List[str] = []
        m = re.search(r"\bwhere\s+([\w'\".= ]+?)(?:\s+(?:and|order|by|with|including|limit|top)\b|$)", q)
        if m:
            explicit_filters.append(self._translate_filter(m.group(1).strip()))
        m = re.search(r"\bfrom\s+([A-Z][\w\s]+?)(?:\s+(?:with|and|order|by|where|top|limit|in)\b|$)", q)
        if m:
            country = m.group(1).strip()
            explicit_filters.append(f"Country eq '{country}'")
        m = re.search(r"\bin\s+(france|germany|uk|usa|mexico|spain|sweden|italy|canada|brazil|argentina|portugal|norway|finland|denmark|ireland|belgium|netherlands|austria|switzerland|poland|japan|china|india|australia)\b", q, re.IGNORECASE)
        if m:
            country = m.group(1).title()
            explicit_filters.append(f"Country eq '{country}'")
        if re.search(r"\bshipped\b", q):
            explicit_filters.append("ShippedDate ne null")
        if re.search(r"\bunshipped\b|\bnot\s+shipped\b", q):
            explicit_filters.append("ShippedDate eq null")
        m = re.search(r"(?:price|amount|total)\s*(>|>=|<|<=)\s*(\d+(?:\.\d+)?)", q)
        if m:
            explicit_filters.append(f"UnitPrice {m.group(1)} {m.group(2)}")
        if explicit_filters:
            filter_expr = " and ".join(explicit_filters)

        m = re.search(r"\border\s+by\s+([\w]+)(?:\s+(asc|desc))?\b", q)
        if m:
            orderby = f"{m.group(1)} {m.group(2) or 'asc'}"
        if not orderby and entity_set in ("Products", "Order_Details", "Order_Details_Extendeds", "Invoices"):
            if any(w in q for w in ["expensive", "highest", "most", "priciest"]):
                orderby = "UnitPrice desc"
            elif any(w in q for w in ["cheapest", "lowest"]):
                orderby = "UnitPrice asc"
        if not orderby and entity_set == "Orders" and any(w in q for w in ["recent", "latest", "newest"]):
            orderby = "OrderDate desc"
        if not orderby and entity_set == "Orders" and any(w in q for w in ["oldest"]):
            orderby = "OrderDate asc"

        valid_expands_for_set = {
            "Customers": ["Orders"],
            "Orders": ["Customer", "Employee", "Order_Details", "Shipper"],
            "Products": ["Category", "Order_Details", "Supplier"],
            "Categories": ["Products"],
            "Suppliers": ["Products"],
            "Shippers": ["Orders"],
            "Employees": ["Orders", "Territories"],
            "Regions": ["Territories"],
            "Territories": ["Region", "Employees"],
        }
        if entity_set and entity_set in valid_expands_for_set:
            allowed = set(valid_expands_for_set[entity_set])
            if entity_set == "Customers" and any(k in q for k in ["with orders", "with their orders", "and orders", "their orders"]):
                expand.append("Orders")
            elif entity_set == "Orders" and any(k in q for k in ["with customer", "with their customer", "and customer"]):
                expand.append("Customer")
            elif entity_set == "Orders" and any(k in q for k in ["with products", "with items", "with details"]):
                expand.append("Order_Details")
            elif entity_set == "Products" and "supplier" in q:
                expand.append("Supplier")
            elif entity_set == "Products" and "category" in q:
                expand.append("Category")
            elif entity_set == "Categories" and "products" in q:
                expand.append("Products")
            elif entity_set == "Suppliers" and "products" in q:
                expand.append("Products")
            expand = [e for e in expand if e in allowed]

        return select, list(dict.fromkeys(expand)), filter_expr, orderby, top

    def _translate_filter(self, raw: str) -> str:
        m = re.match(r"([\w]+)\s*=\s*'([^']*)'", raw)
        if m:
            return f"{m.group(1)} eq '{m.group(2)}'"
        m = re.match(r"([\w]+)\s*=\s*([\w\d\.\-]+)", raw)
        if m:
            v = m.group(2)
            if v.replace(".", "").replace("-", "").isdigit():
                return f"{m.group(1)} eq {v}"
            return f"{m.group(1)} eq '{v}'"
        m = re.match(r"([\w]+)\s+contains\s+'([^']*)'", raw)
        if m:
            return f"contains({m.group(1)},'{m.group(2)}')"
        return raw

    # Columns that are always useful (keep these)
    _KEEP_PATTERNS = [
        "Material", "MaterialType", "Material_Text", "MaterialGroup",
        "MaterialBaseUnit", "MaterialGrossWeight", "MaterialNetWeight",
        "ManufacturingOrder", "MfgOrder", "ProductionPlant",
        "OrderIs", "OrderOpen", "OrderStart", "OrderDelivered",
        "Customer", "Supplier", "Product", "Category", "Price",
        "Name", "Description", "Status", "Date", "Quantity",
        "Country", "City", "Region",
    ]

    # Columns that are never useful (skip these)
    _SKIP_PATTERNS = [
        "InternalNumber", "CharcInternal", "ConfigurableProd",
        "CrossPlantConfigurable", "ProdCharc", "Signature",
        "PDFStandard", "CoverPage", "FormatSet", "TableColumn",
        "MyDocument", "ValueHelp",
    ]

    def _pick_smart_columns(self, q: str, candidate_properties: List[str]) -> List[str]:
        """Pick relevant columns from candidate_properties, keeping most of them."""
        if not candidate_properties or len(candidate_properties) <= 20:
            return candidate_properties

        scored = []
        q_words = set(re.findall(r'[a-z]+', q.lower()))
        for col in candidate_properties:
            score = 0
            col_lower = col.lower()
            for pat in self._KEEP_PATTERNS:
                if pat.lower() in col_lower:
                    score += 3
            for pat in self._SKIP_PATTERNS:
                if pat.lower() in col_lower:
                    score -= 10
            for word in q_words:
                if word in col_lower and len(word) > 2:
                    score += 2
            if len(col) < 25:
                score += 1
            scored.append((col, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        keep = [col for col, sc in scored if sc > -5]
        if len(keep) < 10:
            keep = [col for col, _ in scored][:15]
        return keep

    def _summarize(self, query: str, steps: List[Dict[str, Any]]) -> str:
        if not steps:
            return f"I could not identify a target OData service for: '{query}'"
        s = steps[0]
        parts = [f"Query the {s['entity_set']} entity set"]
        if s.get("filter"):
            parts.append(f"filtered by {s['filter']}")
        if s.get("expand"):
            parts.append(f"with related {', '.join(s['expand'])}")
        if s.get("top"):
            parts.append(f"limited to {s['top']} rows")
        return ", ".join(parts) + "."

    async def _plan_openai(
        self,
        query: str,
        services: List[Dict[str, Any]],
        memory_context: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Dict[str, Any], int]:
        from openai import AsyncOpenAI

        mock_plan = self._plan_mock(query, services, memory_context)
        suggestions = []
        for step in mock_plan.get("steps", []):
            suggestions.append({
                "service_id": step.get("service_id"),
                "entity_set": step.get("entity_set"),
            })
        logger.info(f"Mock suggestions for LLM: {suggestions}")

        system_prompt = (
            "OData planner. Output JSON: intent, target_services, steps (service_id, entity_set, select, filter, top, skip, orderby), summary. "
            "Use ONLY provided entity sets and properties. No navigation properties in $filter. "
            "Use entity_suggestions — they are pre-scored. Only deviate if clearly wrong. "
            "AVOID entities ending in VH, StdVH, ValueHelp — these are SAP dropdown metadata, not real data. "
            "If similar_past_queries are provided, use them as reference for correct entity/filter patterns. "
            "For 'top N X by Y count/total' queries: create 2 steps — one per entity needed. The backend joins them in Python. "
            "Example: 'top 5 customers by order count' → step1: Customers (top=200), step2: Orders (top=200). "
            "OData does NOT support JOINs/GROUP BY — backend does aggregation in Python. "
            "For prediction queries: set intent='predict', add prediction object (entity_key, features, target). No steps. "
            "For write queries (create/update/delete): set intent='create'/'update'/'delete', add write_operation object with "
            "operation, service_id, entity_set, fields (key=value pairs), entity_id (for update/delete), required_fields (list), confirmed (bool). "
            "For create: include all required fields. For update/delete: include entity_id. "
            "IMPORTANT for $select: Include ALL meaningful columns from the entity. Only skip columns ending in InternalNumber, CharcInternal, or starting with __. "
            "Do NOT limit to 5-8 columns — the user needs to see all available business data. The post-filter will hide truly useless columns. "
            "Use friendly labels from entity_labels in summaries (e.g., 'Purchase Order' instead of 'A_PurchaseOrder'). "
            "If entity_labels provided, use the label as display name and technical name for API calls."
        )

        suggested_services = set(s["service_id"] for s in suggestions if s.get("service_id"))
        filtered_services = []
        for s in services:
            truncated = self._truncate_service_for_llm(s)
            if s["id"] in suggested_services:
                filtered_services.append(truncated)
            elif len(s.get("entity_sets", [])) <= 10:
                filtered_services.append(self._truncate_service_for_llm(s))

        # Retrieve similar past plans as few-shot examples (RAG)
        rag_examples = []
        for svc_id in suggested_services:
            examples = query_plan_rag.retrieve_plans(query, service_id=svc_id, n_results=2)
            rag_examples.extend(examples)

        user_prompt_data = {
            "query": query,
            "services": filtered_services,
            "entity_suggestions": suggestions,
        }
        if rag_examples:
            user_prompt_data["similar_past_queries"] = [
                {"query": ex["query"], "plan": ex["plan"]} for ex in rag_examples[:3]
            ]

        user_prompt = json.dumps(user_prompt_data)

        keys = settings.openai_api_keys_list
        last_error = None
        for attempt in range(min(len(keys), 3)):
            api_key = keys[(self._key_index + attempt) % len(keys)] if keys else settings.openai_api_key
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=settings.openai_base_url or None,
                timeout=30.0,
            )
            try:
                resp = await client.chat.completions.create(
                    model=self.model or settings.llm_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                )
                content = resp.choices[0].message.content
                tokens = 0
                try:
                    if hasattr(resp, "usage") and resp.usage:
                        tokens = getattr(resp.usage, "total_tokens", 0) or 0
                except Exception:
                    tokens = 0
                self._key_index = (self._key_index + attempt) % len(keys) if keys else 0
                return json.loads(content), tokens
            except Exception as e:
                last_error = e
                if "429" in str(e) or "rate_limit" in str(e):
                    logger.warning(f"Rate limit on key index {(self._key_index + attempt) % len(keys)}, rotating...")
                    continue
                raise

        raise last_error or Exception("All API keys exhausted")

    async def _plan_openrouter(
        self,
        query: str,
        services: List[Dict[str, Any]],
        memory_context: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Dict[str, Any], int]:
        from openai import AsyncOpenAI

        mock_plan = self._plan_mock(query, services, memory_context)
        suggestions = []
        for step in mock_plan.get("steps", []):
            suggestions.append({
                "service_id": step.get("service_id"),
                "entity_set": step.get("entity_set"),
            })

        system_prompt = (
            "OData planner. Output JSON: intent, target_services, steps (service_id, entity_set, select, filter, top, skip, orderby), summary. "
            "Use ONLY provided entity sets and properties. No navigation properties in $filter. "
            "Use entity_suggestions — they are pre-scored. Only deviate if clearly wrong. "
            "AVOID entities ending in VH, StdVH, ValueHelp — these are SAP dropdown metadata, not real data. "
            "If similar_past_queries are provided, use them as reference for correct entity/filter patterns. "
            "For 'top N X by Y count/total' queries: create 2 steps — one per entity needed. The backend joins them in Python. "
            "Example: 'top 5 customers by order count' → step1: Customers (top=200), step2: Orders (top=200). "
            "OData does NOT support JOINs/GROUP BY — backend does aggregation in Python. "
            "For prediction queries: set intent='predict', add prediction object (entity_key, features, target). No steps. "
            "IMPORTANT for $select: Include ALL meaningful columns from the entity. Only skip columns ending in InternalNumber, CharcInternal, or starting with __. "
            "Do NOT limit to 5-8 columns — the user needs to see all available business data. The post-filter will hide truly useless columns."
        )

        suggested_services = set(s["service_id"] for s in suggestions if s.get("service_id"))
        filtered_services = []
        for s in services:
            truncated = self._truncate_service_for_llm(s)
            if s["id"] in suggested_services:
                filtered_services.append(truncated)
            elif len(s.get("entity_sets", [])) <= 10:
                filtered_services.append(self._truncate_service_for_llm(s))

        user_prompt = json.dumps({
            "query": query,
            "services": filtered_services,
            "entity_suggestions": suggestions,
        })

        client = AsyncOpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            timeout=30.0,
        )
        model = self.model or settings.openrouter_model
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=2048,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        tokens = 0
        try:
            if hasattr(resp, "usage") and resp.usage:
                tokens = getattr(resp.usage, "total_tokens", 0) or 0
        except Exception:
            tokens = 0
        return json.loads(content), tokens

    async def _correct_openrouter(
        self,
        original_query: str,
        failed_plan: Dict[str, Any],
        error_message: str,
        services: List[Dict[str, Any]],
    ) -> Tuple[Optional[Dict[str, Any]], int]:
        from openai import AsyncOpenAI

        system_prompt = (
            "You are an OData query fixer. The previous plan failed at the OData layer. "
            "Diagnose the error and produce a corrected JSON plan. "
            "Rules: do NOT use navigation properties in $filter (use the FK field); "
            "use only valid OData v4 operators (eq, ne, gt, lt, ge, le, and, or, not, contains, startswith); "
            "use only entity sets and properties that exist in the listed services."
        )
        user_prompt = json.dumps({
            "original_query": original_query,
            "failed_plan": failed_plan,
            "error": error_message,
            "services": [self._truncate_service_for_llm(s) for s in services],
        })

        client = AsyncOpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            timeout=30.0,
        )
        model = self.model or settings.openrouter_model
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=2048,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        tokens = 0
        try:
            if hasattr(resp, "usage") and resp.usage:
                tokens = getattr(resp.usage, "total_tokens", 0) or 0
        except Exception:
            tokens = 0
        return json.loads(content), tokens

    async def _plan_nvidia(
        self,
        query: str,
        services: List[Dict[str, Any]],
        memory_context: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Dict[str, Any], int]:
        from openai import AsyncOpenAI

        mock_plan = self._plan_mock(query, services, memory_context)
        suggestions = []
        for step in mock_plan.get("steps", []):
            suggestions.append({
                "service_id": step.get("service_id"),
                "entity_set": step.get("entity_set"),
            })
        logger.info(f"Mock suggestions for NVIDIA LLM: {suggestions}")

        system_prompt = (
            "OData planner. Output JSON: intent, target_services, steps (service_id, entity_set, select, filter, top, skip, orderby), summary. "
            "Use ONLY provided entity sets and properties. No navigation properties in $filter. "
            "Use entity_suggestions — they are pre-scored. Only deviate if clearly wrong. "
            "AVOID entities ending in VH, StdVH, ValueHelp — these are SAP dropdown metadata, not real data. "
            "If similar_past_queries are provided, use them as reference for correct entity/filter patterns. "
            "For 'top N X by Y count/total' queries: create 2 steps — one per entity needed. The backend joins them in Python. "
            "Example: 'top 5 customers by order count' → step1: Customers (top=200), step2: Orders (top=200). "
            "OData does NOT support JOINs/GROUP BY — backend does aggregation in Python. "
            "For prediction queries: set intent='predict', add prediction object (entity_key, features, target). No steps. "
            "IMPORTANT for $select: Include ALL meaningful columns from the entity. Only skip columns ending in InternalNumber, CharcInternal, or starting with __. "
            "Do NOT limit to 5-8 columns — the user needs to see all available business data. The post-filter will hide truly useless columns."
        )

        suggested_services = set(s["service_id"] for s in suggestions if s.get("service_id"))
        filtered_services = []
        for s in services:
            if s["id"] in suggested_services:
                filtered_services.append(self._truncate_service_for_llm(s, max_entities=10, max_props_per_entity=5))

        user_prompt = json.dumps({
            "query": query,
            "services": filtered_services,
            "entity_suggestions": suggestions,
        })

        client = AsyncOpenAI(
            api_key=settings.nvidia_api_key,
            base_url=settings.nvidia_base_url,
            timeout=30.0,
        )
        model = self.model or settings.nvidia_model
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.6,
            top_p=0.95,
            max_tokens=512,
        )
        content = resp.choices[0].message.content
        tokens = 0
        try:
            if hasattr(resp, "usage") and resp.usage:
                tokens = getattr(resp.usage, "total_tokens", 0) or 0
        except Exception:
            tokens = 0
        return json.loads(content), tokens

    async def _correct_nvidia(
        self,
        original_query: str,
        failed_plan: Dict[str, Any],
        error_message: str,
        services: List[Dict[str, Any]],
    ) -> Tuple[Optional[Dict[str, Any]], int]:
        from openai import AsyncOpenAI

        system_prompt = (
            "You are an OData query fixer. The previous plan failed at the OData layer. "
            "Diagnose the error and produce a corrected JSON plan. "
            "Rules: do NOT use navigation properties in $filter (use the FK field); "
            "use only valid OData v4 operators (eq, ne, gt, lt, ge, le, and, or, not, contains, startswith); "
            "use only entity sets and properties that exist in the listed services."
        )
        user_prompt = json.dumps({
            "original_query": original_query,
            "failed_plan": failed_plan,
            "error": error_message,
            "services": [self._truncate_service_for_llm(s, max_entities=10, max_props_per_entity=5) for s in services],
        })

        client = AsyncOpenAI(
            api_key=settings.nvidia_api_key,
            base_url=settings.nvidia_base_url,
            timeout=30.0,
        )
        model = self.model or settings.nvidia_model
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.6,
            top_p=0.95,
            max_tokens=512,
        )
        content = resp.choices[0].message.content
        tokens = 0
        try:
            if hasattr(resp, "usage") and resp.usage:
                tokens = getattr(resp.usage, "total_tokens", 0) or 0
        except Exception:
            tokens = 0
        return json.loads(content), tokens

    async def _plan_gemini(
        self,
        query: str,
        services: List[Dict[str, Any]],
        memory_context: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Dict[str, Any], int]:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)
        model = self.model or settings.llm_model or "gemini-2.0-flash"

        mock_plan = self._plan_mock(query, services, memory_context)
        suggestions = []
        for step in mock_plan.get("steps", []):
            suggestions.append({
                "service_id": step.get("service_id"),
                "entity_set": step.get("entity_set"),
            })

        system_prompt = (
            "OData planner. Output JSON: intent, target_services, steps (service_id, entity_set, select, filter, top, skip, orderby), summary. "
            "Use ONLY provided entity sets and properties. No navigation properties in $filter. "
            "Use entity_suggestions — they are pre-scored. Only deviate if clearly wrong. "
            "AVOID entities ending in VH, StdVH, ValueHelp — these are SAP dropdown metadata, not real data. "
            "If similar_past_queries are provided, use them as reference for correct entity/filter patterns. "
            "For 'top N X by Y count/total' queries: create 2 steps — one per entity needed. The backend joins them in Python. "
            "Example: 'top 5 customers by order count' → step1: Customers (top=200), step2: Orders (top=200). "
            "OData does NOT support JOINs/GROUP BY — backend does aggregation in Python. "
            "For prediction queries: set intent='predict', add prediction object (entity_key, features, target). No steps. "
            "IMPORTANT for $select: Include ALL meaningful columns from the entity. Only skip columns ending in InternalNumber, CharcInternal, or starting with __. "
            "Do NOT limit to 5-8 columns — the user needs to see all available business data. The post-filter will hide truly useless columns."
        )

        suggested_services = set(s["service_id"] for s in suggestions if s.get("service_id"))
        filtered_services = []
        for s in services:
            if s["id"] in suggested_services:
                filtered_services.append(self._truncate_service_for_llm(s))

        user_prompt = json.dumps({
            "query": query,
            "services": filtered_services,
            "entity_suggestions": suggestions,
        })
        resp = await client.aio.models.generate_content(
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
            ),
        )
        content = resp.text or ""
        tokens = 0
        try:
            if hasattr(resp, "usage_metadata") and resp.usage_metadata:
                tokens = getattr(resp.usage_metadata, "total_token_count", 0) or 0
        except Exception:
            tokens = 0
        try:
            return json.loads(content), tokens
        except Exception:
            return self._plan_mock(query, services, memory_context), tokens

    async def _correct_openai(
        self,
        original_query: str,
        failed_plan: Dict[str, Any],
        error_message: str,
        services: List[Dict[str, Any]],
    ) -> Tuple[Optional[Dict[str, Any]], int]:
        from openai import AsyncOpenAI

        system_prompt = (
            "You are an OData query fixer. The previous plan failed at the OData layer. "
            "Diagnose the error and produce a corrected JSON plan. "
            "Rules: do NOT use navigation properties in $filter (use the FK field); "
            "use only valid OData v4 operators (eq, ne, gt, lt, ge, le, and, or, not, contains, startswith); "
            "use only entity sets and properties that exist in the listed services."
        )
        user_prompt = json.dumps({
            "original_query": original_query,
            "failed_plan": failed_plan,
            "error": error_message,
            "services": [self._truncate_service_for_llm(s) for s in services],
        })

        keys = settings.openai_api_keys_list
        last_error = None
        for attempt in range(min(len(keys), 3)):
            api_key = keys[(self._key_index + attempt) % len(keys)] if keys else settings.openai_api_key
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=settings.openai_base_url or None,
                timeout=30.0,
            )
            try:
                resp = await client.chat.completions.create(
                    model=self.model or settings.llm_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                )
                content = resp.choices[0].message.content
                tokens = 0
                try:
                    if hasattr(resp, "usage") and resp.usage:
                        tokens = getattr(resp.usage, "total_tokens", 0) or 0
                except Exception:
                    tokens = 0
                self._key_index = (self._key_index + attempt) % len(keys) if keys else 0
                return json.loads(content), tokens
            except Exception as e:
                last_error = e
                if "429" in str(e) or "rate_limit" in str(e):
                    logger.warning(f"Rate limit on correction key index {(self._key_index + attempt) % len(keys)}, rotating...")
                    continue
                raise

        return None, 0

    async def _correct_gemini(
        self,
        original_query: str,
        failed_plan: Dict[str, Any],
        error_message: str,
        services: List[Dict[str, Any]],
    ) -> Tuple[Optional[Dict[str, Any]], int]:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)
        model = self.model or settings.llm_model or "gemini-2.0-flash"
        system_prompt = (
            "You are an OData query fixer. The previous plan failed at the OData layer. "
            "Diagnose the error and produce a corrected JSON plan. "
            "Rules: do NOT use navigation properties in $filter (use the FK field); "
            "use only valid OData v4 operators (eq, ne, gt, lt, ge, le, and, or, not, contains, startswith); "
            "use only entity sets and properties that exist in the listed services."
        )
        user_prompt = json.dumps({
            "original_query": original_query,
            "failed_plan": failed_plan,
            "error": error_message,
            "services": [self._truncate_service_for_llm(s) for s in services],
        })
        resp = await client.aio.models.generate_content(
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
            ),
        )
        content = resp.text or ""
        tokens = 0
        try:
            if hasattr(resp, "usage_metadata") and resp.usage_metadata:
                tokens = getattr(resp.usage_metadata, "total_token_count", 0) or 0
        except Exception:
            tokens = 0
        try:
            return json.loads(content), tokens
        except Exception:
            return None, tokens

    async def generate(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> Dict[str, Any]:
        """Generic chat completion for any message list."""
        system_prompt = ""
        user_prompt = ""
        for m in messages:
            if m["role"] == "system":
                system_prompt = m["content"]
            elif m["role"] == "user":
                user_prompt = m["content"]

        if self.provider == "openai" and settings.openai_api_key:
            try:
                return await self._generate_openai(system_prompt, user_prompt, temperature, max_tokens)
            except Exception as e:
                logger.warning(f"OpenAI generate failed: {e}")
        elif self.provider == "gemini" and settings.gemini_api_key:
            try:
                return await self._generate_gemini(system_prompt, user_prompt, temperature, max_tokens)
            except Exception as e:
                logger.warning(f"Gemini generate failed: {e}")
        return {"content": f"[Mock LLM] {user_prompt[:200]}", "provider": "mock", "tokens": 0}

    async def _generate_openai(self, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int) -> Dict[str, Any]:
        import httpx
        headers = {"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"}
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        base_url = (settings.openai_base_url or "https://api.openai.com/v1").rstrip("/")
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{base_url}/chat/completions", headers=headers, json=body)
            if resp.status_code != 200:
                logger.error(f"Groq API error {resp.status_code}: {resp.text[:500]}")
                resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            tokens = data.get("usage", {}).get("total_tokens", 0)
            return {"content": content, "provider": "groq", "tokens": tokens}

    async def _generate_gemini(self, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int) -> Dict[str, Any]:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=settings.gemini_api_key)
        model = self.model if self.model != "mock" else "gemini-flash-latest"
        resp = client.models.generate_content(
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        content = resp.text or ""
        tokens = 0
        try:
            if hasattr(resp, "usage_metadata") and resp.usage_metadata:
                tokens = getattr(resp.usage_metadata, "total_token_count", 0) or 0
        except Exception:
            tokens = 0
        return {"content": content, "provider": "gemini", "tokens": tokens}


llm_engine = LLMReasoningEngine()
