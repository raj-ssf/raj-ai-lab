# Raj AI Lab

A full-stack AI platform running on Kubernetes — from document ingestion to agentic AI, all on your laptop.

Built with real production patterns: Kafka event streaming, microservice data pipeline, RAG with vector search, LangGraph agents with guardrails, knowledge graph, multi-tenant isolation, and GitOps deployment.

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                         RAJ AI LAB                                  │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │  INGESTION (Kafka microservice pipeline)                       │ │
│  │                                                                │ │
│  │  User/API → RAG Service → MinIO (S3) + Kafka: document.uploaded│ │
│  │    → Normalizer → Kafka: document.canonical                    │ │
│  │      → Chunker → Kafka: document.chunked                      │ │
│  │        → Embedder → Qdrant (vectors) + Kafka: document.indexed │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │  RAG QUERY PIPELINE (multi-model routing)                      │ │
│  │                                                                │ │
│  │  Question → Route model (fast/reasoning) → Embed → Qdrant     │ │
│  │    → Augment prompt → LLM → Answer with sources               │ │
│  │    → Cached in Redis → Logged to Kafka + Langfuse              │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │  AGENTIC AI (LangGraph)                                        │ │
│  │                                                                │ │
│  │  Question → Reason (LLM) → Act (tools) → Observe → Loop       │ │
│  │    Tools: RAG search, inventory check, supplier lookup (Neo4j) │ │
│  │    Guardrails: max steps, token budget, Redis kill switch      │ │
│  │    Audit: every step → Kafka agent.trace + Langfuse            │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │  OBSERVABILITY & GOVERNANCE                                    │ │
│  │                                                                │ │
│  │  Prometheus + Grafana    Langfuse (LLM tracing)                │ │
│  │  ArgoCD (GitOps)         Kyverno (policy enforcement)          │ │
│  │  Kafka UI                HPA autoscaling                       │ │
│  └───────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────┘
```

## Components

### Data Layer (`ai-data` namespace)

| Component | Purpose |
|-----------|---------|
| **Kafka** | Event streaming — 13 topics for document pipeline, agent traces, query logs |
| **PostgreSQL** | Metadata storage, Langfuse backend, evaluation results |
| **Qdrant** | Vector database for semantic search (384-dim, cosine similarity) |
| **Redis** | Query cache (1hr TTL), agent state, kill switch |
| **Neo4j** | Knowledge graph — suppliers, parts, products, production lines |
| **MinIO** | S3-compatible object storage for raw documents |
| **Kafka UI** | Web UI to browse topics and messages |

### AI Platform (`ai-platform` namespace)

| Component | Purpose |
|-----------|---------|
| **RAG Service** | API gateway — ingest, query, multi-model routing, multi-tenant |
| **Embedding Service** | Text → 384-dim vectors using all-MiniLM-L6-v2 (offline mode) |
| **Normalizer** | Kafka consumer — validates and normalizes documents to canonical format |
| **Chunker** | Kafka consumer — splits canonical documents into passages |
| **Embedder** | Kafka consumer — embeds chunks, stores vectors in tenant-specific Qdrant collections |
| **Agent Orchestrator** | LangGraph agent with RAG search, inventory, Neo4j supplier/graph tools |
| **Graph Updater** | Kafka consumer — writes supply chain triples to Neo4j |
| **RAG UI** | Gradio web UI — query, ingest text, upload files (PDF support) |
| **Langfuse** | LLM observability — traces, latency, token tracking |
| **Evaluation** | Benchmarks RAG quality, stores results in PostgreSQL |

### Infrastructure

| Component | Namespace | Purpose |
|-----------|-----------|---------|
| **ArgoCD** | `argocd` | GitOps — auto-syncs manifests from this repo |
| **Prometheus + Grafana** | `monitoring` | Metrics collection and dashboards |
| **Nginx Ingress** | `ingress-nginx` | Subdomain-based routing for all UIs |
| **Kyverno** | `kyverno` | Policy enforcement (resource limits, labels, probes) |
| **Ollama** | macOS native | LLM inference on Apple Silicon (llama3.2 + deepseek-r1) |

## Quick Start

### Option A: Terraform (recommended)

```bash
# Prerequisites: docker, k3d, kubectl, helm, terraform, ollama
cd terraform
terraform init
terraform apply
```

This provisions the entire lab with one command: k3d cluster, all images, namespaces, data layer, AI platform, Kafka topics, ArgoCD, Prometheus/Grafana, Nginx Ingress, and Kyverno.

### Option B: Manual setup

```bash
# 1. Create cluster
k3d cluster create raj-ai-lab \
  --servers 1 --agents 2 \
  --port "8080:80@loadbalancer" \
  --port "8443:443@loadbalancer" \
  --k3s-arg "--disable=traefik@server:0" \
  --wait

# 2. Build and import images
for svc in embedding rag ui agent graph-updater evaluation; do
  docker build -f Dockerfile.${svc} -t raj/${svc}:latest .
done
for svc in normalizer chunker embedder; do
  code=$(echo ${svc} | sed 's/normalizer/08-normalizer-code.py/;s/chunker/09-chunker-code.py/;s/embedder/10-embedder-code.py/')
  docker build -f Dockerfile.pipeline --build-arg SERVICE_FILE=${code} -t raj/${svc}:latest .
done
k3d image import raj/embedding-service:latest raj/rag-service:latest raj/rag-ui:latest \
  raj/normalizer:latest raj/chunker:latest raj/embedder:latest \
  raj/agent-orchestrator:latest raj/graph-updater:latest raj/evaluation:latest \
  -c raj-ai-lab

# 3. Deploy
kubectl apply -f manifests/ai-data/
kubectl apply -f manifests/ai-platform/

# 4. Start Ollama
ollama serve  # if not already running
ollama pull llama3.2
ollama pull deepseek-r1
```

## Access — All UIs

All UIs are accessible via subdomain-based ingress on port 8080. No port-forwarding needed.

| UI | URL | Credentials |
|----|-----|-------------|
| **RAG UI** | http://raj-ai-lab.localhost:8080 | — |
| **Qdrant** | http://qdrant.raj-ai-lab.localhost:8080 | — |
| **Kafka UI** | http://kafka.raj-ai-lab.localhost:8080 | — |
| **Neo4j Browser** | http://neo4j.raj-ai-lab.localhost:8080 | neo4j / rajailab123 |
| **MinIO Console** | http://minio.raj-ai-lab.localhost:8080 | rajailab / rajailab123 |
| **Langfuse** | http://langfuse.raj-ai-lab.localhost:8080 | raj@lab.local / rajailab123 |
| **Grafana** | http://grafana.raj-ai-lab.localhost:8080 | admin / raj-ai-lab |
| **Prometheus** | http://prometheus.raj-ai-lab.localhost:8080 | — |
| **ArgoCD** | http://argocd.raj-ai-lab.localhost:8080 | admin / `kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" \| base64 -d` |
| **RAG API** | http://api.raj-ai-lab.localhost:8080 | — (`/health`, `/query`, `/ingest`) |
| **Agent API** | http://agent.raj-ai-lab.localhost:8080 | — (`/health`, `/agent/run`) |

> On macOS, `*.localhost` subdomains resolve to 127.0.0.1 natively — no `/etc/hosts` entries needed.

## Supply Chain Demo

A complete crisis scenario demonstrating the full platform end-to-end.

### Seed data

```bash
# Port-forward RAG service (or use ingress)
kubectl port-forward -n ai-platform svc/rag-service 8000:8000 &

# Seed 4 documents for tenant "acme-corp":
# maintenance manual, supplier catalog, production schedule, quality report
python3 demo/seed-supply-chain.py http://localhost:8000
```

### Run the scenario

```bash
kubectl port-forward -n ai-platform svc/agent-orchestrator 8001:8001 &
bash demo/run-scenario.sh
```

**Scenario:** Acme Bearings shipment of 500 SKF-32210 bearings is delayed 5 days. Inventory covers 3.9 days at 320/day consumption. Revenue risk: $1.68M/day.

The demo runs 5 steps:
1. **RAG query** — bearing specs and torque values (fast model)
2. **RAG query** — approved suppliers with prices and lead times (fast model)
3. **RAG query** — production impact and revenue risk (reasoning model)
4. **RAG query** — quality concerns and quarantine history (reasoning model)
5. **Agent** — full crisis resolution using RAG + inventory + Neo4j graph + synthesis

### Inspect the data flow

| What | Where |
|------|-------|
| Kafka events | http://kafka.raj-ai-lab.localhost:8080 → topics: `query.log`, `agent.trace` |
| LLM traces | http://langfuse.raj-ai-lab.localhost:8080 → Traces |
| Vectors | http://qdrant.raj-ai-lab.localhost:8080 → collection `raj-docs-acme-corp` |
| Knowledge graph | http://neo4j.raj-ai-lab.localhost:8080 → `MATCH (n)-[r]->(m) RETURN n,r,m` |
| Raw documents | http://minio.raj-ai-lab.localhost:8080 → bucket `raj-documents` → `acme-corp/` |
| Metrics | http://grafana.raj-ai-lab.localhost:8080 → Raj AI Lab dashboard |

## Key Features

### Multi-Model Routing

Simple questions route to **llama3.2** (3B, fast). Complex questions containing keywords like *why, explain, compare, analyze, recommend* route to **deepseek-r1** (8B, reasoning).

Override per request: `"model": "fast"` or `"model": "reasoning"`.

### Multi-Tenant Isolation

Pass `tenant_id` on `/ingest` and `/query` endpoints. Each tenant gets:
- Separate Qdrant collection (`raj-docs-{tenant_id}`)
- Separate MinIO S3 path (`{tenant_id}/{doc_id}/`)
- Separate Redis cache keys (`query:{tenant_id}:{hash}`)
- `tenant_id` propagated through the entire Kafka pipeline

### Agent Guardrails

- **Max 10 steps** — prevents infinite loops
- **Max 50K tokens** — budget control
- **Redis kill switch** — `POST /agent/kill` stops all agents immediately
- **Full audit trail** — every reasoning step logged to Kafka `agent.trace`

### GitOps with ArgoCD

Two ArgoCD applications auto-sync from this repo:
- `ai-data` → `manifests/ai-data/`
- `ai-platform` → `manifests/ai-platform/`

Changes pushed to `main` are applied automatically.

### Policy Enforcement (Kyverno)

6 policies in Audit mode:
- Require resource limits on all containers
- Disallow `:latest` image tags
- Require `app` label
- Disallow running as root
- Auto-add default labels (mutating)
- Require readiness probes

View violations: `kubectl get policyreport -A`

### HPA Autoscaling

RAG service scales 1-5 replicas based on CPU (70%) and memory (80%).

```bash
kubectl get hpa -n ai-platform -w
```

## Kafka Topics

```
Document Pipeline:
  document.uploaded       → raw document received
  document.canonical      → normalized by normalizer
  document.canonical-dlt  → failed validation (dead letter)
  document.chunked        → split into passages
  document.embedded       → vectors created
  document.indexed        → stored in Qdrant

Supply Chain:
  supply-chain.raw        → raw ERP/IoT events
  supply-chain.canonical  → normalized events
  supply-chain.canonical-dlt → failed validation

Agent Audit:
  agent.trace             → every reasoning step
  agent.action            → actions taken
  agent.cost              → token spend per session

Query:
  query.log               → all queries with latency/tokens
```

## API Reference

### Ingest a document

```bash
curl -X POST http://api.raj-ai-lab.localhost:8080/ingest \
  -H "Content-Type: application/json" \
  -d '{"source": "doc.txt", "text": "Your content...", "tenant_id": "acme-corp"}'
```

### Query (RAG)

```bash
curl -X POST http://api.raj-ai-lab.localhost:8080/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the torque spec for Model X?", "tenant_id": "acme-corp"}'
```

### Run an agent

```bash
curl -X POST http://agent.raj-ai-lab.localhost:8080/agent/run \
  -H "Content-Type: application/json" \
  -d '{"question": "SKF-32210 bearings delayed 5 days. What should we do?", "tenant_id": "acme-corp"}'
```

### Kill switch

```bash
curl -X POST http://agent.raj-ai-lab.localhost:8080/agent/kill    # stop all agents
curl -X POST http://agent.raj-ai-lab.localhost:8080/agent/resume  # resume
```

## Cluster Management

```bash
k3d cluster stop raj-ai-lab     # stop (preserves data)
k3d cluster start raj-ai-lab    # start
k3d cluster delete raj-ai-lab   # delete everything

# Full rebuild from scratch
cd terraform && terraform destroy && terraform apply
```

## CI/CD

GitHub Actions workflow (`.github/workflows/ci.yaml`):
1. **Lint** — ruff + yamllint
2. **Build** — 8 Docker images
3. **Push** — to ghcr.io/raj-ssf/raj-ai-lab/*
4. **Test** — validation checks

## Tech Stack

| Category | Technology |
|----------|-----------|
| **Orchestration** | k3d (k3s in Docker) |
| **Event Streaming** | Apache Kafka (KRaft mode) |
| **Vector Database** | Qdrant |
| **Knowledge Graph** | Neo4j |
| **Object Storage** | MinIO (S3-compatible) |
| **Relational DB** | PostgreSQL |
| **Cache** | Redis |
| **LLM Inference** | Ollama (llama3.2 3B + deepseek-r1 8B) |
| **Embedding Model** | all-MiniLM-L6-v2 (384 dimensions, offline) |
| **Agent Framework** | LangGraph |
| **Web UI** | Gradio |
| **API Framework** | FastAPI + Uvicorn |
| **GitOps** | ArgoCD |
| **Monitoring** | Prometheus + Grafana |
| **LLM Observability** | Langfuse |
| **Policy Engine** | Kyverno |
| **IaC** | Terraform |
| **CI/CD** | GitHub Actions |
| **Ingress** | Nginx Ingress Controller |
| **Language** | Python 3.12 |
