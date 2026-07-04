from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ServiceRegister(BaseModel):
    id: str = Field(..., description="Stable identifier for the service, e.g. 'crm'.")
    name: str
    base_url: str
    description: str = ""
    auth_type: Optional[str] = Field(None, description="Authentication type: 'basic', 'bearer', or 'api_key'")
    auth_username: Optional[str] = Field(None, description="Username for Basic Auth")
    auth_password: Optional[str] = Field(None, description="Password for Basic Auth")
    auth_token: Optional[str] = Field(None, description="Token for Bearer Auth")
    auth_api_key: Optional[str] = Field(None, description="API Key value")
    auth_header_name: Optional[str] = Field(None, description="Header name for API Key")


class ServiceInfo(BaseModel):
    id: str
    name: str
    base_url: str
    description: str = ""
    entity_sets: List[str] = []
    entity_properties: Dict[str, List[str]] = {}


class ChatRequest(BaseModel):
    query: str
    session_id: Optional[str] = None
    user_role: str = "Admin"
    selected_entities: Optional[List[Dict[str, str]]] = None
    # [{"service_id": "sopra-po", "entity_name": "A_PurchaseOrder"}]


class TableData(BaseModel):
    columns: List[str] = []
    rows: List[Dict[str, Any]] = []
    row_count: int = 0
    truncated: bool = False
    total_count: Optional[int] = None


class DiscoveryCandidate(BaseModel):
    service_id: str
    service_name: str
    entity_set: str
    entity_type: Optional[str] = None
    properties: List[str] = []
    score: float = 0.0
    relationships: List[Dict[str, Any]] = []


class PlanStep(BaseModel):
    service_id: str
    entity_set: str
    select: Optional[List[str]] = None
    filter: Optional[str] = None
    expand: Optional[List[str]] = None
    top: Optional[int] = None
    skip: Optional[int] = None
    orderby: Optional[str] = None


class Plan(BaseModel):
    intent: str
    target_services: List[str] = []
    steps: List[PlanStep] = []
    summary: str = ""
    memory_used: List[Dict[str, Any]] = []


class ChatResponse(BaseModel):
    run_id: str
    session_id: Optional[str] = None
    user_query: str
    user_role: str
    summary: str
    plan: Optional[Plan] = None
    discovery: Optional[Dict[str, Any]] = None
    tool_calls: List[Dict[str, Any]] = []
    blocked_steps: List[Dict[str, Any]] = []
    table: Optional[TableData] = None
    primary_url: Optional[str] = None
    primary_service: Optional[str] = None
    error: Optional[str] = None
    memory_used: List[Dict[str, Any]] = []
    llm_provider: str = "unknown"
    llm_latency_ms: int = 0
    llm_tokens: int = 0
    chart_recommendations: List[Dict[str, Any]] = []
    drill_down_links: List[Dict[str, Any]] = []
    cached: bool = False
    intent: Optional[str] = None
    auto_train_result: Optional[Dict[str, Any]] = None


class SessionCreate(BaseModel):
    title: str = "New Chat"
    user_role: str = "Admin"


class SessionInfo(BaseModel):
    id: str
    title: str
    user_role: str
    created_at: str
    updated_at: str


class MessageInfo(BaseModel):
    id: str
    role: str
    content: str
    plan: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    created_at: str


class MCPCallRequest(BaseModel):
    name: str
    arguments: Dict[str, Any] = {}


class MCPCallResponse(BaseModel):
    result: Dict[str, Any]


class EntitySelectRequest(BaseModel):
    service_id: str
    entity_name: str


class AutoJoinRequest(BaseModel):
    entities: List[EntitySelectRequest]


class EntityJoinExecuteRequest(BaseModel):
    entities: List[EntitySelectRequest]
    joins: Optional[List[Dict[str, Any]]] = None
    query: str = ""
    top: int = 50
