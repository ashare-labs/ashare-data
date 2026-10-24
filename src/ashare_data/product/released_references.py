"""Retrospective daily reference facts from the price release's frozen file list.

No collection, calendar synthesis, percentage-limit model or mutable current views.
The admitted evidence is frozen into the ordinary product publication as before.
"""

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ashare_data.core.model import digest, require
from ashare_data.financials.tables import TABLES
from ashare_data.storage.market_release import MarketRelease, connection, sql_literal

ASSUMPTIONS = {"accept_daily_replay": True, "accept_source_observed_limits": True}


class ReleasedReferences:
    def __init__(self, pin, price_release, *, release=None):
        require(
            price_release is not None and all(
                pin[k] == price_release.get(k) for k in ("root", "release_id", "manifest_sha256")
            ),
            "PRODUCT_REFERENCE_RELEASE_CONFLICT",
            "Prices and references must use the same immutable release",
        )
        if release is not None:
            require(
                isinstance(release, MarketRelease)
                and release.root == Path(pin["root"]).expanduser().resolve()
                and release.release_id == pin["release_id"]
                and release.sha256 == pin["manifest_sha256"],
                "PRODUCT_REFERENCE_RELEASE_CONFLICT",
                "Shared reader must use the same immutable release",
            )
        self.release = release or MarketRelease(
            pin["root"], pin["release_id"], expected_manifest_sha256=pin["manifest_sha256"]
        )
        self.connection = connection(memory="512MB")
        self.connection.execute("SET temp_directory='' ")
        self.views = set()

    def close(self):
        self.connection.close()

    def view(self, table):
        require(table in TABLES, "PRODUCT_REFERENCE_TABLE", "Unknown reference table")
        if table not in self.views:
            schema = table.split(".")[0]
            self.connection.execute("CREATE SCHEMA IF NOT EXISTS " + schema)
            paths = [self.release.verify(x) for x in self.release.manifest.get("evidence", [])
                     if x.get("kind") == "financial_table" and x.get("table") == table]
            expression = (
                "SELECT DISTINCT * FROM read_parquet([" + ",".join(map(sql_literal, paths)) + "])"
                if paths else "SELECT " + ",".join(
                    f"NULL::{kind} AS {name}" for name, kind in TABLES[table].items()
                ) + " WHERE false"
            )
            self.connection.execute("CREATE VIEW " + table + " AS " + expression)
            self.views.add(table)
        return table

    def query(self, table, condition, parameters):
        cursor = self.connection.execute(
            "SELECT * FROM " + self.view(table) + " WHERE " + condition + " LIMIT 100001",
            parameters,
        )
        columns = [x[0] for x in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        require(len(rows) <= 100000, "PRODUCT_RESOURCE_LIMIT", "Narrow the reference query")
        return sorted(rows, key=digest)

    def selected(self, table, securities, start=None, end=None):
        clause, values = "security IN (" + ",".join("?" for _ in securities) + ")", list(securities)
        if start is not None:
            clause += " AND trade_date BETWEEN ? AND ?"
            values += [start, end]
        return self.query(table, clause, values)

    def ref(self, row, table):
        return {"table": table, "observation_id": row.get("observation_id", row.get("event_id")),
                "source": row["source"], "source_revision_id": row["source_revision_id"],
                "release_id": self.release.release_id, "manifest_sha256": self.release.sha256}

    def market(self, securities, start, end, days, prices):
        limits = defaultdict(list)
        statuses = defaultdict(list)
        for table, target in (("reference.price_limits", limits),
                              ("reference.security_status", statuses)):
            for row in self.selected(table, securities, start, end):
                require(row["instrument_id"] == self.release.identifiers[row["security"]],
                        "PRODUCT_REFERENCE_IDENTITY", "Reference identity differs from this release")
                target[row["security"], row["trade_date"]].append(row)
        daily = {(r["security"], r["date"]): r for r in prices}
        states, admissions, gaps = [], [], []
        require(len(securities) * len(days) <= 50000, "PRODUCT_RESOURCE_LIMIT",
                "Ordinary coverage supports at most 50000 security sessions")
        for sid in securities:
            for day in days:
                first_gap = len(gaps)
                key = sid, day
                row = daily.get(key, {})
                candidates = defaultdict(list)
                origin = {"table": "market.bars_1d", "row_hash": row.get("row_hash"),
                          "response_id": row.get("response_id"), "source": row.get("source"),
                          "release_id": self.release.release_id,
                          "manifest_sha256": self.release.sha256}
                for field, value in (("suspended", {"0": True, "1": False}.get(row.get("tradestatus"))),
                                     ("is_st", {"0": False, "1": True}.get(row.get("is_st"))),
                                     ("reference_price", row.get("preclose"))):
                    if value is not None and origin["row_hash"] and origin["response_id"]:
                        candidates[field].append((value, origin))
                for item in statuses[key]:
                    for field, native in (("suspended", "is_paused"), ("is_st", "is_st")):
                        value = {"0": False, "1": True}.get(item[native])
                        if value is not None:
                            candidates[field].append((value, self.ref(item, "reference.security_status")))
                for item in limits[key]:
                    if item["unit"] != "CNY/share":
                        gaps.append(_gap(sid, day, "price_limits", "PRODUCT_REFERENCE_UNIT",
                                         "Price limit unit is not confirmed CNY/share"))
                        continue
                    for field, native in (("upper_limit", "up_limit"), ("lower_limit", "down_limit"),
                                          ("reference_price", "previous_close")):
                        if item[native] is not None:
                            candidates[field].append((item[native], self.ref(item, "reference.price_limits")))
                state = {"security": sid, "date": day, "dependency_conflicts": [],
                         "rule": {"kind": "source_observed_limits"}}
                for field in ("suspended", "is_st", "reference_price", "upper_limit", "lower_limit"):
                    fact, problem = _fact(candidates[field], boolean=field in {"suspended", "is_st"})
                    state[field] = fact
                    if problem and not (field == "is_st" and problem == "PRODUCT_STATE_UNAVAILABLE"):
                        gaps.append(dict(_gap(sid, day, field, problem, "Missing, invalid or conflicting source facts"),
                                         owner_issue=fact))
                states.append(state)
                issues = [g["code"] for g in gaps[first_gap:]]
                if state["suspended"]["value"] is True:
                    issues.append("DAILY_SUSPENDED")
                for side in ("BUY", "SELL"):
                    admissions.append(dict(security=sid, date=day, side=side, ready=not issues,
                                           issues=issues, state_sha256=digest(state),
                                           historical_pit=False, execution_permission=False))
        descriptor = dict(binding_sha256=self.release.sha256, release=self.release.receipt(),
                          historical_pit=False, execution_permission=False,
                          contract="released-retrospective-reference.v1")
        return dict(kind="market_release", rows=states, admissions=admissions,
                    accepted_assumptions=dict(ASSUMPTIONS), descriptor=descriptor, reads=[]), gaps


def _gap(sid, day, field, code, detail):
    return dict(security=sid, date=day, field=field, code=code, detail=detail)


def _fact(candidates, *, boolean=False):
    values = set()
    invalid = False
    for value, _ in candidates:
        try:
            valid = type(value) is bool if boolean else Decimal(str(value)).is_finite() and Decimal(str(value)) > 0
            if valid:
                values.add(value if boolean else Decimal(str(value)))
            else:
                invalid = True
        except (InvalidOperation, ValueError):
            invalid = True
    problem = ("PRODUCT_REFERENCE_VALUE" if invalid else "PRODUCT_SOURCE_STATE_CONFLICT"
               if len(values) > 1 else "PRODUCT_STATE_UNAVAILABLE" if not values else None)
    return dict(value=candidates[0][0] if problem is None else None,
                status="source_observed" if problem is None else "unavailable",
                historical_available_at=None,
                source_refs=[dict(ref, value=value) for value, ref in candidates]), problem
