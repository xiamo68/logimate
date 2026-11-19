import asyncio
import json
import os
import subprocess
import sys
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import insert, select

from logimate import store
from logimate.api import Settings, create_app
from logimate.mcp_service import create_service
from logimate.memory import SUMMARY_TOKEN_BUDGET, WINDOW_TOKEN_BUDGET, compact_history, token_upper_bound
from logimate.retrieval import Vectors, hybrid_retrieve, index_policies, keyword_search, policy_chunks
from logimate.seed import fingerprint, snapshot
from logimate.workflow import utc_now
from test_workflow import serve


@pytest.fixture
def seeded(tmp_path):
    engine = store.database(os.getenv("LOGIMATE_TEST_DSN") or f"sqlite:///{tmp_path / 'stage04.db'}")
    try:
        store.migrate(engine)
        data = snapshot()
        store.seed_database(engine, data, fingerprint(data))
        yield engine, data
    finally:
        engine.dispose()


def test_bm25_fallback_returns_before_vector_branch_finishes():
    chunks = policy_chunks({"document_id": "policy", "title": "赔付规则", "body": "## 揽收超时\n实际交接后可核实。"},
                           "v1", "2026-11-19T08:00:00Z")
    assert keyword_search("揽收超时", chunks) == [chunks[0]["id"]]

    async def lookup():
        def delayed():
            sleep(1.2)
            return []

        start = monotonic()
        mode, result = await hybrid_retrieve("揽收超时", chunks, delayed)
        assert monotonic() - start < 1.05
        assert mode == "keyword_only"
        assert result == chunks

    asyncio.run(lookup())


def test_policy_chunk_preserves_sentence_and_title():
    first = "发货前退款流程" * 42 + "。"
    second = "原路退回的条件" * 38 + "。"
    chunks = policy_chunks({"document_id": "refund", "title": "退款政策", "body":
                            f"## 发货前退款\n{first}{second}"}, "v1", "2026-11-19T08:00:00Z")
    assert [item["excerpt"] for item in chunks] == [first, second]
    assert all(item["title_path"] == ["退款政策", "发货前退款"] for item in chunks)


def test_session_memory_compacts_before_message_limit_and_keeps_evidence():
    older = SimpleNamespace(question="LM-1001 " + "交接核实" * 260, result={
        "status": "answered", "diagnoses": [{"classification": "awaiting_handoff"}], "evidence_ids": ["evidence-1"],
    })
    latest = SimpleNamespace(question="检查" * 450, result=None)
    memory = compact_history([older, latest])
    assert memory["window"] == [latest.question]
    assert memory["summary_order_ids"] == ["LM-1001"]
    assert memory["summary"][0]["diagnoses"] == ["awaiting_handoff"]
    assert memory["summary"][0]["evidence_ids"] == ["evidence-1"]
    assert token_upper_bound(memory["window"]) <= WINDOW_TOKEN_BUDGET
    assert token_upper_bound(memory["summary"]) <= SUMMARY_TOKEN_BUDGET


def test_run_resume_uses_saved_order_step(seeded, tmp_path, monkeypatch):
    engine, data = seeded
    checkpoint = tmp_path / "checkpoints.db"
    secret = "recovery-mcp-" * 4
    settings = Settings("", "recovery-secret-" * 4, demo_sign_in=True)
    monkeypatch.setenv("LOGIMATE_MCP_SIGNING_KEY", secret)
    monkeypatch.setenv("LOGIMATE_CHECKPOINT_DB", str(checkpoint))
    session_id, run_id = "recovery-session", "recovery-run"
    actor = {"tenant_id": "alpha", "user_id": "alpha-operator"}
    with engine.begin() as conn:
        conn.execute(insert(store.sessions).values(session_id=session_id, **actor, title="恢复", created_at=utc_now()[:19] + "Z"))
        conn.execute(insert(store.runs).values(run_id=run_id, session_id=session_id, **actor, question="查询 LM-1001",
                     status="running", snapshot={"mode": "demo", "data_version": data["data_version"],
                                                 "as_of": data["as_of"], "versions": store.INITIAL_VERSIONS,
                                                 "created_at": utc_now()}))
    with serve(create_service(engine, secret)) as url:
        monkeypatch.setenv("LOGIMATE_MCP_URL", url)

        process_input = {"question": "查询 LM-1001", "trace": [], "scope": {**actor, "run_id": run_id,
                         "data_version": data["data_version"], "as_of": data["as_of"]}}
        subprocess.run([sys.executable, "-c", """
import asyncio, json, os, sys
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from logimate.workflow import build_workflow, mcp_invoker

async def checkpoint_order():
    async with AsyncSqliteSaver.from_conn_string(os.environ["LOGIMATE_CHECKPOINT_DB"]) as saver:
        graph = build_workflow(mcp_invoker(os.environ["LOGIMATE_MCP_URL"], os.environ["LOGIMATE_MCP_SIGNING_KEY"]),
                               checkpointer=saver, interrupt_after=["order"])
        state = json.load(sys.stdin)
        await graph.ainvoke(state, {"configurable": {"thread_id": state["scope"]["run_id"]}})

asyncio.run(checkpoint_order())
"""], input=json.dumps(process_input), text=True, check=True, timeout=15)
        with TestClient(create_app(settings, engine), headers={"Origin": settings.origin}) as app:
            assert app.post("/api/v1/demo/sign-in", json={"profile": "alpha_operator"}).status_code == 200
            route = f"/api/v1/runs/{run_id}"
            assert app.get(route).json()["status"] == "interrupted"
            headers = {"Idempotency-Key": "resume-stage04"}
            response = app.post(route + "/resume", headers=headers)
            assert response.status_code == 202, response.text
            assert app.post(route + "/resume", headers=headers).json() == response.json()
            done = app.get(route).json()
            assert done["status"] == "completed", done
            assert done["versions"] == store.INITIAL_VERSIONS and done["as_of"] == data["as_of"]
            trace = app.get(route + "/trace").json()["events"]
            assert sum(event["tool_name"] == "order.get" for event in trace) == 1


@pytest.mark.skipif(not os.getenv("LOGIMATE_STAGE04_REDIS"), reason="Requires a local native Redis")
def test_native_milvus_and_redis_across_sessions(seeded, tmp_path, monkeypatch):
    engine, data = seeded
    monkeypatch.setenv("LOGIMATE_MODE", "demo")
    location = str(tmp_path / "milvus.db")
    monkeypatch.setenv("LOGIMATE_MILVUS_URI", location)
    monkeypatch.setenv("REDIS_URL", os.environ["LOGIMATE_STAGE04_REDIS"])
    monkeypatch.setenv("LOGIMATE_CACHE_NAMESPACE", "logimate-stage04-test")
    vectors = Vectors.from_environment()
    try:
        assert index_policies(engine, vectors, data["data_version"]) > 0
    finally:
        vectors.close()
    settings = Settings("", "native-stage04-secret-" * 3, demo_sign_in=True)
    secret = "native-stage04-mcp-" * 3
    monkeypatch.setenv("LOGIMATE_MCP_SIGNING_KEY", secret)
    with serve(create_service(engine, secret)) as url:
        monkeypatch.setenv("LOGIMATE_MCP_URL", url)
        with TestClient(create_app(settings, engine), headers={"Origin": settings.origin}) as client:
            def sign(profile):
                assert client.post("/api/v1/demo/sign-in", json={"profile": profile}).status_code == 200

            def session():
                return client.post("/api/v1/sessions", json={}).json()["session_id"]

            def run(session_id, question):
                submitted = client.post(f"/api/v1/sessions/{session_id}/runs", json={"question": question})
                assert submitted.status_code == 202, submitted.text
                record = client.get(f"/api/v1/runs/{submitted.json()['run_id']}").json()
                assert record["status"] == "completed", record
                return record["result"]

            sign("alpha_operator")
            topic = "查询物流延误时先查承诺送达日期"
            preference = client.post("/api/v1/preferences", json={"text": topic})
            assert preference.status_code == 201 and preference.json()["index_status"] == "ready", preference.text
            preference_id = preference.json()["preference_id"]
            first = session()
            lookup = run(first, "LM-1001 还没出库能退款吗？")
            assert lookup["retrieval_mode"] == "hybrid", lookup
            citations = [client.get(f"/api/v1/evidence/{item}").json() for item in lookup["evidence_ids"]]
            assert any(item["source"] == "policy" and item["policy"]["document_id"] == "refund-v1" for item in citations)
            follow_up = run(first, "再查这个订单的物流")
            assert follow_up["status"] == "answered" and follow_up["diagnoses"]
            from redis import Redis

            cache = Redis.from_url(os.environ["LOGIMATE_STAGE04_REDIS"])
            cache_key = f"logimate-stage04-test:session:alpha:alpha-operator:{first}"
            try:
                assert cache.exists(cache_key)
                cache.delete(cache_key)
                assert run(first, "再查这个订单的物流")["diagnoses"]
            finally:
                cache.delete(cache_key)
                cache.close()
            other = session()
            assert any(topic in item for item in run(other, "LM-1001 " + topic)["next_steps"])
            sign("beta_operator")
            assert client.get("/api/v1/preferences").json()["items"] == []
            assert client.delete(f"/api/v1/preferences/{preference_id}").status_code == 404
            sign("alpha_operator")
            with patch("pymilvus.MilvusClient.delete", side_effect=RuntimeError("test unavailable index")):
                assert client.delete(f"/api/v1/preferences/{preference_id}").status_code == 204
            assert all(topic not in item for item in run(other, "LM-1001 " + topic)["next_steps"])
