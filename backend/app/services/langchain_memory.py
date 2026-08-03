"""ConversationSummaryMemory - auto-summarizes older turns to reduce token
usage while preserving conversational context for the LLM planner.

Uses langchain_core.messages when available. Falls back to lightweight local
message classes so the memory path still works if that optional dependency is
not installed in the active environment.
"""
from typing import Any, Dict, List
from loguru import logger

try:
    from langchain_core.messages import HumanMessage, AIMessage
except ModuleNotFoundError:
    class _BaseMessage:
        def __init__(self, content: str):
            self.content = content

    class HumanMessage(_BaseMessage):
        pass

    class AIMessage(_BaseMessage):
        pass

    logger.warning("langchain_core not installed; using local message shims for conversation memory")


class ConversationSummaryMemory:
    """Sliding-window summary: older turns summarized, recent turns raw."""

    def __init__(self, keep_recent: int = 6, max_summary_tokens: int = 200):
        self.keep_recent = keep_recent
        self.max_summary_tokens = max_summary_tokens

    def build_context(
        self,
        messages: List[Dict[str, Any]],
        session_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Enrich session_context with summary + recent_turns_langchain."""
        if not messages:
            return {
                **session_context,
                "summary": "",
                "recent_turns_langchain": [],
                "token_estimate": 0,
            }

        lc_messages = self._to_langchain_messages(messages)

        if len(lc_messages) <= self.keep_recent:
            recent_messages = lc_messages
            older_messages = []
        else:
            recent_messages = lc_messages[-self.keep_recent:]
            older_messages = lc_messages[:- self.keep_recent]

        summary = self._summarize_older_turns(older_messages) if older_messages else ""

        recent_turns_raw = []
        for msg in recent_messages:
            role = "user" if isinstance(msg, HumanMessage) else "assistant"
            recent_turns_raw.append({"role": role, "content": msg.content})

        total_chars = len(summary) + sum(len(t["content"]) for t in recent_turns_raw)
        token_estimate = total_chars // 4

        enriched = {
            **session_context,
            "summary": summary,
            "recent_turns": recent_turns_raw,
            "recent_turns_langchain": recent_messages,
            "token_estimate": token_estimate,
        }
        logger.debug(
            f"ConversationSummaryMemory: {len(older_messages)} older msgs summarized -> "
            f"{len(summary)} chars, {len(recent_messages)} recent kept, ~{token_estimate} tokens"
        )
        return enriched

    def _to_langchain_messages(
        self, messages: List[Dict[str, Any]]
    ) -> List[HumanMessage | AIMessage]:
        lc = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content:
                continue
            if role == "user":
                lc.append(HumanMessage(content=content))
            elif role == "assistant":
                lc.append(AIMessage(content=content))
        return lc

    def _summarize_older_turns(self, messages: List) -> str:
        """Extractive summarization: key entities, filters, actions."""
        parts = []
        for msg in messages:
            content = msg.content if hasattr(msg, "content") else str(msg)
            short = content[:80].strip()
            if short:
                prefix = "User" if isinstance(msg, HumanMessage) else "Assistant"
                parts.append(f"{prefix}: {short}")

        summary = " | ".join(parts)
        max_chars = self.max_summary_tokens * 4
        if len(summary) > max_chars:
            summary = summary[:max_chars] + "..."
        return summary

    def format_for_llm_prompt(self, enriched_context: Dict[str, Any]) -> str:
        """Format enriched context into a string for LLM injection."""
        parts = []

        summary = enriched_context.get("summary", "")
        if summary:
            parts.append(f"Conversation summary: {summary}")

        last_entity = enriched_context.get("last_entity_set")
        last_service = enriched_context.get("last_service_id")
        if last_entity or last_service:
            parts.append(f"Previous context: entity={last_entity}, service={last_service}")

        recent = enriched_context.get("recent_turns", [])
        if recent:
            recent_str = "; ".join(
                f"{t['role']}: {t['content'][:60]}" for t in recent[-4:]
            )
            parts.append(f"Recent: {recent_str}")

        return "\n".join(parts)


conversation_summary_memory = ConversationSummaryMemory()
