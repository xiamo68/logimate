import asyncio
from contextlib import contextmanager
import json
import os
import socket
from threading import Thread
from time import monotonic, sleep

from fastapi.testclient import TestClient
import httpx
import uvicorn

from logimate import store
from logimate.api import Settings, create_app
from logimate.mcp_service import ToolResult, create_service
from logimate.seed import fingerprint, snapshot
from logimate.workflow import DeepSeek, build_workflow, mcp_invoker


def test_clarifies_missing_order_without_tool_calls():
    async def unavailable(*args, **kwargs):
        raise AssertionError("No tool should run without an order ID")

    output = asyncio.run(build_workflow(unavailable).ainvoke({
        "question": "我的订单怎么还没收到？", "trace": [],
    }))
    assert output["result"]["status"] == "needs_clarification"
    assert output["result"]["evidence_ids"] == []


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
