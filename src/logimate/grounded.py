from datetime import datetime, timedelta
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict

CLAUSE_NAMES = {
    "发货前退款": "refund_before_outbound",
    "在途退款": "refund_in_transit",
    "揽收超时": "pickup_overdue_check",
    "签收争议": "delivery_dispute_check",
}


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["answered", "insufficient"]
    claim: str | None
    evidence_ids: list[str]


def supported(response, evidence):
    return bool(response["claim"] and response["evidence_ids"]
                and all(evidence.get(item) == response["claim"] for item in response["evidence_ids"]))


def force_grounded(answer, evidence):
    if answer and answer["status"] == "answered" and supported(answer, evidence):
        return answer
    return {"status": "insufficient", "claim": None, "evidence_ids": []}


async def answer_question(case, passages, model, guarded):
    candidates = {item["id"]: CLAUSE_NAMES.get(item["title_path"][-1]) for item in passages}
    candidates = {key: value for key, value in candidates.items() if value}
    if model is None:
        first = next(iter(candidates.items()), None)
        if not first:
            return {"status": "insufficient", "claim": None, "evidence_ids": []}
        return {"status": "answered", "claim": first[1], "evidence_ids": [first[0]]}
    instruction = ('Answer the Chinese policy question. Return JSON with "status" as answered or insufficient, '
                   '"claim" as an action ID or null, and "evidence_ids" as an array of passage IDs. ')
    instruction += ("Only use the passages. If none supports a relevant action, return insufficient with no claim."
                    if guarded else "Use your general judgement and the provided passages to answer.")
    context = {}
    if "order_facts" in case:
        instruction += (" Order facts are from this run. Use them to select the relevant clause when the "
                        "question is general. If the operator asks about a specific clause, identify that "
                        "clause even when conditions may not be met. Return the policy action and citations; "
                        "the application validates conditions against the records.")
        context["order_facts"] = case["order_facts"]
    if "asked_clause" in case:
        instruction += (" The question explicitly names the clause in asked_clause. If a supplied passage has "
                        "that title, return answered with its action_id and citation even when order facts "
                        "show a condition does not hold. Answered here means the requested clause was located.")
        context["asked_clause"] = case["asked_clause"]
    try:
        response = await model.complete_json(instruction, {
            "question": case["question"], "known_action_ids": list(CLAUSE_NAMES.values()), **context,
            "passages": [{"id": item["id"], "action_id": candidates.get(item["id"]),
                          "excerpt": item["excerpt"], "section": item["title_path"]} for item in passages],
        }, max_tokens=350)
        answer = Answer.model_validate(response).model_dump()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 402, 403, 429):
            raise ValueError(f"DeepSeek blocked evaluation (HTTP {exc.response.status_code})") from None
        return {"status": "failed", "error_type": f"HTTP {exc.response.status_code}"}
    except (httpx.RequestError, ValueError, KeyError, IndexError, TypeError) as exc:
        return {"status": "failed", "error_type": type(exc).__name__}
    return force_grounded(answer, candidates) if guarded else answer


def assess_order_policy(action, order, warehouse, tracking, as_of, policy_ids):
    """Apply supported policy conditions to this run's authorized business evidence."""
    wh = warehouse[0] if warehouse and warehouse[0].status == "ok" else None
    packages = set(order.data["package_ids"])
    carrier = {item.data["package_id"]: item for item in tracking if item.status == "ok"}
    carrier_complete = bool(packages) and packages <= carrier.keys()
    warehouse_events = wh.data["events"] if wh else []
    carrier_events = [event for item in carrier.values() for event in item.data["events"]]
    accepted = {"picked_up", "in_transit", "delivered"}
    sources = [order, *([wh] if wh else []), *carrier.values()]
    observed_at = datetime.fromisoformat(as_of)
    checks = []

    def check(label, outcome, detail):
        checks.append({"label": label, "status": outcome, "detail": detail})

    if action == "refund_before_outbound":
        dispatched = any(event["event_type"] in {"outbound", "handed_off"} for event in warehouse_events)
        dispatched |= any(event["event_type"] in accepted for event in carrier_events)
        if dispatched:
            check("确认订单尚未出库", "not_met", "已取得出库、实物交接或承运商揽收记录。")
        elif not wh or not carrier_complete or order.data.get("marked_shipped_at"):
            check("确认订单尚未出库", "unknown", "需要核实仓库出库状态与已发货标记，并补齐包裹轨迹。")
        else:
            check("确认订单尚未出库", "met", "仓储与包裹记录均未显示出库、交接或揽收。")
    elif action == "refund_in_transit":
        if any(event["event_type"] in accepted for event in carrier_events):
            check("确认承运商已揽收", "met", "承运商轨迹显示至少一个包裹已揽收。")
        elif carrier_complete:
            check("确认承运商已揽收", "not_met", "已取得的包裹轨迹没有揽收记录。")
        else:
            check("确认承运商已揽收", "unknown", "需要补齐承运商的包裹轨迹。")
        check("核实退回轨迹", "unknown", "请向承运商核实退回轨迹后再处理退款。")
    elif action == "pickup_overdue_check":
        overdue = []
        for package, item in carrier.items():
            handoffs = [datetime.fromisoformat(event["occurred_at"]) for event in warehouse_events
                        if event["package_id"] == package and event["event_type"] == "handed_off"]
            if (handoffs and observed_at - max(handoffs) > timedelta(hours=48)
                    and not any(event["event_type"] in accepted for event in item.data["events"])):
                overdue.append(package)
        if overdue:
            check("交接超过 48 小时且未揽收", "met", "符合条件的包裹：" + "、".join(sorted(overdue)))
        elif wh and carrier_complete:
            check("交接超过 48 小时且未揽收", "not_met", "现有交接和揽收记录未显示符合条件的包裹。")
        else:
            check("交接超过 48 小时且未揽收", "unknown", "需要补齐交接时间和承运商轨迹。")
    else:
        status = "met" if any(event["event_type"] == "delivered" for event in carrier_events) else (
            "not_met" if carrier_complete else "unknown"
        )
        check("核对承运商签收状态", status, "查询本次取得的承运商妥投轨迹。")
        check("补充签收凭证和运单号", "unknown", "请向承运商取得签收凭证和正式运单号。")

    states = {item["status"] for item in checks}
    outcome = "not_met" if "not_met" in states else "needs_evidence" if "unknown" in states else "met"
    return {
        "action": action, "status": outcome, "conditions": checks,
        "missing_information": [item["label"] for item in checks if item["status"] == "unknown"],
        "evidence_ids": list(dict.fromkeys([*policy_ids, *(item.evidence_id for source in sources
                                                           for item in source.evidence)])),
    }
