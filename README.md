# Raj AI Lab

A full-stack AI platform running on Kubernetes — from document ingestion to agentic AI, all on your laptop.

Built with real production patterns: Kafka event streaming, microservice data pipeline, RAG with vector search, LangGraph agents with guardrails, and GitOps deployment.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        RAJ AI LAB                                 │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  INGESTION (Kafka microservice pipeline)                     │ │
│  │                                                              │ │
│  │  User/API → RAG Service → Kafka: document.uploaded           │ │
│  │    → Normalizer → Kafka: document.canonical                  │ │
│  │      → Chunker → Kafka: document.chunked                    │ │
│  │        → Embedder → Qdrant (vectors) + Kafka: document.indexed│ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  RAG QUERY PIPELINE                                          │ │
│  │                                                              │ │
│  │  Question → Embed → Search Qdrant → Augment Prompt → LLM    │ │
│  │    → Answer with sources + cached in Redis                   │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  AGENTIC AI (LangGraph)                                      │ │
│  │                                                              │ │
│  │  Question → Reason (LLM) → Act (tools) → Observe → Loop     │ │
│  │    Tools: RAG search, inventory check, supplier lookup       │ │
│  │    Guardrails: max steps, token budget, Redis kill switch    │ │
│  │    Audit: every step logged to Kafka agent.trace             │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  OBSERVABILITY                                               │ │
│  │                                                              │ │
│  │  Prometheus + Grafana (infrastructure metrics)               │ │
│  │  Langfuse (LLM tracing)                                     │ │
│  │  Kafka UI (event inspection)                                 │ │
│  │  ArgoCD (GitOps deployment)                                  │ │
│  └─────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

## Components

### Data Layer (`ai-data` namespace)

| Component | Type | Purpose |
|-----------|------|---------|
| **Kafka** | StatefulSet | Event streaming backbone — 13 topics for document pipeline, agent traces, query logs |
| **PostgreSQL** | StatefulSet | Metadata storage, Langfuse backend |
| **Qdrant** | StatefulSet | Vector database for semantic search (384-dim, cosine similarity) |
| **Redis** | Deployment | Query cache (1hr TTL), agent state, kill switch |
| **Kafka UI** | Deployment | Web UI to browse topics and messages |

### AI Platform (`ai-platform` namespace)

| Component | Type | Purpose |
|-----------|------|---------|
| **RAG Service** | Deployment | API gateway — ingest documents, query with RAG, logs to Kafka |
| **Embedding Service** | Deployment | Converts text to 384-dim vectors using all-MiniLM-L6-v2 (runs offline) |
| **Normalizer** | Deployment | Kafka consumer — validates and normalizes raw documents to canonical format |
| **Chunker** | Deployment | Kafka consumer — splits canonical documents into passages |
| **Embedder** | Deployment | Kafka consumer — embeds chunks, stores vectors in Qdrant |
| **Agent Orchestrator** | Deployment | LangGraph-based agentic AI with tools, guardrails, and kill switch |
| **RAG UI** | Deployment | Gradio web UI — query, ingest text, upload files (PDF support) |
| **Langfuse** | Deployment | LLM observability — traces, latency, token tracking |

### Infrastructure

| Component | Namespace | Purpose |
|-----------|-----------|---------|
| **ArgoCD** | `argocd` | GitOps — syncs manifests from git to cluster |
| **Prometheus** | `monitoring` | Metrics collection from all services |
| **Grafana** | `monitoring` | Dashboards for Kubernetes and AI metrics |
| **Alertmanager** | `monitoring` | Alert routing |

### Native (macOS)

| Component | Purpose |
|-----------|---------|
| **Ollama** | LLM inference using Apple Silicon GPU (Llama 3.2 3B) |

## Kafka Topics

```
Document Pipeline:
  document.uploaded          → raw document received
  document.canonical         → normalized by normalizer
  document.canonical-dlt     → failed validation (dead letter)
  document.chunked           → split into passages by chunker
  document.embedded          → vectors created by embedder
  document.indexed           → confirmed stored in Qdrant

Supply Chain (for agent scenarios):
  supply-chain.raw           → raw ERP/IoT events
  supply-chain.canonical     → normalized events
  supply-chain.canonical-dlt → failed validation

Agent Audit:
  agent.trace                → every agent reasoning step
  agent.action               → actions taken by agents
  agent.cost                 → token spend per session

Query:
  query.log                  → all user queries with latency/tokens
```

## Prerequisites

- **Docker Desktop** (running)
- **k3d** — `brew install k3d`
- **kubectl** — `brew install kubectl`
- **Helm** — `brew install helm`
- **Ollama** — `brew install ollama` then `ollama pull llama3.2`

## Quick Start

### 1. Create the cluster

```bash
k3d cluster create raj-ai-lab \
  --servers 1 --agents 2 \
  --port "8080:80@loadbalancer" \
  --port "8443:443@loadbalancer" \
  --k3s-arg "--disable=traefik@server:0" \
  --wait
```

### 2. Build and import images

```bash
# Build all images
docker build -f Dockerfile.embedding -t raj/embedding-service:latest .
docker build -f Dockerfile.rag -t raj/rag-service:latest .
docker build -f Dockerfile.ui -t raj/rag-ui:latest .
docker build -f Dockerfile.pipeline --build-arg SERVICE_FILE=08-normalizer-code.py -t raj/normalizer:latest .
docker build -f Dockerfile.pipeline --build-arg SERVICE_FILE=09-chunker-code.py -t raj/chunker:latest .
docker build -f Dockerfile.pipeline --build-arg SERVICE_FILE=10-embedder-code.py -t raj/embedder:latest .
docker build -f Dockerfile.agent -t raj/agent-orchestrator:latest .

# Import into k3d
k3d image import raj/embedding-service:latest raj/rag-service:latest raj/rag-ui:latest \
  raj/normalizer:latest raj/chunker:latest raj/embedder:latest raj/agent-orchestrator:latest \
  -c raj-ai-lab
```

> **Note:** The embedding service requires the all-MiniLM-L6-v2 model baked into the image. Download the model files first:
> ```bash
> mkdir -p model && cd model
> for f in config.json tokenizer.json tokenizer_config.json special_tokens_map.json vocab.txt modules.json sentence_bert_config.json model.safetensors; do
>   curl -sL "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/$f" -o "$f"
> done
> mkdir -p 1_Pooling && curl -sL "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/1_Pooling/config.json" -o 1_Pooling/config.json
> ```

### 3. Deploy the data layer

```bash
kubectl create namespace ai-data
kubectl create namespace ai-platform

kubectl apply -f manifests/01-redis.yaml
kubectl apply -f manifests/02-postgresql.yaml
kubectl apply -f manifests/03-qdrant.yaml
kubectl apply -f manifests/04-kafka.yaml
kubectl apply -f manifests/05-embedding-service.yaml
kubectl apply -f manifests/06-rag-service.yaml
```

### 4. Create Kafka topics

```bash
for topic in document.uploaded document.canonical document.canonical-dlt \
  document.chunked document.embedded document.indexed \
  supply-chain.raw supply-chain.canonical supply-chain.canonical-dlt \
  agent.trace agent.action agent.cost query.log; do
  kubectl exec -n ai-data kafka-0 -- /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 --create --topic "$topic" \
    --partitions 3 --replication-factor 1
done
```

### 5. Deploy the microservices

Deploy normalizer, chunker, embedder, UI, agent orchestrator, and Langfuse using `kubectl apply` with the manifests shown in the Components section above.

### 6. Install observability stack

```bash
# ArgoCD
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml --server-side

# Prometheus + Grafana
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --set grafana.adminPassword=raj-ai-lab \
  --wait
```

### 7. Start Ollama

```bash
ollama serve  # if not already running
```

## Access

Set up port forwards to access the UIs:

```bash
# RAG UI (ingest documents, ask questions)
kubectl port-forward svc/rag-ui -n ai-platform 7860:7860 &

# Agent API
kubectl port-forward svc/agent-orchestrator -n ai-platform 8001:8001 &

# RAG API (direct)
kubectl port-forward svc/rag-service -n ai-platform 8000:8000 &

# Kafka UI (browse topics and messages)
kubectl port-forward svc/kafka-ui -n ai-data 9091:8080 &

# ArgoCD
kubectl port-forward svc/argocd-server -n argocd 9090:443 &

# Grafana
kubectl port-forward svc/monitoring-grafana -n monitoring 3000:80 &

# Prometheus
kubectl port-forward svc/monitoring-kube-prometheus-prometheus -n monitoring 9092:9090 &

# Langfuse (LLM observability)
kubectl port-forward svc/langfuse -n ai-platform 3001:3001 &

# Qdrant Dashboard
kubectl port-forward svc/qdrant -n ai-data 6333:6333 &
```

| UI | URL | Credentials |
|----|-----|-------------|
| RAG UI | http://localhost:7860 | — |
| Kafka UI | http://localhost:9091 | — |
| ArgoCD | https://localhost:9090 | admin / `kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" \| base64 -d` |
| Grafana | http://localhost:3000 | admin / raj-ai-lab |
| Prometheus | http://localhost:9092 | — |
| Langfuse | http://localhost:3001 | — |
| Qdrant | http://localhost:6333/dashboard | — |

## Usage

### Ingest a document

**Via UI:** Open http://localhost:7860 → "Upload File" tab → upload a PDF or text file.

**Via API:**
```bash
curl -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{"source": "my-doc.txt", "text": "Your document content here..."}'
```

### Query (RAG)

**Via UI:** Open http://localhost:7860 → "Query" tab → ask a question.

**Via API:**
```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the torque spec for Model X?"}'
```

### Run an agent

```bash
curl -X POST http://localhost:8001/agent/run \
  -H "Content-Type: application/json" \
  -d '{"question": "Shipment for part BRG-7721-A is delayed 3 days. What should we do?"}'
```

### Kill switch (stop all agents)

```bash
# Activate
curl -X POST http://localhost:8001/agent/kill

# Deactivate
curl -X POST http://localhost:8001/agent/resume
```

### Inspect the data pipeline

```bash
# Watch any Kafka topic for live messages
kubectl exec -n ai-data kafka-0 -- /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server localhost:9092 --topic document.uploaded

# View all topics
kubectl exec -n ai-data kafka-0 -- /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 --list

# Check Qdrant vectors
curl http://localhost:6333/collections/raj-docs

# Check Redis cache
kubectl exec -n ai-data deployment/redis -- redis-cli KEYS "query:*"

# Watch service logs
kubectl logs -n ai-platform deployment/normalizer -f
kubectl logs -n ai-platform deployment/chunker -f
kubectl logs -n ai-platform deployment/embedder -f
```

## Data Flow

```
Document uploaded (UI or API)
  │
  ▼
RAG Service publishes → Kafka: document.uploaded
  │
  ▼
Normalizer consumes → validates schema → enriches
  → publishes → Kafka: document.canonical
  → (failures → Kafka: document.canonical-dlt)
  │
  ▼
Chunker consumes → splits text into passages
  → publishes → Kafka: document.chunked
  │
  ▼
Embedder consumes → generates vectors (all-MiniLM-L6-v2)
  → stores in Qdrant → publishes → Kafka: document.indexed
  │
  ▼
User asks a question
  │
  ▼
RAG Service: embed query → search Qdrant → augment prompt → Ollama LLM
  → answer with sources → cached in Redis → logged to Kafka: query.log
```

## Cluster Management

```bash
# Stop (preserves data)
k3d cluster stop raj-ai-lab

# Start
k3d cluster start raj-ai-lab

# Delete (destroys everything)
k3d cluster delete raj-ai-lab
```

## Tech Stack

- **Kubernetes:** k3d (k3s in Docker)
- **Event Streaming:** Apache Kafka (KRaft mode, no ZooKeeper)
- **Vector Database:** Qdrant
- **Relational Database:** PostgreSQL
- **Cache:** Redis
- **LLM Inference:** Ollama (Llama 3.2 on Apple Silicon)
- **Embedding Model:** all-MiniLM-L6-v2 (384 dimensions)
- **Agent Framework:** LangGraph
- **Web UI:** Gradio (with PDF support via PyMuPDF)
- **API Framework:** FastAPI + Uvicorn
- **GitOps:** ArgoCD
- **Monitoring:** Prometheus + Grafana
- **LLM Observability:** Langfuse
- **Language:** Python 3.12
