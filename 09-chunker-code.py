"""
Chunker Service — consumes document.canonical, splits into chunks, publishes document.chunked
"""
import json
import os
from datetime import datetime, timezone

from kafka import KafkaConsumer, KafkaProducer

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka.ai-data:9092")

producer = KafkaProducer(
    bootstrap_servers=KAFKA_BOOTSTRAP,
    value_serializer=lambda v: json.dumps(v).encode("utf-8")
)

def now():
    return datetime.now(timezone.utc).isoformat()

def publish(topic, message):
    producer.send(topic, message)
    producer.flush()

def chunk_text(text, chunk_size=500):
    """Split by paragraphs, merge small ones"""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    # If paragraphs are too small, merge with next
    chunks = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) < chunk_size:
            current = current + "\n\n" + p if current else p
        else:
            if current:
                chunks.append(current)
            current = p
    if current:
        chunks.append(current)
    return chunks

def consume_loop():
    print(f"[chunker] Starting consumer on {KAFKA_BOOTSTRAP}")
    consumer = KafkaConsumer(
        "document.canonical",
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        group_id="chunker-group",
        auto_offset_reset="earliest"
    )

    print("[chunker] Listening on document.canonical...")
    for message in consumer:
        canonical = message.value
        doc = canonical.get("document", {})
        doc_id = doc.get("id", "unknown")
        source = doc.get("source", "unknown")
        text = doc.get("text", "")

        print(f"[chunker] Received canonical: {doc_id} ({len(text)} chars)")

        chunks = chunk_text(text)
        for i, chunk in enumerate(chunks):
            publish("document.chunked", {
                "doc_id": doc_id,
                "source": source,
                "chunk_index": i,
                "total_chunks": len(chunks),
                "text": chunk,
                "char_count": len(chunk),
                "timestamp": now()
            })

        print(f"[chunker] Published {len(chunks)} chunks for {doc_id}")

if __name__ == "__main__":
    consume_loop()
