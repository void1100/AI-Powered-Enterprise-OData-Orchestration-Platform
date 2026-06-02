"""MCP server wrapper.

This exposes the orchestrator's tools as MCP-style tool calls. It can be
embedded into the FastAPI app via the /mcp endpoint, and also provides a
standalone run helper for `python -m app.mcp.mcp_server`.
"""
import asyncio
import json
from typing import Any, Dict, List

from app.services.service_manager import service_manager
from app.agents.orchestrator import orchestrator
from app.db.sqlite_store import list_sessions, get_messages
from loguru import logger


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "list_services",
        "description": "List all registered OData services and their entity sets.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "register_service",
        "description": "Register a new OData service by id, name, and base URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "base_url": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["id", "name", "base_url"],
        },
    },
    {
        "name": "query_odata",
        "description": "Run a natural-language query against the orchestrated OData services.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "session_id": {"type": "string"},
                "user_role": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_sessions",
        "description": "List chat sessions.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_messages",
        "description": "Get all messages for a session.",
        "input_schema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
]


class MCPServer:
    def __init__(self):
        self.tools = TOOLS

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "list_services":
            return {"services": service_manager.list_services()}
        if name == "register_service":
            svc = await service_manager.register_service(
                service_id=arguments["id"],
                name=arguments["name"],
                base_url=arguments["base_url"],
                description=arguments.get("description", ""),
            )
            return {"service": {"id": svc["id"], "name": svc["name"], "base_url": svc["base_url"]}}
        if name == "query_odata":
            res = await orchestrator.run(
                user_query=arguments["query"],
                session_id=arguments.get("session_id"),
                user_role=arguments.get("user_role", "Admin"),
            )
            return res
        if name == "list_sessions":
            return {"sessions": list_sessions()}
        if name == "get_messages":
            return {"messages": get_messages(arguments["session_id"])}
        return {"error": f"Unknown tool: {name}"}


mcp_server = MCPServer()


if __name__ == "__main__":
    logger.info("MCP server tools available:")
    for t in TOOLS:
        logger.info(f"  - {t['name']}: {t['description']}")
