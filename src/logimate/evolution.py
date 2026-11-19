import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from uuid import uuid4

from sqlalchemy import and_, insert, or_, select, update

from . import store
from .rules import INITIAL_SKILL, evaluate, evolve_skill, generate_patch, initial_diagnosis, matchable_features, validate_rules
from .seed import fingerprint
from .workflow import utc_now

log = logging.getLogger(__name__)
DETAIL_FIELDS = ("version", "parent_version_id", "baseline_release_id", "submitted_by", "created_at",
                 "changes", "source_feedback_ids", "reviewed_by", "reviewed_report_id", "review_reason")
CRITICAL = {"normal", "needs_verification"}


def query_payload(connection, table, tenant, **filters):
    return connection.scalar(select(table.c.payload).filter_by(tenant_id=tenant, **filters))


def version_payload(connection, tenant, version_id):
    if version_id == "ver_initial":
        return {"version": {"version_id": version_id, "kind": "initial", "state": "released",
                            "skill_version_id": "skill_initial", "rule_version_id": "rules_initial",
                            "content_hash": fingerprint([[], INITIAL_SKILL])},
                "parent_version_id": None, "baseline_release_id": None, "submitted_by": "system",
                "created_at": "2026-11-19T00:00:00Z", "changes": [], "source_feedback_ids": [],
                "reviewed_by": None, "reviewed_report_id": None, "review_reason": None,
                "rules": [], "skill": INITIAL_SKILL, "generation_parent": None}
    return query_payload(connection, store.versions, tenant, version_id=version_id)


def utc_after(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def claim_job(engine):
    now = utc_now()
    table = store.jobs
    available = or_(table.c.status == "queued", and_(table.c.status == "running", table.c.lease_until < now))
    with engine.begin() as connection:
        row = connection.execute(select(table).where(available).order_by(
            table.c.payload["created_at"].as_string(), table.c.job_id,
        ).limit(1)).mappings().one_or_none()
        if not row:
            return None
        token = uuid4().hex
        result = connection.execute(update(table).where(table.c.job_id == row["job_id"], available).values(
            status="running", lease_token=token, lease_until=utc_after(120),
        ))
        return {**row, "lease_token": token} if result.rowcount == 1 else None


class LeaseLost(Exception):
    pass


def update_job(connection, job, **values):
    updated = connection.execute(update(store.jobs).where(
        store.jobs.c.job_id == job["job_id"], store.jobs.c.lease_token == job["lease_token"],
        store.jobs.c.status == "running", store.jobs.c.lease_until >= utc_now(),
    ).values(lease_until=utc_after(120), **values))
    if updated.rowcount != 1:
        raise LeaseLost(job["job_id"])


def load_validation(connection, path, tenant, data_version):
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    cases = json.loads((path / "validation" / "logistics.json").read_text(encoding="utf-8"))
    if manifest["data_version"] != data_version or manifest["datasets"]["validation"]["sha256"] != fingerprint(cases):
        raise ValueError("Validation data does not match its version or fingerprint")
    read = lambda table: list(connection.execute(select(table).filter_by(
        tenant_id=tenant, data_version=data_version,
    )).mappings())
    orders = {row["order_id"]: row["payload"] for row in read(store.orders)}
    packages = {row["payload"]["order_id"]: row["payload"] for row in read(store.packages)}
    events = {}
    for source, table in (("warehouse", store.warehouse_events), ("tracking", store.tracking_events),
                          ("support", store.aftersales_cases)):
        for row in read(table):
            event = row["payload"]
            events.setdefault((event["order_id"], source), []).append(event)
    output = []
    for case in cases:
        if case["tenant_id"] != tenant:
            continue
        order_id = case["order_id"]
        as_of = manifest["as_of"]
        warehouse = events.get((order_id, "warehouse"), [])
        tracking = sorted(events.get((order_id, "tracking"), []), key=lambda event: event["occurred_at"])
        available = packages[order_id]["tracking_available"]
        features = matchable_features(
            any(event["event_type"] == "handed_off" for event in warehouse),
            (tracking[-1]["event_type"] if tracking else "no_events") if available else None,
            len(events.get((order_id, "support"), [])),
        )
        fallback = initial_diagnosis(tracking, orders[order_id]["promised_by"], as_of)[0] if available else "needs_verification"
        output.append({**case, "features": features, "fallback": fallback})
    if not output:
        raise ValueError("No validation cases for tenant")
    return output


def get_verified(connection, tenant, ids, data_version):
    feedback = [query_payload(connection, store.feedback, tenant, feedback_id=item, status="verified") for item in ids]
    if not feedback or not all(item and item["data_version"] == data_version for item in feedback):
        raise ValueError("Feedback must be verified for the same tenant and dataset")
    return feedback


def propose_patch(skill, feedback, model):
    allowed = generate_patch(skill, feedback)
    if model is None:
        return allowed
    response = asyncio.run(model.complete_json(
        'Return JSON {"rules":[...]} with the production rules you propose from verified candidates. '
        'Copy chosen objects verbatim. Select only supplied rule objects. Keep useful non-conflicting rules.',
        {"skill": skill, "verified_candidates": allowed},
    ))
    chosen = validate_rules(response["rules"])
    if any(rule not in allowed for rule in chosen):
        raise ValueError("Generated rules do not match their verified feedback")
    return chosen


def propose_skill(old, feedback, examples, model):
    allowed = evolve_skill(old, feedback, examples)
    if model is None or old == allowed:
        return allowed
    response = asyncio.run(model.complete_json(
        'Return JSON {"fields":[...]}. Review validation false negatives and choose either the current '
        'feature list or the proposed feature list. Keep handoff and tracking. Drop incidental support-case '
        'presence only if it prevented a valid correction from matching.',
        {"current": old, "proposed": allowed, "replay": examples},
    ))
    fields = response["fields"]
    if fields not in (allowed, old):
        raise ValueError("Invalid generated skill")
    return fields


def candidate_plan(connection, job, cases, model):
    stored = job["payload"]
    tenant, kind = job["tenant_id"], stored["kind"]
    model_id = model.model if model else None
    active, active_rules = store.active_bundle(connection, tenant)
    active_payload = version_payload(connection, tenant, active["version_id"])
    fixed_rules, fixed_skill_id = None, None
    if kind == "base":
        parent = active_payload
        feedback = get_verified(connection, tenant, stored["request"]["feedback_ids"], stored["data_version"])
        skill = list(parent["skill"])
        generated = validate_rules([*propose_patch(skill, feedback, model), *parent["rules"]])
    else:
        if kind == "replay":
            existing = version_payload(connection, tenant, stored["request"]["version_id"])
            if not existing or existing["version"]["state"] in ("draft", "replaying", "released"):
                raise ValueError("Replay requires a finished, unpublished candidate")
            parent = version_payload(connection, tenant, existing["generation_parent"])
            feedback = get_verified(connection, tenant, existing["source_feedback_ids"], stored["data_version"])
            skill, generated = existing["skill"], existing["rules"]
            model_id = existing["model_id"]
            kind = existing["version"]["kind"]
            fixed_rules, fixed_skill_id = existing["fixed_rules"], existing["fixed_version_id"]
        else:
            history = connection.execute(select(store.versions.c.payload).filter_by(tenant_id=tenant)
                                         .order_by(store.versions.c.payload["created_at"].as_string().desc()))
            previous = next((item for item in history.scalars()
                             if item["version"]["kind"] == "base" and item["data_version"] == stored["data_version"]
                             and item["model_id"] == model_id and item["version"]["state"] not in ("draft", "replaying")), None)
            if not previous:
                raise ValueError("Meta evolution requires a completed base candidate")
            parent = version_payload(connection, tenant, previous["generation_parent"])
            feedback = get_verified(connection, tenant, previous["source_feedback_ids"], stored["data_version"])
            fixed_rules = previous["rules"]
            fixed_skill_id = previous["version"]["version_id"]
            historical_examples = [
                {**case, "candidate": evaluate(fixed_rules, case["features"], case["fallback"])} for case in cases
            ]
            skill = propose_skill(previous["skill"], feedback, historical_examples, model)
            generated = validate_rules([*propose_patch(skill, feedback, model), *parent["rules"]])
    content_hash = fingerprint([generated, skill])
    identifier = "ver_" + job["job_id"]
    return {"version": {"version_id": identifier, "kind": kind, "state": "draft",
                        "rule_version_id": "r_" + fingerprint(generated)[:32], "skill_version_id": "s_" + fingerprint(skill)[:32],
                        "content_hash": content_hash},
            "rules": generated, "skill": skill, "baseline_rules": active_rules,
            "fixed_rules": fixed_rules, "fixed_version_id": fixed_skill_id,
            "parent_version_id": active["version_id"], "generation_parent": parent["version"]["version_id"],
            "baseline_release_id": active["release_id"], "submitted_by": stored["submitted_by"], "created_at": stored["created_at"],
            "changes": ["规则条件字段 " + "、".join(skill), f"规则数 {len(generated)}"],
            "source_feedback_ids": [row["feedback_id"] for row in feedback], "reviewed_by": None,
            "reviewed_report_id": None, "review_reason": None, "data_version": stored["data_version"],
            "model_id": model_id, "validation_hash": fingerprint(cases),
            "config_hash": fingerprint([cases, active_rules, generated, fixed_rules, skill])}


def report(plan, comparison, outcomes, baseline_key):
    baseline_errors = sum(case[baseline_key] != case["expected"] for case in outcomes)
    candidate_errors = sum(case["candidate"] != case["expected"] for case in outcomes)
    critical = sum(case["expected"] in CRITICAL and case[baseline_key] == case["expected"]
                   and case["candidate"] != case["expected"] for case in outcomes)
    return {
        "report_id": uuid4().hex, "comparison": comparison, "candidate_version_id": plan["version"]["version_id"],
        "baseline_version_id": plan["fixed_version_id"] if baseline_key == "fixed" else plan["parent_version_id"],
        "baseline_release_id": None if baseline_key == "fixed" else plan["baseline_release_id"],
        "candidate_content_hash": plan["version"]["content_hash"], "data_version": plan["data_version"],
        "config_hash": plan["config_hash"], "sample_count": len(outcomes), "baseline_error_count": baseline_errors,
        "candidate_error_count": candidate_errors, "added_critical_errors": critical,
        "passed": candidate_errors < baseline_errors and critical == 0,
        "mode": "deepseek" if plan["model_id"] else "demo", "model_id": plan["model_id"],
        "examples": [{"case_id": case["case_id"], "expected": case["expected"],
                      "baseline": case[baseline_key], "candidate": case["candidate"], "evidence_ids": []}
                     for case in outcomes],
        "created_at": utc_now(),
    }


def run_one(engine, dataset_dir=Path(".logimate/datasets"), model=None):
    job = claim_job(engine)
    if job is None:
        return False
    payload = job["payload"]
    try:
        with engine.begin() as connection:
            cases = load_validation(connection, dataset_dir, job["tenant_id"], payload["data_version"])
            plan = payload.get("plan")
            if plan is None:
                plan = candidate_plan(connection, job, cases, model)
                plan["version"]["state"] = "replaying"
                version_id = plan["version"]["version_id"]
                payload = {**payload, "plan": plan, "total_cases": len(cases), "version_id": version_id}
                update_job(connection, job, payload=payload)
                connection.execute(insert(store.versions).values(
                    tenant_id=job["tenant_id"], version_id=version_id, state="replaying", payload=plan,
                ))
            elif plan["validation_hash"] != fingerprint(cases):
                raise ValueError("Validation data changed after replay started")
            prior = dict(connection.execute(select(store.replay_cases.c.case_id, store.replay_cases.c.payload)
                                            .filter_by(job_id=job["job_id"])).tuples().all())
        outcomes = []
        for case in cases:
            outcome = prior.get(case["case_id"])
            if outcome is None:
                outcome = {**case, "baseline": evaluate(plan["baseline_rules"], case["features"], case["fallback"]),
                           "candidate": evaluate(plan["rules"], case["features"], case["fallback"]),
                           "fixed": evaluate(plan["fixed_rules"], case["features"], case["fallback"])
                           if plan["fixed_rules"] is not None else None}
                with engine.begin() as connection:
                    payload = {**payload, "processed_cases": len(prior) + 1}
                    update_job(connection, job, payload=payload)
                    connection.execute(insert(store.replay_cases).values(job_id=job["job_id"],
                                       case_id=case["case_id"], payload=outcome))
                prior[case["case_id"]] = outcome
            outcomes.append(outcome)
        reports = [report(plan, "active_baseline", outcomes, "baseline")]
        if plan["fixed_rules"] is not None:
            reports.append(report(plan, "skill_ablation", outcomes, "fixed"))
        state = "passed" if all(item["passed"] for item in reports) else "failed"
        plan["version"]["state"] = state
        version_id = plan["version"]["version_id"]
        with engine.begin() as connection:
            update_job(connection, job, status="completed", payload={
                **payload, "version_id": version_id, "report_ids": [item["report_id"] for item in reports],
            })
            connection.execute(update(store.versions).where(store.versions.c.version_id == version_id)
                               .values(state=state, payload=plan))
            connection.execute(insert(store.reports), [
                {"report_id": item["report_id"], "tenant_id": job["tenant_id"], "version_id": version_id, "payload": item}
                for item in reports
            ])
    except LeaseLost:
        log.warning("Lease lost for evolution job %s", job["job_id"])
    except Exception:
        log.exception("Evolution job %s failed", job["job_id"])
        try:
            with engine.begin() as connection:
                update_job(connection, job, status="failed", payload={
                    **payload, "error": {"code": "evolution_failed", "message": "候选生成或回放失败。", "request_id": uuid4().hex},
                })
                if payload.get("plan"):
                    candidate = payload["plan"]
                    candidate["version"]["state"] = "failed"
                    connection.execute(update(store.versions).where(
                        store.versions.c.version_id == candidate["version"]["version_id"],
                    ).values(state="failed", payload=candidate))
        except LeaseLost:
            log.warning("Lease lost for evolution job %s", job["job_id"])
    return True
