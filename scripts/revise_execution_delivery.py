"""Create an immutable delivery-reference revision; do not change bound data."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys

from execution_delivery_refs import action_reference, audit_delivery, check, digest, load


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def rebase(value, old, new):
    if isinstance(value, dict):
        return {k: rebase(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [rebase(v, old, new) for v in value]
    if isinstance(value, str) and value.startswith(str(old) + "/"):
        return str(new) + value[len(str(old)) :]
    return value


def seal(root, commit):
    """Seal only after semantic checks and any verification reports are final."""
    inventory = [
        dict(path=str(p.relative_to(root)), bytes=p.stat().st_size, sha256=digest(p))
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and "wheel-runtime" not in p.parts
        and p.name not in {"evidence-manifest.json", "delivery-checksums.json"}
    ]
    handoff = load(root / "handoff.json")
    save(
        root / "evidence-manifest.json",
        dict(
            schema="ad.order-impact-evidence.v1",
            role="Analysis provenance only; not a DailyExecutionView binding",
            source_commit=commit,
            api_version="0.9.1.dev13",
            source_facts_changed=False,
            source_bindings=dict(
                research=handoff["research_binding_sha256"],
                execution=handoff["execution_binding_sha256"],
            ),
            inventory=inventory,
        ),
    )
    save(
        root / "delivery-checksums.json",
        {
            name: digest(root / name)
            for name in (
                "HANDOFF.md",
                "handoff.json",
                "consumer-contract.json",
                "evidence-manifest.json",
                "dist/ashare_data-0.9.1.dev13-py3-none-any.whl",
            )
        },
    )


def revise(source, root, research, execution, commit):
    source, root, research, execution = (p.resolve() for p in (source, root, research, execution))
    check(not root.exists(), "Refusing to overwrite an existing delivery")
    old = load(source / "handoff.json")
    check(
        digest(research / "binding.json") == old["research_binding_sha256"],
        "Research binding changed",
    )
    check(
        digest(execution / "binding.json") == old["execution_binding_sha256"],
        "Execution binding changed",
    )
    shutil.copytree(source, root, ignore=shutil.ignore_patterns("wheel-runtime", "__pycache__"))
    scripts = Path(__file__).resolve().parent
    for filename in (
        "execution_delivery_refs.py",
        "verify_order_impact.py",
        "revise_execution_delivery.py",
    ):
        shutil.copyfile(scripts / filename, root / "verification" / filename)

    # Replay only the frozen minimal local order requests, not a running AQ tree.
    subprocess.run(
        [
            sys.executable,
            str(root / "verification/verify_order_impact.py"),
            "--research-package",
            str(research),
            "--execution-package",
            str(execution),
            "--output-package",
            str(root),
            "--captured-orders",
            str(source),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    cash = load(root / "reports/cash-event-contract.json")
    check(
        cash["paths"]["research_event"]
        == action_reference(research / "binding-data.json", cash["source_event"]),
        "Generated raw research event reference differs",
    )
    check(
        load(root / "reports/impact-summary.json") == load(source / "reports/impact-summary.json"),
        "Order impact changed during a reference-only revision",
    )
    check(
        cash["source_event"] == load(source / "reports/cash-event-contract.json")["source_event"],
        "Cash event facts changed",
    )

    contract = rebase(load(source / "consumer-contract.json"), source, root)
    contract["unresolved"]["details"] = "reports/exact-gap-scope.json"
    contract.update(delivery_revision="r1", source_commit=commit, path_base="package_root")
    save(root / "consumer-contract.json", contract)
    handoff = rebase(old, source, root)
    handoff.update(
        commit=commit,
        parent_commit=old["commit"],
        delivery_revision="r1",
        created_at=datetime.now(timezone.utc).isoformat(),
        previous_delivery=dict(
            root=str(source),
            evidence_manifest_sha256=digest(source / "evidence-manifest.json"),
            findings=["AD-R1", "AD-R2"],
            preserved=True,
        ),
        reference_validation=str(root / "reports/reference-audit.json"),
        source_facts_changed=False,
        runtime_gate_changed=False,
        wheel_changed=False,
        verification_note="Original regression and smoke records remain historical evidence. Revision reference audit and captured-order replay are new; no runtime code changed.",
    )
    handoff["consumer_next_steps"][0] = (
        "Parent integrates prior code chain through 56dbd17 and this delivery-tooling fix; keep both data bindings and the dev13 runtime wheel unchanged."
    )
    save(root / "handoff.json", handoff)

    # The old smoke report is historical. Make the copied runnable script target
    # this revision rather than overwrite the original frozen smoke report.
    smoke = root / "verification/verify_followup_wheel.py"
    contents = smoke.read_text().replace(
        'root = here / "execution-followup"', "root = Path(__file__).resolve().parents[1]"
    )
    contents = contents.replace(
        'here.parent / "task-15/research-package/binding.json"',
        'json.loads((root / "handoff.json").read_text())["research_binding"]',
    ).replace(
        'here / "execution-package/binding.json"',
        'json.loads((root / "handoff.json").read_text())["execution_binding"]',
    )
    smoke.write_text(contents)
    (root / "HANDOFF.md").write_text(f"""# AD 交付引用修订 r1

修复提交：`{commit}`；父提交 `{old["commit"]}`。API 和 wheel 仍为 0.9.1.dev13。

独立验收 AD-R1 / AD-R2 已修复：格力研究事件指针由生成器按 security + ex_date 唯一定位，核验现金、登记/支付日、送转比例和除权扣减后生成，固定研究包结果为 **/actions/3**；执行补充事件为 /actions/0。消费契约的 unresolved.details 现指向本包 **reports/exact-gap-scope.json**，路径基准为包根目录。

原冻结包保留在 [{source / "HANDOFF.md"}]({source / "HANDOFF.md"})。其中旧错误只作历史记录，以本修订契约消费。未改行情事实、两个数据 binding、dev13 wheel 或运行门禁，未调用 AQ/模拟或 JQ，未重新采集。

- [机器交接](handoff.json)；[消费契约](consumer-contract.json)；[现金事件及来源指针](reports/cash-event-contract.json)。
- [全部同类引用审计](reports/reference-audit.json)；[精确缺口](reports/exact-gap-scope.json)；[订单影响复算](reports/impact-summary.json)。
- [原定向/完整回归证据](reports/regression.xml) 为此前记录。修订测试结果在最终交接附录列明；不能把旧记录冒充本轮全量测试。

2024 仍为 **232 个证券日 / 696 字段**未知，三策略涉及 **8/17/45 笔**已记录订单；严格执行仍拒绝所需未知字段。格力现金权益仍为每股 1 元，除权参考扣减仍为 0.936124 元，实际现金日内可用时点仍未知。来源事实与条件研究情景分开。

复现路径/事件检查（只读）：

```sh
python verification/execution_delivery_refs.py . --research '{research}' --execution '{execution}'
```

生成器和检查器在 verification/，已同时提交到独立 AD 树 scripts/。verify_order_impact.py 从 --captured-orders 指定的固定包读取最小订单请求，不访问正在运行的 AQ 树。evidence-manifest.json 是分析清单，不能代替数据 binding。
""")
    # The audit names its output, so establish the declared path before checking
    # it; sealing below happens only after the complete audit succeeds.
    save(root / "reports/reference-audit.json", dict(result="pending"))
    audit = audit_delivery(root, research, execution)
    save(root / "reports/reference-audit.json", audit)
    seal(root, commit)
    return dict(
        result="passed",
        root=str(root),
        commit=commit,
        evidence_manifest_sha256=digest(root / "evidence-manifest.json"),
        paths=len(audit["paths"]),
        pointers=len(audit["pointers"]),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "output", "research", "execution"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            revise(args.source, args.output, args.research, args.execution, args.commit), indent=2
        )
    )


if __name__ == "__main__":
    main()
