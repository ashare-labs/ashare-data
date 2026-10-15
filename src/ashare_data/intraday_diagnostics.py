"""Source diagnostics; these never relax admission or infer a clock's cause."""

from datetime import datetime
import json

from .model import TZ, timestamp
from .session_reference import snapshot


def source_diagnostics(packet, bodies):
    result = []
    for name, source in sorted(packet["sources"].items()):
        issue = dict(
            source=name, raw_sha256=source.get("sha256"), received_at=source["observed_at"]
        )
        if "error" in source:
            result.append(dict(issue, category="source_unavailable", error=source["error"]))
        elif name.startswith("sse_quote_"):
            try:
                value = json.loads(bodies[source["sha256"]])
                snapshot(value, name[-6:], packet["day"], source["observed_at"])
            except Exception as exc:
                issue.update(
                    category="invalid_response",
                    code=getattr(exc, "code", type(exc).__name__),
                    message=str(exc),
                )
                # Only classify a clock after the normal parser reports its clock gate.
                # Keep the original receipt and integer microseconds, never round it.
                if issue["code"] == "SSE_SNAPSHOT_CLOCK":
                    try:
                        at = datetime.strptime(
                            str(value["date"]) + str(value["time"]).zfill(6), "%Y%m%d%H%M%S"
                        ).replace(tzinfo=TZ)
                        delta = at - timestamp(source["observed_at"])
                        issue.update(
                            source_as_of=at.isoformat(),
                            source_minus_receipt_microseconds=(
                                (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
                            ),
                            category="source_date_mismatch"
                            if at.date().isoformat() != packet["day"]
                            else "source_time_ahead_of_receipt",
                        )
                    except (ValueError, KeyError, TypeError):
                        pass
                result.append(issue)
    return result
