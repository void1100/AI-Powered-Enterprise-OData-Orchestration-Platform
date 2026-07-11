# Advanced OData Service Orchestration Platform

## Resume Description

AI-powered enterprise OData orchestration platform that converts natural-language questions into executable OData queries across SAP and generic OData services. The system was validated with real Sopra Steria-provided service data and public Northwind OData services.

## Resume Bullets

- Built a full-stack OData orchestration platform using FastAPI, Neo4j, ChromaDB, Docker, Nginx, and a vanilla JavaScript frontend for natural-language querying of enterprise services.
- Integrated and tested against real Sopra Steria-provided OData service data and public Northwind services, supporting metadata discovery, entity selection, pagination, joins, and tabular result rendering.
- Implemented an LLM reasoning layer with fallback mock planning, query-plan caching, vector memory/RAG, service registry recovery, and policy-based execution controls.
- Added JWT authentication, role-based admin workflows, audit logging, guarded create/update/delete operations, ML analytics, data cleaning, model training, and n8n-based result sharing.

## Technical Highlights

- Backend: FastAPI, Pydantic, httpx, SQLite, JWT, bcrypt
- Data intelligence: Neo4j graph indexing, ChromaDB vector memory, query-plan RAG
- AI planning: OpenAI-compatible, Gemini, NVIDIA, OpenRouter, and mock fallback providers
- OData features: metadata parsing, service registration, query construction, SAP CPI URL handling, pagination, entity joins
- Analytics: data profiling, cleaning, supervised ML training, prediction endpoints
- Deployment: Docker Compose stack with backend, frontend, Neo4j, mock OData writer, and n8n

## Demo Script

1. Start the stack with `docker compose up -d --build`.
2. Open `http://localhost:3000/app/`.
3. Register the mock writer service: `http://localhost:5000/DemoService`.
4. Ask: `Show top 5 products`.
5. Ask: `Join products and suppliers`.
6. Open the admin portal at `http://localhost:3000/admin/` and show users, services, audit, and usage.
7. Demonstrate guarded write preview using a create/update/delete prompt against the mock writer.

## Academic Positioning

This is suitable for a final-year project because it combines distributed service orchestration, natural-language query planning, graph/vector storage, security controls, analytics, and containerized deployment. The project should be presented as an advanced prototype validated with real enterprise data, not as a production-certified platform.

## Pre-Submission Checklist

- Use `.env.example` for documentation and keep real `.env` files private.
- Remove generated files from git tracking: `__pycache__`, `*.pyc`, and `backend/data`.
- Run tests before submission: `python -m unittest discover -s tests`.
- Include screenshots or a short screen recording of the main demo flow.
