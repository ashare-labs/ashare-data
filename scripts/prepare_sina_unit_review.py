"""Prepare the finite unit definitions reviewed against six archived PDF pages.

The lists below are statement rows on the visually checked pages, not a blanket
rule for financial reports. Native field IDs/titles remain distinct. Grammatical
cash-flow title variants do not imply that different fields are interchangeable.
"""

from datetime import datetime, timezone
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re

from ashare_data.financials.unit_resolution import file_reference
from ashare_data.storage.financial_release import immutable_json
from ashare_data.storage.market_release import sha_file

DOCS = {
    "57891513": "f649722afcf3e0a1f3f498f98f8ae343f0e88e410270e76d9d5c556d28f14af9",
    "1201110958": "c357ba6b8d391fac1c7ee762aa390619ba670a8943d5d8bfde0fa167815194aa",
}
PAGES = {
    ("fzb", 8): """
货币资金|结算备付金|拆出资金|交易性金融资产|应收票据|应收账款|预付款项|应收保费|
应收分保账款|应收分保合同准备金|应收利息|应收股利|其他应收款|买入返售金融资产|存货|
一年内到期的非流动资产|其他流动资产|流动资产合计|发放委托贷款及垫款|可供出售金融资产|
持有至到期投资|长期应收款|长期股权投资|投资性房地产|固定资产|在建工程|工程物资|
固定资产清理|生产性生物资产|油气资产|无形资产|开发支出|商誉|长期待摊费用|
递延所得税资产|其他非流动资产|非流动资产合计|资产总计|短期借款
""",
    ("fzb", 9): """
向中央银行借款|吸收存款及同业存放|拆入资金|交易性金融负债|应付票据|应付账款|预收款项|
卖出回购金融资产款|应付手续费及佣金|应付职工薪酬|应交税费|应付利息|应付股利|其他应付款|
应付分保账款|保险合同准备金|代理买卖证券款|代理承销证券款|一年内到期的非流动负债|
其他流动负债|流动负债合计|长期借款|应付债券|长期应付款|专项应付款|预计负债|
递延所得税负债|其他非流动负债|非流动负债合计|负债合计|实收资本（或股本）|资本公积|
减：库存股|专项储备|盈余公积|一般风险准备|未分配利润|外币报表折算差额|
归属于母公司所有者权益合计|少数股东权益|所有者权益合计|负债和所有者权益总计
""",
    ("lrb", 12): """
营业总收入|营业收入|利息收入|已赚保费|手续费及佣金收入|营业总成本|营业成本|利息支出|
手续费及佣金支出|退保金|赔付支出净额|提取保险合同准备金净额|保单红利支出|分保费用|
营业税金及附加|销售费用|管理费用|财务费用|资产减值损失|公允价值变动收益|投资收益|
对联营企业和合营企业的投资收益|汇兑收益|营业利润|营业外收入|营业外支出|非流动资产处置损失|
利润总额|所得税费用|净利润|归属于母公司所有者的净利润|少数股东损益|其他综合收益|
综合收益总额|归属于母公司所有者的综合收益总额|归属于少数股东的综合收益总额
""",
    ("llb", 14): """
销售商品、提供劳务收到的现金|客户存款和同业存放款项净增加额|向中央银行借款净增加额|
向其他金融机构拆入资金净增加额|收到原保险合同保费取得的现金|收到再保险业务现金净额|
保户储金及投资款净增加额|处置交易性金融资产净增加额|收取利息、手续费及佣金的现金|
拆入资金净增加额|回购业务资金净增加额|收到的税费返还|收到其他与经营活动有关的现金|
经营活动现金流入小计|购买商品、接受劳务支付的现金|客户贷款及垫款净增加额|
存放中央银行和同业款项净增加额|支付原保险合同赔付款项的现金|支付利息、手续费及佣金的现金|
支付保单红利的现金|支付给职工以及为职工支付的现金|支付的各项税费|
支付其他与经营活动有关的现金|经营活动现金流出小计|经营活动产生的现金流量净额|
收回投资收到的现金|取得投资收益收到的现金|处置固定资产、无形资产和其他长期资产收回的现金净额|
处置子公司及其他营业单位收到的现金净额|收到其他与投资活动有关的现金|投资活动现金流入小计|
购建固定资产、无形资产和其他长期资产支付的现金|投资支付的现金|质押贷款净增加额|
取得子公司及其他营业单位支付的现金净额|支付其他与投资活动有关的现金|投资活动现金流出小计|
投资活动产生的现金流量净额
""",
    ("llb", 15): """
吸收投资收到的现金|子公司吸收少数股东投资收到的现金|取得借款收到的现金|发行债券收到的现金|
收到其他与筹资活动有关的现金|筹资活动现金流入小计|偿还债务支付的现金|
分配股利、利润或偿付利息支付的现金|子公司支付给少数股东的股利、利润|
支付其他与筹资活动有关的现金|筹资活动现金流出小计|筹资活动产生的现金流量净额|
汇率变动对现金及现金等价物的影响|现金及现金等价物净增加额|期初现金及现金等价物余额|
期末现金及现金等价物余额
""",
}


def label(value):
    value = re.sub(r"\s+", "", value).replace("（", "(").replace("）", ")").replace("：", ":")
    value = re.sub(r"^(?:其中|加|减):", "", value)
    for before, after in (
        ("所收到的现金", "收到的现金"), ("所支付的现金", "支付的现金"),
        ("所收回的现金", "收回的现金"),
        ("收到的其他与", "收到其他与"), ("支付的其他与", "支付其他与"),
    ):
        value = value.replace(before, after)
    return value


def prepare_review(root):
    root = Path(root)
    audit_path = root / "coordination/unit-evidence-20261020/metadata-audit/audit.json"
    audit = json.loads(audit_path.read_bytes())
    anchors = {}
    for (statement, page), values in PAGES.items():
        for value in values.split("|"):
            value = value.strip()
            if value:
                anchors[(statement, label(value))] = ("57891513", page, value, "CNY")
    for value in ("基本每股收益", "稀释每股收益"):
        anchors[("lrb", value)] = ("1201110958", 27, value + "(元/股)", "CNY/share")
    groups = {tuple(g["key"][:3]) for g in audit["groups"]
              if g["key"][7] == "unverified" and g["key"][9]}
    definitions = []
    for statement, field, title in sorted(groups):
        anchor = anchors.get((statement, label(title)))
        if not anchor:
            continue
        aid, page, anchor_label, unit = anchor
        definitions.append({
            "statement": statement, "source_field": field, "source_title": title,
            "unit": unit, "scale_decimal_text": "1", "reviewed": True,
            "anchor_document_sha256": DOCS[aid], "anchor_announcement_id": aid,
            "anchor_page": page, "anchor_label": anchor_label,
            "header_page": {"fzb": 8, "lrb": 12, "llb": 14}[statement] if unit == "CNY" else None,
            "native_title_match": "exact_after_punctuation_prefix_and_reviewed_cashflow_grammar",
            "unit_only_not_canonical_semantic_equivalence": True,
        })
    evidence = []
    official = root / "batches/20261019-official-v1"
    for aid, digest in DOCS.items():
        receipt_path = official / "receipts" / f"pdf-{aid}.json"
        receipt = json.loads(receipt_path.read_bytes())
        path = official / receipt["path"]
        assert hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest() == digest == receipt["sha256"]
        evidence.extend([file_reference(path), file_reference(receipt_path)])
    for relative in (
        "source-review-20261019/sina-finance-page.body.gz",
        "source-review-20261019/sina-finance-page.receipt.json",
        "coordination/unit-evidence-20261020/sina-cncw2022.js.gz",
        "coordination/unit-evidence-20261020/sina-cncw2022.receipt.json",
    ):
        evidence.append(file_reference(root / relative))
    script = gzip.decompress((root / "coordination/unit-evidence-20261020/sina-cncw2022.js.gz").read_bytes())
    assert hashlib.sha256(script).hexdigest() == "5a79412a56ba365e41fc8a5efa1d4bfc9ead2aae86491f8fad382a52096eb339"
    return {
        "schema": "ad.financial-unit-definition-review.v1",
        "method": "reviewed_statement_headers_and_native_provider_display",
        "audit_sha256": sha_file(audit_path),
        "available_from": datetime.now(timezone.utc).isoformat(),
        "reviewed_document_sha256": list(DOCS.values()),
        "evidence": evidence, "definitions": definitions,
        "historical_visibility_added": False,
        "evidence_boundary": [
            "Statement headers define money as yuan/CNY; EPS rows explicitly say yuan/share.",
            "Sina report rCurrency=CNY and rType=consolidated are audited for the complete frozen scope.",
            "Sina Yaccuracy forwards raw f2/ps4 numbers to Yuniter; wan/yi are display abbreviations.",
            "Display precision alone is not a unit: MINYSHARINCO also uses ps4.",
            "Only listed field/title/statement combinations qualify. No blanket CNY fallback.",
            "This is a reviewed source-definition inference, not a native per-field unit declaration.",
            "PDF template rows establish unit classes, not correctness or historical visibility of Sina values.",
        ],
        "reviewed_pages": {"57891513": [8, 9, 12, 14, 15], "1201110958": [27]},
        "derivation_script_sha256": sha_file(__file__),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    immutable_json(Path(args.output), prepare_review(args.root))
