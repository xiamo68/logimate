import json

from fastapi.testclient import TestClient

from logimate import store
from logimate.api import Settings, create_app


def test_workbench_and_empty_evaluation_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("LOGIMATE_EVALUATION_DIR", str(tmp_path / "no-reports"))
    engine = store.database(f"sqlite:///{tmp_path / 'workbench.db'}")
    try:
        store.migrate(engine)
        settings = Settings("", "workbench-test-secret-" * 3, demo_sign_in=True)
        with TestClient(create_app(settings, engine), headers={"Origin": settings.origin}) as client:
            page = client.get("/")
            assert page.status_code == 200 and "订单异常排查" in page.text
            assert 'id="order-number"' in page.text
            assert '<details class="panel technical" id="technical">' in page.text
            assert client.get("/assets/workbench.js").status_code == 200
            assert client.get("/api/v1/evaluations").status_code == 401
            assert client.post("/api/v1/demo/sign-in", json={"profile": "alpha_operator"}).is_success
            assert client.get("/api/v1/evaluations").json() == {"items": [], "next_cursor": None}
            reports = tmp_path / "no-reports"
            reports.mkdir()
            sample = {
                "evaluation_id": "fixture", "kind": "logistics", "mode": "demo", "model_id": None,
                "data_version": "synthetic-v1", "data_hash": "a" * 64, "code_ref": "fixture",
                "config_hash": "b" * 64, "baseline_group": "initial",
                "groups": [{"name": "initial", "sample_count": 1, "metrics": {"error_count": 1}}],
                "case_examples": [{"case_id": "case-1", "scenario": "合成示例", "expected": "等待交接",
                                   "results": [{"group": "initial", "actual": "揽收超时", "correct": False}]}],
                "artifact_path": "artifacts/evaluation-fixture.json", "created_at": "2026-11-19T08:00:00Z",
            }
            (reports / "evaluation-fixture.json").write_text(json.dumps(sample), encoding="utf-8")
            (reports / "evaluation-invalid.json").write_text(json.dumps({
                **sample, "evaluation_id": "invalid", "artifact_path": "../private.json",
            }), encoding="utf-8")
            result = client.get("/api/v1/evaluations?mode=demo&kind=logistics").json()
            assert result["items"] == [sample]
            assert client.get("/api/v1/evaluations?mode=deepseek").json()["items"] == []
    finally:
        engine.dispose()
