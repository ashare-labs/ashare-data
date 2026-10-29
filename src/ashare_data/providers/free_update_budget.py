"""Persistent admission budgets for scheduled public-source requests.

Reserve a request and its maximum body before transport. Unknown/interrupted
responses keep the reservation. This counts application attempts/body bytes,
not TCP packets, TLS overhead or independently launched manual processes.
"""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

ENV = "ASHARE_PUBLIC_REQUEST_BUDGET"


class ResourceBudgetReached(RuntimeError):
    """Local runtime protection, never evidence that the source refused access."""


class RequestBudget:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.config = json.loads(self.path.read_text())
        c = self.config
        if (c.get("schema") != "ad.public-request-budget.v1"
                or not 1 <= c["requests_per_day"] <= 1000
                or not 1 <= c["response_bytes_per_day"] <= 1024**3):
            raise ValueError("invalid public request budget")
        self.root = self.path.parent
        if any((self.root / name).exists() for name in ("CURRENT", "CURRENT.json", "releases")):
            raise ValueError("request budget cannot use a production root")

    @contextmanager
    def locked(self):
        with (self.root / "request-budget.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.root / "request-budget-state.json"
            state = json.loads(path.read_text()) if path.exists() else {"days": {}, "attempts": {}}
            yield state
            pending = path.with_suffix(".next")
            with pending.open("w") as stream:
                json.dump(state, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, path)

    def reconcile(self, day, observed):
        with self.locked() as state:
            total = state["days"].setdefault(day, {"requests": 0, "response_bytes": 0})
            for key in total:
                total[key] = max(total[key], int(observed[key]))
            return dict(total)

    def reserve(self, url, maximum_body, *, day=None):
        day = day or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        if not 0 <= maximum_body <= 16 * 1024**2 + 1:
            raise ValueError("unreviewed per-response body reservation")
        with self.locked() as state:
            total = state["days"].setdefault(day, {"requests": 0, "response_bytes": 0})
            if (total["requests"] + 1 > self.config["requests_per_day"]
                    or total["response_bytes"] + maximum_body > self.config["response_bytes_per_day"]):
                raise ResourceBudgetReached("local request/body budget: no request sent")
            token = uuid4().hex
            total["requests"] += 1
            total["response_bytes"] += maximum_body
            state["attempts"][token] = {"day": day, "reserved_bytes": maximum_body,
                "url_sha256": hashlib.sha256(url.encode()).hexdigest(), "settled": False}
            return token

    def settle(self, token, received_bytes):
        with self.locked() as state:
            attempt = state["attempts"][token]
            if attempt["settled"] or not 0 <= received_bytes <= attempt["reserved_bytes"]:
                raise ValueError("invalid or repeated response settlement")
            total = state["days"][attempt["day"]]
            total["response_bytes"] -= attempt["reserved_bytes"] - received_bytes
            attempt.update(settled=True, received_bytes=received_bytes)


def from_environment():
    path = os.environ.get(ENV)
    if not path:
        return None
    expected = os.environ.get(ENV + "_SHA256")
    if not expected or hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
        raise ValueError("public request budget config changed")
    return RequestBudget(path)


def financial_fetcher(source):
    """Keep the existing financial normalizer and checkpoints unchanged."""
    budget = from_environment()
    if not budget:
        return None

    def fetch(url):
        original = source.SafeRedirect
        class BudgetedRedirect(original):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                request = super().redirect_request(req, fp, code, msg, headers, newurl)
                if request is not None:
                    budget.reserve(newurl, source.MAX_BODY + 1)
                return request
        token = budget.reserve(url, source.MAX_BODY + 1)
        source.SafeRedirect = BudgetedRedirect
        try:
            status, body, headers = source.fetch(url)
            budget.settle(token, len(body))
            return status, body, headers
        finally:
            source.SafeRedirect = original
    return fetch
