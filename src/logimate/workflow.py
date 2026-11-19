import asyncio
from datetime import datetime, timezone
import json
import logging
from operator import add, or_
import re
from time import monotonic
from typing import Annotated, Literal, TypedDict
from uuid import uuid4

import httpx
from langgraph.graph import END, START, StateGraph
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict

from .mcp_service import Evidence, ToolResult, signer
from .rules import generated_match, initial_diagnosis, matchable_features

log = logging.getLogger(__name__)
ORDER_ID = re.compile(r"(?<![\w.-])LM-[A-Za-z0-9][A-Za-z0-9-]{0,55}(?![A-Za-z0-9.-])", re.ASCII | re.IGNORECASE)


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["logistics", "aftersales"]


class GraphState(TypedDict, total=False):
    question: str
    rules: list[dict]
    scope: dict
    intent: str
    order_id: str | None
    package_ids: list[str]
    allowed: bool
    deadline: float
    trace: Annotated[list[dict], add]
    responses: Annotated[dict, or_]
    evidence: Annotated[list[Evidence], add]
    result: dict


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class DeepSeek:
    def __init__(self, model, key, *, transport=None):
        if not model or not key:
            raise ValueError("DeepSeek requires DEEPSEEK_MODEL and DEEPSEEK_API_KEY")
        self.model, self.key = model, key
        self.transport = transport

    async def identify(self, question):
        return Intent.model_validate(await self.complete_json(
            'Classify the supplied question. Return JSON with only kind, either "aftersales" '
            'for refunds/claims or "logistics".', question, max_tokens=100,
        ))

    async def complete_json(self, instructions, input_data, max_tokens=1500):
        async with httpx.AsyncClient(timeout=15, trust_env=False, transport=self.transport) as client:
            response = await client.post("https://api.deepseek.com/chat/completions",
                                         headers={"Authorization": f"Bearer {self.key}"}, json={
                "model": self.model, "response_format": {"type": "json_object"},
                "thinking": {"type": "disabled"}, "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": input_data if isinstance(input_data, str)
                     else json.dumps(input_data, ensure_ascii=False)},
                ],
            })
            response.raise_for_status()
        return json.loads(response.json()["choices"][0]["message"]["content"])


def mcp_invoker(url, key):
    tokens = signer(key)

    async def invoke(scope, tool, arguments):
        token = tokens.dumps({**scope, "aud": "logimate-mcp", "tool": tool})
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, trust_env=False) as client:
            async with streamable_http_client(url, http_client=client, terminate_on_close=False) as (reader, writer, _):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments)
                    if result.isError or result.structuredContent is None:
                        return ToolResult(status="invalid_input")
                    return ToolResult.model_validate(result.structuredContent)

    return invoke


def event(trace, node, started_at, start, *, status="ok", call_id=None, tool=None, attempt=None, result=None):
    trace.append({
        "sequence": len(trace) + 1, "node_id": node, "kind": "tool" if tool else "node", "status": status,
        "started_at": started_at, "duration_ms": round((monotonic() - start) * 1000),
        "tool_call_id": call_id, "tool_name": tool, "attempt": attempt,
        "evidence_ids": [item.evidence_id for item in result.evidence] if result else [],
        "error_code": result.status if result and result.status != "ok" else None,
    })


def build_workflow(invoke, model=None, attempt_timeout=1.5, total_timeout=8):
    def traced(name, function):
        async def step(state):
            trace = []
            started_at, start = utc_now(), monotonic()
            update = await function(state, trace)
            event(trace, name, started_at, start)
            return {**update, "trace": trace}

        return step

    async def intent(state, trace):
        question = state["question"]
        identifiers = set(ORDER_ID.findall(question))
        kind = "aftersales" if any(word in question for word in ("售后", "退款", "赔付", "工单")) else "logistics"
        if model and len(identifiers) == 1:
            kind = (await model.identify(question)).kind
        return {"intent": kind, "order_id": next(iter(identifiers)) if len(identifiers) == 1 else None}

    async def guard(state, trace):
        scope = state.get("scope") or {}
        return {"allowed": bool(state["order_id"] and all(scope.get(key) for key in
                                               ("tenant_id", "user_id", "data_version", "as_of")))}

    async def plan(state, trace):
        return {"deadline": monotonic() + total_timeout, "responses": {}, "evidence": [], "package_ids": []}

    async def call(state, trace, node, tool, arguments):
        call_id = uuid4().hex
        answer = ToolResult(status="timeout")
        for attempt in (1, 2):
            remaining = min(attempt_timeout, state["deadline"] - monotonic())
            if remaining <= 0:
                break
            started_at, start = utc_now(), monotonic()
            try:
                async with asyncio.timeout(remaining):
                    answer = await invoke(state["scope"], tool, arguments)
            except TimeoutError:
                answer = ToolResult(status="timeout")
            except Exception as exc:
                log.warning("MCP %s failed (%s)", tool, type(exc).__name__)
                answer = ToolResult(status="unavailable")
            event(trace, node, started_at, start, status="ok" if answer.status == "ok" else "error",
                  call_id=call_id, tool=tool, attempt=attempt, result=answer)
            if answer.status not in ("timeout", "unavailable"):
                break
        return answer

    async def order(state, trace):
        answer = await call(state, trace, "order", "order.get", {"order_id": state["order_id"]})
        return {"responses": {"order": answer}, "evidence": answer.evidence,
                "package_ids": (answer.data or {}).get("package_ids", [])[:8]}

    def domain(name, tool, args):
        async def run(state, trace):
            if state["responses"]["order"].status != "ok":
                return {}
            answers = [await call(state, trace, name, tool, arguments) for arguments in args(state)]
            return {"responses": {name: answers},
                    "evidence": [item for answer in answers for item in answer.evidence]}

        return run

    async def conclusion(state, trace):
        if not state.get("allowed"):
            return {"result": result("needs_clarification", "请提供一个明确的订单编号。", missing=["订单编号"])}
        responses = state["responses"]
        order_result = responses["order"]
        if order_result.status != "ok":
            missing = "订单记录" if order_result.status == "not_found" else "订单来源暂不可用"
            return {"result": result("partial", "当前无法确认该订单。", missing=[missing])}
        facts, missing, diagnoses = [], [], []
        for evidence in state["evidence"]:
            for field in evidence.fields:
                if field["value"] is not None:
                    facts.append({"text": f"{evidence.source} · {field['name']} = {field['value']}",
                                  "evidence_ids": [evidence.evidence_id]})
        for name in ("warehouse", "logistics", "aftersales"):
            if any(answer.status != "ok" for answer in responses.get(name, [])):
                missing.append(f"{name} 来源未取得完整证据")
        warehouse = responses.get("warehouse", [])
        if warehouse and warehouse[0].status == "ok":
            for answer in responses.get("logistics", []):
                if answer.status == "ok":
                    aftersales = responses.get("aftersales", [])
                    support = aftersales[0] if aftersales and aftersales[0].status == "ok" else None
                    diagnoses.append(classify(order_result, warehouse[0], answer, support,
                                              state["scope"]["as_of"], state.get("rules", [])))
        if state["intent"] == "aftersales":
            missing.append("适用的售后政策条款")
        if not state["package_ids"]:
            missing.append("包裹映射")
        return {"result": result("partial" if missing else "answered",
                                 "已查询业务记录，请核对证据与诊断。", facts, diagnoses, missing)}

    graph = StateGraph(GraphState)
    nodes = {
        "intent": intent, "compliance": guard, "plan": plan, "order": order,
        "warehouse": domain("warehouse", "warehouse.get_events", lambda state: [{"order_id": state["order_id"]}]),
        "logistics": domain("logistics", "logistics.get_tracking",
                            lambda state: [{"package_id": package} for package in state["package_ids"]]),
        "aftersales": domain("aftersales", "aftersales.get_cases", lambda state: [{"order_id": state["order_id"]}]),
        "conclusion": conclusion,
    }
    for name, node in nodes.items():
        graph.add_node(name, traced(name, node))
    graph.add_edge(START, "intent")
    graph.add_edge("intent", "compliance")
    graph.add_conditional_edges("compliance", lambda state: "plan" if state["allowed"] else "conclusion",
                                {"plan": "plan", "conclusion": "conclusion"})
    graph.add_edge("plan", "order")
    domains = ["warehouse", "logistics", "aftersales"]
    for domain_name in domains:
        graph.add_edge("order", domain_name)
    graph.add_edge(domains, "conclusion")
    graph.add_edge("conclusion", END)
    return graph.compile()


def result(status, summary, facts=None, diagnoses=None, missing=None):
    evidence_ids = list(dict.fromkeys(evidence_id for item in [*(facts or []), *(diagnoses or [])]
                                     for evidence_id in item["evidence_ids"]))
    return {"status": status, "summary": summary, "facts": facts or [], "diagnoses": diagnoses or [],
            "missing_information": missing or [], "next_steps": ["核实缺少的证据。"] if missing else [],
            "evidence_ids": evidence_ids, "retrieval_mode": None}


def classify(order, warehouse, tracking, support, as_of, rules):
    classification, rule_id = initial_diagnosis(tracking.data["events"], order.data["promised_by"], as_of)
    features = matchable_features(
        any(event["event_type"] == "handed_off" and event["package_id"] == tracking.data["package_id"]
            for event in warehouse.data["events"]), tracking.data["status"],
        len(support.data["cases"]) if support else None,
    )
    learned = generated_match(rules, features)
    sources = [order, warehouse, tracking]
    if learned:
        classification, rule_id = learned["classification"], "feedback-" + learned["feedback_id"]
        if support:
            sources.append(support)
    return {"classification": classification, "rule_id": rule_id,
            "evidence_ids": [answer.evidence[0].evidence_id for answer in sources]}
