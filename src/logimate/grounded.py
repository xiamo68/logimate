import httpx
from pydantic import BaseModel, ConfigDict
from typing import Literal

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
    try:
        response = await model.complete_json(instruction, {
            "question": case["question"], "known_action_ids": list(CLAUSE_NAMES.values()),
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
