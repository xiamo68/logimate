import asyncio
from datetime import datetime, timezone
import json
from math import ceil
import os
from pathlib import Path
import subprocess
from time import monotonic
from uuid import uuid4

from .benchmark_data import load_frozen
from .grounded import CLAUSE_NAMES, answer_question, supported
from .evolution import version_payload
from .retrieval import POLICY_COLLECTION, Vectors, keyword_search, load_chunks, rrf
from .rules import generated_match, initial_diagnosis
from .seed import fingerprint



def score_answer(case, response, evidence):
    if response is None:
        return False, False
    if response["status"] == "insufficient":
        return case["expected"] is None and not response["evidence_ids"] and not response["claim"], bool(
            response["claim"] or response["evidence_ids"])
    grounded = supported(response, evidence)
    valid_refs = case["relevant"].get(case["expected"], set())
    correct = bool(grounded and response["claim"] == case["expected"]
                   and set(response["evidence_ids"]).issubset(valid_refs))
    return correct, not grounded


def code_version():
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=False)
        status = subprocess.run(["git", "status", "--porcelain"], text=True, capture_output=True, check=False)
    except OSError:
        return "installed-build"
    if result.returncode != 0:
        return "installed-build"
    return result.stdout.strip() + ("-dirty" if status.stdout.strip() else "")


def rate(numerator, denominator):
    return round(numerator / denominator, 6) if denominator else None


def percentile(values, proportion):
    return round(sorted(values)[max(0, ceil(len(values) * proportion) - 1)], 2) if values else None


def evaluate_rules(case, rules):
    if case["tracking"] is None:
        return "needs_verification", ["tracking"]
    matched = generated_match(rules, case)
    if matched:
        return matched["classification"], []
    return initial_diagnosis(case["events"], case["promised_by"], case["as_of"])[0], []


def logistics(engine, data, tenant, fixed_id, evolved_id, mode, model_id):
    with engine.connect() as connection:
        fixed = version_payload(connection, tenant, fixed_id)
        evolved = version_payload(connection, tenant, evolved_id)
    if (not fixed or not evolved or fixed["version"]["kind"] != "base" or evolved["version"]["kind"] != "meta"
            or fixed["model_id"] != model_id or evolved["model_id"] != model_id
            or fixed["data_version"] != data["data_version"] or evolved["data_version"] != data["data_version"]
            or evolved["fixed_version_id"] != fixed_id or fixed["generation_parent"] != evolved["generation_parent"]
            or fixed["source_feedback_ids"] != evolved["source_feedback_ids"]):
        raise ValueError("Use comparable fixed and evolved version IDs with the same provenance and model")
    rulesets = {"A 初始规则": [], "B 固定技能": fixed["rules"], "C 演化技能": evolved["rules"]}
    outcomes = []
    for case in data["logistics"]:
        results = {}
        for group, rules in rulesets.items():
            actual, missing = evaluate_rules(case, rules)
            results[group] = {"actual": actual, "missing": missing, "correct": actual == case["expected"]
                              and set(case["expected_missing"]).issubset(missing)}
        outcomes.append({"case_id": case["case_id"], "scenario": case["family"], "expected": case["expected"],
                         "expected_missing": case["expected_missing"], "results": results})
    groups = []
    prior_error_rate = None
    for name in rulesets:
        count = len(outcomes)
        errors = sum(not case["results"][name]["correct"] for case in outcomes)
        error_rate = errors / count
        metrics = {"error_count": errors, "error_rate": rate(errors, count)}
        if prior_error_rate is not None:
            metrics["improvement_pp_vs_previous"] = round(100 * (prior_error_rate - error_rate), 6)
            metrics["relative_improvement_vs_previous"] = rate(prior_error_rate - error_rate, prior_error_rate)
        prior_error_rate = error_rate
        groups.append({"name": name, "sample_count": count, "metrics": metrics})
    return groups, outcomes, {"rule_versions": ["rules_initial", fixed["version"]["rule_version_id"],
                                                 evolved["version"]["rule_version_id"]],
                               "skill_versions": [fixed["version"]["skill_version_id"],
                                                  evolved["version"]["skill_version_id"]],
                               "generation_usage": {"fixed": fixed.get("generation_usage", {}),
                                                    "evolved": evolved.get("generation_usage", {})},
                               "candidate_versions": [fixed_id, evolved_id], "mode": mode}



def ready_answer(answer):
    return answer if answer and answer["status"] != "failed" else None


async def score_policies(engine, data, experiment, model):
    chunks = load_chunks(engine, data["data_version"], data["as_of"])
    if not chunks:
        raise ValueError("Policy corpus is not seeded")
    vectors = Vectors.from_environment()
    by_id = {item["id"]: item for item in chunks}
    evidence = {key: CLAUSE_NAMES.get(item["title_path"][-1]) for key, item in by_id.items()}
    relevant = {claim: {key for key, value in evidence.items() if value == claim} for claim in CLAUSE_NAMES.values()}
    observations = []
    timings = {"BM25": [], "稠密": [], "混合": []}
    fallback_count = 0
    answer_names = ("无校验汇总", "证据校验汇总") if experiment == "grounding" else ("混合问答",)
    answers = {name: [] for name in answer_names}
    try:
        vectors.prepare(POLICY_COLLECTION)
        if not vectors.client.has_collection(POLICY_COLLECTION, timeout=2):
            raise ValueError("Policy vector index missing; run index-policies first")
        for raw in data["aftersales"]:
            case = {**raw, "relevant": relevant}
            async def timed(operation):
                started = monotonic()
                try:
                    return await asyncio.to_thread(operation), (monotonic() - started) * 1000
                except Exception:
                    return None, (monotonic() - started) * 1000

            started = monotonic()
            keyword_task = asyncio.create_task(timed(lambda: keyword_search(case["question"], chunks)))
            dense_task = asyncio.create_task(timed(lambda: vectors.search(
                POLICY_COLLECTION, case["question"], {"version": data["data_version"]},
            )))
            _, pending = await asyncio.wait((keyword_task, dense_task), timeout=0.8)
            completed = [task.result() if task not in pending else (None, 800.0)
                         for task in (keyword_task, dense_task)]
            for task in pending:
                task.cancel()
            (keyword, keyword_ms), (dense, dense_ms) = completed
            if dense is None:
                fallback_count += 1
            keyword, dense = keyword or [], dense or []
            hybrid = rrf(keyword, dense)
            hybrid_ms = (monotonic() - started) * 1000
            rankings = {"BM25": keyword[:5], "稠密": dense[:5], "混合": hybrid}
            for group, duration in zip(rankings, (keyword_ms, dense_ms, hybrid_ms)):
                timings[group].append(duration)
            rows = {}
            known = relevant.get(case["expected"], set())
            for name, hits in rankings.items():
                if experiment == "rag":
                    rows[name] = {"actual": ",".join(hits) or "无结果",
                                  "correct": bool(set(hits) & known) if known else None,
                                  "recall": rate(len(set(hits) & known), len(known))}
            retrieved = [by_id[key] for key in hybrid if key in by_id]
            returned_evidence = {item["id"]: evidence[item["id"]] for item in retrieved}
            for name in answer_names:
                elapsed_start = monotonic()
                answer = await answer_question(case, retrieved, model, guarded=name != "无校验汇总")
                elapsed = (monotonic() - elapsed_start) * 1000
                correct, hallucinated = score_answer(case, ready_answer(answer), returned_evidence)
                rows[name] = {"actual": answer.get("claim") or answer["status"], "correct": correct,
                              "hallucinated": hallucinated, "answer_ms": round(elapsed, 2), "answer": answer}
                answers[name].append((case["expected"], rows[name]))
            observations.append({"case_id": case["case_id"], "scenario": case["question"],
                                 "expected": case["expected"] or "insufficient", "results": rows,
                                 "evidence_ids": list(returned_evidence)})
    finally:
        vectors.close()
    count = len(observations)
    groups = []
    if experiment == "rag":
        answerable = sum(item["expected"] is not None for item in data["aftersales"])
        for group, times in timings.items():
            recalls = [item["results"][group]["recall"] for item in observations if item["results"][group]["recall"] is not None]
            groups.append({"name": group, "sample_count": count, "metrics": {
                "recall_at_5": rate(sum(recalls), answerable), "recall_denominator": answerable,
                "retrieval_p50_ms": percentile(times, 0.50), "retrieval_p95_ms": percentile(times, 0.95),
                "unanswerable_count": count - answerable,
                **({"vector_fallback_count": fallback_count} if group == "混合" else {}),
            }})
    for name, results in answers.items():
        answered = sum(row["answer"]["status"] == "answered" for _, row in results)
        eligible = sum(expected is not None for expected, _ in results)
        answered_eligible = sum(expected is not None and row["answer"]["status"] == "answered"
                                for expected, row in results)
        unsupported = sum(row["hallucinated"] for _, row in results)
        groups.append({"name": name, "sample_count": count, "metrics": {
            "accuracy": rate(sum(row["correct"] for _, row in results), count),
            "hallucination_rate": rate(unsupported, count),
            "hallucination_rate_answered": rate(unsupported, answered),
            "answered_count": answered, "coverage_answerable": rate(answered_eligible, eligible),
            "failed_count": sum(row["answer"]["status"] == "failed" for _, row in results),
            "refusal_count": sum(row["answer"]["status"] == "insufficient" for _, row in results),
            "answer_p95_ms": percentile([row["answer_ms"] for _, row in results], 0.95),
        }})
    return groups, observations, {"embedding_model": os.getenv("LOGIMATE_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"),
                                  "threshold": 0.45, "retrieval_budget_ms": 800}


def evaluate(engine, args, model=None):
    data = load_frozen(args.frozen)
    output = args.output
    if not output.resolve().is_relative_to(Path.cwd()):
        raise ValueError("Evaluation output must be inside the project to expose a relative artifact path")
    if args.kind == "logistics":
        if not args.fixed_version or not args.evolved_version:
            raise ValueError("Logistics evaluation requires fixed and evolved candidate version IDs")
        groups, cases, config = logistics(engine, data, args.tenant, args.fixed_version, args.evolved_version,
                                          args.mode, model.model if model else None)
    else:
        groups, cases, config = asyncio.run(score_policies(engine, data, args.kind, model))
    identifier = uuid4().hex
    output.mkdir(parents=True, exist_ok=True)
    cases_name = f"cases-{identifier}.json"
    version = code_version()
    made_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    config = {**config, "experiment": args.kind, "benchmark_revision": data["benchmark_revision"], "code_ref": version,
              "model_id": model.model if model else None, "mode": args.mode,
              "request_limit": model.request_limit if model else None, "answer_max_tokens": 350 if model else None}
    detail_path = output.resolve() / cases_name
    report = {"evaluation_id": identifier, "kind": args.kind, "mode": args.mode, "model_id": config["model_id"],
              "data_version": data["data_version"], "data_hash": data["sha256"], "code_ref": version,
              "config_hash": fingerprint(config), "baseline_group": groups[0]["name"], "groups": groups,
              "case_examples": [{"case_id": case["case_id"], "scenario": case["scenario"], "expected": case["expected"],
                                 "results": [{"group": group, "actual": row["actual"],
                                              "correct": bool(row["correct"])} for group, row in case["results"].items()
                                             if row["correct"] is not None]}
                                for case in cases[:5]],
              "artifact_path": detail_path.relative_to(Path.cwd()).as_posix(), "created_at": made_at}
    if model:
        config["actual_usage"] = model.usage
    write_report(detail_path, {"config": config, "cases": cases})
    write_report(output / f"evaluation-{identifier}.json", report)
    return report


def write_report(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)
