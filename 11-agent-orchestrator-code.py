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
from kafka import KafkaProducer
from langfuse import Langfuse
from langgraph.graph import END, StateGraph

app = FastAPI(title="Raj Agent Orchestrator")

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka.ai-data:9092")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.k3d.internal:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
RAG_URL = os.environ.get("RAG_URL", "http://rag-service.ai-platform:8000")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis.ai-data:6379")
MAX_STEPS = int(os.environ.get("MAX_STEPS", "10"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "50000"))

producer = KafkaProducer(
    bootstrap_servers=KAFKA_BOOTSTRAP,
    value_serializer=lambda v: json.dumps(v).encode("utf-8")
)
cache = redis.from_url(REDIS_URL)

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


# --- Tools the agent can use ---
def tool_rag_search(question: str) -> dict:
    """Search the RAG pipeline for relevant documents"""
    try:
        r = httpx.post(f"{RAG_URL}/query", json={"question": question}, timeout=120)
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
        "BRG-7721-A": {"on_hand": 1250, "daily_usage": 500, "days_of_supply": 2.5},
        "MTR-4400-B": {"on_hand": 50, "daily_usage": 10, "days_of_supply": 5.0},
        "GKT-9900-C": {"on_hand": 0, "daily_usage": 25, "days_of_supply": 0},
    }
    data = inventory.get(part_number, {"on_hand": 0, "daily_usage": 0, "days_of_supply": 0, "error": "part not found"})
    return {"tool": "inventory_check", "part": part_number, "result": data}


def tool_supplier_lookup(part_number: str) -> dict:
    """Look up alternative suppliers (simulated knowledge graph query)"""
    suppliers = {
        "BRG-7721-A": [
            {"name": "GlobalBearings Inc", "lead_time_days": 2, "unit_price": 4.20},
            {"name": "PrecisionParts Ltd", "lead_time_days": 5, "unit_price": 3.80},
        ],
        "MTR-4400-B": [
            {"name": "MotorWorld", "lead_time_days": 3, "unit_price": 120.00},
        ],
    }
    data = suppliers.get(part_number, [])
    return {"tool": "supplier_lookup", "part": part_number, "result": data}


TOOLS = {
    "rag_search": tool_rag_search,
    "inventory_check": tool_inventory_check,
    "supplier_lookup": tool_supplier_lookup,
}


# --- LangGraph Nodes ---
def reason_node(state: AgentState) -> AgentState:
    """LLM reasons about what to do next"""
    step_num = state["current_step"]

    # Build context from previous steps
    history = ""
    for s in state["steps"]:
        history += f"\nStep {s['step']}: Used {s['tool']} → {json.dumps(s['result'])[:200]}"

    prompt = f"""You are a supply chain AI agent. You have these tools:
- rag_search: Search documents for information. Input: a question string.
- inventory_check: Check inventory levels. Input: a part number like BRG-7721-A.
- supplier_lookup: Find alternative suppliers. Input: a part number.

Previous steps:{history if history else " (none yet)"}

Question: {state['question']}

What tool should you use next? Reply in this exact JSON format:
{{"tool": "tool_name", "input": "input_value", "reasoning": "why you chose this"}}

If you have enough information to answer, reply:
{{"tool": "final_answer", "input": "your complete answer here", "reasoning": "why you're done"}}"""

    try:
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
        except:
            decision = {"tool": "final_answer", "input": response_text, "reasoning": "could not parse as JSON"}

        state["total_tokens"] += tokens

        # Log to Kafka
        publish("agent.trace", {
            "session_id": state["session_id"],
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
        state["answer"] = tool_input
        state["status"] = "completed"
        publish("agent.trace", {
            "session_id": state["session_id"],
            "step": state["current_step"],
            "action": "final_answer",
            "answer": tool_input[:200],
            "timestamp": now()
        })
        return state

    # Execute the tool
    if tool_name in TOOLS:
        start = time.time()
        result = TOOLS[tool_name](tool_input)
        latency = int((time.time() - start) * 1000)

        last_step["result"] = result

        publish("agent.trace", {
            "session_id": state["session_id"],
            "step": state["current_step"],
            "action": f"tool:{tool_name}",
            "input": tool_input,
            "result": result,
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
    session_id = f"sess-{uuid.uuid4().hex[:8]}"
    start_time = time.time()

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
        status="running"
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
        "model": OLLAMA_MODEL,
        "total_steps": final_state["current_step"],
        "total_tokens": final_state["total_tokens"],
        "latency_total_ms": latency_ms,
        "status": final_state["status"],
        "timestamp": now()
    })

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
