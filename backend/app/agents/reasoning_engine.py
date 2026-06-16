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


class LLMReasoningEngine:
    def __init__(self):
        self.provider = settings.llm_provider
        self.model = settings.llm_model
        self._lock = None

    def set_config(self, provider: Optional[str] = None, model: Optional[str] = None) -> None:
        """Update the active LLM provider/model at runtime.

        Both arguments are optional; pass only the one(s) you want to change.
        """
        if provider is not None:
            self.provider = provider
        if model is not None:
            self.model = model
        logger.info(f"LLM config updated: provider={self.provider}, model={self.model}")

    def get_config(self) -> Dict[str, Any]:
        return {"provider": self.provider, "model": self.model}

    async def plan(
        self,
        query: str,
        available_services: List[Dict[str, Any]],
        memory_context: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if self.provider == "openai" and settings.openai_api_key:
            t0 = time.perf_counter()
            try:
                plan, tokens = await self._plan_openai(query, available_services, memory_context)
                return plan, {"provider": "openai", "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens": tokens}
            except Exception as e:
                logger.warning(f"OpenAI planning failed, falling back to mock: {e}")
        elif self.provider == "gemini" and settings.gemini_api_key:
            t0 = time.perf_counter()
            try:
                plan, tokens = await self._plan_gemini(query, available_services, memory_context)
                return plan, {"provider": "gemini", "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens": tokens}
            except Exception as e:
                logger.warning(f"Gemini planning failed, falling back to mock: {e}")
        t0 = time.perf_counter()
        plan = self._plan_mock(query, available_services, memory_context)
        return plan, {"provider": "mock", "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens": 0}

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
        elif self.provider == "gemini" and settings.gemini_api_key:
            t0 = time.perf_counter()
            try:
                plan, tokens = await self._correct_gemini(original_query, failed_plan, error_message, available_services)
                return plan, {"provider": "gemini", "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens": tokens}
            except Exception as e:
                logger.warning(f"Gemini self-correction failed: {e}")
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
        select, expand, filter_expr, orderby, top = self._build_query_parts(
            q, entity_set, candidate_properties
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
        return {
            "intent": intent,
            "target_services": [chosen_service] if chosen_service else [],
            "steps": steps,
            "summary": summary,
            "memory_used": memory_context or [],
        }

    def _infer_intent(self, q: str) -> str:
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
        for svc in services:
            tokens = re.findall(r"[a-zA-Z]+", (svc.get("name", "") + " " + svc.get("description", "")).lower())
            if any(t and t in q for t in tokens):
                return svc["id"]
        for svc in services:
            for es in svc.get("entity_sets", []):
                if es.lower() in q:
                    return svc["id"]
        return services[0]["id"]

    def _pick_entity_set(self, services: List[Dict[str, Any]], service_id: Optional[str], q: str):
        svc = next((s for s in services if s["id"] == service_id), None)
        if not svc:
            return None, []
        qn = q.lower()
        for es in svc.get("entity_sets", []):
            if es.lower() in qn:
                return es, []
        synonyms = {
            "customer": ["Customers", "Customer_and_Suppliers_by_Cities"],
            "customers": ["Customers", "Customer_and_Suppliers_by_Cities"],
            "client": ["Customers", "Customer_and_Suppliers_by_Cities"],
            "order": ["Orders", "Invoices", "Order_Subtotals", "Order_Details_Extendeds"],
            "orders": ["Orders", "Invoices", "Order_Subtotals", "Order_Details_Extendeds"],
            "purchase": ["Orders", "Invoices"],
            "purchases": ["Orders", "Invoices"],
            "product": ["Products", "Alphabetical_list_of_products", "Products_by_Categories", "Products_Above_Average_Prices"],
            "products": ["Products", "Alphabetical_list_of_products", "Products_by_Categories", "Products_Above_Average_Prices"],
            "item": ["Products", "Order_Details"],
            "items": ["Products", "Order_Details"],
            "category": ["Categories", "Products_by_Categories", "Category_Sales_for_1997"],
            "categories": ["Categories", "Products_by_Categories", "Category_Sales_for_1997"],
            "supplier": ["Suppliers", "Customer_and_Suppliers_by_Cities"],
            "suppliers": ["Suppliers", "Customer_and_Suppliers_by_Cities"],
            "vendor": ["Suppliers", "Customer_and_Suppliers_by_Cities"],
            "employee": ["Employees"],
            "employees": ["Employees"],
            "staff": ["Employees"],
            "shipper": ["Shippers"],
            "shippers": ["Shippers"],
            "region": ["Regions"],
            "regions": ["Regions"],
            "territory": ["Territories"],
            "territories": ["Territories"],
            "invoice": ["Invoices", "Orders"],
            "invoices": ["Invoices", "Orders"],
            "line item": ["Order_Details", "Order_Details_Extendeds"],
            "line items": ["Order_Details", "Order_Details_Extendeds"],
        }
        for token, candidates in synonyms.items():
            if token in qn:
                for c in candidates:
                    if c in svc.get("entity_sets", []):
                        return c, []
        if "how many" in qn or "count" in qn:
            for c in ("Customers", "Orders", "Products", "Suppliers", "Employees"):
                if c in svc.get("entity_sets", []):
                    return c, []
        for es in svc.get("entity_sets", []):
            es_l = es.lower().replace("_", " ")
            if es_l in qn or es_l.replace(" ", "") in qn.replace(" ", ""):
                return es, []
        return svc.get("entity_sets", [None])[0], []

    def _build_query_parts(self, q: str, entity_set: Optional[str], candidate_properties: List[str]):
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
        if entity_set in ("Products", "Alphabetical_list_of_products"):
            category_map = {
                "beverages": 1,
                "condiments": 2,
                "confections": 3,
                "dairy products": 4,
                "grains/cereals": 5,
                "meat/poultry": 6,
                "produce": 7,
                "seafood": 8,
            }
            for kw, cat_id in category_map.items():
                if re.search(rf"\b{re.escape(kw)}\b", q):
                    explicit_filters.append(f"CategoryID eq {cat_id}")
                    break
        if entity_set == "Products_by_Categories":
            category_names = {
                "beverages": "Beverages",
                "condiments": "Condiments",
                "confections": "Confections",
                "dairy products": "Dairy Products",
                "grains/cereals": "Grains/Cereals",
                "meat/poultry": "Meat/Poultry",
                "produce": "Produce",
                "seafood": "Seafood",
            }
            for kw, cat in category_names.items():
                if re.search(rf"\b{re.escape(kw)}\b", q):
                    explicit_filters.append(f"CategoryName eq '{cat}'")
                    break
        if entity_set in ("Orders", "Invoices"):
            if re.search(r"\bshipped\b", q):
                explicit_filters.append("ShippedDate ne null")
            if re.search(r"\bunshipped\b|\bnot\s+shipped\b", q):
                explicit_filters.append("ShippedDate eq null")
        m = re.search(r"(?:price|amount|total)\s*(>|>=|<|<=)\s*(\d+(?:\.\d+)?)", q)
        if m and entity_set in ("Products", "Order_Details", "Order_Details_Extendeds"):
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

        client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )
        system_prompt = (
            "You are an OData query planner. Given a natural language question "
            "and a list of available OData services with their entity sets and properties, "
            "output JSON with keys: "
            "intent, target_services, steps (each with service_id, entity_set, select, filter, expand, orderby, top, skip), "
            "and summary. Use only services, entity sets, and properties provided. "
            "IMPORTANT: Each entity_set includes a 'properties' list - use ONLY those property names in $select and $filter. "
            "Do NOT guess or invent property names. "
            "Do NOT use navigation properties (e.g. 'Category/Name') in $filter. "
            "Use the foreign key field directly (e.g. 'CategoryID eq 1'). "
            "PREDICTION: If the user asks 'what will X be if Y is Z' or 'predict X given Y', "
            "set intent to 'predict' and add a 'prediction' object to the plan with: "
            "entity_key (service_entity), features (dict of known values), target (the column to predict). "
            "Do NOT create steps for prediction queries - only use the prediction object."
        )
        user_prompt = json.dumps({
            "query": query,
            "services": [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "entity_sets": s.get("entity_sets", []),
                    "entity_properties": s.get("entity_properties", {}),
                    "description": s.get("description", ""),
                }
                for s in services
            ],
            "memory_context": memory_context or [],
        })
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
        try:
            return json.loads(content), tokens
        except Exception:
            return self._plan_mock(query, services, memory_context), tokens

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
        system_prompt = (
            "You are an OData query planner. Given a natural language question "
            "and a list of available OData services with their entity sets and properties, "
            "output JSON with keys: "
            "intent, target_services, steps (each with service_id, entity_set, select, filter, expand, orderby, top, skip), "
            "and summary. Use only services, entity sets, and properties provided. "
            "IMPORTANT: Each entity_set includes a 'properties' list - use ONLY those property names in $select and $filter. "
            "Do NOT guess or invent property names. "
            "Do NOT use navigation properties (e.g. 'Category/Name') in $filter. "
            "Use the foreign key field directly (e.g. 'CategoryID eq 1'). "
            "PREDICTION: If the user asks 'what will X be if Y is Z' or 'predict X given Y', "
            "set intent to 'predict' and add a 'prediction' object to the plan with: "
            "entity_key (service_entity), features (dict of known values), target (the column to predict). "
            "Do NOT create steps for prediction queries - only use the prediction object."
        )
        user_prompt = json.dumps({
            "query": query,
            "services": [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "entity_sets": s.get("entity_sets", []),
                    "entity_properties": s.get("entity_properties", {}),
                    "description": s.get("description", ""),
                }
                for s in services
            ],
            "memory_context": memory_context or [],
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

        client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )
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
            "services": [
                {"id": s["id"], "name": s["name"], "entity_sets": s.get("entity_sets", []), "entity_properties": s.get("entity_properties", {})}
                for s in services
            ],
        })
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
        try:
            return json.loads(content), tokens
        except Exception:
            return None, tokens

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
            "services": [
                {"id": s["id"], "name": s["name"], "entity_sets": s.get("entity_sets", []), "entity_properties": s.get("entity_properties", {})}
                for s in services
            ],
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
