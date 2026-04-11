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
step "Kafka — delete ALL topics then recreate from manifest"
# Delete every KafkaTopic CR (not just the file — ensures nothing is missed).
# The topic-operator finalizer deletes the actual Kafka topic before the CR is removed.
# Then re-apply the manifest to create fresh empty topics.
TOPICS_FILE="$(dirname "$0")/../manifests/ai-data/05-kafka-topics.yaml"
kubectl delete kafkatopics -n "$NS_DATA" --all --wait=true --timeout=120s 2>&1 | tail -3 || true
if [[ -f "$TOPICS_FILE" ]]; then
  kubectl apply -f "$TOPICS_FILE" 2>&1 | tail -5
fi
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
step "Qdrant dashboard — inject api-key header into ingress"
"$(dirname "$0")/patch-qdrant-header.sh"
ok "Qdrant dashboard header patched"

# ---------------------------------------------------------------------------
step "Restart pipeline pods so they reconnect to fresh topics"
kubectl rollout restart -n "$NS_PLATFORM" \
  deploy/normalizer deploy/chunker deploy/embedder deploy/graph-updater \
  deploy/rag-service deploy/agent-orchestrator >/dev/null
echo "  waiting for all pods to be ready..."
kubectl rollout status -n "$NS_PLATFORM" deploy/rag-service --timeout=120s >/dev/null 2>&1 || true
kubectl rollout status -n "$NS_PLATFORM" deploy/graph-updater --timeout=60s >/dev/null 2>&1 || true
ok "Restart complete"

# ---------------------------------------------------------------------------
# Wipe Qdrant and Neo4j AFTER restarts settle so ensure_collection / graph-updater
# don't re-create data from stale in-flight messages.
step "Qdrant — delete all collections (post-restart)"
QDRANT_KEY=$(kubectl get secret qdrant-credentials -n "$NS_DATA" -o jsonpath='{.data.api_key}' | base64 -d)
kubectl port-forward -n "$NS_DATA" pod/qdrant-0 16337:6333 >/dev/null 2>&1 &
PF_PID=$!
sleep 2
COLLECTIONS=$(curl -sf "http://127.0.0.1:16337/collections" -H "api-key: ${QDRANT_KEY}" \
  | python3 -c "import sys,json; [print(c['name']) for c in json.load(sys.stdin)['result']['collections']]" 2>/dev/null || true)
if [[ -n "$COLLECTIONS" ]]; then
  while IFS= read -r col; do
    echo "  deleting $col"
    curl -sf -X DELETE "http://127.0.0.1:16337/collections/$col" -H "api-key: ${QDRANT_KEY}" >/dev/null
  done <<< "$COLLECTIONS"
fi
kill "$PF_PID" 2>/dev/null || true
ok "Qdrant cleaned"

# ---------------------------------------------------------------------------
# NOTE: Neo4j is NOT wiped post-restart because graph-updater seeds sample data
# on startup via seed_sample_data(). A second wipe would erase that seed data.

printf '\n\033[1;32mAll stores cleaned. Ready for the next scenario.\033[0m\n'
printf 'Next: python3 demo/seed-supply-chain.py http://localhost:8000\n'
