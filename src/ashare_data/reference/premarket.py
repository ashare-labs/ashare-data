"""Frozen source-native reference markers for explicitly bounded research valuation.

This does not turn a retrospectively captured daily file into a historical PIT feed.
Only a separately encoded prevclose is projected; ex-day OHLC is never consumed.
"""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from ashare_data.research.independent_queries import IndependentQueryView, _decimal, _json
from ashare_data.core.model import day, require, symbol, timestamp

ROLE = "premarket_reference_reconstruction"
POLICY = "sina_native_prevclose_known_cash_research.v1"
CAPITALIZATION_POLICY = "sina_native_prevclose_known_cash_capitalization_research.v1"


class PremarketReferenceView(IndependentQueryView):
    """Read one frozen ex-session marker, with issuer and calendar crosschecks."""

    def read(self, security, *, as_of, role):
        require(role == ROLE, "PREMARKET_ROLE", "Explicit reconstruction role required")
        b = _json(self._checked({"path": self.path.name, "sha256": self.sha256}))
        require(
            b.get("schema") == "ad.premarket-reference-binding.v1"
            and b.get("policy") in {POLICY, CAPITALIZATION_POLICY}
            and b.get("role") == ROLE
            and b.get("historical_pit") is False
            and b.get("strategy_visible") is False
            and b.get("matching_eligible") is False
            and b.get("security") == symbol(security),
            "PREMARKET_SCOPE",
            "Invalid reconstruction binding or security",
        )
        at, model_at = timestamp(as_of), timestamp(b["model_available_at"])
        ex, prior = day(b["ex_date"]), day(b["previous_session"])
        require(
            at == model_at
            and str(at.date()) == ex
            and at.utcoffset() == timedelta(hours=8)
            and (at.hour, at.minute, at.second, at.microsecond) == (9, 0, 0, 0),
            "PREMARKET_CLOCK",
            "Only the bound ex-session 09:00 model phase is admitted",
        )
        calendar = _json(self._checked(b["calendar"]))
        dates = calendar["dates"]
        require(
            dates == sorted(set(day(d) for d in dates))
            and ex in dates
            and dates.index(ex) > 0
            and dates[dates.index(ex) - 1] == prior,
            "PREMARKET_CALENDAR",
            "Bound raw close must be the adjacent previous session",
        )
        action = _json(self._checked(b["cash_fact"]))
        capitalization = _decimal(action["capitalization_share_ratio"])
        with_capitalization = b["policy"] == CAPITALIZATION_POLICY
        require(
            action.get("schema") == "ad.document-cash-dividend-fact.v1"
            and action.get("security") == security
            and action.get("event_type")
            == ("cash_dividend_capitalization" if with_capitalization else "cash_dividend")
            and day(action["announcement_date"]) < prior
            and action.get("record_date") == prior
            and action.get("ex_date") == ex
            and action.get("payment_date") == ex
            and action.get("currency") == "CNY"
            and _decimal(action["bonus_share_ratio"]) == 0
            and (
                (
                    capitalization > 0
                    and action.get("new_shares_tradable_date") == ex
                    and action.get("shares_credit_date") == ex
                )
                if with_capitalization
                else capitalization == 0
            ),
            "PREMARKET_ACTION",
            "Requires an earlier announced cash event and policy-consistent share dates",
        )
        document = b["primary_document"]
        self._checked(document)
        require(
            document.get("document_date") == action["announcement_date"],
            "PREMARKET_ACTION",
            "Primary document date conflicts with action",
        )
        s = b["source"]
        require(
            s.get("kind") == "sina_daily_klc_decoded"
            and s.get("security") == security
            and s.get("adjustment") == "none"
            and s.get("source_publication_time") is None
            and s.get("historical_received_at") is None
            and timestamp(s["captured_start"]) <= timestamp(s["captured_end"]),
            "PREMARKET_SOURCE",
            "Invalid independent source identity or capture",
        )
        raw = self._checked(s["raw"])
        self._checked(s["decoder"])
        prefix = (
            "var KLC_K2_" + ("sz" if security.endswith("XSHE") else "sh") + security[:6] + '="'
        ).encode()
        require(raw.startswith(prefix), "PREMARKET_SOURCE", "Raw symbol envelope mismatch")
        decoded = _json(self._checked(s))
        require(
            decoded.get("raw_sha256") == s["raw"]["sha256"]
            and decoded.get("decoder_sha256") == s["decoder"]["sha256"]
            and type(decoded.get("rows")) is list,
            "PREMARKET_SOURCE",
            "Decoder lineage mismatch",
        )
        # No daily projection: it would read same-day OHLC/turnover unnecessarily.
        selected = {}
        for r in decoded["rows"]:
            label = r.get("date")
            if label not in (prior + "T00:00:00.000Z", ex + "T00:00:00.000Z"):
                continue
            require(label not in selected, "PREMARKET_SOURCE", "Duplicate selected date")
            selected[label] = r
        require(
            len(selected) == 2 and "prevclose" in selected[ex + "T00:00:00.000Z"],
            "PREMARKET_MISSING",
            "Source-native ex-session prevclose is required; no fallback",
        )
        raw_close = _decimal(selected[prior + "T00:00:00.000Z"]["close"])
        reference = _decimal(selected[ex + "T00:00:00.000Z"]["prevclose"])
        gross = _decimal(action["gross_cash_per_share"])
        tick = _decimal(b["source_price_unit"])
        theoretical = (raw_close - gross) / (1 + capitalization)
        require(
            raw_close > gross > 0
            and reference > 0
            and tick == Decimal("0.01")
            and reference % tick == 0
            and abs(reference - theoretical) < tick,
            "PREMARKET_CONSISTENCY",
            "Source marker inconsistent with documented distribution within one price unit",
        )
        return {
            "schema": "ad.premarket-reference-result.v1",
            "binding_sha256": self.sha256,
            "security": security,
            "status": "available",
            "role": ROLE,
            "policy": b["policy"],
            "historical_pit": False,
            "strategy_visible": False,
            "matching_eligible": False,
            "current_quote": False,
            "historical_available_at": None,
            "model_available_at": b["model_available_at"],
            "source_capture": {k: s[k] for k in ("captured_start", "captured_end")},
            "row": {
                "ex_date": ex,
                "previous_session": prior,
                "reference_price": str(reference),
                "raw_previous_close": str(raw_close),
                "gross_cash_per_share": str(gross),
                **(
                    {"capitalization_share_ratio": str(capitalization)}
                    if with_capitalization
                    else {}
                ),
            },
            "origin": "source_encoded_prevclose; no rounding or factor inversion",
            "consistency": {
                "raw_close_minus_gross": str(raw_close - gross),
                **(
                    {"cash_and_capitalization_theoretical": str(theoretical)}
                    if with_capitalization
                    else {}
                ),
                "source_marker_minus_theoretical": str(reference - theoretical),
                "test": "difference smaller than one source price unit; not a rounding rule",
            },
            "evidence": {
                "decoded": {k: s[k] for k in ("path", "sha256")},
                **{k: deepcopy(s[k]) for k in ("raw", "decoder")},
                **{k: deepcopy(b[k]) for k in ("cash_fact", "primary_document", "calendar")},
            },
            "limitations": [
                "Retrospective source capture, no historical receipt/revision proof",
                "09:00 applicability is an explicit bounded reconstruction assumption",
                "No exchange certification or inference of provider/JQ rounding",
                "Same-day OHLC, turnover, later factors and later prices excluded",
            ],
        }
