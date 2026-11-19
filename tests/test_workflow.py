import asyncio
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import json
import os
import socket
from threading import Thread
from time import monotonic, sleep

from fastapi.testclient import TestClient
import httpx
import uvicorn
from sqlalchemy import update

from logimate import store
from logimate import evolution
from logimate.api import Settings, create_app
from logimate.evolution import run_one
from logimate.mcp_service import ToolResult, create_service
from logimate.seed import export_cases, fingerprint, snapshot
from logimate.workflow import DeepSeek, build_workflow, mcp_invoker


def test_clarifies_missing_order_without_tool_calls():
    async def unavailable(*args, **kwargs):
        raise AssertionError("No tool should run without an order ID")

    output = asyncio.run(build_workflow(unavailable).ainvoke({
        "question": "我的订单怎么还没收到？", "trace": [],
    }))
    assert output["result"]["status"] == "needs_clarification"
    assert output["result"]["evidence_ids"] == []
    assert all(event["kind"] == "node" for event in output["trace"])


@contextmanager
def serve(app):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 10
            while not server.started and thread.is_alive() and monotonic() < deadline:
                sleep(0.01)
            assert server.started
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            server.should_exit = True
            thread.join(10)


def test_live_mcp_investigation_and_permissions(tmp_path, monkeypatch):
    dsn = os.getenv("LOGIMATE_TEST_DSN") or f"sqlite:///{tmp_path / 'logimate.db'}"
    engine = store.database(dsn)
    try:
        store.migrate(engine)
        data = snapshot()
        store.seed_database(engine, data, fingerprint(data))
        secret = "integration-mcp-key-" * 3
        monkeypatch.setenv("LOGIMATE_MCP_SIGNING_KEY", secret)
        monkeypatch.setenv("LOGIMATE_MODE", "demo")
        with serve(create_service(engine, secret)) as url:
            claims = {"tenant_id": "alpha", "user_id": "alpha-operator", "run_id": "auth-check",
                      "data_version": data["data_version"], "as_of": data["as_of"]}
            assert asyncio.run(mcp_invoker(url, "incorrect-key-" * 3)(
                claims, "order.get", {"order_id": "LM-1001"},
            )).status == "forbidden"
            assert asyncio.run(mcp_invoker(url, secret)(
                claims, "order.get", {"order_id": "bad id"},
            )).status == "invalid_input"
            monkeypatch.setenv("LOGIMATE_MCP_URL", url)
            settings = Settings(dsn, "integration-session-key-" * 3, demo_sign_in=True)
            with TestClient(create_app(settings, engine), headers={"Origin": settings.origin}) as client:
                def sign(profile):
                    assert client.post("/api/v1/demo/sign-in", json={"profile": profile}).status_code == 200

                sign("alpha_operator")
                session_id = client.post("/api/v1/sessions", json={}).json()["session_id"]

                def investigate(question, key=None):
                    headers = {"Idempotency-Key": key} if key else {}
                    submitted = client.post(f"/api/v1/sessions/{session_id}/runs",
                                            json={"question": question}, headers=headers)
                    assert submitted.status_code == 202
                    run_id = submitted.json()["run_id"]
                    path = f"/api/v1/runs/{run_id}"
                    deadline = monotonic() + 10
                    while monotonic() < deadline:
                        run = client.get(path).json()
                        if run["status"] not in ("queued", "running"):
                            break
                        sleep(0.05)
                    assert run["status"] == "completed", run
                    return run, client.get(path + "/trace").json()["events"]

                run, trace = investigate("LM-1001 三天没更新，是否有售后工单？", "stable-run")
                ids = run["result"]["evidence_ids"]
                assert run["result"]["diagnoses"][0]["classification"] == "pickup_overdue", (run, trace)
                assert {event["tool_name"] for event in trace if event["kind"] == "tool"} == {
                    "order.get", "warehouse.get_events", "logistics.get_tracking", "aftersales.get_cases",
                }
                assert client.get(f"/api/v1/evidence/{ids[0]}").status_code == 200
                assert investigate("LM-1001 三天没更新，是否有售后工单？", "stable-run")[0]["run_id"] == run["run_id"]
                page = client.get(f"/api/v1/sessions/{session_id}", params={"limit": 1}).json()
                assert page["messages"][0]["role"] == "user" and page["runs"][0]["run_id"] == run["run_id"]
                next_page = client.get(f"/api/v1/sessions/{session_id}", params={"cursor": page["next_cursor"]}).json()
                assert next_page["messages"][0]["role"] == "assistant"
                absent, absent_trace = investigate("查询 LM-1002 的订单")
                assert absent["result"]["evidence_ids"] == []
                assert [event["error_code"] for event in absent_trace if event["kind"] == "tool"] == ["not_found"]
                sign("beta_operator")
                assert client.get(f"/api/v1/runs/{run['run_id']}").status_code == 404
                assert client.get(f"/api/v1/evidence/{ids[0]}").status_code == 404
                session_id = client.post("/api/v1/sessions", json={}).json()["session_id"]
                failed, trace = investigate("查询 LM-1004 的订单")
                tracking = [event for event in trace if event["tool_name"] == "logistics.get_tracking"]
                assert [event["attempt"] for event in tracking] == [1, 2]
                assert failed["result"]["status"] == "partial"
                export_cases(data, tmp_path)
                sign("alpha_operator")
                owned_session = client.post("/api/v1/sessions", json={}).json()["session_id"]
                session_id = owned_session
                original, _ = investigate("LM-1001 三天没更新")
                original_id = original["run_id"]
                endpoints = [client.get(f"/api/v1/evidence/{item}").json() for item in original["result"]["evidence_ids"]]
                citations = [item["evidence_id"] for item in endpoints if item["source"] in ("warehouse", "logistics")]
                correction = {"corrected_classification": "awaiting_handoff", "reason": "仓库尚无交接记录",
                              "evidence_ids": citations}
                feedback = client.post(f"/api/v1/runs/{original_id}/feedback", json=correction)
                assert feedback.status_code == 201, feedback.text
                feedback_id = feedback.json()["feedback_id"]
                job_body = {"kind": "base", "feedback_ids": [feedback_id]}
                assert client.post("/api/v1/evolution/jobs", json=job_body).status_code == 422
                review_body = {"decision": "approve", "reason": "已核对仓库和承运商记录"}
                assert client.post(f"/api/v1/feedback/{feedback_id}/verify", json=review_body).status_code == 403
                sign("beta_reviewer")
                assert client.post(f"/api/v1/feedback/{feedback_id}/verify", json=review_body).status_code == 404
                sign("alpha_reviewer")
                assert client.get(f"/api/v1/evidence/{citations[0]}").status_code == 200
                assert client.post(f"/api/v1/feedback/{feedback_id}/verify", json=review_body).json()["status"] == "verified"
                sign("alpha_operator")

                def generate(body, interrupt=False):
                    submitted = client.post("/api/v1/evolution/jobs", json=body)
                    assert submitted.status_code == 202, submitted.text
                    job_id = submitted.json()["job_id"]
                    if interrupt:
                        update_job = evolution.update_job

                        def lose_lease(connection, job, **changes):
                            if changes.get("payload", {}).get("processed_cases") == 2:
                                raise evolution.LeaseLost(job["job_id"])
                            return update_job(connection, job, **changes)

                        with monkeypatch.context() as scoped:
                            scoped.setattr(evolution, "update_job", lose_lease)
                            assert run_one(engine, tmp_path)
                        interrupted = client.get(f"/api/v1/evolution/jobs/{job_id}").json()
                        assert interrupted["status"] == "running" and interrupted["processed_cases"] == 1
                        with engine.begin() as connection:
                            connection.execute(update(store.jobs).where(store.jobs.c.job_id == job_id)
                                               .values(lease_until="2000-01-01T00:00:00.000000Z"))
                    assert run_one(engine, tmp_path)
                    job = client.get(f"/api/v1/evolution/jobs/{job_id}").json()
                    assert job["status"] == "completed", job
                    assert job["total_cases"] == job["processed_cases"] > 0
                    result = client.get(f"/api/v1/versions/{job['version_id']}").json()
                    assert result["version"]["state"] == "passed", result
                    return job["version_id"], result["reports"]

                version, reports = generate(job_body, interrupt=True)
                competing_version, competing_reports = generate(job_body)
                assert reports[0]["baseline_error_count"] > reports[0]["candidate_error_count"]
                sign("alpha_reviewer")
                release = {"expected_release_id": "rel_initial"}
                assert client.post(f"/api/v1/versions/{version}/release", json=release).status_code == 409
                approval = {**review_body, "report_id": reports[0]["report_id"]}
                assert client.post(f"/api/v1/versions/{version}/review", json=approval).status_code == 200
                competing_approval = {**review_body, "report_id": competing_reports[0]["report_id"]}
                assert client.post(f"/api/v1/versions/{competing_version}/review", json=competing_approval).status_code == 200
                def publish(version_id):
                    return client.post(f"/api/v1/versions/{version_id}/release", json=release)

                if os.getenv("LOGIMATE_TEST_DSN"):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        responses = list(pool.map(publish, (version, competing_version)))
                else:
                    responses = [publish(version), publish(competing_version)]
                assert sorted(response.status_code for response in responses) == [200, 409]
                released = next(response for response in responses if response.status_code == 200)
                winner = released.json()["active"]["version_id"]
                winning_report = reports[0] if winner == version else competing_reports[0]
                assert client.get(f"/api/v1/versions/{winner}").json()["reviewed_report_id"] == winning_report["report_id"]
                sign("alpha_operator")
                after, _ = investigate("LM-1001 三天没更新")
                assert after["versions"]["version_id"] == winner
                assert after["result"]["diagnoses"][0]["classification"] == "awaiting_handoff"
                meta, meta_reports = generate({"kind": "meta"})
                assert {item["comparison"] for item in meta_reports} == {"active_baseline", "skill_ablation"}
                assert all(item["candidate_error_count"] < item["baseline_error_count"] for item in meta_reports)
                assert client.get(f"/api/v1/versions/{meta}").json()["version"]["skill_version_id"] != after["versions"]["skill_version_id"]
                sign("alpha_reviewer")
                meta_report = next(item for item in meta_reports if item["comparison"] == "active_baseline")
                assert client.post(f"/api/v1/versions/{meta}/review",
                                   json={**review_body, "report_id": meta_report["report_id"]}).status_code == 200
                meta_released = client.post(f"/api/v1/versions/{meta}/release",
                                            json={"expected_release_id": released.json()["active"]["release_id"]})
                assert meta_released.status_code == 200, meta_released.text
                rollback = client.post("/api/v1/versions/ver_initial/rollback", json={
                    "expected_release_id": meta_released.json()["active"]["release_id"], "reason": "验收回滚",
                })
                assert rollback.status_code == 200, rollback.text
                sign("alpha_operator")
                restored, _ = investigate("LM-1001 三天没更新")
                assert restored["result"]["diagnoses"][0]["classification"] == "pickup_overdue"
    finally:
        engine.dispose()


def test_timeout_retries_within_budget():
    async def slow(*args):
        await asyncio.sleep(1)
        return ToolResult(status="not_found")

    state = {"question": "查询 LM-1001", "trace": [],
             "scope": {"tenant_id": "alpha", "user_id": "alpha-operator",
                       "data_version": "synthetic-v1", "as_of": "2026-11-19T08:00:00Z"}}
    output = asyncio.run(build_workflow(slow, attempt_timeout=0.02).ainvoke(state))
    attempts = [event for event in output["trace"] if event["kind"] == "tool"]
    assert [event["error_code"] for event in attempts] == ["timeout", "timeout"]
    assert output["result"]["status"] == "partial"


def test_deepseek_adapter_parses_json_contract_without_provider_calls():
    def reply(request):
        assert request.url.host == "api.deepseek.com"
        assert request.url.path == "/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        request_body = json.loads(request.content)
        assert request_body["model"] == "deepseek-flash"
        assert request_body["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"kind":"aftersales"}'}}]})

    model = DeepSeek("deepseek-flash", "test-key", transport=httpx.MockTransport(reply))
    assert asyncio.run(model.identify("查询 LM-1001 的退款工单")).kind == "aftersales"


def test_workflow_records_provider_calls_and_stops_on_failed_intent():
    async def unexpected(*args):
        raise AssertionError("A policy lookup without an order must not query MCP")

    async def retrieve(*args):
        return "hybrid", [{
            "id": "refund", "document_id": "synthetic-policy", "version": "v1",
            "title_path": ["发货前退款"], "excerpt": "未出库可取消订单。",
            "effective_at": "2026-11-18T08:00:00Z",
        }]

    state = {"question": "没出库可以退款吗？", "trace": [],
             "scope": {"tenant_id": "alpha", "user_id": "alpha-operator", "run_id": "test",
                       "data_version": "synthetic-v1", "as_of": "2026-11-19T08:00:00Z"}}

    def reply(request):
        instructions = json.loads(request.content)["messages"][0]["content"]
        payload = {"kind": "aftersales"} if instructions.startswith("Classify") else {
            "status": "answered", "claim": "refund_before_outbound", "evidence_ids": ["refund"],
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

    model = DeepSeek("deepseek-flash", "test-key", transport=httpx.MockTransport(reply))
    output = asyncio.run(build_workflow(unexpected, model, retrieve=retrieve).ainvoke(state))
    assert output["result"]["status"] == "answered"
    assert [(event["node_id"], event["status"]) for event in output["trace"] if event["kind"] == "model"] == [
        ("model.intent", "ok"), ("model.aftersales", "ok"),
    ]

    blocked = DeepSeek("deepseek-flash", "test-key",
                       transport=httpx.MockTransport(lambda request: httpx.Response(429)))
    failure = asyncio.run(build_workflow(unexpected, blocked, retrieve=unexpected).ainvoke(state))
    assert failure["model_failed"]
    assert "result" not in failure
    assert [event["error_code"] for event in failure["trace"] if event["kind"] == "model"] == ["model_call_failed"]


def test_query_plan_controls_mcp_and_policy_uses_order_evidence(tmp_path):
    engine = store.database(f"sqlite:///{tmp_path / 'plans.db'}")
    data = snapshot()
    store.migrate(engine)
    store.seed_database(engine, data, fingerprint(data))
    secret = "test-mcp-plan-key-" * 3
    clause = {"id": "refund", "document_id": "policies", "version": data["data_version"],
              "title_path": ["发货前退款"], "effective_at": data["as_of"], "excerpt": "尚未出库可申请取消并退款。"}

    async def retrieve(*_):
        return "hybrid", [clause]

    def reply(request):
        system, user = json.loads(request.content)["messages"]
        if system["content"].startswith("Classify"):
            payload = {"kind": "aftersales" if "退款" in user["content"] else "logistics"}
        elif system["content"].startswith("Plan"):
            assert "json" in system["content"].lower()
            inputs = json.loads(user["content"])
            payload = {"goal": "order_status", "sources": []}
            if inputs["intent"] == "aftersales":
                payload = {"goal": "policy", "sources": ["warehouse", "logistics"]}
            elif "无效" in inputs["question"]:
                payload = {"goal": "diagnosis", "sources": []}
        else:
            assert json.loads(user["content"])["order_facts"]["warehouse_events"] == ["outbound"]
            payload = {"status": "answered", "claim": "refund_before_outbound", "evidence_ids": ["refund"]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

    model = DeepSeek("deepseek-flash", "test-key", transport=httpx.MockTransport(reply))
    scope = {"tenant_id": "alpha", "user_id": "alpha-operator", "run_id": "test-plan",
             "data_version": data["data_version"], "as_of": data["as_of"]}
    try:
        with serve(create_service(engine, secret)) as url:
            graph = build_workflow(mcp_invoker(url, secret), model, retrieve=retrieve)
            def run(question):
                return asyncio.run(graph.ainvoke({"question": question, "trace": [], "scope": scope}))

            order = run("LM-1001 只查订单状态")
            assert order["result"]["query_plan"] == {"goal": "order_status", "sources": [], "origin": "model"}
            assert [event["tool_name"] for event in order["trace"] if event["kind"] == "tool"] == ["order.get"]
            fallback = run("LM-1001 无效计划")
            assert fallback["result"]["query_plan"]["origin"] == "fallback"
            assert any(event["node_id"] == "model.plan" and event["status"] == "error"
                       for event in fallback["trace"])
            policy = run("LM-1001 可以按发货前政策退款吗？")
            decision = policy["result"]["policy_decision"]
            assert decision["status"] == "not_met"
            evidence = {item["evidence_id"]: item["source"] for item in policy["evidence"]}
            assert {evidence[item] for item in decision["evidence_ids"]} == {"order", "warehouse", "logistics", "policy"}
    finally:
        engine.dispose()
