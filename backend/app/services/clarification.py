import re
from typing import Any, Dict, List, Optional


def is_generic_scope_query(query: str) -> bool:
    q_lower = query.lower().strip()
    if not q_lower:
        return True

    generic_terms = {
        "data", "details", "info", "information", "records", "record", "report", "reports",
        "status", "items", "item", "things", "thing", "something", "anything",
        "help", "stuff",
    }
    generic_verbs = {"show", "list", "get", "find", "fetch", "display"}
    tokens = re.findall(r"[a-z0-9]+", q_lower)
    if len(tokens) <= 1:
        return True
    if len(tokens) == 2 and generic_terms.intersection(tokens):
        return True
    if len(tokens) == 2 and tokens[0] in generic_verbs and tokens[1] in generic_terms:
        return True
    if generic_terms.intersection(tokens):
        return True
    return False


def extract_pending_clarification(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        result = message.get("result") or {}
        clarification = result.get("clarification")
        if isinstance(clarification, dict) and clarification.get("type"):
            return clarification
        break
    return None


def normalize_choice_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def match_clarification_option(answer: str, options: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    answer_clean = answer.strip().lower()
    if not answer_clean:
        return None

    numeric_map = {
        "1": 0, "first": 0, "one": 0,
        "2": 1, "second": 1, "two": 1,
        "3": 2, "third": 2, "three": 2,
        "4": 3, "fourth": 3, "four": 3,
        "5": 4, "fifth": 4, "five": 4,
    }
    if answer_clean in numeric_map:
        idx = numeric_map[answer_clean]
        if 0 <= idx < len(options):
            return options[idx]

    answer_compact = normalize_choice_text(answer_clean)
    for option in options:
        probes = [
            option.get("label", ""),
            option.get("value", ""),
            option.get("entity_set", ""),
            option.get("service_name", ""),
            option.get("service_id", ""),
            option.get("entity_label", ""),
        ]
        if any(normalize_choice_text(probe) == answer_compact for probe in probes if probe):
            return option
        if any(answer_compact and answer_compact in normalize_choice_text(probe) for probe in probes if probe):
            return option
    return None


def resolve_pending_clarification(clarification: Dict[str, Any], answer: str) -> Dict[str, Any]:
    ctype = clarification.get("type")
    options = clarification.get("options") or clarification.get("candidates") or []

    if ctype == "entity_choice":
        choice = match_clarification_option(answer, options)
        if not choice:
            retry = dict(clarification)
            retry["prompt"] = "I still couldn't tell which entity you meant. Reply with the option number or pick one below."
            return {"clarification": retry}
        base_query = clarification.get("query", "")
        entity_set = choice.get("entity_set") or choice.get("value") or choice.get("label", "")
        service_id = choice.get("service_id", "")
        if is_generic_scope_query(base_query):
            return {
                "query": choice.get("query") or f"show {entity_set}",
                "selection": choice,
                "direct_select": True,
            }
        resolved_query = f"{base_query}\nUse entity {entity_set} from service {service_id}."
        return {"query": resolved_query, "selection": choice}

    if ctype == "query_scope":
        choice = match_clarification_option(answer, options)
        if choice:
            return {"query": choice.get("query") or choice.get("value") or choice.get("label", "")}

        normalized = answer.strip()
        if not normalized:
            retry = dict(clarification)
            retry["prompt"] = "I need a bit more detail. Tell me which data you want to work with, for example 'purchase orders' or 'products'."
            return {"clarification": retry}

        if re.search(r"\b(show|list|get|find|fetch|display|compare|count|how many)\b", normalized, re.IGNORECASE):
            return {"query": normalized}
        return {"query": f"show {normalized}"}

    return {"query": answer}


def build_entity_choice_clarification(query: str, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    options = []
    for candidate in candidates:
        service_name = candidate.get("service_name") or candidate.get("service_id", "service")
        entity_set = candidate.get("entity_set", "")
        entity_label = candidate.get("entity_label", "")
        label = entity_label or entity_set
        options.append({
            "label": label,
            "value": entity_set,
            "query": f"show {entity_set}",
            "entity_set": entity_set,
            "entity_label": entity_label,
            "service_id": candidate.get("service_id", ""),
            "service_name": service_name,
            "description": f"{service_name} · {entity_set}",
            "properties": candidate.get("properties", []),
        })
    return {
        "type": "entity_choice",
        "query": query,
        "prompt": "I found multiple possible entities for your request. Which one do you mean?",
        "options": options,
        "candidates": candidates,
    }


def build_scope_clarification(query: str, services: List[Dict[str, Any]], limit: int = 4) -> Optional[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []
    for svc in services:
        labels = svc.get("entity_labels", {})
        for entity in svc.get("healthy_entity_sets") or svc.get("entity_sets", []):
            meta = labels.get(entity, {})
            label = meta.get("entity_label") or entity
            options.append({
                "label": label,
                "value": entity,
                "query": f"show {entity}",
                "entity_set": entity,
                "service_id": svc.get("id", ""),
                "service_name": svc.get("name", ""),
                "description": f"{svc.get('name', svc.get('id', 'service'))} · {entity}",
            })
            if len(options) >= limit:
                return {
                    "type": "query_scope",
                    "query": query,
                    "prompt": "Your request is too broad right now. What kind of data do you want to work with?",
                    "options": options,
                }
    if not options:
        return None
    return {
        "type": "query_scope",
        "query": query,
        "prompt": "Your request is too broad right now. What kind of data do you want to work with?",
        "options": options,
    }


def should_ask_scope_clarification(
    query: str,
    candidates: List[Dict[str, Any]],
    selected_entities: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    if selected_entities:
        return False
    if candidates and not is_generic_scope_query(query):
        return False

    return is_generic_scope_query(query)
