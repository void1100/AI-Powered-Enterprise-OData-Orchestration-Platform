"""Type conversions between existing dict-based messages and
langchain_core.messages types.
"""
from typing import Any, Dict, List
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage


def dicts_to_langchain_messages(
    messages: List[Dict[str, Any]],
) -> List[HumanMessage | AIMessage | SystemMessage]:
    """Convert role/content dicts to LangChain message objects."""
    result = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            result.append(HumanMessage(content=content))
        elif role == "assistant":
            result.append(AIMessage(content=content))
        elif role == "system":
            result.append(SystemMessage(content=content))
    return result


def langchain_messages_to_dicts(
    messages: List[HumanMessage | AIMessage | SystemMessage],
) -> List[Dict[str, str]]:
    """Convert LangChain message objects back to role/content dicts."""
    result = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            result.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage):
            result.append({"role": "assistant", "content": msg.content})
        elif isinstance(msg, SystemMessage):
            result.append({"role": "system", "content": msg.content})
    return result
