"""
Agent Orchestrator — LangGraph-based agentic AI service
Reason → Act → Observe → Loop with guardrails
"""
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import TypedDict

import httpx
import redis
import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from kafka import KafkaProducer
from langfuse import Langfuse
from langgraph.graph import END, StateGraph
from neo4j import GraphDatabase
from opentelemetry import trace as otel_trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from prometheus_client import Counter, Gauge, Histogram, generate_latest

from kafka_config import kafka_kwargs

# --- OpenTelemetry setup ---
resource = Resource.create({"service.name": "agent-orchestrator", "service.version": "1.0.0"})
provider = TracerProvider(resource=resource)
OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
if OTLP_ENDPOINT:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT)))
else:
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
otel_trace.set_tracer_provider(provider)
tracer = otel_trace.get_tracer("agent-orchestrator")

app = FastAPI(title="Raj Agent Orchestrator")
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()

# --- Prometheus metrics ---
AGENT_RUNS = Counter("agent_runs_total", "Total agent runs", ["status"])
AGENT_STEPS = Histogram("agent_steps_count", "Steps per agent run", buckets=[1, 2, 3, 5, 7, 10])
AGENT_LATENCY = Histogram("agent_run_duration_seconds", "Agent run latency", buckets=[1, 5, 10, 20, 30, 60])
AGENT_TOKENS = Histogram("agent_tokens_total", "Tokens per agent run", buckets=[100, 500, 1000, 5000, 10000])
TOOL_CALLS = Counter("agent_tool_calls_total", "Tool calls by tool name", ["tool"])
ACTIVE_AGENTS = Gauge("agent_active_runs", "Currently running agents")

@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return generate_latest()

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka.ai-data:9092")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.k3d.internal:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
RAG_URL = os.environ.get("RAG_URL", "http://rag-service.ai-platform:8000")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis.ai-data:6379")
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://graphdb.ai-data:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "rajailab123")
MAX_STEPS = int(os.environ.get("MAX_STEPS", "10"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "50000"))

producer = KafkaProducer(
    **kafka_kwargs(),
    value_serializer=lambda v: json.dumps(v).encode("utf-8")
)
cache = redis.from_url(REDIS_URL)

# Neo4j knowledge graph
try:
    neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    neo4j_driver.verify_connectivity()
    NEO4J_ENABLED = True
except:
    NEO4J_ENABLED = False

# Langfuse tracing
try:
    lf = Langfuse(
        public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
        secret_key=os.environ.get("LANGFUSE_SECRET_KEY", ""),
        host=os.environ.get("LANGFUSE_HOST", "http://langfuse.ai-platform:3001"),
    )
    LANGFUSE_ENABLED = True
except:
    LANGFUSE_ENABLED = False

def now():
    return datetime.now(timezone.utc).isoformat()

def publish(topic, message):
    producer.send(topic, message)
    producer.flush()


# --- Agent State ---
class AgentState(TypedDict):
    session_id: str
    question: str
    steps: list
    current_step: int
    total_tokens: int
    answer: str
    sources: list
    status: str  # "running", "completed", "failed", "killed"
    tenant_id: str


# --- Tools the agent can use ---
def tool_rag_search(question: str, tenant_id: str = "") -> dict:
    """Search the RAG pipeline for relevant documents"""
    try:
        payload = {"question": question}
        if tenant_id:
            payload["tenant_id"] = tenant_id
        r = httpx.post(f"{RAG_URL}/query", json=payload, timeout=120)
        data = r.json()
        return {
            "tool": "rag_search",
            "result": data.get("answer", "No answer"),
            "sources": data.get("sources", []),
            "cached": data.get("cached", False)
        }
    except Exception as e:
        return {"tool": "rag_search", "error": str(e)}


def tool_inventory_check(part_number: str) -> dict:
    """Check inventory levels (simulated)"""
    # Simulated inventory data
    inventory = {
        "SKF-32210": {"on_hand": 1250, "daily_usage": 320, "days_of_supply": 3.9},
        "MTR-4400-B": {"on_hand": 340, "daily_usage": 80, "days_of_supply": 4.25},
        "GKT-9900-C": {"on_hand": 500, "daily_usage": 80, "days_of_supply": 6.25},
    }
    data = inventory.get(part_number, {"on_hand": 0, "daily_usage": 0, "days_of_supply": 0, "error": "part not found"})
    return {"tool": "inventory_check", "part": part_number, "result": data}


def tool_supplier_lookup(part_number: str) -> dict:
    """Look up alternative suppliers from Neo4j knowledge graph"""
    if not NEO4J_ENABLED:
        return {"tool": "supplier_lookup", "part": part_number, "error": "Neo4j not connected", "result": []}
    try:
        with neo4j_driver.session() as session:
            result = session.run("""
                MATCH (s:Supplier)-[:SUPPLIES]->(p:Part {partNumber: $part})
                RETURN s.id as id, s.name as name
            """, part=part_number)
            suppliers = [{"id": r["id"], "name": r["name"]} for r in result]
        return {"tool": "supplier_lookup", "part": part_number, "source": "neo4j", "result": suppliers}
    except Exception as e:
        return {"tool": "supplier_lookup", "part": part_number, "error": str(e), "result": []}


def tool_graph_query(query: str) -> dict:
    """Run a natural language query against the Neo4j knowledge graph.
    Translates common questions to Cypher queries."""
    if not NEO4J_ENABLED:
        return {"tool": "graph_query", "error": "Neo4j not connected", "result": []}

    # Map natural language patterns to Cypher
    q_lower = query.lower()
    try:
        with neo4j_driver.session() as session:
            if "supplier" in q_lower and "part" in q_lower:
                # "Which suppliers supply part X?"
                part = query.split()[-1].upper() if query.split() else ""
                result = session.run("""
                    MATCH (s:Supplier)-[:SUPPLIES]->(p:Part)
                    WHERE p.partNumber CONTAINS $part OR p.description CONTAINS $part
                    RETURN s.name as supplier, p.partNumber as part, p.description as description
                """, part=part)
            elif "product" in q_lower or "affected" in q_lower:
                # "What products are affected?"
                result = session.run("""
                    MATCH (p:Part)-[:USED_IN]->(prod:Product)-[:PRODUCED_ON]->(l:Line)-[:LOCATED_AT]->(f:Facility)
                    RETURN p.partNumber as part, prod.name as product, l.name as line, f.name as facility
                """)
            elif "line" in q_lower or "production" in q_lower:
                # "Which production lines?"
                result = session.run("""
                    MATCH (l:Line)-[:LOCATED_AT]->(f:Facility)
                    OPTIONAL MATCH (prod:Product)-[:PRODUCED_ON]->(l)
                    RETURN l.name as line, f.name as facility, collect(prod.name) as products
                """)
            else:
                # General: return all relationships
                result = session.run("""
                    MATCH (a)-[r]->(b)
                    RETURN labels(a)[0] as from_type, a.name as from_name,
                           type(r) as relationship,
                           labels(b)[0] as to_type, b.name as to_name
                    LIMIT 20
                """)
            data = [dict(r) for r in result]
        return {"tool": "graph_query", "query": query, "source": "neo4j", "result": data}
    except Exception as e:
        return {"tool": "graph_query", "query": query, "error": str(e), "result": []}


TOOLS = {
    "rag_search": tool_rag_search,
    "inventory_check": tool_inventory_check,
    "supplier_lookup": tool_supplier_lookup,
    "graph_query": tool_graph_query,
}


# --- LangGraph Nodes ---
def reason_node(state: AgentState) -> AgentState:
    """LLM reasons about what to do next"""
    step_num = state["current_step"]

    # Build context from previous steps
    history = ""
    for s in state["steps"]:
        history += f"\nStep {s['step']}: Used {s['tool']} → {json.dumps(s['result'])[:200]}"

    # Force final answer after gathering enough data
    force_final = step_num >= 7

    if force_final and history:
        prompt = f"""You are a supply chain crisis response agent. Based on all the data gathered below, provide your final recommendation.

Data gathered:{history}

Question: {state['question']}

You MUST reply with this JSON:
{{"tool": "final_answer", "input": "your detailed recommendation covering: 1) immediate actions, 2) alternative suppliers, 3) production impact, 4) risk mitigation", "reasoning": "synthesizing all gathered data"}}"""
    else:
        prompt = f"""You are a supply chain crisis response agent. Answer the question by gathering information using tools.

TOOLS (use exactly one per step):
1. rag_search — search company documents. Input: a question string.
2. inventory_check — check stock levels. Input: exact part number (e.g. SKF-32210).
3. supplier_lookup — find alternative suppliers. Input: exact part number (e.g. SKF-32210).
4. graph_query — query supply chain relationships. Input: a question about suppliers, parts, or production.

STRATEGY: Start with rag_search to understand the situation, then use inventory_check and supplier_lookup for specifics. Use final_answer once you have enough info.

Previous steps:{history if history else " (none yet)"}

Question: {state['question']}

Reply with ONLY this JSON (no other text):
{{"tool": "tool_name", "input": "value", "reasoning": "why"}}

When ready to answer:
{{"tool": "final_answer", "input": "your detailed answer", "reasoning": "done"}}"""

    try:
        with tracer.start_as_current_span("agent-reason", attributes={"step": step_num, "model": OLLAMA_MODEL}):
            r = httpx.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "format": "json"},
                timeout=60
            )
        llm_data = r.json()
        response_text = llm_data.get("response", "{}")
        tokens = llm_data.get("eval_count", 0)

        try:
            decision = json.loads(response_text)
            # Normalize: LLM sometimes uses "query" or "value" instead of "input"
            if "input" not in decision:
                for key in ("query", "value", "question", "part", "part_number"):
                    if key in decision:
                        decision["input"] = decision[key]
                        break
        except:
            decision = {"tool": "final_answer", "input": response_text, "reasoning": "could not parse as JSON"}

        state["total_tokens"] += tokens

        # Log to Kafka
        publish("agent.trace", {
            "session_id": state["session_id"],
            "tenant_id": state.get("tenant_id", ""),
            "step": step_num,
            "action": "reason",
            "decision": decision,
            "tokens_used": tokens,
            "total_tokens": state["total_tokens"],
            "timestamp": now()
        })

        # Store decision for act_node
        state["steps"].append({
            "step": step_num,
            "phase": "reason",
            "tool": decision.get("tool", "unknown"),
            "input": decision.get("input", ""),
            "reasoning": decision.get("reasoning", ""),
            "result": None
        })

    except Exception as e:
        state["status"] = "failed"
        state["answer"] = f"Reasoning failed: {e}"
        publish("agent.trace", {
            "session_id": state["session_id"],
            "tenant_id": state.get("tenant_id", ""),
            "step": step_num,
            "action": "reason_error",
            "error": str(e),
            "timestamp": now()
        })

    return state


def act_node(state: AgentState) -> AgentState:
    """Execute the tool the LLM chose"""
    if not state["steps"]:
        return state

    last_step = state["steps"][-1]
    tool_name = last_step["tool"]
    tool_input = last_step["input"]

    if tool_name == "final_answer":
        # Don't accept final_answer before step 4 — force the agent to gather data first
        if state["current_step"] < 4:
            last_step["tool"] = "rag_search"
            last_step["input"] = state["question"]
            tool_name = "rag_search"
            tool_input = state["question"]
        else:
            state["answer"] = tool_input
            state["status"] = "completed"
        publish("agent.trace", {
            "session_id": state["session_id"],
            "tenant_id": state.get("tenant_id", ""),
            "step": state["current_step"],
            "action": "final_answer",
            "answer": tool_input[:200],
            "timestamp": now()
        })
        return state

    # Execute the tool
    if tool_name in TOOLS:
        TOOL_CALLS.labels(tool=tool_name).inc()
        start = time.time()
        with tracer.start_as_current_span(f"tool:{tool_name}", attributes={"tool": tool_name, "input": str(tool_input)[:200]}):
            if tool_name == "rag_search":
                result = TOOLS[tool_name](tool_input, tenant_id=state.get("tenant_id", ""))
            else:
                result = TOOLS[tool_name](tool_input)
        latency = int((time.time() - start) * 1000)

        last_step["result"] = result

        publish("agent.trace", {
            "session_id": state["session_id"],
            "tenant_id": state.get("tenant_id", ""),
            "step": state["current_step"],
            "action": f"tool:{tool_name}",
            "input": tool_input,
            "result": result,
            "latency_ms": latency,
            "timestamp": now()
        })

        publish("agent.action", {
            "session_id": state["session_id"],
            "tenant_id": state.get("tenant_id", ""),
            "step": state["current_step"],
            "tool": tool_name,
            "input": tool_input[:500],
            "success": "error" not in result,
            "latency_ms": latency,
            "timestamp": now()
        })

        # If it was a RAG search, capture sources
        if tool_name == "rag_search" and "sources" in result:
            state["sources"].extend(result.get("sources", []))
    else:
        last_step["result"] = {"error": f"Unknown tool: {tool_name}"}

    state["current_step"] += 1
    return state


def should_continue(state: AgentState) -> str:
    """Guardrail check — should the agent continue or stop?"""

    # Check kill switch
    kill = cache.get("agent:kill_switch")
    if kill and kill.decode() == "true":
        state["status"] = "killed"
        state["answer"] = "Agent killed by operator"
        publish("agent.trace", {
            "session_id": state["session_id"],
            "tenant_id": state.get("tenant_id", ""),
            "step": state["current_step"],
            "action": "killed",
            "reason": "kill_switch=true",
            "timestamp": now()
        })
        return END

    # Check if completed
    if state["status"] in ("completed", "failed", "killed"):
        return END

    # Check step limit
    if state["current_step"] >= MAX_STEPS:
        state["status"] = "failed"
        state["answer"] = f"Exceeded max steps ({MAX_STEPS})"
        publish("agent.trace", {
            "session_id": state["session_id"],
            "tenant_id": state.get("tenant_id", ""),
            "step": state["current_step"],
            "action": "guardrail_stop",
            "reason": f"max_steps={MAX_STEPS}",
            "timestamp": now()
        })
        return END

    # Check token limit
    if state["total_tokens"] >= MAX_TOKENS:
        state["status"] = "failed"
        state["answer"] = f"Exceeded token budget ({MAX_TOKENS})"
        publish("agent.trace", {
            "session_id": state["session_id"],
            "tenant_id": state.get("tenant_id", ""),
            "step": state["current_step"],
            "action": "guardrail_stop",
            "reason": f"max_tokens={MAX_TOKENS}",
            "timestamp": now()
        })
        return END

    return "reason"


# --- Build the LangGraph ---
workflow = StateGraph(AgentState)
workflow.add_node("reason", reason_node)
workflow.add_node("act", act_node)
workflow.set_entry_point("reason")
workflow.add_edge("reason", "act")
workflow.add_conditional_edges("act", should_continue)
agent = workflow.compile()


# --- API Endpoints ---
@app.get("/health")
def health():
    kill = cache.get("agent:kill_switch")
    return {
        "status": "ok",
        "kill_switch": kill.decode() if kill else "false",
        "max_steps": MAX_STEPS,
        "max_tokens": MAX_TOKENS
    }


@app.post("/agent/run")
def run_agent(req: dict):
    """Run an agent to answer a question using tools"""
    question = req.get("question", "")
    tenant_id = req.get("tenant_id", "")
    session_id = f"sess-{uuid.uuid4().hex[:8]}"
    start_time = time.time()
    ACTIVE_AGENTS.inc()

    # Start Langfuse trace for entire agent session
    trace = None
    if LANGFUSE_ENABLED:
        try:
            trace = lf.trace(name="agent-run", id=session_id, input={"question": question},
                             metadata={"max_steps": MAX_STEPS, "max_tokens": MAX_TOKENS, "model": OLLAMA_MODEL})
        except:
            pass

    publish("agent.trace", {
        "session_id": session_id,
        "tenant_id": tenant_id,
        "step": 0,
        "action": "start",
        "question": question,
        "timestamp": now()
    })

    initial_state = AgentState(
        session_id=session_id,
        question=question,
        steps=[],
        current_step=1,
        total_tokens=0,
        answer="",
        sources=[],
        status="running",
        tenant_id=tenant_id
    )

    # Run the agent graph
    final_state = agent.invoke(initial_state)

    latency_ms = int((time.time() - start_time) * 1000)

    # Log each step to Langfuse as spans
    if trace:
        try:
            for step in final_state.get("steps", []):
                tool = step.get("tool", "unknown")
                if step.get("phase") == "reason":
                    gen = trace.generation(
                        name=f"reason-step-{step.get('step',0)}",
                        model=OLLAMA_MODEL,
                        input=f"Reasoning: {step.get('reasoning', '')}",
                        output=f"Chose tool: {tool}, input: {step.get('input', '')}",
                    )
                elif step.get("result"):
                    span = trace.span(
                        name=f"tool:{tool}-step-{step.get('step',0)}",
                        input={"tool": tool, "input": step.get("input", "")},
                    )
                    span.end(output=step.get("result", {}))
        except:
            pass

    # Complete Langfuse trace
    if trace:
        try:
            trace.update(
                output={"answer": final_state.get("answer", "")[:200], "status": final_state.get("status", "")},
                metadata={"total_steps": final_state["current_step"], "total_tokens": final_state["total_tokens"],
                          "latency_ms": latency_ms, "status": final_state["status"]}
            )
            lf.flush()
        except:
            pass

    # Log completion to Kafka
    publish("agent.cost", {
        "session_id": session_id,
        "tenant_id": tenant_id,
        "model": OLLAMA_MODEL,
        "total_steps": final_state["current_step"],
        "total_tokens": final_state["total_tokens"],
        "latency_total_ms": latency_ms,
        "status": final_state["status"],
        "timestamp": now()
    })

    ACTIVE_AGENTS.dec()
    AGENT_RUNS.labels(status=final_state["status"]).inc()
    AGENT_STEPS.observe(final_state["current_step"])
    AGENT_LATENCY.observe(time.time() - start_time)
    AGENT_TOKENS.observe(final_state["total_tokens"])

    return {
        "session_id": session_id,
        "answer": final_state["answer"],
        "sources": final_state["sources"],
        "steps_taken": final_state["current_step"],
        "total_tokens": final_state["total_tokens"],
        "status": final_state["status"],
        "latency_ms": latency_ms
    }


@app.post("/agent/kill")
def kill_agent(req: dict = {}):
    """Activate kill switch — all agents stop at next checkpoint"""
    cache.set("agent:kill_switch", "true")
    publish("agent.trace", {
        "action": "kill_switch_activated",
        "activated_at": now()
    })
    return {"status": "kill switch activated"}


@app.post("/agent/resume")
def resume_agent(req: dict = {}):
    """Deactivate kill switch"""
    cache.set("agent:kill_switch", "false")
    return {"status": "kill switch deactivated"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
