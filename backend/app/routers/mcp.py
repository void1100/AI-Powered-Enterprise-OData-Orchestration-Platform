"""MCP router — Model Context Protocol tool listing and invocation."""
from fastapi import APIRouter, HTTPException, Request

from app.auth import get_current_user
from app.schemas.models import MCPCallRequest, MCPCallResponse
from app.mcp.mcp_server import mcp_server

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("/tools")
async def mcp_tools():
    return {"tools": mcp_server.tools}


@router.post("/call", response_model=MCPCallResponse)
async def mcp_call(payload: MCPCallRequest, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    result = await mcp_server.call_tool(payload.name, payload.arguments)
    return MCPCallResponse(result=result)
