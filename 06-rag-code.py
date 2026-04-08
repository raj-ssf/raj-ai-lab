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
from kafka import KafkaProducer
from langfuse import Langfuse
from minio import Minio
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

app = FastAPI(title="Raj RAG Service")
qdrant = QdrantClient(url=os.environ["QDRANT_URL"])
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
    bootstrap_servers=KAFKA_BOOTSTRAP,
    value_serializer=lambda v: json.dumps(v).encode("utf-8")
)

try:
    qdrant.get_collection(COLLECTION)
except:
    qdrant.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=384, distance=Distance.COSINE)
    )

def embed(text):
    r = httpx.post(f"{EMBEDDING_URL}/embed", json={"inputs": text}, timeout=30)
    return r.json()[0]

def now():
    return datetime.now(timezone.utc).isoformat()

def publish(topic, message):
    producer.send(topic, message)
    producer.flush()

@app.get("/health")
def health():
    return {
        "status": "ok", "kafka": "connected", "pipeline": "microservices", "s3": S3_ENABLED,
        "models": {"fast": OLLAMA_MODEL_FAST, "reasoning": OLLAMA_MODEL_REASONING}
    }

@app.post("/ingest")
def ingest(doc: dict):
    """Store in S3 (MinIO) → publish to Kafka → pipeline microservices handle the rest.

    Flow: RAG service → MinIO (permanent storage)
          → Kafka: document.uploaded
          → Normalizer → Kafka: document.canonical
          → Chunker → Kafka: document.chunked
          → Embedder → Qdrant + Kafka: document.indexed
    """
    text = doc.get("text", "")
    source = doc.get("source", "unknown")
    doc_id = f"doc-{uuid.uuid4().hex[:12]}"
    s3_path = ""

    # Store original document in MinIO (S3)
    if S3_ENABLED:
        try:
            import io
            s3_key = f"{doc_id}/{source}"
            data = text.encode("utf-8")
            s3.put_object(MINIO_BUCKET, s3_key, io.BytesIO(data), len(data), content_type="text/plain")
            s3_path = f"s3://{MINIO_BUCKET}/{s3_key}"
        except Exception as e:
            s3_path = f"s3-error: {e}"

    # Publish to Kafka — pipeline does the rest
    publish("document.uploaded", {
        "event_id": f"evt-{uuid.uuid4().hex[:8]}",
        "doc_id": doc_id,
        "source": source,
        "text": text,
        "text_length": len(text),
        "s3_path": s3_path,
        "timestamp": now()
    })

    return {
        "status": "accepted",
        "doc_id": doc_id,
        "source": source,
        "s3_path": s3_path,
        "message": "Document stored in S3 → published to pipeline → Normalizer → Chunker → Embedder will process it."
    }

@app.post("/query")
def query(q: dict):
    """RAG query with multi-model routing, Kafka logging, and Langfuse tracing"""
    question = q.get("question", "")
    force_model = q.get("model", None)  # optional: override router
    query_id = f"q-{uuid.uuid4().hex[:8]}"
    start_time = time.time()

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
            trace = lf.trace(name="rag-query", input={"question": question, "model_route": route},
                             id=query_id, metadata={"model": selected_model, "route": route})
        except:
            pass

    cache_key = f"query:{hashlib.md5(question.encode()).hexdigest()}"
    cached = cache.get(cache_key)
    if cached:
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

    # Embed
    embed_span = None
    if trace:
        try: embed_span = trace.span(name="embed-query", input={"text": question})
        except: pass
    query_vector = embed(question)
    if embed_span:
        try: embed_span.end(output={"dimensions": len(query_vector)})
        except: pass
    # Search Qdrant
    retrieval_span = None
    if trace:
        try: retrieval_span = trace.span(name="qdrant-search", input={"top_k": TOP_K})
        except: pass
    results = qdrant.query_points(
        collection_name=COLLECTION, query=query_vector, limit=TOP_K
    ).points

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
        llm_response = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": selected_model, "prompt": prompt, "stream": False},
            timeout=120
        )
        llm_data = llm_response.json()
        answer = llm_data.get("response", "LLM error")
        tokens_used = llm_data.get("eval_count", 0)
        prompt_tokens = llm_data.get("prompt_eval_count", 0)
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
        "model": selected_model, "route": route,
        "answer_length": len(answer), "sources": [s["source"] for s in sources],
        "top_score": sources[0]["score"] if sources else 0,
        "tokens_used": tokens_used, "latency_ms": latency_ms,
        "cached": False, "timestamp": now()
    })

    result = {
        "answer": answer, "sources": sources, "cached": False,
        "query_id": query_id, "model": selected_model, "route": route
    }
    cache.setex(cache_key, 3600, json.dumps(result))
    return result

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
