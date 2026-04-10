import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone

import httpx
import redis
import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from kafka import KafkaProducer
from langfuse import Langfuse
from minio import Minio
from opentelemetry import trace as otel_trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from kafka_config import kafka_kwargs

# --- OpenTelemetry setup ---
resource = Resource.create({"service.name": "rag-service", "service.version": "1.0.0"})
provider = TracerProvider(resource=resource)

# Export to OTLP collector if configured, otherwise console
OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
if OTLP_ENDPOINT:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT)))
else:
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

otel_trace.set_tracer_provider(provider)
tracer = otel_trace.get_tracer("rag-service")

app = FastAPI(title="Raj RAG Service")
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()

# --- Prometheus metrics ---
QUERY_LATENCY = Histogram("rag_query_duration_seconds", "RAG query latency", ["model", "route", "tenant_id"], buckets=[0.5, 1, 2, 5, 10, 30, 60])
QUERY_COUNT = Counter("rag_query_total", "Total RAG queries", ["model", "route", "tenant_id", "cached"])
INGEST_COUNT = Counter("rag_ingest_total", "Total documents ingested", ["tenant_id"])
TOKEN_COUNT = Counter("rag_tokens_total", "LLM tokens consumed", ["model", "route"])
CACHE_HITS = Counter("rag_cache_hits_total", "Cache hits")
CACHE_MISSES = Counter("rag_cache_misses_total", "Cache misses")
EMBEDDING_LATENCY = Histogram("rag_embedding_duration_seconds", "Embedding call latency", buckets=[0.1, 0.5, 1, 2, 5])
LLM_LATENCY = Histogram("rag_llm_duration_seconds", "LLM call latency", ["model"], buckets=[1, 2, 5, 10, 30, 60])
VECTOR_SEARCH_LATENCY = Histogram("rag_vector_search_duration_seconds", "Qdrant search latency", buckets=[0.01, 0.05, 0.1, 0.5, 1])
ACTIVE_QUERIES = Gauge("rag_active_queries", "Currently running queries")

@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return generate_latest()
qdrant = QdrantClient(url=os.environ["QDRANT_URL"], api_key=os.environ.get("QDRANT_API_KEY"))
cache = redis.from_url(os.environ["REDIS_URL"])

# Langfuse tracing
try:
    lf = Langfuse(
        public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-raj-ai-lab"),
        secret_key=os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-raj-ai-lab"),
        host=os.environ.get("LANGFUSE_HOST", "http://langfuse.ai-platform:3001"),
    )
    LANGFUSE_ENABLED = True
except:
    LANGFUSE_ENABLED = False

# MinIO (S3-compatible storage)
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "minio.ai-data:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "rajailab")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "rajailab123")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "raj-documents")

try:
    s3 = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=False)
    if not s3.bucket_exists(MINIO_BUCKET):
        s3.make_bucket(MINIO_BUCKET)
    S3_ENABLED = True
except:
    S3_ENABLED = False

EMBEDDING_URL = os.environ["EMBEDDING_URL"]
OLLAMA_URL = os.environ["OLLAMA_URL"]
OLLAMA_MODEL_FAST = os.environ.get("OLLAMA_MODEL_FAST", "llama3.2:latest")
OLLAMA_MODEL_REASONING = os.environ.get("OLLAMA_MODEL_REASONING", "deepseek-r1:latest")
COLLECTION = os.environ["QDRANT_COLLECTION"]
TOP_K = int(os.environ.get("TOP_K", "3"))
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka.ai-data:9092")

# Multi-model router
COMPLEX_KEYWORDS = [
    "why", "explain", "compare", "analyze", "what if", "how would",
    "trade-off", "tradeoff", "pros and cons", "difference between",
    "design", "architect", "strategy", "evaluate", "recommend",
    "alternative", "should we", "impact", "risk", "optimize"
]

def route_model(question: str) -> tuple:
    """Route to fast model (simple Q) or reasoning model (complex Q)"""
    q_lower = question.lower()
    word_count = len(question.split())

    # Complex if: matches complex keywords, or long question, or multi-part
    is_complex = (
        any(kw in q_lower for kw in COMPLEX_KEYWORDS)
        or word_count > 25
        or "?" in question and question.count("?") > 1
    )

    if is_complex:
        return OLLAMA_MODEL_REASONING, "reasoning"
    return OLLAMA_MODEL_FAST, "fast"

producer = KafkaProducer(
    **kafka_kwargs(),
    value_serializer=lambda v: json.dumps(v).encode("utf-8")
)

# Discover embedding dimensions from embedding service
VECTOR_DIM = 384
for _i in range(30):
    try:
        _r = httpx.get(f"{EMBEDDING_URL}/health", timeout=5)
        VECTOR_DIM = _r.json().get("dimensions", 384)
        print(f"[rag] Discovered vector dimensions: {VECTOR_DIM}")
        break
    except:
        print("[rag] Waiting for embedding service...")
        time.sleep(5)

def ensure_collection(name):
    """Create or recreate collection if dimensions mismatch. Snapshots before delete, restores if available."""
    from qdrant_snapshots import restore_collection, snapshot_collection
    try:
        info = qdrant.get_collection(name)
        existing_dim = info.config.params.vectors.size
        if existing_dim != VECTOR_DIM:
            print(f"[rag] Dimension mismatch on {name}: {existing_dim} vs {VECTOR_DIM}")
            # Snapshot before deleting
            if info.points_count > 0:
                snapshot_collection(name, existing_dim)
            qdrant.delete_collection(name)
            # Try to restore from a previous snapshot for the new dimensions
            if not restore_collection(name, VECTOR_DIM):
                qdrant.create_collection(
                    collection_name=name,
                    vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE)
                )
                print(f"[rag] Created fresh {name} with dim={VECTOR_DIM}")
            else:
                print(f"[rag] Restored {name} from snapshot with dim={VECTOR_DIM}")
    except:
        # Try restore first, then create fresh
        if not restore_collection(name, VECTOR_DIM):
            qdrant.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE)
            )
            print(f"[rag] Created {name} with dim={VECTOR_DIM}")

ensure_collection(COLLECTION)

DEFAULT_TENANT = os.environ.get("DEFAULT_TENANT", "default")

def embed(text):
    with tracer.start_as_current_span("embed-text", attributes={"text.length": len(text)}):
        r = httpx.post(f"{EMBEDDING_URL}/embed", json={"inputs": text}, timeout=30)
        return r.json()[0]

def now():
    return datetime.now(timezone.utc).isoformat()

def publish(topic, message):
    producer.send(topic, message)
    producer.flush()

def get_tenant_collection(tenant_id):
    """Get or create a tenant-specific Qdrant collection, auto-recreate on dimension mismatch"""
    collection = f"raj-docs-{tenant_id}"
    ensure_collection(collection)
    return collection

@app.get("/health")
def health():
    return {
        "status": "ok", "kafka": "connected", "pipeline": "microservices", "s3": S3_ENABLED,
        "models": {"fast": OLLAMA_MODEL_FAST, "reasoning": OLLAMA_MODEL_REASONING}
    }

@app.post("/ingest")
def ingest(doc: dict):
    """Multi-tenant ingest: S3 (per-tenant path) → Kafka (with tenant_id) → pipeline.

    Flow: RAG service → MinIO (s3://{bucket}/{tenant_id}/{doc_id}/{filename})
          → Kafka: document.uploaded (with tenant_id)
          → Normalizer → Chunker → Embedder → Qdrant (tenant-specific collection)
    """
    text = doc.get("text", "")
    source = doc.get("source", "unknown")
    tenant_id = doc.get("tenant_id", DEFAULT_TENANT)
    doc_id = f"doc-{uuid.uuid4().hex[:12]}"
    s3_path = ""

    # Store in tenant-specific S3 path
    if S3_ENABLED:
        try:
            import io
            s3_key = f"{tenant_id}/{doc_id}/{source}"
            data = text.encode("utf-8")
            s3.put_object(MINIO_BUCKET, s3_key, io.BytesIO(data), len(data), content_type="text/plain")
            s3_path = f"s3://{MINIO_BUCKET}/{s3_key}"
        except Exception as e:
            s3_path = f"s3-error: {e}"

    INGEST_COUNT.labels(tenant_id=tenant_id).inc()

    # Publish to Kafka with tenant_id
    publish("document.uploaded", {
        "event_id": f"evt-{uuid.uuid4().hex[:8]}",
        "doc_id": doc_id,
        "source": source,
        "tenant_id": tenant_id,
        "text": text,
        "text_length": len(text),
        "s3_path": s3_path,
        "timestamp": now()
    })

    return {
        "status": "accepted",
        "doc_id": doc_id,
        "source": source,
        "tenant_id": tenant_id,
        "collection": f"raj-docs-{tenant_id}",
        "s3_path": s3_path,
        "message": f"Document stored for tenant '{tenant_id}' → pipeline will process into collection raj-docs-{tenant_id}."
    }

@app.post("/query")
def query(q: dict):
    """Multi-tenant RAG query with multi-model routing, Kafka logging, and Langfuse tracing"""
    question = q.get("question", "")
    tenant_id = q.get("tenant_id", DEFAULT_TENANT)
    force_model = q.get("model", None)
    query_id = f"q-{uuid.uuid4().hex[:8]}"
    start_time = time.time()

    # Get tenant-specific collection
    collection = get_tenant_collection(tenant_id)

    # Route to appropriate model
    if force_model == "fast":
        selected_model, route = OLLAMA_MODEL_FAST, "fast (forced)"
    elif force_model == "reasoning":
        selected_model, route = OLLAMA_MODEL_REASONING, "reasoning (forced)"
    else:
        selected_model, route = route_model(question)

    # Start Langfuse trace
    trace = None
    if LANGFUSE_ENABLED:
        try:
            trace = lf.trace(name="rag-query", input={"question": question, "model_route": route, "tenant_id": tenant_id},
                             id=query_id, metadata={"model": selected_model, "route": route, "tenant_id": tenant_id})
        except:
            pass

    ACTIVE_QUERIES.inc()

    # Tenant-specific cache key
    cache_key = f"query:{tenant_id}:{hashlib.md5(question.encode()).hexdigest()}"
    cached = cache.get(cache_key)
    if cached:
        CACHE_HITS.inc()
        QUERY_COUNT.labels(model=selected_model, route=route, tenant_id=tenant_id, cached="true").inc()
        QUERY_LATENCY.labels(model=selected_model, route=route, tenant_id=tenant_id).observe(time.time() - start_time)
        ACTIVE_QUERIES.dec()
        result = json.loads(cached) | {"cached": True, "query_id": query_id}
        if trace:
            try: trace.update(output={"cached": True}, metadata={"cache": "hit"})
            except: pass
        publish("query.log", {
            "query_id": query_id, "question": question,
            "cached": True, "latency_ms": int((time.time() - start_time) * 1000),
            "timestamp": now()
        })
        return result

    CACHE_MISSES.inc()

    # Embed
    embed_span = None
    if trace:
        try: embed_span = trace.span(name="embed-query", input={"text": question})
        except: pass
    embed_start = time.time()
    query_vector = embed(question)
    EMBEDDING_LATENCY.observe(time.time() - embed_start)
    if embed_span:
        try: embed_span.end(output={"dimensions": len(query_vector)})
        except: pass
    # Search tenant-specific Qdrant collection
    retrieval_span = None
    if trace:
        try: retrieval_span = trace.span(name="qdrant-search", input={"top_k": TOP_K, "collection": collection})
        except: pass
    vs_start = time.time()
    with tracer.start_as_current_span("qdrant-search", attributes={"collection": collection, "top_k": TOP_K}):
        results = qdrant.query_points(
            collection_name=collection, query=query_vector, limit=TOP_K
        ).points
    VECTOR_SEARCH_LATENCY.observe(time.time() - vs_start)

    context_parts = []
    sources = []
    for i, r in enumerate(results):
        context_parts.append(f"[{i+1}] Source: {r.payload['source']}\n{r.payload['text']}")
        sources.append({"source": r.payload["source"], "score": r.score})
    if retrieval_span:
        try: retrieval_span.end(output={"chunks_found": len(results), "top_score": sources[0]["score"] if sources else 0})
        except: pass

    context = "\n\n".join(context_parts)
    prompt = f"""Answer based on the provided context only. If the answer is not in the context, say 'I don't have that information.' Cite sources.

Context:
{context}

Question: {question}"""

    # Call LLM with Langfuse generation tracking (using routed model)
    generation = None
    if trace:
        try:
            generation = trace.generation(
                name=f"ollama-{route}",
                model=selected_model,
                input=prompt,
                metadata={"sources": len(sources), "route": route}
            )
        except: pass
    try:
        llm_start = time.time()
        with tracer.start_as_current_span("llm-generate", attributes={"model": selected_model, "route": route}) as llm_span:
            llm_response = httpx.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": selected_model, "prompt": prompt, "stream": False},
                timeout=120
            )
            llm_data = llm_response.json()
        LLM_LATENCY.labels(model=selected_model).observe(time.time() - llm_start)
        answer = llm_data.get("response", "LLM error")
        tokens_used = llm_data.get("eval_count", 0)
        prompt_tokens = llm_data.get("prompt_eval_count", 0)
        TOKEN_COUNT.labels(model=selected_model, route=route).inc(tokens_used + prompt_tokens)
        if generation:
            try: generation.end(output=answer, usage={"input": prompt_tokens, "output": tokens_used})
            except: pass
    except Exception as e:
        answer = f"LLM unavailable: {e}. Retrieved context: {context[:500]}"
        tokens_used = 0

    latency_ms = int((time.time() - start_time) * 1000)

    # Complete Langfuse trace
    if trace:
        try:
            trace.update(
                output={"answer": answer[:200], "sources": sources},
                metadata={"latency_ms": latency_ms, "tokens": tokens_used}
            )
            lf.flush()
        except: pass

    publish("query.log", {
        "query_id": query_id, "question": question,
        "tenant_id": tenant_id, "collection": collection,
        "model": selected_model, "route": route,
        "answer_length": len(answer), "sources": [s["source"] for s in sources],
        "top_score": sources[0]["score"] if sources else 0,
        "tokens_used": tokens_used, "latency_ms": latency_ms,
        "cached": False, "timestamp": now()
    })

    QUERY_COUNT.labels(model=selected_model, route=route, tenant_id=tenant_id, cached="false").inc()
    QUERY_LATENCY.labels(model=selected_model, route=route, tenant_id=tenant_id).observe(time.time() - start_time)
    ACTIVE_QUERIES.dec()

    # Get current trace ID for correlation
    span = otel_trace.get_current_span()
    trace_id = format(span.get_span_context().trace_id, '032x') if span.get_span_context().trace_id else ""

    result = {
        "answer": answer, "sources": sources, "cached": False,
        "query_id": query_id, "tenant_id": tenant_id,
        "collection": collection, "model": selected_model, "route": route,
        "trace_id": trace_id
    }
    cache.setex(cache_key, 3600, json.dumps(result))
    return result

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
