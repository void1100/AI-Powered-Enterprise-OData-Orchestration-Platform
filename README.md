# Advanced OData Service Orchestration (MCP-Based)

A self-contained, locally-runnable implementation of the **Advanced OData
Service Orchestration Architecture** shown in the design. It connects to any
OData v4 service, orchestrates queries through an LLM reasoning engine, and
exposes a chat-style frontend for natural-language interaction.

## Quick start (Docker)

```bash
docker compose up -d --build
```

Wait ~30s for everything to start, then open **`http://localhost:3000`**.

This brings up:
- **Frontend** (nginx) at `http://localhost:3000`
- **Backend** (FastAPI) at `http://localhost:8000` (docs at `/docs`)
- **Neo4j** graph DB at `bolt://localhost:7687` (browser at `http://localhost:7474`)
- **Sample OData service** at `http://localhost:5000`
- A one-shot **seeder** that registers the Northwind and sample OData services

After the first build, services (including Northwind) are auto-registered.
On subsequent restarts the backend re-hydrates its in-memory service
registry from Neo4j, so the seed only needs to run once per fresh
`neo4j_data` volume.

To stop:
```bash
docker compose down
```

To wipe all data (including Neo4j):
```bash
docker compose down -v
```

To use the real OpenAI LLM instead of the mock planner, set `OPENAI_API_KEY`:
```bash
OPENAI_API_KEY=sk-... docker compose up -d
```

### Sample queries to try in the chat

```
Show top 5 customers from Germany
List all products in Beverages category
Show top 10 orders with status Shipped
How many customers are in France?
Show top 5 most expensive products
Show customers with their orders
```

## Architecture (matching the diagram)

```
        Problem & Input                Orchestration Layer                  Service Execution Layer
        ───────────────                ──────────────────                  ──────────────────────
        User Interface                 Service Discovery Agent             MCP Protocol Bridge
        Natural Language Query ─►      Tool Registry (ChromaDB)            OData Request Builder
                                       Schema Store (OData CSDL)           Response Sanitizer
                                       LLM Reasoning Engine                OData Endpoints
                                       Relationship & Access Manager
                                       Graph DB (Neo4j + in-memory fallback)
                                       Authorization & Policy Engine
                                       Vector Memory (ChromaDB)
```

## Project structure

```
project_root/
├── backend/                       # FastAPI backend
│   ├── app/
│   │   ├── agents/                # discovery, reasoning, policy, orchestrator
│   │   ├── db/                    # neo4j, chroma, sqlite, in-memory graph
│   │   ├── mcp/                   # MCP tool server
│   │   ├── schemas/               # Pydantic models
│   │   ├── services/              # OData client, builder, sanitizer, manager
│   │   ├── config.py
│   │   └── main.py
│   ├── scripts/seed_sample_service.py
│   ├── requirements.txt
│   ├── .env.example
│   └── run.py
├── frontend/                      # Static HTML/CSS/JS chat UI
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── sample_odata_service/          # A tiny local OData v4 service for testing
│   ├── app.py
│   └── requirements.txt
└── README.md
```

## Prerequisites

- Python 3.10+
- (Optional) Neo4j 5.x running on `bolt://localhost:7687`. The backend will
  fall back to an in-memory graph if Neo4j is unavailable.
- (Optional) OpenAI API key in `backend/.env` for the OpenAI LLM provider.
  By default, the system uses a deterministic **mock LLM** that does not
  require any external service.

## 1. Backend setup

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
copy .env.example .env          # Windows
# cp .env.example .env          # macOS / Linux
```

Run the backend:

```bash
python run.py
```

The API is available at `http://localhost:8000`. OpenAPI docs at `/docs`.

## 2. Start the local sample OData service (recommended for offline testing)

```bash
cd ../sample_odata_service
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --port 5000 --reload
```

This exposes a minimal OData v4 service at
`http://localhost:5000` with entity sets `Customers`, `Orders`, and `Products`.

## 3. Register services and seed the system

In a separate terminal:

```bash
cd backend
.venv\Scripts\activate
python -m scripts.seed_sample_service
```

This registers the public Northwind OData v4 service. The seed script
discovers **26 entity sets** (Customers, Orders, Products, Categories,
Suppliers, Employees, Shippers, etc.) and indexes them into the graph DB and
vector store. The system is verified end-to-end against Northwind - see
`scripts/smoke_test_northwind.py` for a smoke test.

You can also register additional services via the frontend's **Services**
button or the API:

```bash
curl -X POST http://localhost:8000/services \
  -H "Content-Type: application/json" \
  -d '{"id":"sample","name":"Sample","base_url":"http://localhost:5000","description":"Local OData sample"}'
```

## 4. Run the frontend

The frontend is a static page. You can either:

**a) Open the file directly:**
Open `frontend/index.html` in your browser. Then click the gear icon (not
shown, use the **Services** button) and ensure the API base is
`http://localhost:8000`. You can change this in the browser console:

```js
localStorage.setItem("apiBase", "http://localhost:8000");
```

**b) Serve it locally (recommended, avoids CORS issues):**
```bash
cd frontend
python -m http.server 3000
```

Open `http://localhost:3000` in your browser.

## 5. Try it

Example natural-language queries (verified against the Northwind OData
service):

- `Show top 5 customers from Germany`
- `List all products in Beverages category`
- `Show top 10 orders with status Shipped`
- `How many customers are in France?`
- `Show top 5 most expensive products`
- `Show customers with their orders`

Each response is rendered as a table, and the orchestrator exposes the OData
URL it called plus the tool calls it made. The chat history is stored in
SQLite; relevant prior turns are recalled via vector memory (ChromaDB) for
context.

## API surface

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/services` | GET / POST | List / register OData services |
| `/services/{id}` | DELETE | Remove a service |
| `/services/{id}/refresh` | POST | Re-fetch metadata |
| `/roles` | GET | List role policies |
| `/chat` | POST | Natural-language query endpoint |
| `/sessions` | GET / POST | Chat sessions |
| `/sessions/{id}` | PATCH / DELETE | Rename / delete a session |
| `/sessions/{id}/messages` | GET | Message history |
| `/mcp/tools` | GET | List MCP-style tools |
| `/mcp/call` | POST | Call an MCP-style tool |

## Using the MCP server from an MCP-compatible client

The backend exposes MCP-style tools at `/mcp/tools` and `/mcp/call`. The
included `app/mcp/mcp_server.py` also includes the tool definitions
(`list_services`, `register_service`, `query_odata`, `list_sessions`,
`get_messages`) which can be wired into a stdio MCP transport by adapting the
`call_tool` method into an MCP server entrypoint.
