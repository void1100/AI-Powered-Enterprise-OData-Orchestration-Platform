# Advanced OData Service Orchestration Architecture (MCP-Based)

> **Version:** 3.0 | **Last Updated:** July 2026  
> **Classification:** Enterprise SAP OData Chatbot  
> **Architecture:** MCP-Based Multi-Service Orchestration  
> **Repository:** [github.com/Karanpr-18/ODATA](https://github.com/Karanpr-18/ODATA)

A self-contained, locally-runnable implementation of the **Advanced OData Service Orchestration Architecture**. It connects to any OData v4 service, orchestrates queries through an LLM reasoning engine, and exposes a chat-style frontend for natural-language interaction with SAP and enterprise data systems.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Tech Stack](#tech-stack)
- [Quick Start](#quick-start)
- [Features](#features)
- [Query Engine](#query-engine)
- [ML & Analytics](#ml--analytics)
- [LLM + ML Integration](#llm--ml-integration)
- [Token Usage Tracker](#token-usage-tracker)
- [Security](#security)
- [System Requirements](#system-requirements)
- [Infrastructure](#infrastructure)
- [API Reference](#api-reference)
- [Cost Analysis](#cost-analysis)
- [Architecture Comparison](#architecture-comparison)
- [Recommendations](#recommendations)
- [Troubleshooting](#troubleshooting)

---

## Architecture Overview

### High-Level Flow

```
User Query (Browser)
      │
      ▼
┌─────────────────────┐
│  nginx Frontend     │  ← Landing page, Chat UI, Admin Portal
│  (port 3000)        │    Static HTML/CSS/JS, no build step
└──────────┬──────────┘
           │ HTTP /api/*
           ▼
┌─────────────────────┐
│  FastAPI Backend    │  ← REST API, Reasoning Engine, Orchestrator
│  (port 8000)        │    Python 3.10, uvicorn
└──────────┬──────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────┐
│               MCP-Based Orchestration Layer                  │
│                                                              │
│  Query Optimizer → Reasoning Engine → Orchestrator → OData   │
│       │                │                  │                  │
│   Plan Cache     LLM Provider      Service Manager          │
│   ChromaDB RAG   (Groq/NVIDIA)     (SAP CPI, Generic)       │
│                      │                  │                    │
│              ┌───────┘                  │                    │
│              ▼                          ▼                    │
│     Neo4j Graph DB              OData HTTP Client           │
│     (Entity Relations)          (Basic Auth, Pagination)    │
│              │                          │                    │
│              └──────────► ML Engine ◄───┘                    │
│                    (scikit-learn, xgboost)                   │
└─────────────────────────────────────────────────────────────┘
```

### Active Services

| Service | Entities | Description |
|---------|----------|-------------|
| **Sopra PO Service** | 8 | SAP Purchase Order management (SAP CPI) |
| **PP MPE Order Manage** | 158 | SAP Manufacturing Production execution (SAP CPI) |

---

## Tech Stack

### Backend

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Framework | FastAPI + uvicorn | REST API server, async handling |
| Graph DB | Neo4j | Entity relationships, service registry |
| Vector DB | ChromaDB | Query plan caching, RAG retrieval |
| Auth DB | SQLite | Users, roles, audit logs, token usage |
| LLM Primary | Groq (llama-3.3-70b-versatile) | Query plan generation, data insights |
| LLM Secondary | NVIDIA (llama-3.3-70b-instruct) | Free alternative |
| HTTP Client | httpx | Async OData with Basic Auth |
| ML | scikit-learn + xgboost + catboost | 17 algorithms |
| Data Profiler | Custom (numpy) | Column analysis, correlations, outliers |
| Auth | JWT + bcrypt | Password hashing, RBAC |

### Frontend

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Server | nginx 1.27 | Static files, API proxy |
| Chat UI | HTML/CSS/JavaScript | No build step, instant load |
| Charts | Chart.js | Pie, Bar, Network visualization |
| Admin | HTML/CSS/JavaScript | User management, RBAC, Usage dashboard |

### Infrastructure

| Service | Port | Description |
|---------|------|-------------|
| Frontend | 3000 | Landing, Chat, Admin |
| Backend | 8000 | REST API, ML engines |
| Neo4j | 7474/7687 | Graph database |
| n8n | 5678 | Workflow automation |

---

## Quick Start

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
- Git installed
- (Optional) A free LLM API key

### 1. Clone & Run

```bash
git clone https://github.com/Karanpr-18/ODATA.git
cd ODATA
docker compose up -d --build
```

Wait ~30-60 seconds for all services to start.

### 2. (Optional) Configure LLM API Key

Create a `.env` file in the project root:

```bash
# Groq (recommended — free tier, 14,400 RPD)
LLM_PROVIDER=openai
OPENAI_API_KEY=gsk_your_key_here
OPENAI_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=llama-3.3-70b-versatile

# Multiple keys for rate limit rotation
OPENAI_API_KEYS=gsk_key1,gsk_key2,gsk_key3

# NVIDIA (free, no rate limit)
LLM_PROVIDER=nvidia
NVIDIA_API_KEY=nvapi-your_key_here

# Gemini
LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza_your_key_here

# OpenRouter MiniMax M3
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-your_key_here
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=minimax/minimax-m3
```

Free API keys:
- Groq: https://console.groq.com/keys
- OpenRouter: https://openrouter.ai/keys
- NVIDIA: https://build.nvidia.com/
- Gemini: https://aistudio.google.com/apikey

> **Note:** Without an API key, the system uses a mock LLM that echoes queries back. API keys are never pasted in chat — always use `.env` file.

### 3. Open the Application

| Page | URL |
|------|-----|
| **Landing Page** | http://localhost:3000 |
| **Chat App** | http://localhost:3000/app/ |
| **Admin Portal** | http://localhost:3000/admin/ |
| **API Docs (Swagger)** | http://localhost:8000/docs |
| **Neo4j Browser** | http://localhost:7474 |
| **n8n Workflows** | http://localhost:5678 |

### Default Credentials

| Service | Username | Password |
|---------|----------|----------|
| Admin Portal | `admin` | `admin123!` |
| Neo4j | `neo4j` | `password` |
| n8n | `admin` | `admin` |

---

## Features

### Chat Interface

- Natural language input with chat history
- Tabular results with sorting and CSV export
- Session management (create, rename, delete)
- Vector memory for context from prior conversations
- Processing indicator (animated dots) while query executes
- Metadata pills hidden by default (click "Details" to expand)
- LLM provider/badge shows provider, latency, tokens, and intent
- Real-time LLM model switcher in the UI
- Dark mode toggle (persists across sessions)

### Entity Selector & Auto-Join

- **Visual Entity Selector**: Pick specific entities from any service
- **Property Labels**: Each property classified as Key, ForeignKey, Date, Measure, Status, Description, Attribute
- **Auto-Join Detection**: Automatically finds common columns between selected entities
- **Join Priority Labels**: Primary Key (green), Foreign Key (blue), Fuzzy Match (purple), Confirmed (yellow)
- **Multi-Entity Chains**: Join 3+ entities in sequence
- **Cross-Service Joins**: Join entities across different OData services
- **Chat Context**: Selected entities scope chat queries — pick entities, then ask questions that auto-join at runtime

### Query Engine

- **Query Optimizer**: Intent classification, query plan caching, smart $select
- **Query RAG**: Retrieves similar past query plans as few-shot examples (ChromaDB)
- **Multi-entity aggregation**: Automatically detects and joins entities across services
- **Post-fetch aggregation**: GROUP BY computed in Python (OData doesn't support GROUP BY natively)
- **Post-aggregation computation**: Percentage, comparison, extremum, ratio
- **Auto-pagination**: Fetches all rows from OData services using $skip/$top loops
- **$top limit respected**: When user says "top 5", only 5 rows returned
- **Multiple API key rotation**: Automatically retries with next key on rate limit (429)
- **Basic Auth support**: For protected OData services (SAP CPI, enterprise backends)
- **SAP CPI non-standard URL pattern**: Supports `?service=X&entity=Y&top=N` format
- **Entity property metadata**: LLM receives property names/columns for accurate queries
- **Query caching**: MD5-keyed, 5-minute TTL, 100 max entries

### Chart Visualization

- **Table**: Full data table with OData metadata filtered out
- **Graph**: Auto-detects best visualization:
  - Pie Chart (categorical data)
  - Bar Chart (numerical comparisons)
  - Network Graph (entity relationships)
- Sub-tabs for manual override (Auto/Pie/Bar/Network)

### ML Analysis (17 Algorithms)

Click the **Analyze** tab to run ML on query results:

**Unsupervised (5):**
- Summary Statistics, Anomaly Detection, Correlation, K-Means, Feature Importance

**Supervised (12):**
- Decision Tree, Random Forest, Linear Regression, Logistic Regression
- XGBoost, CatBoost, KNN, SVM, Gradient Boosting, Ada Boost, Extra Trees, Naive Bayes

**Business Insights:** Automatically generates actionable recommendations from ML results

**Predictive Queries:** Use trained models in chat queries (e.g., "Predict if PO 4500000001 will be approved")

**Data Cleaning Pipeline:** Missing values, outlier removal, normalization, encoding, deduplication

### LLM + ML Integration

- **Data Profiler**: Automatically scans fetched data — detects column types, correlations, outliers, distributions, data quality score (0-100)
- **Auto-Train**: After every query with 10+ rows, the system profiles data, selects the best ML algorithm (not just Random Forest), trains a model, then deletes it to save memory
- **Algorithm Selection**: Decision Tree for small datasets, Random Forest for balanced classification, Gradient Boosting for complex tasks, Linear Regression for normal distributions
- **On-Demand Insights**: Click the "Analyze" button on any query result to get LLM-powered data insights, ML recommendations, chart suggestions, and follow-up queries
- **Insights Panel**: Shows data summary, profile stats, correlations, outliers, ML recommendation with algorithm + target + reasoning, and clickable suggestion chips

### Token Usage Tracker

- **Per-Query Tracking**: Every chat query logs provider, tokens, latency, intent, and cached status to SQLite
- **Admin Usage Dashboard**: Accessible via Admin Portal → Usage page
  - **Summary Cards**: Today's tokens, 7-day average, weekly total
  - **Provider Breakdown**: Bar chart showing tokens per provider with estimated cost
  - **Daily Chart**: Token usage over the last 7 days
  - **Recent Queries Table**: Timestamp, query text, provider, tokens, latency, cached status
- **Cost Estimation**: Automatic cost calculation per provider (Groq $0.59/$0.79 per M tokens, NVIDIA free)
- **API Endpoint**: `GET /admin/usage` returns full usage data (today, this_week, by_provider, recent_queries, daily_average)

### Cross-Service Joins

- **Union**: Stack rows from multiple services
- **Match**: Join by common key
- **Enrichment**: Primary + secondary lookup

### Custom Entities

- Create virtual entities from real OData entities
- Auto-generates MCP tools per custom entity
- Persisted in Neo4j graph

### Authentication & Authorization

- JWT tokens with httpOnly cookies
- Password strength validation
- Account lockout after 5 failed attempts
- Role-based access control (5 default roles)
- Audit logging for all admin actions

### Chat Sharing via n8n

- Share queries via Email, WhatsApp, Slack, or Copy Link
- n8n workflow automation for webhook processing
- Pre-built workflow JSONs in `n8n-workflows/` directory

**Setup Instructions:**

1. **Open n8n** at http://localhost:5678 (login: `admin`/`admin`)
2. **Import workflow**: Click "..." → "Import from File" → select `n8n-workflows/unified-share.json`
3. **Configure credentials** in n8n:
   - **SMTP** (Email): Go to Credentials → Add SMTP credential with your email provider
   - **Twilio** (WhatsApp): Go to Credentials → Add HTTP Basic Auth with Twilio Account SID + Auth Token
4. **Set environment variables** in `.env`:
   ```
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=your-email@gmail.com
   SMTP_PASSWORD=your-app-password
   TWILIO_ACCOUNT_SID=your-account-sid
   TWILIO_AUTH_TOKEN=your-auth-token
   TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886
   ```
5. **Activate the workflow** in n8n
6. **Restart backend**: `docker restart odata-backend`

---

## Query Engine

### Query Processing Pipeline

1. User types natural language query
2. Query Optimizer classifies intent (read/aggregate/compute/comparison)
3. Reasoning Engine detects explicit service mentions
4. If explicit: builds plan directly (0 tokens). If ambiguous: calls LLM
5. LLM generates query plan with entity, filters, selects
6. Orchestrator executes plan via OData HTTP client
7. Post-fetch aggregation: GROUP BY computed in Python
8. Response sanitizer: filters OData metadata, orders columns
9. Results returned with chart recommendations and suggestions

### Token Optimization

The system minimizes LLM token consumption:

- **Explicit service detection**: "from Sopra PO Service" → 0 LLM tokens
- **Smart $select**: Only requests columns relevant to the query
- **Plan caching**: Identical queries return cached plans (0 tokens)
- **RAG**: Similar past plans reduce prompt complexity
- **Mock planner**: Handles simple read queries without LLM

### SAP CPI Integration

Special handling for SAP Cloud Platform Integration services:

- Non-standard URL pattern: `?service=X&entity=Y&top=N`
- Basic Auth authentication with username/password
- Metadata recovery from Neo4j when metadata endpoint returns 500
- Auto-cap $top for entities with strict row limits
- Health probe includes auth headers for degraded status detection

---

## ML & Analytics

### Algorithm Coverage

| Algorithm | Type | Use Case |
|-----------|------|----------|
| Summary Statistics | Unsupervised | Data overview |
| Anomaly Detection | Unsupervised | Outlier identification |
| Correlation Analysis | Unsupervised | Feature relationships |
| K-Means Clustering | Unsupervised | Data grouping |
| Feature Importance | Unsupervised | Key variables |
| Decision Tree | Supervised | Interpretable classification |
| Random Forest | Supervised | Ensemble classification |
| Linear Regression | Supervised | Continuous prediction |
| Logistic Regression | Supervised | Binary classification |
| XGBoost | Supervised | High-performance boosting |
| CatBoost | Supervised | Categorical handling |
| KNN | Supervised | Instance-based |
| SVM | Supervised | Margin-based |
| Gradient Boosting | Supervised | Sequential ensemble |
| Ada Boost | Supervised | Weak learner boosting |
| Extra Trees | Supervised | Randomized ensemble |
| Naive Bayes | Supervised | Probabilistic |

### Business Insights Engine

Supervised ML results include automated Business Insights:
- Analyzes model accuracy and feature importance
- Generates domain-specific actionable recommendations
- Provides risk assessment based on prediction distribution
- Suggests next steps for data collection or process improvement

### Predictive Queries

Trained ML models can be used directly in chat:
- Train a model on query results
- Use in subsequent queries: "Predict if PO 4500000001 will be approved"
- Supports any service: predictions work across all registered OData services
- Results show Yes/No with confidence scores

---

## Security

### Authentication

- JWT tokens stored in httpOnly cookies (XSS-resistant)
- bcrypt password hashing with salt
- Password strength validation (min 8 chars, uppercase, lowercase, digit, special)
- Account lockout after 5 consecutive failed attempts
- Session-based authentication with automatic expiry

### Role-Based Access Control

| Role | Permissions | Description |
|------|------------|-------------|
| Super Admin | ALL | Full system access |
| Admin | manage_users, manage_services, view_analytics | User and service management |
| Data Analyst | execute_queries, view_services, export_data | Query execution and export |
| Viewer | view_services | Read-only access |
| Guest | execute_queries | Basic query execution |

### Audit Logging

All admin actions are logged with timestamp, user, action, and IP address.

---

## System Requirements

### Minimum Requirements (Works)

| Component | Specification |
|-----------|--------------|
| **Operating System** | Windows 10/11 (64-bit), macOS 12+, or Ubuntu 20.04+ |
| **CPU** | 4 cores / 2 GHz (Intel i5 or AMD Ryzen 5 equivalent) |
| **RAM** | 8 GB total (6 GB available for Docker) |
| **Disk** | 10 GB free space (SSD recommended) |
| **Docker** | Docker Desktop 4.0+ or Docker Engine 20.10+ |
| **Docker Compose** | v2.0+ (included with Docker Desktop) |
| **Network** | Internet connection (for LLM API calls) |
| **Browser** | Chrome 90+, Firefox 90+, Edge 90+, Safari 14+ |

### Recommended Requirements (Smooth)

| Component | Specification |
|-----------|--------------|
| **Operating System** | Windows 11, macOS 13+, or Ubuntu 22.04+ |
| **CPU** | 8 cores / 3 GHz (Intel i7 or AMD Ryzen 7 equivalent) |
| **RAM** | 16 GB total (12 GB available for Docker) |
| **Disk** | 25 GB free space (NVMe SSD recommended) |
| **Docker** | Docker Desktop 4.20+ |
| **Network** | 100 Mbps broadband connection |
| **Browser** | Latest Chrome, Firefox, or Edge |

### Enterprise Requirements (Production)

| Component | Specification |
|-----------|--------------|
| **Operating System** | Ubuntu 22.04 LTS or RHEL 8+ |
| **CPU** | 16+ cores / 3 GHz (Intel Xeon or AMD EPYC) |
| **RAM** | 64 GB ECC RAM |
| **Disk** | 500 GB NVMe SSD (RAID 10 recommended) |
| **Docker** | Docker Engine 24+ with Swarm or Kubernetes |
| **Network** | 1 Gbps dedicated, low-latency connection |
| **GPU** | Optional: NVIDIA A10G 24GB (for local embeddings) |

### Actual RAM Consumption (Live Measurements)

> Measured on development machine (Intel i7-14650HX, 31.7 GB RAM, Windows 11)

| Service | RAM (Idle) | RAM (Under Load) | CPU (Idle) | CPU (Load) |
|---------|-----------|-----------------|-----------|-----------|
| **odata-backend** | 890 MB | ~1.2 GB | 0.18% | 10-30% |
| **odata-neo4j** | 830 MB | ~1.5 GB | 2.81% | 5-15% |
| **odata-n8n** | 323 MB | ~500 MB | 0.20% | 5-10% |
| **odata-frontend** | 19 MB | ~50 MB | 0.00% | 1-5% |
| **Total** | **~2.06 GB** | **~3.2 GB** | **~3.2%** | **20-60%** |

### Actual Disk Usage (Live Measurements)

| Component | Size | Notes |
|-----------|------|-------|
| **Project source code** | 1.6 MB | Backend (1.3 MB) + Frontend (0.3 MB) |
| **Docker images** | 32.69 GB | 69% reclaimable (22.8 GB) |
| **Docker build cache** | 21.34 GB | All reclaimable |
| **Docker containers** | 701 MB | 4 running containers |
| **Docker volumes** | 3.16 GB | Neo4j, ChromaDB, n8n, backend data |
| **Total Docker** | **~57 GB** | Images + cache + volumes |

### What to Delete to Save Space

| What to Delete | Command | Space Recovered |
|---------------|---------|----------------|
| Docker build cache | `docker builder prune -a` | **~21 GB** |
| Unused Docker images | `docker image prune -a` | **~22 GB** |
| Docker volumes (careful!) | `docker volume prune` | **~2.4 GB** |
| **Total recoverable** | | **~45 GB** |

> **Warning:** `docker volume prune` will delete Neo4j data, ChromaDB cache, and n8n workflows. Back up first.

### Software Dependencies

| Software | Version | Purpose |
|----------|---------|---------|
| Docker Desktop | 4.0+ | Container runtime |
| Docker Compose | v2.0+ | Multi-container orchestration |
| Git | 2.30+ | Version control |

### Network Requirements

| Requirement | Specification |
|-------------|--------------|
| **Inbound Ports** | 3000 (frontend), 8000 (API), 7474 (Neo4j), 7687 (bolt), 5678 (n8n) |
| **Outbound Ports** | 443 (HTTPS for LLM APIs) |
| **Bandwidth** | 10 Mbps minimum for API calls |
| **Latency** | <100ms to LLM providers (Groq, NVIDIA, Gemini) |

---

## Infrastructure

### Docker Architecture

```bash
# Start all services
docker compose up -d --build

# Stop all services
docker compose down

# Stop and wipe all data
docker compose down -v

# Rebuild a specific service
docker compose up -d --build backend

# View logs
docker logs odata-backend -f
docker logs odata-frontend -f

# Save 21GB — clear build cache
docker builder prune -a

# Save 22GB — remove unused images
docker image prune -a
```

### Container Specifications

| Container | Image | RAM | Volume |
|-----------|-------|-----|--------|
| odata-frontend | nginx:1.27-alpine | 19 MB | Static files |
| odata-backend | python:3.10-slim | 890 MB | backend_data |
| odata-neo4j | neo4j:5.26 | 830 MB | neo4j_data |
| odata-n8n | n8nio/n8n | 323 MB | n8n_data |

### Data Persistence

- **neo4j_data**: Graph database (entity relationships, service registry, join relationships)
- **backend_data**: SQLite auth database (users, roles, audit logs, token usage)
- **chroma_cache**: ChromaDB vector store (query plan cache, RAG)
- **n8n_data**: n8n workflow configurations

---

## API Reference

### Chat & Analysis

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/chat` | POST | Natural-language query (supports `selected_entities` for scoped queries) |
| `/chat/analyze` | POST | On-demand LLM insights for session data |
| `/analyze` | POST | Unsupervised ML analysis |
| `/ml/train` | POST | Train supervised ML model |
| `/ml/clean` | POST | Data cleaning pipeline |
| `/ml/predict` | POST | Predict using trained model |
| `/ml/algorithms` | GET | List supported algorithms |
| `/ml/models` | GET | List trained models |
| `/share` | POST | Share chat via n8n webhook |

### Services

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/services` | GET / POST | List / register OData services |
| `/services/{id}` | DELETE | Remove a service |
| `/services/{id}/refresh` | POST | Re-fetch metadata |
| `/services/health` | GET | Health check all services |

### Entity Selector & Auto-Join

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/entities/{service_id}` | GET | Get entities with labeled properties |
| `/entities/auto-join` | POST | Detect joins between selected entities |
| `/entities/execute-join` | POST | Execute multi-entity join query |

### Custom Entities & Joins

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/custom_entities` | GET | List custom entities |
| `/custom_entities/{svc}` | POST / DELETE | Create / delete custom entity |
| `/joins` | GET / POST | List / create joins |
| `/joins/{id}` | DELETE | Delete a join |
| `/joins/{id}/execute` | POST | Execute a join |
| `/joins/{id}/chat` | POST | Chat about join data |

### Auth & Admin

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/auth/login` | POST | Login |
| `/auth/logout` | POST | Logout |
| `/auth/me` | GET | Current user |
| `/admin/users` | GET / POST | List / create users |
| `/admin/roles` | GET / POST | List / create roles |
| `/admin/analytics` | GET | Query analytics |
| `/admin/audit` | GET | Audit log |
| `/admin/dashboard` | GET | Dashboard summary |
| `/admin/usage` | GET | Token usage dashboard (today, weekly, by provider, recent queries) |

### LLM, Sessions & Query Enhancements

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/llm/config` | GET / POST | Get / set LLM provider/model |
| `/sessions` | GET / POST | Chat sessions |
| `/sessions/{id}/messages` | GET | Message history |
| `/suggestions` | GET | Query autocomplete suggestions |
| `/cache/stats` | GET | Query cache + optimizer stats |
| `/cache/clear` | POST | Clear query cache |
| `/health` | GET | Backend health + stats |

### MCP

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/mcp/tools` | GET | List MCP tools |
| `/mcp/call` | POST | Call an MCP tool |

---

## Cost Analysis

### LLM API Pricing (Current)

| Provider | Model | Input Cost | Output Cost | Free Tier |
|----------|-------|-----------|-------------|-----------|
| **Groq** | llama-3.3-70b-versatile | $0.59/M tokens | $0.79/M tokens | 14,400 RPD |
| **Groq** | llama-3.1-8b-instant | $0.05/M tokens | $0.08/M tokens | 14,400 RPD |
| **NVIDIA** | llama-3.3-70b-instruct | Free | Free | Unlimited |
| **Gemini** | gemini-2.0-flash | $0.075/M tokens | $0.30/M tokens | 1500 RPD |
| **Gemini** | gemini-2.5-pro | $1.25/M tokens | $10.00/M tokens | 50 RPD |

### Cost Per Query Estimate

| Query Type | Tokens (est.) | Groq Cost | NVIDIA Cost | Gemini Flash Cost |
|------------|--------------|-----------|-------------|-------------------|
| Simple read ("Show top 5 POs") | ~600 | $0.0004 | $0.00 | $0.00005 |
| Aggregation ("Count by country") | ~800 | $0.0005 | $0.00 | $0.00006 |
| Complex ("Compare orders with items") | ~1200 | $0.0008 | $0.00 | $0.0001 |
| Multi-entity join | ~1500 | $0.001 | $0.00 | $0.0001 |
| ML training request | ~2000 | $0.0013 | $0.00 | $0.0002 |

### Monthly Cost Estimates

| Usage Level | Queries/Day | Groq Cost | NVIDIA Cost | Gemini Flash |
|-------------|------------|-----------|-------------|--------------|
| **Prototype** | 10 | ~$1 | $0 | ~$0.10 |
| **Development** | 100 | ~$5 | $0 | ~$0.50 |
| **Small Business** | 1,000 | ~$50 | $0 | ~$5 |
| **Enterprise** | 10,000 | ~$374 | $0 | ~$37 |

### Infrastructure Costs

| Tier | Infrastructure | API | Total Monthly |
|------|---------------|-----|--------------|
| **Prototype (local)** | $0 | $0-5 | **~$5** |
| **Small Enterprise (cloud)** | ~$500 | ~$40 | **~$540** |
| **Full Enterprise (HA)** | ~$1,530 | ~$300 | **~$1,830** |
| **Full Enterprise (Gemini)** | ~$800 | ~$600 | **~$1,400** |

---

## Architecture Comparison

| Dimension | Current (Groq + Neo4j) | Gemini + Neo4j | SurrealDB Alternative |
|-----------|------------------------|----------------|----------------------|
| **LLM Quality** | Llama 3.3 70B (excellent) | Gemini 2.5 Pro (best-in-class) | Same |
| **LLM Speed** | Groq is fastest globally | Gemini Flash is very fast | Same |
| **Data Privacy** | Embeddings never leave org | All text sent to Google | Same |
| **Infrastructure** | Medium (Neo4j needed) | Medium (Neo4j needed) | Low (single DB) |
| **Prototype Cost** | ~$5/month | ~$1/month | ~$5/month |
| **Enterprise Cost** | ~$1,830/month | ~$1,400/month | ~$1,500/month |
| **Vendor Lock-in** | Low (open models) | Medium (Google APIs) | Low |
| **Context Window** | 128K tokens | 1M tokens | N/A |
| **Multi-modal** | Text only | Images, PDFs, Audio | Text only |
| **Graph Relationships** | Native Neo4j Cypher | Native Neo4j Cypher | SurrealDB edges |

---

## Recommendations

### Use Current Stack (Groq + Neo4j) When:

- Data sovereignty / intranet deployment is required
- Embeddings must not leave the organisation's network
- Real-time UX speed is the top priority (Groq is ~10x faster)
- Offline or air-gapped deployments are needed
- Graph relationships need native Cypher queries

### Switch to Gemini When:

- Cost reduction at scale is the priority (51% cheaper at enterprise volume)
- Simplifying infrastructure is desired
- Better reasoning quality is needed (Gemini 2.5 Pro outperforms Llama 70B)
- Multimodal inputs (documents, images from SAP) are required
- 1M-token context window is beneficial (full OData schema in context)

### Quick Wins for Improvement

1. **Add OpenRouter key rotation** for better rate limit handling
2. **Implement session ownership** — bind sessions to user IDs
3. **Enable n8n Email/WhatsApp nodes** for automated sharing
4. **Fine-tune LLM** on accumulated RAG query plan data
5. **Clear Docker build cache** to save 21 GB: `docker builder prune -a`
6. **Add rate limiting** per user for API endpoints
7. **Implement horizontal scaling** with Docker Swarm

---

## Query Examples

### Simple Queries (0 tokens — mock planner)
```
Show top 5 customers from Northwind
List all products from Northwind
Show me materials from PP MPE Order Manage
```

### Aggregation Queries (computed — 0 tokens)
```
Count customers per country from Northwind
Show me the top 5 customers by order count from Northwind
Total sales by country from Northwind
```

### Complex Queries (LLM — ~600-1000 tokens)
```
List all products from Northwind
How many customers are in France?
Show top 5 most expensive products
```

### Multi-Entity Queries (computed — 0 tokens)
```
Show me the top 5 customers by order count, including their country and contact name from Northwind
```

### Entity Selector Queries
```
1. Click "Select Entities" button
2. Select A_PurchaseOrder + A_PurchaseOrderItem from Sopra PO Service
3. See detected join: PurchaseOrder = PurchaseOrder (Primary Key, 95%)
4. Click "Execute Join" (optional) or just type a chat query
5. Chat queries auto-scope to selected entities with runtime join detection
```

### On-Demand Insights
```
1. Run any query (e.g., "Show me purchase orders")
2. Click the "Analyze" button below the results
3. See LLM-powered insights:
   - Data quality score, correlations, outliers
   - ML recommendation (best algorithm + target column)
   - Chart suggestions
   - Follow-up query suggestions (clickable chips)
```

---

## Future Roadmap

### Completed
- [x] n8n Email/WhatsApp nodes enabled

### Optional Next Steps
- [ ] Get a Groq API key from a different org/account for key rotation
- [ ] Top up OpenRouter account
- [ ] Add session ownership — bind sessions to user IDs
- [ ] Fine-tune LLM on accumulated RAG query plan data
- [ ] Persist trained models to SQLite for durability across restarts

---

## Troubleshooting

**Services won't start:**
- Ensure Docker Desktop is running
- Check ports 3000, 7474, 7687, 8000, 5678 are not in use

**LLM returns mock responses:**
- Check `.env` file exists with valid API key
- Restart backend: `docker restart odata-backend`

**Query returns 0 rows:**
- Check the service is registered: `http://localhost:8000/services`
- Try a simpler query: "Show top 5 customers"

**SAP CPI services return 500 errors:**
- SAP CPI enforces strict entity row limits
- The system auto-recovers entities from Neo4j
- Some entities may not support `$filter`, `$select`, or `$orderby`

**Groq rate limit exceeded:**
- Free tier: 100K tokens/day shared across all keys
- Add multiple keys: `OPENAI_API_KEYS=key1,key2,key3`
- Switch to NVIDIA (free, no rate limit)

**Entity join returns 0 rows:**
- Data may not overlap at current `top` value
- Try selecting different entities
- Check detected joins for Primary Key matches

**Docker using too much disk:**
- Clear build cache: `docker builder prune -a` (saves ~21 GB)
- Remove unused images: `docker image prune -a` (saves ~22 GB)
- Check volume sizes: `docker system df -v`

---

## License

MIT
