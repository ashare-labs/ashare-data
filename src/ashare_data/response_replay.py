"""Offline transport for the real intraday collector, not a market simulator."""

from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path

from .model import canonical, require, timestamp
from .daily_observation import sha


class SealedTransport:
    """One strict request sequence; both receipt time and failed responses survive.

    Clock calls follow capture's start, budget/start/receipt per request, then
    completion/read/finish. Exhaustion is an error, never a live-network fallback.
    """

    def __init__(self, event, blobs, *, mutation=None, corpus_sha256=None, quote_refresh_rounds=0):
        self.event = deepcopy(event)
        self.blobs = Path(blobs)
        self.overrides = {}
        self.position = 0
        self.mutation = mutation
        if mutation:
            self._mutate(mutation)
        self.provenance = dict(
            schema="sealed-response-replay.v1",
            market_result=False,
            input_sha256=sha(canonical(event)),
            original_record_sha256=event["record_sha256"],
            classification="derived_fault_injection" if mutation else "recorded_response_replay",
            mutation=mutation,
            completion_basis=event["completion_basis"],
            corpus_sha256=corpus_sha256,
            derived_schedule=event.get("derived_schedule"),
            quote_refresh_rounds=quote_refresh_rounds,
        )
        if event.get("derived_schedule"):
            self.provenance["classification"] = "derived_response_schedule"
        self.identity = sha(canonical(self.provenance))
        times = [self.event["started_at"]]
        for response in self.event["responses"]:
            times += [response["started_at"], response["started_at"], response["observed_at"]]
        times += [self.event["completed_at"]] * 3
        self.times = iter(times)

    def now(self):
        value = next(self.times)
        timestamp(value)  # validate awareness without rewriting the recorded offset
        return datetime.fromisoformat(value)

    def fetch(self, url, referer):
        require(self.position < len(self.event["responses"]), "REPLAY_EXHAUSTED", url)
        item = self.event["responses"][self.position]
        self.position += 1
        require(
            (url, referer) == (item["url"], item["referer"]), "REPLAY_REQUEST_ORDER", item["name"]
        )
        if "error" in item:
            error = item["error"]
            # Preserve the reported exception name/message, without reconstructing
            # SSL objects or wrapping an already-rendered HTTPError message again.
            cls = type(error["type"], (OSError,), {})
            exc = cls(error["message"])
            exc.code = error.get("http_status")
            raise exc
        raw = self.overrides.get(item["name"])
        if raw is None:
            raw = (self.blobs / item["sha256"]).read_bytes()
        require(sha(raw) == item["sha256"], "REPLAY_BLOB_CHANGED", item["name"])
        return raw

    def _mutate(self, mutation):
        name = mutation["source"]
        item = next(r for r in self.event["responses"] if r["name"] == name)
        operation = mutation["operation"]
        if operation in {"disconnect", "429", "503"}:
            item.pop("sha256", None)
            item.pop("http_status", None)
            status = int(operation) if operation != "disconnect" else None
            item["error"] = dict(
                type="HTTPError" if status else "ConnectionError",
                message="derived replay fault: " + operation,
                http_status=status,
            )
            return
        if operation == "receipt_before_request":
            item["observed_at"] = self.event["started_at"]
            return
        if operation == "overlapping_requests":
            item["started_at"] = self.event["started_at"]
            return
        if operation == "utc_equivalent":
            from datetime import timezone

            for row in self.event["responses"]:
                for key in ("started_at", "observed_at"):
                    row[key] = timestamp(row[key]).astimezone(timezone.utc).isoformat()
            self.event["not_before"] = (
                timestamp(self.event["not_before"]).astimezone(timezone.utc).isoformat()
            )
            return
        raw = (self.blobs / item["sha256"]).read_bytes()
        value = json.loads(raw)
        if operation == "old_quote_day":
            value["date"] = int(mutation["date"])
        elif operation == "milliseconds_as_hhmmss":
            value["time"] *= 1000
        elif operation == "missing_snapshot_field":
            value["list"][0].pop()
        elif operation == "partial_list":
            value["pageHelp"]["total"] += 1
        elif operation in {
            "minute_volume_lots",
            "fractional_volume",
            "missing_minute_field",
            "old_minute_day",
            "zero_volume_and_future_quote",
        }:
            for row in value:
                if operation == "minute_volume_lots":
                    row["volume"] = str(int(row["volume"]) // 100)
                elif operation == "fractional_volume":
                    row["volume"] = "0.5"
                elif operation == "missing_minute_field":
                    row.pop("volume", None)
                elif operation == "zero_volume_and_future_quote":
                    row["volume"] = "0"
                else:
                    row["day"] = mutation["date"] + row["day"][10:]
        else:
            raise ValueError("Unknown derived mutation: " + operation)
        raw = canonical(value)
        item["sha256"] = sha(raw)
        self.overrides[name] = raw
        # Unit tests of volume/labels modify both actual observations explicitly,
        # so they test volume/causality rather than merely triggering revision.
        if operation in {"minute_volume_lots", "old_minute_day", "zero_volume_and_future_quote"}:
            confirm = next(
                r for r in self.event["responses"] if r["name"] == "minute_confirm_" + name[-6:]
            )
            confirm["sha256"] = sha(raw)
            self.overrides[confirm["name"]] = raw
        if operation == "zero_volume_and_future_quote":
            quote = next(
                r for r in self.event["responses"] if r["name"] == "sse_quote_" + name[-6:]
            )
            value = json.loads((self.blobs / quote["sha256"]).read_bytes())
            # Explicit fault injection, not a correction of the original label.
            future = timestamp(quote["observed_at"]) + timedelta(seconds=1)
            value["time"] = int(future.strftime("%H%M%S"))
            raw = canonical(value)
            quote["sha256"] = sha(raw)
            self.overrides[quote["name"]] = raw


def replay(root, event, blobs, *, mutation=None, corpus_sha256=None, quote_refresh_rounds=0):
    from .intraday_observation import capture, read

    transport = SealedTransport(
        event,
        blobs,
        mutation=mutation,
        corpus_sha256=corpus_sha256,
        quote_refresh_rounds=quote_refresh_rounds,
    )
    attempt = capture(
        root,
        event["kind"],
        event["day"],
        transport.event["not_before"],
        replay=transport,
        quote_refresh_rounds=quote_refresh_rounds,
    )
    require(
        transport.position == len(event["responses"]),
        "REPLAY_INCOMPLETE",
        "collector stopped early",
    )
    packet = None
    if attempt["capture_id"]:
        packet = read(
            root, attempt["capture_id"], replay_clock=lambda: timestamp(event["completed_at"])
        )
    return attempt, packet
