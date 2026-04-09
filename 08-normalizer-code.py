"""
Normalizer Service — consumes document.uploaded, validates, publishes document.canonical
This is the EDBL equivalent from Myriad.
"""
import json
import os
import uuid
from datetime import datetime, timezone

from kafka import KafkaConsumer, KafkaProducer

from kafka_config import kafka_kwargs

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka.ai-data:9092")

producer = KafkaProducer(
    **kafka_kwargs(),
    value_serializer=lambda v: json.dumps(v).encode("utf-8")
)

def now():
    return datetime.now(timezone.utc).isoformat()

def publish(topic, message):
    producer.send(topic, message)
    producer.flush()

def validate_document(doc):
    """Schema validation — reject bad data to DLT"""
    errors = []
    if not doc.get("text"):
        errors.append("missing 'text' field")
    if not doc.get("source"):
        errors.append("missing 'source' field")
    if doc.get("text") and len(doc["text"]) < 10:
        errors.append("text too short (minimum 10 chars)")
    return errors

def normalize_document(doc):
    """Normalize to canonical format — like Myriad EDBL → FHIR canonical"""
    return {
        "document": {
            "id": doc.get("doc_id", f"doc-{uuid.uuid4().hex[:12]}"),
            "title": doc.get("source", "unknown").replace("-", " ").replace("_", " ").title(),
            "source": doc.get("source", "unknown"),
            "version": 1,
            "identifiers": [
                {"system": "raj/doc_id", "value": doc.get("doc_id", "unknown")},
                {"system": "source/filename", "value": doc.get("source", "unknown")}
            ],
            "text": doc["text"],
            "text_length": len(doc["text"]),
            "language": detect_language(doc["text"]),
            "normalized_at": now()
        },
        "tenant_id": doc.get("tenant_id", "default"),
        "eventRevision": 1
    }

def detect_language(text):
    """Simple language detection"""
    spanish_words = ["que", "como", "esta", "por", "las", "los", "una", "para", "con", "del"]
    words = text.lower().split()[:50]
    spanish_count = sum(1 for w in words if w in spanish_words)
    return "es" if spanish_count > 3 else "en"

def consume_loop():
    print(f"[normalizer] Starting consumer on {KAFKA_BOOTSTRAP}")
    consumer = KafkaConsumer(
        "document.uploaded",
        **kafka_kwargs(),
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        group_id="normalizer-group",
        auto_offset_reset="earliest"
    )

    print("[normalizer] Listening on document.uploaded...")
    for message in consumer:
        doc = message.value
        print(f"[normalizer] Received: {doc.get('doc_id', 'unknown')} from {doc.get('source', '?')}")

        # Validate
        errors = validate_document(doc)
        if errors:
            print(f"[normalizer] VALIDATION FAILED: {errors}")
            publish("document.canonical-dlt", {
                "original": doc,
                "errors": errors,
                "timestamp": now()
            })
            continue

        # Normalize to canonical
        canonical = normalize_document(doc)
        publish("document.canonical", canonical)
        print(f"[normalizer] Published canonical for {canonical['document']['id']} (lang: {canonical['document']['language']})")

if __name__ == "__main__":
    consume_loop()
