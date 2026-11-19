import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.pool import StaticPool

from logimate import store
from logimate.api import Settings, create_app
from logimate.seed import export_cases, fingerprint, snapshot


@pytest.fixture
def engine():
    dsn = os.getenv("LOGIMATE_TEST_DSN")
    if dsn:
        engine = store.database(dsn)
    else:
        from sqlalchemy import create_engine

        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    yield engine
    engine.dispose()


def test_repeat_seed_and_splits(engine, tmp_path):
    from json import loads

    store.migrate(engine)
    store.migrate(engine)
    data = snapshot()
    digest = fingerprint(data)
    assert digest == fingerprint(snapshot())
    assert store.seed_database(engine, data, digest)
    assert not store.seed_database(engine, data, digest)
    with pytest.raises(ValueError, match="different fingerprint"):
        store.seed_database(engine, data, "f" * 64)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(store.orders)) == 130
        assert connection.scalar(select(func.count()).select_from(store.packages)) == 130
        assert connection.scalar(select(func.count()).select_from(store.policy_documents)) == 2
    export_cases(data, tmp_path)
    manifest = loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["datasets"]["final"]["count"] == 100
    assert manifest["datasets"]["aftersales_final"]["count"] == 60
    families = [{case["family"] for case in group} for group in data["logistics"].values()]
    assert all(not left & right for index, left in enumerate(families) for right in families[index + 1:])
    for dataset in manifest["datasets"].values():
        assert dataset["sha256"] == fingerprint(loads((tmp_path / dataset["file"]).read_text(encoding="utf-8")))


def test_demo_auth_and_idempotent_sessions(engine):
    store.migrate(engine)
    settings = Settings("", "test-secret-" * 4, demo_sign_in=True)
    headers = {"Origin": settings.origin, "Idempotency-Key": "create-001"}
    with TestClient(create_app(settings, engine)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.post("/api/v1/sessions", headers=headers).status_code == 401
        assert client.post("/api/v1/demo/sign-in", json={"profile": "alpha_operator"}).status_code == 403
        signed = client.post("/api/v1/demo/sign-in", headers=headers, json={"profile": "alpha_operator"})
        assert signed.status_code == 200
        assert signed.json()["tenant_id"] == "alpha"
        assert "HttpOnly" in signed.headers["set-cookie"]
        first = client.post("/api/v1/sessions", headers=headers, json={"title": " 主线 "})
        replay = client.post("/api/v1/sessions", headers=headers, json={"title": "主线"})
        assert first.status_code == replay.status_code == 201
        assert first.json() == replay.json()
        assert client.post("/api/v1/sessions", headers=headers, json={"title": "其他"}).status_code == 409
        uri = f"/api/v1/sessions/{first.json()['session_id']}"
        assert client.get(uri).json()["title"] == "主线"
        assert client.post("/api/v1/demo/sign-in", headers=headers, json={"profile": "beta_operator"}).status_code == 200
        assert client.get(uri).status_code == 404
        assert client.post("/api/v1/sessions", headers=headers, json={"title": "主线"}).status_code == 201
