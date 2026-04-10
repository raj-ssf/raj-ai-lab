"""
Evaluation CronJob — runs benchmark queries against the RAG pipeline,
scores quality, stores results in PostgreSQL, publishes metrics to Kafka.
"""
import json
import os
import time
from datetime import datetime, timezone

import httpx

from kafka_config import kafka_kwargs

RAG_URL = os.environ.get("RAG_URL", "http://rag-service.ai-platform:8000")
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka.ai-data:9092")
PG_URL = os.environ["PG_URL"]

# Benchmark dataset — known questions with expected keywords in answers
BENCHMARKS = [
    {
        "question": "What is the torque spec for Model X bearing?",
        "expected_keywords": ["85", "nm", "torque", "bearing"],
        "source_expected": "model-x-maintenance-manual.pdf",
    },
    {
        "question": "What is vLLM?",
        "expected_keywords": ["serving", "engine", "gpu", "model"],
        "source_expected": "raj-prep-guide.md",
    },
    {
        "question": "What is PagedAttention?",
        "expected_keywords": ["memory", "kv cache", "gpu", "pages"],
        "source_expected": "raj-prep-guide.md",
    },
    {
        "question": "How does a RAG pipeline work?",
        "expected_keywords": ["retrieval", "augmented", "generation", "vector", "embed"],
        "source_expected": None,
    },
    {
        "question": "What is a StatefulSet used for?",
        "expected_keywords": ["stateful", "persistent", "storage", "identity", "database"],
        "source_expected": "raj-prep-guide.md",
    },
]

def now():
    return datetime.now(timezone.utc).isoformat()

def score_answer(answer, expected_keywords):
    """Score answer quality based on keyword presence (0.0 - 1.0)"""
    if not answer:
        return 0.0
    answer_lower = answer.lower()
    matches = sum(1 for kw in expected_keywords if kw.lower() in answer_lower)
    return round(matches / len(expected_keywords), 2) if expected_keywords else 0.0

def score_source(sources, expected_source):
    """Score if the correct source was retrieved (0.0 or 1.0)"""
    if not expected_source:
        return 1.0  # no source expectation
    for s in sources:
        if expected_source.lower() in s.get("source", "").lower():
            return 1.0
    return 0.0

def wait_for_rag():
    """Wait for RAG service to be reachable"""
    for i in range(10):
        try:
            r = httpx.get(f"{RAG_URL}/health", timeout=5)
            if r.status_code == 200:
                print("[eval] RAG service ready")
                return True
        except:
            pass
        print(f"[eval] Waiting for RAG service... ({i+1}/10)")
        time.sleep(5)
    print("[eval] RAG service not reachable after 50s")
    return False

def run_evaluation():
    print(f"[eval] Starting evaluation at {now()}")
    print(f"[eval] RAG endpoint: {RAG_URL}")
    print(f"[eval] Benchmarks: {len(BENCHMARKS)}")
    print()

    if not wait_for_rag():
        print("[eval] ABORTED — RAG service unavailable")
        return

    results = []
    total_answer_score = 0
    total_source_score = 0
    total_latency = 0

    for i, bench in enumerate(BENCHMARKS):
        question = bench["question"]
        print(f"[eval] [{i+1}/{len(BENCHMARKS)}] {question}")

        start = time.time()
        try:
            r = httpx.post(f"{RAG_URL}/query", json={"question": question}, timeout=120)
            data = r.json()
            answer = data.get("answer", "")
            sources = data.get("sources", [])
            cached = data.get("cached", False)
            latency_ms = int((time.time() - start) * 1000)
        except Exception as e:
            print(f"[eval]   ERROR: {e}")
            results.append({
                "question": question,
                "answer_score": 0,
                "source_score": 0,
                "latency_ms": 0,
                "error": str(e)
            })
            continue

        a_score = score_answer(answer, bench["expected_keywords"])
        s_score = score_source(sources, bench["source_expected"])

        total_answer_score += a_score
        total_source_score += s_score
        total_latency += latency_ms

        result = {
            "question": question,
            "answer_preview": answer[:100],
            "answer_score": a_score,
            "source_score": s_score,
            "latency_ms": latency_ms,
            "cached": cached,
            "sources": [s.get("source", "") for s in sources],
        }
        results.append(result)

        print(f"[eval]   Answer score: {a_score} | Source score: {s_score} | Latency: {latency_ms}ms | Cached: {cached}")

    # Summary
    n = len(BENCHMARKS)
    avg_answer = round(total_answer_score / n, 3) if n else 0
    avg_source = round(total_source_score / n, 3) if n else 0
    avg_latency = int(total_latency / n) if n else 0

    summary = {
        "timestamp": now(),
        "benchmarks_run": n,
        "avg_answer_score": avg_answer,
        "avg_source_score": avg_source,
        "avg_latency_ms": avg_latency,
        "results": results,
    }

    print()
    print("[eval] ═══════════════════════════════════")
    print("[eval]  EVALUATION SUMMARY")
    print(f"[eval]  Benchmarks:       {n}")
    print(f"[eval]  Avg answer score: {avg_answer} (0-1)")
    print(f"[eval]  Avg source score: {avg_source} (0-1)")
    print(f"[eval]  Avg latency:      {avg_latency}ms")
    print("[eval] ═══════════════════════════════════")

    # Publish to Kafka
    try:
        from kafka import KafkaProducer
        producer = KafkaProducer(
            **kafka_kwargs(),
            value_serializer=lambda v: json.dumps(v).encode("utf-8")
        )
        producer.send("query.log", {
            "type": "evaluation",
            "summary": {
                "avg_answer_score": avg_answer,
                "avg_source_score": avg_source,
                "avg_latency_ms": avg_latency,
                "benchmarks_run": n,
            },
            "timestamp": now()
        })
        producer.flush()
        print("[eval] Published to Kafka: query.log")
    except Exception as e:
        print(f"[eval] Kafka publish failed: {e}")

    # Store in PostgreSQL
    try:
        import psycopg2
        conn = psycopg2.connect(PG_URL)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS evaluation_results (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ DEFAULT NOW(),
                benchmarks_run INTEGER,
                avg_answer_score FLOAT,
                avg_source_score FLOAT,
                avg_latency_ms INTEGER,
                details JSONB
            )
        """)
        cur.execute("""
            INSERT INTO evaluation_results (benchmarks_run, avg_answer_score, avg_source_score, avg_latency_ms, details)
            VALUES (%s, %s, %s, %s, %s)
        """, (n, avg_answer, avg_source, avg_latency, json.dumps(results)))
        conn.commit()
        cur.close()
        conn.close()
        print("[eval] Stored in PostgreSQL: evaluation_results")
    except Exception as e:
        print(f"[eval] PostgreSQL store failed: {e}")

    # Alert if quality drops
    if avg_answer < 0.3:
        print(f"[eval] WARNING: Answer quality dropped below 0.3 ({avg_answer})")
    if avg_source < 0.5:
        print(f"[eval] WARNING: Source retrieval dropped below 0.5 ({avg_source})")

    print(f"[eval] Done at {now()}")

if __name__ == "__main__":
    run_evaluation()
