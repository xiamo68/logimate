import asyncio
import json

import httpx
import pytest

from logimate.benchmark_data import freeze, load_frozen
from logimate.evaluation import score_answer
from logimate.workflow import DeepSeek


def test_frozen_records_and_checksum(tmp_path):
    archive = tmp_path / "frozen.json"
    freeze(archive)
    data = load_frozen(archive)
    assert len(data["logistics"]) == 100
    assert len({case["question"] for case in data["aftersales"]}) == 60
    archive.write_text(json.dumps({**data, "logistics": data["logistics"][:-1]}), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        load_frozen(archive)


def test_grounding_scores_missing_and_fabricated_ids():
    case = {"expected": "refund_before_outbound", "relevant": {"refund_before_outbound": {"real"}}}
    response = {"status": "answered", "claim": "refund_before_outbound", "evidence_ids": ["made-up"]}
    assert score_answer(case, response, {"real": "refund_before_outbound"}) == (False, True)
    assert score_answer(case, {**response, "evidence_ids": ["real"]}, {"real": "refund_before_outbound"}) == (True, False)


def test_deepseek_evaluation_request_cap_and_usage():
    def reply(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"kind":"logistics"}'}}],
            "usage": {"prompt_tokens": 19, "completion_tokens": 7, "prompt_cache_hit_tokens": 3},
        })

    model = DeepSeek("deepseek-flash", "test-only", request_limit=1, transport=httpx.MockTransport(reply))
    assert asyncio.run(model.identify("LM-1001")).kind == "logistics"
    with pytest.raises(ValueError, match="request limit"):
        asyncio.run(model.identify("LM-1001"))
    assert model.usage == {"attempts": 1, "prompt_tokens": 19, "completion_tokens": 7, "cached_prompt_tokens": 3}
