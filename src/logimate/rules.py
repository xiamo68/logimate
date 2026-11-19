"""Constrained, auditable rule generation and execution."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

Classification = Literal["normal", "awaiting_handoff", "awaiting_pickup", "pickup_overdue",
                         "in_transit_delayed", "delivery_disputed", "needs_verification"]
Feature = Literal["handoff", "tracking", "has_support_case"]
Tracking = Literal["label_created", "picked_up", "in_transit", "delivered", "carrier_exception", "no_events"]
INITIAL_SKILL = ["handoff", "tracking", "has_support_case"]
CAUSAL_FIELDS = ("handoff", "tracking")


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Feature
    equals: Tracking | StrictBool

    @model_validator(mode="after")
    def check_field_type(self):
        if (self.field == "tracking") != isinstance(self.equals, str):
            raise ValueError("Condition value does not match its field")
        return self


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feedback_id: str
    classification: Classification
    when: list[Condition] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def check_conditions(self):
        fields = {item.field for item in self.when}
        if len(fields) != len(self.when) or not set(CAUSAL_FIELDS).issubset(fields):
            raise ValueError("Rules require unique handoff and tracking conditions")
        return self


def matchable_features(handoff, tracking, case_count):
    return {"handoff": handoff, "tracking": tracking,
            "has_support_case": case_count > 0 if case_count is not None else None}


def generated_match(rules, features):
    for rule in rules:
        if all(features.get(condition["field"]) == condition["equals"] for condition in rule["when"]):
            return rule
    return None


def evaluate(rules, features, fallback):
    match = generated_match(rules, features)
    return match["classification"] if match else fallback


def validate_rules(rules):
    validated = [Rule.model_validate(rule).model_dump() for rule in rules]
    for index, left in enumerate(validated):
        left_conditions = {condition["field"]: condition["equals"] for condition in left["when"]}
        for right in validated[index + 1:]:
            if left["classification"] == right["classification"]:
                continue
            right_conditions = {condition["field"]: condition["equals"] for condition in right["when"]}
            if all(left_conditions[field] == right_conditions[field]
                   for field in left_conditions.keys() & right_conditions.keys()):
                raise ValueError("Conflicting rule conditions")
    return validated


def generate_patch(skill, feedback):
    rules = []
    for row in feedback:
        features = row["features"]
        if any(features.get(field) is None for field in skill):
            continue
        rules.append({"feedback_id": row["feedback_id"], "classification": row["corrected_classification"],
                      "when": [{"field": field, "equals": features[field]} for field in skill]})
    return validate_rules(rules)


def evolve_skill(skill, feedback, examples):
    if "has_support_case" not in skill:
        return skill
    for example in examples:
        if example["candidate"] == example["expected"]:
            continue
        for row in feedback:
            features = row["features"]
            if (example["expected"] == row["corrected_classification"]
                    and all(example["features"].get(field) == features.get(field) for field in CAUSAL_FIELDS)
                    and example["features"].get("has_support_case") != features.get("has_support_case")):
                return [field for field in skill if field != "has_support_case"]
    return skill


def initial_diagnosis(events, promised, as_of):
    picked_up = any(item["event_type"] == "picked_up" for item in events)
    delivered = any(item["event_type"] == "delivered" for item in events)
    label = next((item["occurred_at"] for item in events if item["event_type"] == "label_created"), None)
    if not picked_up and label and (datetime.fromisoformat(as_of) - datetime.fromisoformat(label)).total_seconds() > 48 * 3600:
        return "pickup_overdue", "initial-label-clock"
    if picked_up and not delivered and promised and promised < as_of:
        return "in_transit_delayed", "initial-promise-clock"
    return ("normal" if picked_up or delivered else "awaiting_pickup"), "initial-status"
