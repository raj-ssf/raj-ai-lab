#!/usr/bin/env bash
# cleanup.sh — wipe all stateful stores in the lab so a fresh scenario can run.
# Targets: Qdrant, Redis, Neo4j, Kafka topics, MinIO, Langfuse PostgreSQL.
# Safe to re-run. No re-seed; pair with demo/seed-supply-chain.py afterwards.

set -euo pipefail

NS_DATA=ai-data
NS_PLATFORM=ai-platform

step() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }
ok()   { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[0;33m!\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------------
step "Qdrant — delete all collections"
kubectl exec -n "$NS_PLATFORM" deploy/rag-service -- python3 - <<'PY'
import os, urllib.request, json
url = os.environ["QDRANT_URL"]
key = os.environ["QDRANT_API_KEY"]
req = urllib.request.Request(f"{url}/collections", headers={"api-key": key})
data = json.loads(urllib.request.urlopen(req).read())
for c in data["result"]["collections"]:
    name = c["name"]
    print(f"  deleting {name}")
    r = urllib.request.Request(f"{url}/collections/{name}", method="DELETE", headers={"api-key": key})
    urllib.request.urlopen(r).read()
print("  qdrant clean")
PY
ok "Qdrant cleaned"

# ---------------------------------------------------------------------------
step "Redis — FLUSHALL"
kubectl exec -n "$NS_DATA" deploy/redis -- sh -c 'redis-cli -a "$REDIS_PASSWORD" FLUSHALL' 2>&1 | tail -1
ok "Redis flushed"

# ---------------------------------------------------------------------------
step "Neo4j — DETACH DELETE all nodes"
kubectl exec -n "$NS_DATA" neo4j-0 -- sh -c '
PASS=$(echo "$NEO4J_AUTH" | cut -d/ -f2)
cypher-shell -u neo4j -p "$PASS" "MATCH (n) DETACH DELETE n;"
cypher-shell -u neo4j -p "$PASS" "MATCH (n) RETURN count(n) AS remaining;"
'
ok "Neo4j wiped"

# ---------------------------------------------------------------------------
step "Kafka — recreate topics via Strimzi KafkaTopic CRs"
# Deleting the KafkaTopic CR triggers the topic-operator to delete the actual
# Kafka topic (it owns the strimzi.io/topic-operator finalizer). Re-applying
# creates fresh empty topics. This is the only reliable path because the broker
# enforces ACLs on the plain listener and CLI tools from inside the pod can't
# authenticate.
TOPICS_FILE="$(dirname "$0")/../manifests/ai-data/05-kafka-topics.yaml"
if [[ ! -f "$TOPICS_FILE" ]]; then
  warn "topics manifest not found at $TOPICS_FILE — skipping"
else
  kubectl delete -f "$TOPICS_FILE" --wait=true --timeout=120s 2>&1 | tail -5 || true
  kubectl apply -f "$TOPICS_FILE" 2>&1 | tail -5
  echo "  waiting for topics to be ready..."
  for _ in $(seq 1 30); do
    READY=$(kubectl get kafkatopics -n "$NS_DATA" --no-headers 2>/dev/null | awk '{print $5}' | grep -c True || true)
    TOTAL=$(kubectl get kafkatopics -n "$NS_DATA" --no-headers 2>/dev/null | wc -l | tr -d ' ')
    if [[ "$READY" == "$TOTAL" && "$TOTAL" -gt 0 ]]; then
      ok "Kafka topics recreated ($READY/$TOTAL ready)"
      break
    fi
    sleep 2
  done
fi

# ---------------------------------------------------------------------------
step "MinIO — empty raj-documents and qdrant-snapshots buckets"
kubectl exec -n "$NS_DATA" minio-0 -- sh -c '
mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1
for bucket in raj-documents qdrant-snapshots; do
  if mc ls local/"$bucket" >/dev/null 2>&1; then
    mc rm --recursive --force local/"$bucket" 2>&1 | tail -3 || true
  fi
done
echo "  raj-documents:" $(mc ls local/raj-documents 2>/dev/null | wc -l) "items"
echo "  qdrant-snapshots:" $(mc ls local/qdrant-snapshots 2>/dev/null | wc -l) "items"
'
ok "MinIO cleaned"

# ---------------------------------------------------------------------------
step "Langfuse — truncate traces, observations, scores, events"
kubectl exec -n "$NS_DATA" postgresql-0 -- sh -c '
PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d ai_metadata <<SQL
TRUNCATE TABLE observations, traces, scores, events, observation_media, trace_media RESTART IDENTITY CASCADE;
SELECT (SELECT count(*) FROM traces) AS traces, (SELECT count(*) FROM observations) AS observations;
SQL
'
ok "Langfuse cleaned"

# ---------------------------------------------------------------------------
step "Restart pipeline pods so they reconnect to fresh topics"
kubectl rollout restart -n "$NS_PLATFORM" \
  deploy/normalizer deploy/chunker deploy/embedder deploy/graph-updater \
  deploy/rag-service deploy/agent-orchestrator >/dev/null
ok "Restart triggered"

printf '\n\033[1;32mAll stores cleaned. Ready for the next scenario.\033[0m\n'
printf 'Next: python3 demo/seed-supply-chain.py http://localhost:8000\n'
