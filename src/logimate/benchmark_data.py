"""Frozen synthetic evaluation inputs, separate from development replay data."""

from datetime import datetime, timedelta
import json
from pathlib import Path

from .seed import DATA_VERSION, fingerprint

BENCHMARK_REVISION = "synthetic-final-v1"
OBSERVED_AT = "2026-11-19T08:00:00Z"
DEFAULT_PATH = Path(".logimate/benchmarks/final.json")


# Labels are assigned to scenario templates, independently of the rule interpreter.
SCENARIOS = (
    ("unhanded_label", False, "label_created", True, "awaiting_handoff"),
    ("accepted_label_old", True, "label_created", True, "pickup_overdue"),
    ("fresh_pickup", True, "picked_up", True, "normal"),
    ("carrier_unreachable", True, None, False, "needs_verification"),
    ("beyond_promised_date", True, "picked_up", True, "in_transit_delayed"),
    ("recent_handoff", True, "label_created", True, "awaiting_pickup"),
    ("unhanded_no_label", False, None, True, "awaiting_handoff"),
    ("delivered_late", True, "delivered", True, "normal"),
    ("before_promised_date", True, "in_transit", True, "normal"),
    ("carrier_exception_without_handoff", False, "carrier_exception", True, "needs_verification"),
)

QUERIES = {
    "refund_before_outbound": (
        "订单尚未出库，能取消并退款吗？", "包裹还在仓库，我可以撤单退款吗？",
        "仓库还没出货，怎么申请取消订单？", "发货前反悔了，政策允许退款吗？",
        "订单未出库可以原路退回吗？", "待出库订单是否符合退款条件？",
        "仓库未发件，客户要求取消，怎么处理？", "商品还未出库，是否可申请退单？",
        "订单没有出库记录，能不能退款取消？", "想在仓库出库之前取消发货并退款。",
        "货还没出仓，政策允许申请退款么？", "待发货的订单可以取消并拿回款项吗？",
    ),
    "refund_in_transit": (
        "物流已揽收，还能直接退款吗？", "货已在运输路上，办退款要先查什么？",
        "承运商收货以后申请退款的前置条件是什么？", "已寄出的包裹要退款需要核对什么轨迹？",
        "快递已经揽收，客服处理退单前要确认什么？", "对于在途订单，退款前怎样确认退回？",
        "已被快递公司接收的件能否立即退款？", "申请在途退款时，应该检查回程记录吗？",
        "承运商已收走货物，付款怎么退？", "包裹运输中，先检查退回轨迹还是直接退钱？",
        "已经有揽收扫描，客户想退款，应先核实什么？", "快递未到但已经上路，售后退款要查哪些资料？",
    ),
    "pickup_overdue_check": (
        "实际交接后两天多没揽收，可以要求核实吗？", "交给承运商 50 小时，仍无揽收扫描怎么办？",
        "仓库确认移交后 49 小时未揽件，能走物流核实吗？", "发起揽收超时核实的条件是什么？",
        "真实交接满三天却没有揽收，是否可找承运商？", "只有打印面单，能否直接按交接后的48小时核实？",
        "赔付规则写的揽收超时从哪个动作开始算？", "从仓库实际交给物流起，多久没揽收可核实？",
        "货已交接超过48小时，物流仍没签收揽件，有适用条款吗？", "超过两日无收件记录且仓库已交货，如何核实？",
        "交接之后迟迟没入物流网，多少小时可以要求核实？", "承运商在实物移交后 60 小时没揽收，能提交核实吗？",
    ),
    "delivery_dispute_check": (
        "显示签收但我没有收到，核实需要什么资料？", "包裹显示妥投却不见了，怎样受理争议？",
        "客户否认签收，需要准备哪两个材料？", "争议件查询要提供签收凭证和什么？",
        "快递被标记已签，收货人没收到，怎样发起核实？", "客服处理签收争议时必须拿到哪些信息？",
        "运单写着已送到，申请核实要提供什么？", "疑似代签，需要签收记录和单号吗？",
        "签收证明和运单号齐全能提交争议吗？", "客户说包裹未收且物流已签收，需要哪种凭证？",
        "妥投信息和实际不符，有争议怎么报？", "已经显示签收，收件人否认，查件材料有哪些？",
    ),
    "insufficient": (
        "购买的设备包含几年延保？", "海外维修能否走全球联保？", "可以补开电子增值税发票吗？",
        "积分过期后能重新激活吗？", "国家补贴如何申请？", "门店能提供上门安装吗？",
        "会员升级后的折扣比例是多少？", "拆封商品是否可以换颜色？", "半年内电子产品保修期怎么算？",
        "过期优惠券可以恢复吗？", "购买时赠送的上门服务怎么预约？", "可以更换发票上的公司名称吗？",
    ),
}


def records():
    as_of = datetime.fromisoformat(OBSERVED_AT)
    shipping = []
    for index in range(100):
        family, handoff, tracking, available, expected = SCENARIOS[index % 10]
        variant = index // 10
        event_age = 2 + variant if family == "recent_handoff" else 51 + variant * 4 + index % 4
        occurred_at = (as_of - timedelta(hours=event_age)).isoformat().replace("+00:00", "Z")
        delivery_late = family in ("beyond_promised_date", "delivered_late")
        promised = (as_of + timedelta(hours=-10 if delivery_late else 26 + variant)).isoformat().replace("+00:00", "Z")
        events = [{"event_type": tracking, "occurred_at": occurred_at}] if tracking else []
        shipping.append({"case_id": f"frozen-lm-{index+1:03d}", "family": family, "as_of": OBSERVED_AT,
                         "tracking": tracking or ("no_events" if available else None), "handoff": handoff,
                         "has_support_case": (variant * 3 + index % 10) % 5 in (0, 2),
                         "promised_by": promised, "events": events, "expected": expected,
                         "expected_missing": ["tracking"] if not available else ["handoff_proof"]
                         if family.startswith("carrier_exception") else []})
    aftersales = [{"case_id": f"frozen-rag-{number:03d}", "question": question,
                   "expected": None if expected == "insufficient" else expected}
                  for number, (expected, question) in enumerate(
                      ((expected, question) for expected, questions in QUERIES.items() for question in questions), 1)]
    return {"benchmark_revision": BENCHMARK_REVISION, "data_version": DATA_VERSION,
            "as_of": OBSERVED_AT, "logistics": shipping, "aftersales": aftersales}


def freeze(path):
    data = records()
    snapshot = {**data, "sha256": fingerprint(data)}
    if path.exists():
        if load_frozen(path) != snapshot:
            raise ValueError("Existing freeze differs; use a new version and path")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as output:
            json.dump(snapshot, output, ensure_ascii=False, indent=2)
            output.write("\n")
    return snapshot


def load_frozen(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    body = {key: value for key, value in payload.items() if key != "sha256"}
    if fingerprint(body) != payload.get("sha256"):
        raise ValueError("Frozen dataset fingerprint mismatch")
    if (payload["benchmark_revision"] != BENCHMARK_REVISION or len(payload["logistics"]) != 100
            or len(payload["aftersales"]) != 60):
        raise ValueError("Invalid frozen corpus revision or case count")
    return payload
