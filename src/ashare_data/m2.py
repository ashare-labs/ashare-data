"""Finite, explicit-opt-in, offline M2 owner view. No execution or account authority."""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from . import m2_source as source
from .m2_payloads import QUERY_CONTRACT_SHA256, economic_payload, query_payload
from .m2_types import (
    M2AD08Call,
    M2ConsumerBinding,
    M2Document,
    M2Read,
    M2ReadContext,
    canonical_hash,
    exact_day,
    exact_int,
    sha,
)
from .model import DataError, require

VERSION = "0.8.0.dev1"
MODE = "conditional_research"
TZ = ZoneInfo("Asia/Shanghai")


def code_identity():
    root = Path(__file__).parent
    return canonical_hash(
        {
            p.relative_to(root).as_posix(): source.raw_hash(p.read_bytes())
            for p in sorted(root.rglob("*"))
            if p.suffix in (".py", ".json")
        }
    )


def _profile(dataset_id, name, policy=None):
    p = policy if policy is not None else source.policy()
    require(name in p["profiles"], "M2_PROFILE_UNREVIEWED", "未登记的有限profile")
    specs = {wid: p["windows"][wid] for wid in p["profiles"][name]}
    assumptions = {}
    for wid, spec in specs.items():
        assumptions[wid] = []
        for aid, body in p["assumptions"].items():
            claim = {
                **body,
                "assumption_id": aid,
                "version": "m2." + aid.lower() + ".v1",
                "window_id": wid,
                "window_spec_sha256": canonical_hash(spec),
                "expansion_annex_sha256": p["annex_sha256"],
                "depends_on": ["A-EQ"] if aid in ("A-AD08", "A-NORMAL") else [],
            }
            ref = {
                "assumption_id": aid,
                "version": claim["version"],
                "content_sha256": canonical_hash(claim),
                "window_id": wid,
            }
            assumptions[wid].append({"reference": ref, "content": claim})
    return {
        "version": name,
        "dataset_id": dataset_id,
        "input_manifest_sha256": p["input_manifest_sha256"],
        "query_contract_sha256": QUERY_CONTRACT_SHA256,
        "rule_policy_sha256": canonical_hash(p["rules"]),
        "window_specs": specs,
        "assumptions": assumptions,
        "original_quality": p["quality"],
        "initial_position": 0,
        "initial_legacy_entitlements": 0,
        "terminal_holding_allowed": True,
        "source_policy": "finite_posthoc_research_under_four_explicit_assumptions",
        "owner_read_permission": "conditional_only",
        "backend_execution_authorized": False,
        "external_product_review": "PENDING",
    }


def profiles(store, dataset_id):
    _, data = source.load(store, dataset_id)
    policy = data.get("_policy", source.policy())
    return tuple(
        M2Document.of(
            {"version": n, "profile_sha256": canonical_hash(_profile(dataset_id, n, policy))}
        )
        for n in policy["profiles"]
    )


class M2View:
    def __init__(self, store, dataset_id, *, profile_sha256, mode):
        require(
            type(mode) is str and mode == MODE,
            "M2_EXPLICIT_MODE_REQUIRED",
            "仅明确conditional_research；不扩strict/paper",
        )
        sha(dataset_id)
        sha(profile_sha256)
        self._store, self._id, self._profile_id = store, dataset_id, profile_sha256
        _, data = source.load(store, dataset_id)
        self._policy = data.get("_policy", source.policy())
        candidates = [_profile(dataset_id, n, self._policy) for n in self._policy["profiles"]]
        matches = [p for p in candidates if canonical_hash(p) == profile_sha256]
        require(len(matches) == 1, "M2_PROFILE_UNREVIEWED", "固定profile不属于此dataset/代码契约")
        self._profile = M2Document.of(matches[0])
        self._owner = self._make_owner()
        self._fresh()

    def _make_owner(self):
        return M2Document.of(
            {
                "version": VERSION,
                "runtime_code_sha256": code_identity(),
                "dataset_id": self._id,
                "manifest_sha256": self._id,
                "profile_sha256": self._profile_id,
                "query_contract_sha256": QUERY_CONTRACT_SHA256,
                "parser_sha256": source.source_identity(),
                "rule_policy_sha256": canonical_hash(self._policy["rules"]),
            }
        )

    def _fresh(self):
        require(
            self._make_owner() == self._owner, "M2_IDENTITY_MISMATCH", "已绑定代码/规则身份改变"
        )
        return source.load(self._store, self._id)[1]

    def descriptor(self):
        self._fresh()
        return self._owner

    def profile(self):
        self._fresh()
        return self._profile

    def _spec(self, window_id):
        specs = self._profile.to_dict()["window_specs"]
        require(
            type(window_id) is str and window_id in specs,
            "M2_WINDOW_UNKNOWN",
            "仅注册的window_id，不能传后端window SHA",
        )
        return specs[window_id]

    def _plan(self, window_id):
        spec = self._spec(window_id)
        body = {
            "schema": "m2.window-plan.v1",
            "owner_pin_sha256": self._owner.sha256,
            "query_contract_sha256": QUERY_CONTRACT_SHA256,
            "window_spec_sha256": canonical_hash(spec),
            "geometry": {
                k: spec[k]
                for k in (
                    "window_id",
                    "security",
                    "start",
                    "end",
                    "warmup",
                    "settlement_successor",
                    "calendar_links",
                )
            },
            "read_roles": spec["read_roles"],
            "source_evidence": spec["evidence_references"],
        }
        return M2Document.of({**body, "window_plan_sha256": canonical_hash(body)})

    def windows(self):
        self._fresh()
        return tuple(self._plan(wid) for wid in self._profile.to_dict()["window_specs"])

    def assumptions(self, window_id):
        self._fresh()
        self._spec(window_id)
        return tuple(
            M2Document.of(a["reference"]) for a in self._profile.to_dict()["assumptions"][window_id]
        )

    def quality(self):
        self._fresh()
        return M2Document.of(self._policy["quality"])

    def lineage(self):
        data = self._fresh()
        return M2Document.of(
            {
                "dataset_id": self._id,
                "source_identities": data["identities"],
                "expansion_annex": data["annex"],
                "input_manifest_sha256": self._policy["input_manifest_sha256"],
            }
        )

    def capabilities(self):
        self._fresh()
        return M2Document.of(
            {
                "version": VERSION,
                "security": "600000.XSHG",
                "frequency": "1d",
                "mode": MODE,
                "network_required": False,
                "snapshot_required": True,
                "windows": list(self._profile.to_dict()["window_specs"]),
                "generic_adjustment": False,
                "historical_pit_verified": False,
                "execution_permission": False,
                "paper_supported": False,
            }
        )

    def use(self, window_id, *, assumption_ack, consumer_binding):
        self._fresh()
        plan = self._plan(window_id)
        expected = [d.to_dict() for d in self.assumptions(window_id)]
        require(
            type(assumption_ack) in (list, tuple) and list(assumption_ack) == expected,
            "M2_ASSUMPTION_REQUIRED",
            "须按公开顺序精确确认四项ID/版本/内容hash/window；旧ack不继承",
        )
        if type(consumer_binding) is dict:
            consumer_binding = M2ConsumerBinding.from_dict(consumer_binding)
        require(
            type(consumer_binding) is M2ConsumerBinding
            and consumer_binding.owner_pin_sha256 == self._owner.sha256
            and consumer_binding.window_id == window_id
            and consumer_binding.window_plan_sha256 == plan.to_dict()["window_plan_sha256"],
            "M2_IDENTITY_MISMATCH",
            "消费声明须匹配owner/window/plan",
        )
        return M2UseView(self, window_id, consumer_binding)


class M2UseView:
    def __init__(self, view, window_id, binding):
        self._view, self._window_id, self._binding = view, window_id, binding

    def envelope(self):
        self._view._fresh()
        plan = self._view._plan(self._window_id).to_dict()
        spec = self._view._spec(self._window_id)
        body = {
            "schema": "m2.envelope.v1",
            "owner": self._view._owner.to_dict(),
            "owner_pin_sha256": self._view._owner.sha256,
            "window_plan": plan,
            "assumption_refs": [x.to_dict() for x in self._view.assumptions(self._window_id)],
            "original_quality": self._view._policy["quality"],
            "data_read_status": "conditional_reads_permitted",
            "modeled_no_relevant_actions": True,
            "historical_pit_verified": False,
            "backend_execution_authorized": False,
            "external_product_review": "PENDING",
            "price_basis_domain": spec["price_basis_domain"],
            "registration_domain": spec["potential_holding_registration_domain"],
        }
        return M2Document.of({**body, "envelope_sha256": canonical_hash(body)})

    def context(self, **kwargs):
        """Construct canonical wire context from the use's fixed consumer declaration."""
        return M2ReadContext(**self._binding.to_dict(), **kwargs)

    def _context(self, context):
        if type(context) is dict:
            context = M2ReadContext.from_dict(context)
        require(type(context) is M2ReadContext, "M2_SCHEMA", "须为M2ReadContext")
        require(
            {f.name: getattr(context, f.name) for f in fields(M2ConsumerBinding)}
            == self._binding.to_dict(),
            "M2_IDENTITY_MISMATCH",
            "跨run/request/strategy/ack/owner/window声明",
        )
        spec = self._view._spec(self._window_id)
        day = context.current_date.isoformat()
        links = {x["current"]: x for x in spec["calendar_links"]}
        require(day in links, "M2_ROLE_SCOPE", "当前日须为本窗执行日，terminal不是执行日")
        local = context.logical_at.astimezone(TZ)
        require(local.date() == context.current_date, "M2_CLOCK_INVALID", "时钟与上海当前日不符")
        if context.phase == "INITIALIZE":
            require(
                day == spec["start"] and local.time() < time(9),
                "M2_CLOCK_INVALID",
                "初始化须首日09前",
            )
        elif context.phase == "DECIDE":
            require(local.time() == time(9), "M2_CLOCK_INVALID", "决策严格09:00")
        elif context.phase == "SUBMIT_MATCH":
            require(local.time() == time(15), "M2_CLOCK_INVALID", "撮合严格15:00")
        else:
            require(local.time() >= time(15), "M2_CLOCK_INVALID", "日末不得早于15:00")
        cutoff = (
            datetime.combine(exact_day(links[day]["previous_open"]), time(23, 59, 59, 999999), TZ)
            if context.phase in ("INITIALIZE", "DECIDE")
            else datetime.combine(context.current_date, time(15), TZ)
        )
        require(
            context.query_end == cutoff, "M2_LOOKAHEAD", "query_end必须等于该角色的固定模型边界"
        )
        return context, spec, links[day]

    def _query(self, context, operation, *, target_date=None, role="current", call=None, n=None):
        c, spec, link = self._context(context)
        target = exact_day(target_date or c.current_date).isoformat()
        if operation == "decision_prev_close":
            require(
                c.consumer == "strategy" and c.phase == "DECIDE",
                "M2_ROLE_SCOPE",
                "P-only决策仅strategy/DECIDE",
            )
            if type(call) is dict:
                call = M2AD08Call.from_dict(call)
            require(
                type(call) is M2AD08Call
                and call.to_dict() in spec["allowed_ad08_calls"]
                and (
                    call.trade_date.isoformat(),
                    call.history_dt.isoformat(),
                    call.adjust_orig.isoformat(),
                )
                == (c.current_date.isoformat(), link["previous_open"], c.current_date.isoformat()),
                "M2_UNREVIEWED_AD08",
                "call必须完整匹配当前T、源P与adjust_orig=T",
            )
            target, role = link["previous_open"], "previous"
        else:
            require(
                c.consumer == "engine", "M2_ROLE_SCOPE", "该接口仅engine，策略不能读取T日模型数据"
            )
        require(
            any(
                r["phase"] == c.phase
                and r["current_date"] == c.current_date.isoformat()
                and r["target_date"] == target
                and r["role"] == role
                and operation in r["operations"]
                for r in spec["read_roles"]
            ),
            "M2_ROLE_SCOPE",
            "operation/phase/target/role不在固定WindowPlan",
        )
        q = query_payload(
            {
                "schema": "m2.query.v1",
                "operation": operation,
                "security": "600000.XSHG",
                "current_date": c.current_date,
                "target_date": target,
                "role": role,
                "call": call.to_dict() if call is not None else None,
                "n": n,
            }
        )
        return c, spec, q

    @staticmethod
    def _listing(q):
        return {
            **{k: v for k, v in q.items() if k not in ("call", "n")},
            "schema": "m2.result.v1",
            "model_listed": True,
            "model_delisted": False,
            "absolute_delisting_date": None,
            "absolute_date_status": "unknown",
            "evidence_status": "derived_under_declared_assumptions",
        }

    def _result(self, q, spec, data):
        op, t, target = q["operation"], q["current_date"], q["target_date"]
        result = {k: v for k, v in q.items() if k not in ("call", "n")}
        result["schema"] = "m2.result.v1"
        p = self._view._policy
        sources = {
            "schema": "m2.source-references.v1",
            "rows": [],
            "annex_sha256": p["annex_sha256"],
            "input_manifest_sha256": p["input_manifest_sha256"],
        }
        if op == "decision_prev_close":
            result.update(
                value=data["bars"][target]["close"],
                unit="CNY/share",
                value_basis="previous_raw_close_times_scoped_ratio",
                scoped_ratio="1",
            )
            sources["rows"] = [
                {k: v for k, v in data["row_sources"][target].items() if k.startswith("price_")}
            ]
            sources["rows"][0].update(date=target, field="close")
        elif op in ("execution_bar", "valuation_bar"):
            row = data["bars"][t]
            result.update({k: row[k] for k in ("open", "high", "low", "close")})
            result.update(
                volume=int(row["volume"]),
                amount=row.get("amount") or None,
                price_basis="raw_unadjusted",
                price_unit="CNY/share",
                volume_unit="share",
                amount_unit="CNY",
                bar_start=None,
                bar_end=None,
            )
            sources["rows"] = [
                dict(
                    data["row_sources"][t],
                    date=t,
                    raw_price_fields=row,
                    raw_state_fields=data["states"][t],
                )
            ]
        elif op == "trading_state":
            s = data["states"][t]
            ref = Decimal(s["preclose"])
            rule = p["rules"]
            result.update(
                suspended=False,
                is_st=False,
                state_evidence_status="source_observed",
                source_preclose=s["preclose"],
                lower_limit=(ref * Decimal("0.9")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                ),
                upper_limit=(ref * Decimal("1.1")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                ),
                price_tick=rule["price_tick"],
                buy_round_lot=rule["buy_round_lot"],
                resale_rule=rule["resale_rule"],
                formula_version=rule["formula_version"],
                rule_policy_sha256=canonical_hash(rule),
                listing=self._listing({**q, "operation": "listing"}),
            )
            sources["rows"] = [
                dict(
                    data["row_sources"][t],
                    date=t,
                    raw_state_fields=s,
                    tradestatus=data["bars"][t]["tradestatus"],
                )
            ]
        elif op in ("listing", "successor"):
            result = self._listing(q)
        else:
            result.update(
                modeled_events=[],
                known_event_ids=[e["id"] for e in data["events"]],
                coverage_evidence_sha256=p["annex_sha256"],
                verified_absent=False,
                source_completeness="unknown",
                status="assumed_no_relevant_events",
                price_basis_domain=spec["price_basis_domain"],
                registration_domain=spec["potential_holding_registration_domain"],
            )
        return economic_payload(result), sources

    def _read(
        self,
        context,
        operation,
        *,
        target_date=None,
        role="current",
        call=None,
        n=None,
        _auth_kind="fresh_read",
    ):
        c, spec, q = self._query(
            context, operation, target_date=target_date, role=role, call=call, n=n
        )
        data = self._view._fresh()
        result, sources = self._result(q, spec, data)
        qdoc, rdoc, sdoc = map(M2Document.of, (q, result, sources))
        plan = self._view._plan(self._window_id).to_dict()
        e = {
            "schema": "m2.read-evidence.v1",
            "owner": self._view._owner.to_dict(),
            "owner_pin_sha256": self._view._owner.sha256,
            "window_id": self._window_id,
            "window_plan_sha256": plan["window_plan_sha256"],
            "query_sha256": qdoc.sha256,
            "result_sha256": rdoc.sha256,
            "source_references_sha256": sdoc.sha256,
            "role": q["role"],
            "current_date": q["current_date"],
            "target_date": q["target_date"],
            "quality": self._view._policy["quality"],
            "assumption_refs": [x.to_dict() for x in self._view.assumptions(self._window_id)],
            "source_evidence_kind": "source_claim_unverified",
        }
        edoc = M2Document.of({**e, "evidence_sha256": canonical_hash(e)})
        auth = {
            "schema": "m2.read-authorization.v1",
            "evidence_sha256": edoc.to_dict()["evidence_sha256"],
            "context": c.to_dict(),
            "authorization_kind": _auth_kind,
            "data_read_permission": "conditional_research",
            "execution_permission": False,
        }
        adoc = M2Document.of({**auth, "authorization_sha256": canonical_hash(auth)})
        return M2Read(qdoc, rdoc, sdoc, edoc, adoc)

    def decision_prev_close(self, context, *, call):
        return self._read(context, "decision_prev_close", call=call)

    def execution_bar(self, context):
        return self._read(context, "execution_bar")

    def valuation_bar(self, context):
        return self._read(context, "valuation_bar")

    def trading_state(self, context):
        return self._read(context, "trading_state")

    def listing(self, context, *, target_date, role):
        return self._read(context, "listing", target_date=target_date, role=role)

    def successor(self, context, *, candidate_date, n=1):
        exact_int(n, minimum=1, maximum=1)
        return self._read(
            context, "successor", target_date=candidate_date, role="settlement_successor", n=n
        )

    def modeled_actions(self, context):
        return self._read(context, "modeled_actions")

    def verify(self, read, *, context):
        """Rebuild all payloads and every identity from the pinned offline inputs before consumption."""
        if type(read) is dict:
            read = M2Read.from_dict(read)
        require(type(read) is M2Read, "M2_SCHEMA", "须为完整M2Read")
        q = query_payload(read.query.to_dict())
        kind = read.authorization.to_dict().get("authorization_kind")
        require(kind in ("fresh_read", "cache_revalidation"), "M2_CONTEXT_STALE", "未知授权类型")
        fresh = self._read(
            context,
            q["operation"],
            target_date=q["target_date"],
            role=q["role"],
            call=q["call"],
            n=q["n"],
            _auth_kind=kind,
        )
        require(
            read == fresh,
            "M2_RECEIPT_MISMATCH",
            "query/result/source/evidence/owner/window/context/auth不符，禁止消费",
        )
        return fresh

    def revalidate(self, read, *, context):
        """New authorization only. Old evidence remains historical, never a new native observation."""
        if type(read) is dict:
            read = M2Read.from_dict(read)
        require(type(read) is M2Read, "M2_SCHEMA", "须为完整M2Read")
        old = M2ReadContext.from_dict(read.authorization.to_dict()["context"])
        self.verify(read, context=old)
        q = read.query.to_dict()
        fresh = self._read(
            context,
            q["operation"],
            target_date=q["target_date"],
            role=q["role"],
            call=q["call"],
            n=q["n"],
            _auth_kind="cache_revalidation",
        )
        require(
            (read.query, read.result, read.sources, read.evidence)
            == (fresh.query, fresh.result, fresh.sources, fresh.evidence),
            "M2_CONTEXT_STALE",
            "缓存经济读域/来源已改变",
        )
        return fresh.authorization

    def require_execution(self):
        raise DataError(
            "M2_BACKEND_ADMISSION_REQUIRED", "owner数据读许可不授予后端执行；须独审与宿主准入"
        )
