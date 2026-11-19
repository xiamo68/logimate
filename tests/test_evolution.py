from types import SimpleNamespace

import pytest

from logimate.evolution import propose_patch, propose_skill
from logimate.rules import evaluate, generate_patch, evolve_skill, INITIAL_SKILL


def test_skill_learns_to_remove_incidental_support_field():
    feedback = {"feedback_id": "f1", "corrected_classification": "awaiting_handoff",
                "features": {"handoff": False, "tracking": "label_created", "has_support_case": True}}
    base = generate_patch(INITIAL_SKILL, [feedback])
    unseen = {**feedback["features"], "has_support_case": False}
    assert evaluate(base, feedback["features"], "pickup_overdue") == "awaiting_handoff"
    assert evaluate(base, unseen, "pickup_overdue") == "pickup_overdue"
    improved = evolve_skill(INITIAL_SKILL, [feedback], [
        {"features": unseen, "expected": "awaiting_handoff", "candidate": "pickup_overdue"},
    ])
    assert evaluate(generate_patch(improved, [feedback]), unseen, "pickup_overdue") == "awaiting_handoff"


def test_model_cannot_replace_verified_classification():
    async def invent_rule(instructions, body):
        rule = body["verified_candidates"][0]
        return {"rules": [{**rule, "classification": "delivery_disputed"}]}

    feedback = [{"feedback_id": "f1", "corrected_classification": "awaiting_handoff",
                 "features": {"handoff": False, "tracking": "label_created", "has_support_case": True}}]
    with pytest.raises(ValueError, match="verified feedback"):
        propose_patch(INITIAL_SKILL, feedback, SimpleNamespace(complete_json=invent_rule))


def test_skill_decision_retries_invalid_response():
    responses = iter([{"fields": []}, {"decision": "adopt"}])

    async def complete(instructions, body, **options):
        return next(responses)

    feedback = [{"corrected_classification": "awaiting_handoff",
                 "features": {"handoff": False, "tracking": "label_created", "has_support_case": True}}]
    failures = [{"expected": "awaiting_handoff", "candidate": "pickup_overdue",
                 "features": {**feedback[0]["features"], "has_support_case": False}}]
    assert propose_skill(INITIAL_SKILL, feedback, failures, SimpleNamespace(complete_json=complete)) == [
        "handoff", "tracking",
    ]
