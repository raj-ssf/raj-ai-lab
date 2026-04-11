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
│  │  Grafana Tempo (traces)  OpenTelemetry (instrumentation)       │ │
│  │  ArgoCD (GitOps)         Kyverno (policy enforcement)          │ │
│  │  Kafka UI                HPA autoscaling                       │ │
│  └───────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────┘
```

## Components

### Data Layer (`ai-data` namespace)

| Component | Purpose |
|-----------|---------|
| **Kafka** | Strimzi-managed (KRaft, no ZooKeeper) — 13 topics, mTLS encryption, per-service ACLs |
| **PostgreSQL** | Metadata storage, Langfuse backend, evaluation results |
| **Qdrant** | Vector database for semantic search (384-dim, cosine similarity) |
| **Redis** | Query cache (1hr TTL), agent state, kill switch |
| **Neo4j Enterprise** | Knowledge graph — suppliers, parts, products, production lines. OIDC SSO via Keycloak (PKCE) |
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
| **Portal** | Dashboard with 13 service tiles, shared SSO cookie |

### Infrastructure

| Component | Namespace | Purpose |
|-----------|-----------|---------|
| **ArgoCD** | `argocd` | GitOps — auto-syncs manifests from this repo. Keycloak OIDC SSO |
| **Prometheus + Grafana** | `monitoring` | Metrics collection and dashboards. Grafana auto-login via Keycloak |
| **Grafana Tempo** | `monitoring` | Distributed trace backend (OTLP gRPC). Traces dashboard in Grafana |
| **Keycloak** | `auth` | OIDC identity provider — SSO for all services. Realm import with 5 clients |
| **oauth2-proxy** | `auth` | SSO gate for Kafka UI, Qdrant, Prometheus, RAG UI |
| **Vault** | `vault` | Production mode (file backend, auto-unseal CronJob, audit logging) |
| **External Secrets** | `vault` | Vault → K8s Secrets across 4 namespaces (16 ExternalSecrets) |
| **Nginx Ingress** | `ingress-nginx` | Subdomain-based routing with mkcert wildcard TLS |
| **Kyverno** | `kyverno` | Policy enforcement (resource limits, labels, probes) |
| **Ollama** | macOS native | LLM inference on Apple Silicon (llama3.2 + deepseek-r1) |
| **Local Registry** | Docker | `k3d-registry.localhost:5555` — 32 cached third-party images |

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

All UIs are accessible via subdomain-based ingress on port 8443 (HTTPS) with mkcert wildcard TLS. No port-forwarding needed. Browsers trust the certificates natively.

**Portal:** https://raj-ai-lab.localhost:8443 — 13 tiles, one-click access to everything.

**SSO:** All services use Keycloak OIDC. Login: `raj` / `rajailab`.

| UI | URL | Auth |
|----|-----|------|
| **Portal** | https://raj-ai-lab.localhost:8443 | SSO (shared cookie) |
| **RAG UI** | https://rag.raj-ai-lab.localhost:8443 | SSO (oauth2-proxy) |
| **Qdrant** | https://qdrant.raj-ai-lab.localhost:8443/dashboard | SSO (oauth2-proxy + auto api-key) |
| **Kafka UI** | https://kafka.raj-ai-lab.localhost:8443 | SSO (oauth2-proxy) |
| **Neo4j Browser** | https://neo4j.raj-ai-lab.localhost:8443 | SSO (click "Connect with SSO") |
| **MinIO Console** | https://minio.raj-ai-lab.localhost:8443 | SSO (click "Sign in with Keycloak") |
| **Langfuse** | https://langfuse.raj-ai-lab.localhost:8443 | SSO gate + own login (`raj@lab.local` / `rajailab`) |
| **Grafana** | https://grafana.raj-ai-lab.localhost:8443 | SSO (auto-login, lands on Raj AI Lab dashboard) |
| **Tempo Traces** | https://grafana.raj-ai-lab.localhost:8443/d/tempo-traces/tempo-traces | SSO (via Grafana) |
| **Prometheus** | https://prometheus.raj-ai-lab.localhost:8443 | SSO (oauth2-proxy) |
| **ArgoCD** | https://argocd.raj-ai-lab.localhost:8443 | SSO (click "Log in via Keycloak") |
| **Keycloak** | https://keycloak.raj-ai-lab.localhost:8443 | `raj` / `rajailab` |
| **Vault** | https://vault.raj-ai-lab.localhost:8443/ui/ | Token from `vault-unseal-keys` secret |
| **RAG API** | https://rag.raj-ai-lab.localhost:8443 | — (`/health`, `/query`, `/ingest`) |
| **Agent API** | https://agent.raj-ai-lab.localhost:8443 | — (`/health`, `/agent/run`) |

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
| Kafka events | https://kafka.raj-ai-lab.localhost:8443 → topics: `query.log`, `agent.trace` |
| LLM traces | https://langfuse.raj-ai-lab.localhost:8443 → Traces |
| Distributed traces | https://grafana.raj-ai-lab.localhost:8443/d/tempo-traces/tempo-traces |
| Vectors | https://qdrant.raj-ai-lab.localhost:8443/dashboard → collection `raj-docs-acme-corp` |
| Knowledge graph | https://neo4j.raj-ai-lab.localhost:8443 → `MATCH (n)-[r]->(m) RETURN n,r,m` |
| Raw documents | https://minio.raj-ai-lab.localhost:8443 → bucket `raj-documents` → `acme-corp/` |
| Metrics | https://grafana.raj-ai-lab.localhost:8443 → Raj AI Lab dashboard |

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

- **Max 15 steps** — prevents infinite loops
- **Max 100K tokens** — budget control
- **LLM tuning** — temperature=0.3, num_predict=2048, top_p=0.9
- **Redis kill switch** — `POST /agent/kill` stops all agents immediately
- **Full audit trail** — every reasoning step logged to Kafka `agent.trace` + Langfuse + Tempo

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

### Distributed Tracing (OpenTelemetry + Tempo)

End-to-end distributed tracing across the RAG and agent pipelines:

- **OpenTelemetry SDK** instrumented in rag-service and agent-orchestrator (FastAPI, httpx auto-instrumentation)
- **Grafana Tempo** receives traces via OTLP gRPC (port 4317)
- **Grafana dashboard** (`Tempo Traces`) with trace search, service map, and per-service panels
- **Service names**: `rag-service`, `agent-orchestrator` (via `OTEL_SERVICE_NAME`)
- **Portal tile**: "Tempo" links directly to the traces dashboard

Each RAG query generates a trace spanning: HTTP request → embedding → vector search → LLM call → response.

### Security

**TLS:** mkcert wildcard certificate (`*.raj-ai-lab.localhost`) — browsers trust natively.

**SSO:** Keycloak 26.2 OIDC with 5 clients (oauth2-proxy, minio, grafana, argocd, neo4j). Shared SSO cookie across all services.

**Secrets:** Zero secrets in git. Vault (production mode, file backend) → External Secrets Operator → K8s Secrets across 4 namespaces.

**Network Policies:** Default-deny ingress on all namespaces. Targeted egress rules for ai-platform (DNS, intra-namespace, ai-data ports, auth:8080, Ollama, monitoring:4317).

**Pod Security:** PSS baseline enforce, restricted warn. seccompProfile: RuntimeDefault. automountServiceAccountToken: false.

**Image Security:** Digest pinning on all third-party images. Local registry cache at `k3d-registry.localhost:5555`.

### Secrets Management (Vault + ESO)

```
vault-bootstrap-secrets (K8s Secret, manual)
  → Vault init Job seeds 12 paths
    → ExternalSecrets sync to K8s Secrets:
        auth:      keycloak-credentials, keycloak-client-secrets, oauth2-proxy-secrets
        ai-data:   neo4j, postgresql, redis, qdrant, minio credentials
        ai-platform: langfuse, qdrant, minio, redis credentials
        monitoring: grafana-credentials
```

Vault runs in production mode with file backend, auto-unseal CronJob (every 2 min), and audit logging.

### Kafka Security (Strimzi mTLS + ACLs)

Production-grade Kafka security stack:

**Stage 1 — Operator-managed:** Kafka is deployed via the **Strimzi operator** in KRaft mode (no ZooKeeper). Topics are declarative `KafkaTopic` CRs in git, managed by the Topic Operator.

**Stage 2 — mTLS encryption + authentication:** All Kafka traffic uses **TLS 1.3** on port 9093. Each service has its own X.509 client certificate generated by the Strimzi User Operator from a `KafkaUser` CR. The broker verifies clients against the `clients-ca`, and clients verify the broker against the `cluster-ca`. Cert distribution across namespaces is handled by **emberstack/reflector**.

**Stage 3 — Per-service ACLs (default deny):** Each `KafkaUser` has explicit topic permissions:

| User | Allowed Operations |
|------|-------------------|
| `rag-service` | Write `document.uploaded`, `query.log` |
| `agent-orchestrator` | Write `agent.trace`, `agent.action`, `agent.cost` |
| `normalizer` | Read `document.uploaded` → Write `document.canonical(-dlt)` |
| `chunker` | Read `document.canonical` → Write `document.chunked` |
| `embedder` | Read `document.chunked` → Write `document.embedded`, `document.indexed` |
| `graph-updater` | Read `supply-chain.canonical` |
| `evaluation` | Write `query.log` |
| `kafka-ui-admin` | Read all topics + groups (inspection only) |

Verify ACL enforcement:
```bash
# Should succeed (rag-service is allowed on document.uploaded)
kubectl exec -n ai-platform deployment/rag-service -- python3 -c \
  "from kafka_config import kafka_kwargs; from kafka import KafkaProducer; \
   p = KafkaProducer(**kafka_kwargs()); p.send('document.uploaded', b'test').get()"

# Should fail with TopicAuthorizationFailedError
kubectl exec -n ai-platform deployment/rag-service -- python3 -c \
  "from kafka_config import kafka_kwargs; from kafka import KafkaProducer; \
   p = KafkaProducer(**kafka_kwargs()); p.send('agent.trace', b'test').get()"
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
| **Distributed Tracing** | OpenTelemetry + Grafana Tempo |
| **LLM Observability** | Langfuse |
| **Identity & SSO** | Keycloak 26.2 (OIDC) + oauth2-proxy |
| **Secrets Management** | HashiCorp Vault + External Secrets Operator |
| **TLS** | mkcert (browser-trusted wildcard certificates) |
| **Policy Engine** | Kyverno |
| **Network Security** | Kubernetes NetworkPolicies (default-deny) |
| **Pod Security** | Pod Security Standards (baseline enforce) |
| **IaC** | Terraform |
| **CI/CD** | GitHub Actions |
| **Ingress** | Nginx Ingress Controller |
| **Language** | Python 3.12 |
