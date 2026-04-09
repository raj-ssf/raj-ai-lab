"""
Embedder Service — consumes document.chunked, generates vectors, stores in Qdrant, publishes document.indexed
"""
import json
import os
import time as _time
from datetime import datetime, timezone

import httpx
from kafka import KafkaConsumer, KafkaProducer
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka.ai-data:9092")
EMBEDDING_URL = os.environ.get("EMBEDDING_URL", "http://embedding-service.ai-platform:8080")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant.ai-data:6333")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "raj-docs")

# Discover embedding dimensions from embedding service

VECTOR_DIM = 384
for _i in range(30):
    try:
        _r = httpx.get(f"{EMBEDDING_URL}/health", timeout=5)
        VECTOR_DIM = _r.json().get("dimensions", 384)
        print(f"[embedder] Discovered vector dimensions: {VECTOR_DIM}")
        break
    except:
        print("[embedder] Waiting for embedding service...")
        _time.sleep(5)

producer = KafkaProducer(
    bootstrap_servers=KAFKA_BOOTSTRAP,
    value_serializer=lambda v: json.dumps(v).encode("utf-8")
)

qdrant = QdrantClient(url=QDRANT_URL)

# Ensure collection exists with correct dimensions
try:
    qdrant.get_collection(COLLECTION)
except:
    qdrant.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE)
    )

def now():
    return datetime.now(timezone.utc).isoformat()

def publish(topic, message):
    producer.send(topic, message)
    producer.flush()

def embed(text):
    r = httpx.post(f"{EMBEDDING_URL}/embed", json={"inputs": text}, timeout=30)
    return r.json()[0]

def consume_loop():
    print(f"[embedder] Starting consumer on {KAFKA_BOOTSTRAP}")
    consumer = KafkaConsumer(
        "document.chunked",
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        group_id="embedder-group",
        auto_offset_reset="earliest"
    )

    # Buffer chunks by doc_id for batch upsert
    doc_buffers = {}

    print("[embedder] Listening on document.chunked...")
    for message in consumer:
        chunk = message.value
        doc_id = chunk.get("doc_id", "unknown")
        source = chunk.get("source", "unknown")
        tenant_id = chunk.get("tenant_id", "default")
        chunk_index = chunk.get("chunk_index", 0)
        total_chunks = chunk.get("total_chunks", 1)
        text = chunk.get("text", "")

        # Tenant-specific collection
        tenant_collection = f"raj-docs-{tenant_id}"
        try:
            qdrant.get_collection(tenant_collection)
        except:
            qdrant.create_collection(
                collection_name=tenant_collection,
                vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE)
            )

        # Generate embedding
        vector = embed(text)
        point_id = abs(hash(f"{source}-{chunk_index}")) % (2**63)

        # Store in tenant-specific Qdrant collection
        qdrant.upsert(
            collection_name=tenant_collection,
            points=[PointStruct(
                id=point_id, vector=vector,
                payload={"text": text, "source": source, "chunk_index": chunk_index, "doc_id": doc_id}
            )]
        )

        publish("document.embedded", {
            "doc_id": doc_id,
            "source": source,
            "tenant_id": tenant_id,
            "chunk_index": chunk_index,
            "vector_dimensions": len(vector),
            "collection": tenant_collection,
            "point_id": point_id,
            "timestamp": now()
        })

        print(f"[embedder] Embedded chunk {chunk_index}/{total_chunks-1} for {doc_id}")

        # Track completion
        if doc_id not in doc_buffers:
            doc_buffers[doc_id] = {"total": total_chunks, "done": 0, "source": source}
        doc_buffers[doc_id]["done"] += 1

        # If all chunks done, publish indexed event
        if doc_buffers[doc_id]["done"] >= doc_buffers[doc_id]["total"]:
            publish("document.indexed", {
                "doc_id": doc_id,
                "source": source,
                "tenant_id": tenant_id,
                "chunks_indexed": doc_buffers[doc_id]["done"],
                "collection": tenant_collection,
                "timestamp": now()
            })
            print(f"[embedder] COMPLETED indexing {doc_id}: {doc_buffers[doc_id]['done']} chunks")
            del doc_buffers[doc_id]

if __name__ == "__main__":
    consume_loop()
