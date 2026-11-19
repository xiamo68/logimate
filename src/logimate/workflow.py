import asyncio
from contextvars import ContextVar
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
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import store
from .grounded import CLAUSE_NAMES, answer_question, assess_order_policy
from .mcp_service import ToolResult, signer
from .rules import generated_match, initial_diagnosis, matchable_features

log = logging.getLogger(__name__)
ORDER_ID = re.compile(r"(?<![\w.-])LM-[A-Za-z0-9][A-Za-z0-9-]{0,55}(?![A-Za-z0-9.-])", re.ASCII | re.IGNORECASE)
resume_deadline = ContextVar("resume_deadline", default=None)


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["logistics", "aftersales"]


Domain = Literal["warehouse", "logistics", "aftersales"]
Goal = Literal["order_status", "warehouse_status", "carrier_status", "support_cases", "diagnosis", "policy"]
PLAN_REQUIREMENTS: dict[Goal, set[Domain]] = {
    "order_status": set(), "warehouse_status": {"warehouse"}, "carrier_status": {"logistics"},
    "support_cases": {"aftersales"}, "diagnosis": {"warehouse", "logistics", "aftersales"},
    "policy": {"warehouse", "logistics"},
}


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: Goal
    sources: list[Domain] = Field(max_length=3)

    @model_validator(mode="after")
    def verify_sources(self):
        selected = set(self.sources)
        required = PLAN_REQUIREMENTS[self.goal]
        if len(selected) != len(self.sources) or not required <= selected:
            raise ValueError("A query plan requires distinct sources and all evidence prerequisites")
        return self


class GraphState(TypedDict, total=False):
    question: str
    rules: list[dict]
    memory: dict
    scope: dict
    intent: str
    model_failed: bool
    order_id: str | None
    package_ids: list[str]
    allowed: bool
    deadline: float
    query_plan: dict
    trace: Annotated[list[dict], add]
    responses: Annotated[dict, or_]
    evidence: Annotated[list[dict], add]
    policy_only: bool
    ambiguous: bool
    result: dict


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class DeepSeek:
    def __init__(self, model, key, *, transport=None, request_limit=None):
        if not model or not key:
            raise ValueError("DeepSeek requires DEEPSEEK_MODEL and DEEPSEEK_API_KEY")
        self.model, self.key = model, key
        self.transport = transport
        self.request_limit = request_limit
        self.usage = {"attempts": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_prompt_tokens": 0}

    async def identify(self, question):
        return Intent.model_validate(await self.complete_json(
            'Classify the supplied question. Return JSON with only kind, either "aftersales" '
            'for refunds/claims or "logistics".', question, max_tokens=100,
        ))

    async def plan_order(self, question, intent, prior_context):
        plan = QueryPlan.model_validate(await self.complete_json(
            'Plan a read-only order investigation. Return only a JSON object, for example '
            '{"goal":"order_status","sources":[]}. The goal must be exactly one key from allowed_goals. '
            'Never translate, expand, or invent a goal key. Include all sources listed for that goal. '
            'Order lookup is automatic. For explicit order status use order_status; warehouse only use '
            'warehouse_status; carrier tracking only use carrier_status; existing support tickets only use support_cases. '
            'For eligibility, refunds or compensation policy use exactly {"goal":"policy",'
            '"sources":["warehouse","logistics"]}. For a bare order number, delays, causes, ambiguous or cross-domain '
            'questions use diagnosis. If intent is aftersales use policy or support_cases, '
            'or diagnosis if asked to investigate a cross-domain exception together with existing tickets. '
            'Previous context is reference data. Do not output identifiers, tool arguments or other keys.',
            {"question": question, "intent": intent, "prior_context": prior_context,
             "allowed_goals": {goal: sorted(sources) for goal, sources in PLAN_REQUIREMENTS.items()}},
            max_tokens=160,
        ))
        if intent == "aftersales" and plan.goal not in ("policy", "support_cases", "diagnosis"):
            raise ValueError("An aftersales query requires a policy or support case plan")
        return plan

    async def complete_json(self, instructions, input_data, max_tokens=1500):
        content = input_data if isinstance(input_data, str) else json.dumps(input_data, ensure_ascii=False)
        if self.request_limit is not None:
            if self.usage["attempts"] >= self.request_limit:
                raise ValueError("DeepSeek request limit reached")
            if len((instructions + content).encode("utf-8")) > 8192:
                raise ValueError("Evaluation prompt exceeds its declared budget")
        self.usage["attempts"] += 1
        async with httpx.AsyncClient(timeout=15, trust_env=False, transport=self.transport) as client:
            response = await client.post("https://api.deepseek.com/chat/completions",
                                         headers={"Authorization": f"Bearer {self.key}"}, json={
                "model": self.model, "response_format": {"type": "json_object"},
                "thinking": {"type": "disabled"}, "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": content},
                ],
            })
            response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("DeepSeek returned an invalid response envelope")
        stats = data.get("usage") or {}
        for name, field in (("prompt_tokens", "prompt_tokens"), ("completion_tokens", "completion_tokens"),
                            ("cached_prompt_tokens", "prompt_cache_hit_tokens")):
            self.usage[name] += stats.get(field) or 0
        return json.loads(data["choices"][0]["message"]["content"])


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


def event(trace, node, started_at, start, *, status="ok", call_id=None, tool=None, attempt=None, result=None,
          model_call=False):
    trace.append({
        "sequence": len(trace) + 1, "node_id": node,
        "kind": "model" if model_call else ("tool" if tool else "node"), "status": status,
        "started_at": started_at, "duration_ms": round((monotonic() - start) * 1000),
        "tool_call_id": call_id, "tool_name": tool, "attempt": attempt,
        "evidence_ids": [item.evidence_id for item in result.evidence] if result else [],
        "error_code": "model_call_failed" if model_call and status == "error" else (
            result.status if result and result.status != "ok" else None
        ),
    })


def build_workflow(invoke, model=None, attempt_timeout=1.5, total_timeout=8, checkpointer=None, retrieve=None,
                   interrupt_after=None):
    async def call_model(trace, name, operation):
        started_at, start = utc_now(), monotonic()
        try:
            value = await operation
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("Model %s failed (%s)", name, type(exc).__name__)
            value = None
        failed = value is None or (isinstance(value, dict) and value.get("status") == "failed")
        event(trace, name, started_at, start, status="error" if failed else "ok", model_call=True)
        return None if failed else value

    def traced(name, function):
        async def step(state):
            trace = []
            started_at, start = utc_now(), monotonic()
            update = await function(state, trace)
            skipped = name in PLAN_REQUIREMENTS["diagnosis"] and not update
            event(trace, name, started_at, start, status="skipped" if skipped else "ok")
            return {**update, "trace": trace}

        return step

    async def intent(state, trace):
        question = state["question"]
        identifiers = set(ORDER_ID.findall(question))
        kind = "aftersales" if any(word in question for word in (
            "售后", "退款", "退货", "赔付", "赔偿", "工单", "延保", "签收争议", "签收却没收到",
        )) else "logistics"
        if not identifiers and any(word in question for word in ("这个订单", "这单", "该订单", "再查", "还有", "它的")):
            previous = state.get("memory", {}).get("last_order")
            if previous:
                identifiers.add(previous)
        if model and (len(identifiers) == 1 or kind == "aftersales"):
            classified = await call_model(trace, "model.intent", model.identify(question))
            if classified is None:
                return {"intent": kind, "order_id": None, "ambiguous": False, "model_failed": True}
            kind = classified.kind
        return {"intent": kind, "order_id": next(iter(identifiers)) if len(identifiers) == 1 else None,
                "ambiguous": len(identifiers) > 1}

    async def guard(state, trace):
        if state.get("model_failed"):
            return {"allowed": False, "policy_only": False}
        scope = state.get("scope") or {}
        authorized = all(scope.get(key) for key in ("tenant_id", "user_id", "data_version", "as_of"))
        return {"allowed": bool(state["order_id"] and authorized),
                "policy_only": bool(authorized and retrieve and not state["order_id"]
                                    and not state["ambiguous"] and state["intent"] == "aftersales")}

    async def plan(state, trace):
        goal = "policy" if state["intent"] == "aftersales" else "diagnosis"
        selected = None
        if model:
            memory = state.get("memory", {})
            context = memory.get("summary", []) if state["order_id"] == memory.get("last_order") else []
            selected = await call_model(trace, "model.plan", model.plan_order(
                state["question"], state["intent"], context,
            ))
        sources = selected.sources if selected else ["warehouse", "logistics", "aftersales"]
        query_plan = {"goal": selected.goal if selected else goal, "sources": sources,
                      "origin": "model" if selected else "fallback" if model else "offline"}
        return {"deadline": monotonic() + total_timeout, "responses": {}, "evidence": [],
                "package_ids": [], "query_plan": query_plan}

    async def call(state, trace, node, tool, arguments):
        call_id = uuid4().hex
        answer = ToolResult(status="timeout")
        for attempt in (1, 2):
            remaining = min(attempt_timeout, (resume_deadline.get() or state["deadline"]) - monotonic())
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
        return {"responses": {"order": answer.model_dump()}, "evidence": [item.model_dump() for item in answer.evidence],
                "package_ids": (answer.data or {}).get("package_ids", [])[:8]}

    def domain(name, tool, args):
        async def run(state, trace):
            if state["responses"]["order"]["status"] != "ok":
                return {}
            if state.get("query_plan") and name not in state["query_plan"]["sources"]:
                return {}
            answers = [await call(state, trace, name, tool, arguments) for arguments in args(state)]
            return {"responses": {name: [answer.model_dump() for answer in answers]},
                    "evidence": [item.model_dump() for answer in answers for item in answer.evidence]}

        return run

    async def conclusion(state, trace):
        if state.get("model_failed"):
            return {}
        if not state.get("allowed") and not state.get("policy_only"):
            return {"result": result("needs_clarification", "请提供一个明确的订单编号。", missing=["订单编号"])}
        raw = state.get("responses", {})
        query_plan = state.get("query_plan")
        responses = {name: [ToolResult.model_validate(item) for item in entries]
                     for name, entries in raw.items() if name != "order"}
        order_result = ToolResult.model_validate(raw["order"]) if "order" in raw else None
        if order_result and order_result.status != "ok":
            missing = "订单记录" if order_result.status == "not_found" else "订单来源暂不可用"
            payload = result("partial", "当前无法确认该订单。", missing=[missing])
            if query_plan:
                payload["query_plan"] = query_plan
            return {"result": payload}
        facts, missing, diagnoses = [], [], []
        for evidence in state.get("evidence", []):
            for field in evidence["fields"]:
                if field["value"] is not None:
                    facts.append({"text": f"{evidence['source']} · {field['name']} = {field['value']}",
                                  "evidence_ids": [evidence["evidence_id"]]})
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
        policy = []
        retrieval_mode = None
        needs_policy = query_plan["goal"] == "policy" if query_plan else state["intent"] == "aftersales"
        policy_decision = None
        if needs_policy:
            if retrieve:
                retrieval_mode, chunks = await retrieve(state["question"], state["scope"])
                answer = None
                if chunks:
                    case = {"question": state["question"]}
                    named = {title for title in CLAUSE_NAMES if title in state["question"]}
                    if not model and not named and any(word in state["question"] for word in ("没出库", "未出库", "未发货")):
                        named = {"发货前退款"}
                    selected_clauses = [chunk for chunk in chunks if chunk["title_path"][-1] in named]
                    if len(named) == 1 and selected_clauses:
                        chunks = selected_clauses
                        case["asked_clause"] = next(iter(named))
                    if order_result:
                        case["order_facts"] = {
                            "order_status": order_result.data["status"],
                            "warehouse_events": sorted({event["event_type"] for item in warehouse if item.status == "ok"
                                                        for event in item.data["events"]}),
                            "warehouse_available": any(item.status == "ok" for item in warehouse),
                            "carrier_statuses": [item.data["status"] for item in responses.get("logistics", [])
                                                 if item.status == "ok"],
                        }
                    if model or "asked_clause" in case:
                        lookup = answer_question(case, chunks, model, guarded=True)
                        answer = await call_model(trace, "model.aftersales", lookup) if model else await lookup
                        if answer is None:
                            missing.append("模型政策判定")
                        selected = set(answer["evidence_ids"]) if answer and answer["status"] == "answered" else set()
                        chunks = [chunk for chunk in chunks if chunk["id"] in selected]
                for chunk in chunks:
                    scope = state["scope"]
                    evidence_id = store.hash_identifier(scope["tenant_id"], scope["run_id"], chunk["id"])
                    policy.append({"evidence_id": evidence_id, "source": "policy", "resource_id": chunk["document_id"],
                                   "locator": " / ".join(chunk["title_path"]), "observed_at": scope["as_of"], "fields": [],
                                   "policy": {**{key: chunk[key] for key in ("document_id", "version", "title_path",
                                                                             "excerpt", "effective_at")},
                                              "chunk_id": chunk["id"]}})
                    facts.append({"text": chunk["excerpt"], "evidence_ids": [evidence_id]})
                if policy and order_result and answer:
                    policy_decision = assess_order_policy(
                        answer["claim"], order_result, warehouse, responses.get("logistics", []),
                        state["scope"]["as_of"], [item["evidence_id"] for item in policy],
                    )
                    missing.extend(policy_decision["missing_information"])
            if not policy:
                missing.append("适用的售后政策条款")
        if order_result and not state.get("package_ids") and (not query_plan or "logistics" in query_plan["sources"]):
            missing.append("包裹映射")
        summary = "请核对引用的业务证据和政策条款。"
        if policy_decision:
            summary = " ".join(check["detail"] for check in policy_decision["conditions"])
        payload = result("partial" if missing else "answered", summary, facts, diagnoses, missing)
        payload["retrieval_mode"] = retrieval_mode
        if query_plan:
            payload["query_plan"] = query_plan
        if policy_decision:
            payload["policy_decision"] = policy_decision
        for preference in state.get("memory", {}).get("preferences", []):
            payload["next_steps"].append(f"处理偏好：{preference}")
        return {"result": payload, "evidence": policy}

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
    return graph.compile(checkpointer=checkpointer, interrupt_after=interrupt_after)


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
