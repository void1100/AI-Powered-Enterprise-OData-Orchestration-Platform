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
        self.tools = list(TOOLS)
        self._custom_tool_names: set = set()

    def register_custom_entity_tool(self, service_id: str, entity_name: str, description: str, allowed_columns: list, base_entity_set: str):
        tool_name = f"query_{service_id}_{entity_name}"
        if tool_name in self._custom_tool_names:
            return
        properties = {}
        if allowed_columns:
            properties["select"] = {
                "type": "array",
                "items": {"type": "string", "enum": allowed_columns},
                "description": f"Columns to return. Allowed: {', '.join(allowed_columns)}",
            }
        properties["filter"] = {"type": "string", "description": "OData filter expression"}
        properties["expand"] = {"type": "array", "items": {"type": "string"}, "description": "Related entities to expand"}
        properties["orderby"] = {"type": "string", "description": "Order by column asc/desc"}
        properties["top"] = {"type": "integer", "description": "Max rows to return"}
        properties["skip"] = {"type": "integer", "description": "Rows to skip"}
        tool = {
            "name": tool_name,
            "description": f"[Custom] {description}. Base: {base_entity_set}. Service: {service_id}",
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": [],
            },
        }
        self.tools.append(tool)
        self._custom_tool_names.add(tool_name)
        logger.info(f"Registered MCP tool: {tool_name}")

    def remove_custom_entity_tool(self, service_id: str, entity_name: str):
        tool_name = f"query_{service_id}_{entity_name}"
        self.tools = [t for t in self.tools if t["name"] != tool_name]
        self._custom_tool_names.discard(tool_name)
        logger.info(f"Removed MCP tool: {tool_name}")

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
        if name in self._custom_tool_names:
            return await self._call_custom_entity_tool(name, arguments)
        return {"error": f"Unknown tool: {name}"}

    async def _call_custom_entity_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        parts = tool_name.split("_", 2)
        if len(parts) < 3:
            return {"error": f"Invalid custom tool name: {tool_name}"}
        service_id = parts[1]
        entity_name = parts[2]
        plan = {
            "service_id": service_id,
            "entity_set": entity_name,
            "select": arguments.get("select"),
            "filter": arguments.get("filter"),
            "expand": arguments.get("expand"),
            "top": arguments.get("top", 50),
            "skip": arguments.get("skip"),
            "orderby": arguments.get("orderby"),
        }
        try:
            result = await service_manager.execute_plan(
                service_id=service_id,
                plan=plan,
                max_rows=arguments.get("top", 50),
            )
            return result
        except Exception as e:
            return {"error": str(e)}


mcp_server = MCPServer()


if __name__ == "__main__":
    logger.info("MCP server tools available:")
    for t in TOOLS:
        logger.info(f"  - {t['name']}: {t['description']}")
