#!/bin/bash
# Full Supply Chain Agent Scenario Demo
# Run after: python3 demo/seed-supply-chain.py

RAG_URL="${RAG_URL:-http://localhost:8000}"
AGENT_URL="${AGENT_URL:-http://localhost:8001}"

echo "════════════════════════════════════════════════════════════"
echo "  SUPPLY CHAIN SCENARIO: Shipment Delay Crisis"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "  Situation: Acme Bearings shipment of 500 SKF-32210 bearings"
echo "  is delayed 5 days. Current inventory: 3.9 days supply."
echo "  Daily consumption: 320 bearings across 2 production lines."
echo "  Revenue at risk: \$1.68M/day if lines stop."
echo ""
echo "════════════════════════════════════════════════════════════"
echo ""

echo "Step 1: Query RAG — What's the bearing spec?"
echo "─────────────────────────────────────────────"
curl -s -X POST "$RAG_URL/query" \
  -H "Content-Type: application/json" \
  -d '{"question": "What bearing does the Model X turbine use and what is the torque spec?", "tenant_id": "acme-corp"}' | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
print(f'Model: {d.get(\"model\",\"?\")} ({d.get(\"route\",\"?\")})')
print(f'Answer: {d.get(\"answer\",\"\")[:800]}')
print()
"

echo "Step 2: Query RAG — Who are the approved suppliers?"
echo "───────────────────────────────────────────────────"
curl -s -X POST "$RAG_URL/query" \
  -H "Content-Type: application/json" \
  -d '{"question": "Which suppliers are approved for SKF-32210 bearings and what are their lead times and prices?", "tenant_id": "acme-corp"}' | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
print(f'Model: {d.get(\"model\",\"?\")} ({d.get(\"route\",\"?\")})')
print(f'Answer: {d.get(\"answer\",\"\")[:800]}')
print()
"

echo "Step 3: Query RAG — What's the production impact?"
echo "─────────────────────────────────────────────────"
curl -s -X POST "$RAG_URL/query" \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the daily bearing consumption and revenue impact if production lines stop?", "tenant_id": "acme-corp"}' | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
print(f'Model: {d.get(\"model\",\"?\")} ({d.get(\"route\",\"?\")})')
print(f'Answer: {d.get(\"answer\",\"\")[:800]}')
print()
"

echo "Step 4: Query RAG (complex) — Quality concerns?"
echo "────────────────────────────────────────────────"
curl -s -X POST "$RAG_URL/query" \
  -H "Content-Type: application/json" \
  -d '{"question": "Why were bearings from Acme quarantined? Explain the quality issue and what corrective actions were taken.", "tenant_id": "acme-corp"}' | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
print(f'Model: {d.get(\"model\",\"?\")} ({d.get(\"route\",\"?\")})')
print(f'Answer: {d.get(\"answer\",\"\")[:800]}')
print()
"

echo "Step 5: Run Agent — Full crisis resolution"
echo "───────────────────────────────────────────"
echo "(Agent will: check inventory, query Neo4j for suppliers, search docs, recommend action)"
echo ""
curl -s --max-time 300 -X POST "$AGENT_URL/agent/run" \
  -H "Content-Type: application/json" \
  -d '{"question": "URGENT: Acme Bearings shipment of 500 SKF-32210 bearings delayed 5 days. We have 3.9 days inventory at 320/day consumption. Revenue risk $1.68M/day. Find alternative suppliers, check inventory, and recommend immediate action.", "tenant_id": "acme-corp"}' | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
print(f'Session: {d.get(\"session_id\")}')
print(f'Steps:   {d.get(\"steps_taken\")}')
print(f'Tokens:  {d.get(\"total_tokens\")}')
print(f'Status:  {d.get(\"status\")}')
print(f'Latency: {d.get(\"latency_ms\")}ms')
ans = d.get('answer', '(agent did not produce final answer — check Kafka agent.trace for reasoning)')
if isinstance(ans, dict):
    print(f'Answer:  {json.dumps(ans, indent=2)}')
else:
    print(f'Answer:  {ans}')
"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  INSPECT THE DATA FLOW"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "  Kafka UI:    http://kafka.raj-ai-lab.localhost:8080 → Topics → query.log, agent.trace"
echo "  Langfuse:    http://langfuse.raj-ai-lab.localhost:8080 → Traces"
echo "  Qdrant:      http://qdrant.raj-ai-lab.localhost:8080/dashboard → collection raj-docs-acme-corp"
echo "  Neo4j:       http://neo4j.raj-ai-lab.localhost:8080 → MATCH (n)-[r]->(m) RETURN n,r,m"
echo "  MinIO:       http://minio.raj-ai-lab.localhost:8080 → bucket raj-documents → acme-corp/"
echo "  Grafana:     http://grafana.raj-ai-lab.localhost:8080 → Raj AI Lab dashboard"
echo ""
