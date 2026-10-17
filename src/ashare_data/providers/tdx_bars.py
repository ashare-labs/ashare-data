"""Explicit native TDX minute research, with sealed local payloads and no fallback."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import subprocess
import sys

import pandas as pd

from ashare_data.core.model import DataError, require, timestamp
from ashare_data.research.datasets import ResearchResult
from ashare_data.providers.tdx_collection import (
    MAX_PAGE_BYTES,
    SCHEMA,
    encoded,
    now,
    request,
    save_journal,
    sha,
    validate_page,
)

ROLE = "retrospective"
FILE_PATTERN = r"(?:request|journal)\.json|page-\d{3}\.(?:bin|json)"


def _read(path, limit=MAX_PAGE_BYTES):
    require(
        path.is_file() and not path.is_symlink() and path.stat().st_size <= limit,
        "TDX_INTEGRITY",
        "capture file missing, linked or oversized",
    )
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, "TDX_INTEGRITY", "capture file oversized")
    return raw


def _json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, "TDX_INTEGRITY", "duplicate JSON key")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("nonfinite JSON constant")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def _bounds(start, end, labels):
    low = timestamp(start, minute=True) if start is not None else None
    high = timestamp(end, minute=True) if end is not None else None
    require(low is None or high is None or low <= high, "INVALID_RANGE", "start after end")
    require(
        labels is None or (type(labels) in (list, tuple) and 1 <= len(labels) <= 1000),
        "INVALID_ARGUMENT",
        "labels must contain 1..1000 explicit aware minute labels",
    )
    wanted = None if labels is None else {timestamp(t, minute=True) for t in labels}
    require(
        wanted is None
        or all((low is None or t >= low) and (high is None or t <= high) for t in wanted),
        "INVALID_RANGE",
        "labels outside start/end",
    )
    return low, high, wanted


def _seal(root):
    files = {
        p.name: sha(_read(p)) for p in sorted(root.iterdir()) if re.fullmatch(FILE_PATTERN, p.name)
    }
    require({"request.json", "journal.json"} <= files.keys(), "TDX_INTEGRITY", "missing receipts")
    body = encoded(dict(schema=SCHEMA, files=files))
    with (root / "manifest.json").open("xb") as stream:
        stream.write(body)
    return sha(body)


class TDXBarSource:
    """One explicitly named node and existing SDK. Instantiation does not connect."""

    def __init__(self, *, host, python_executable=None, timeout=6, min_interval=1):
        try:
            request("600000.XSHG", host, "1m", 0, 1, 1, 1, timeout, min_interval)
        except (ValueError, TypeError) as exc:
            raise DataError("INVALID_ARGUMENT", str(exc)) from exc
        self.host = host
        self.python_executable = str(python_executable or sys.executable)
        self.timeout, self.min_interval = timeout, min_interval

    @staticmethod
    def capabilities():
        return dict(
            source="tdx_native_0x052d",
            frequencies=["1m", "5m"],
            optional_sdk="existing eltdx 3.0.3; never installed automatically",
            max_page_size=800,
            max_pages=32,
            max_start_offset=65535,
            max_count=25600,
            network="explicit_fetch_only; one node/connection",
            date_selection="offline source-label filter; not a remote date query",
            complete_history=False,
            historical_pit=False,
            execution_admitted=False,
        )

    def fetch(
        self,
        security,
        *,
        directory,
        frequency="1m",
        count=3200,
        start_offset=0,
        page_size=800,
        max_pages=4,
    ):
        """Capture <=32 pages. A fresh directory is mandatory; failures are retained."""
        try:
            config = request(
                security,
                self.host,
                frequency,
                start_offset,
                count,
                page_size,
                max_pages,
                self.timeout,
                self.min_interval,
            )
        except (ValueError, TypeError) as exc:
            raise DataError("INVALID_ARGUMENT", str(exc)) from exc
        root = Path(directory).expanduser().absolute()
        require(not root.exists(), "TDX_CAPTURE_EXISTS", "use a new capture directory")
        root.mkdir(parents=True, exist_ok=False)
        (root / "request.json").write_bytes(encoded(config))
        failure = None
        deadline = min(600, (max_pages + 2) * self.timeout + max_pages * self.min_interval + 15)
        try:
            # The worker uses only stdlib and the caller's installed eltdx. AD is
            # not injected into or installed in that existing SDK environment.
            result = subprocess.run(
                [
                    self.python_executable,
                    "-B",
                    str(Path(__file__).with_name("tdx_bars_worker.py")),
                    str(root),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=deadline,
                check=False,
            )
            if result.returncode != 0:
                failure = "worker_failed"
        except subprocess.TimeoutExpired:
            failure = "worker_timeout"
        except OSError:
            failure = "sdk_python_unavailable"
        if failure or not (root / "journal.json").exists():
            journal = (
                _json(_read(root / "journal.json"))
                if (root / "journal.json").exists()
                else dict(
                    schema=SCHEMA,
                    sdk={},
                    pages=[],
                    accepted_rows=0,
                    next_offset=start_offset,
                    complete_history=False,
                )
            )
            journal.update(stop_reason=failure or "worker_failed", completed_at=now())
            save_journal(root, journal)
        return TDXBarView(root, expected_sha256=_seal(root))

    def get_price(
        self,
        security,
        *,
        directory,
        mode,
        start=None,
        end=None,
        labels=None,
        allow_partial=False,
        **capture_options,
    ):
        require(mode == ROLE, "TDX_RESEARCH_MODE", "explicit mode='retrospective' required")
        require(type(allow_partial) is bool, "INVALID_ARGUMENT", "allow_partial must be bool")
        _bounds(start, end, labels)  # Invalid selection must not cause network activity.
        view = self.fetch(security, directory=directory, **capture_options)
        result = view.get_price(
            mode=mode, start=start, end=end, labels=labels, allow_partial=allow_partial
        )
        report = dict(result.report, network_used=True)
        result.data.attrs = report
        return ResearchResult(result.data, report)


class TDXBarView:
    """Hash-pinned offline view. Reads recheck the manifest and every source file."""

    def __init__(self, directory, *, expected_sha256):
        require(
            isinstance(expected_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", expected_sha256),
            "TDX_INTEGRITY",
            "pin the manifest SHA256 from acquisition",
        )
        self.directory = Path(directory).expanduser().absolute()
        self.manifest_sha256 = expected_sha256
        self._load()

    def _load(self):
        try:
            body = _read(self.directory / "manifest.json")
            require(sha(body) == self.manifest_sha256, "TDX_INTEGRITY", "manifest hash mismatch")
            manifest = _json(body)
            require(
                manifest["schema"] == SCHEMA
                and type(manifest["files"]) is dict
                and 2 <= len(manifest["files"]) <= 66,
                "TDX_INTEGRITY",
                "unsupported bounded capture manifest",
            )
            files = {}
            for name, digest in manifest["files"].items():
                require(
                    re.fullmatch(FILE_PATTERN, name) is not None,
                    "TDX_INTEGRITY",
                    "unexpected capture filename",
                )
                files[name] = _read(self.directory / name)
                require(sha(files[name]) == digest, "TDX_INTEGRITY", "source file hash mismatch")
            config = _json(files["request.json"])
            require(config == request(**config), "TDX_INTEGRITY", "invalid request")
            journal = _json(files["journal.json"])
            require(
                journal["schema"] == SCHEMA
                and journal["complete_history"] is False
                and type(journal["pages"]) is list
                and len(journal["pages"]) <= config["max_pages"],
                "TDX_INTEGRITY",
                "invalid collection journal",
            )
            rows, known, previous, offset = [], set(), None, config["start_offset"]
            for index, entry in enumerate(journal["pages"]):
                require(
                    entry["index"] == index
                    and entry["offset"] == offset
                    and entry["requested"] == min(config["page_size"], config["count"] - len(rows)),
                    "TDX_INTEGRITY",
                    "page request chain mismatch",
                )
                if "payload_file" in entry:
                    require(
                        entry["payload_file"] == f"page-{index:03d}.bin"
                        and entry["rows_file"] == f"page-{index:03d}.json"
                        and sha(files[entry["payload_file"]]) == entry["payload_sha256"]
                        and sha(files[entry["rows_file"]]) == entry["rows_sha256"],
                        "TDX_INTEGRITY",
                        "page receipt binding mismatch",
                    )
                if not entry["accepted"]:
                    require(
                        index == len(journal["pages"]) - 1,
                        "TDX_INTEGRITY",
                        "collection continued after rejected page",
                    )
                    continue
                page_rows = _json(files[entry["rows_file"]])
                labels = validate_page(
                    page_rows,
                    files[entry["payload_file"]],
                    entry["requested"],
                    entry["response_completed_at"],
                )
                require(
                    labels
                    and not known.intersection(labels)
                    and (previous is None or max(labels) < previous)
                    and entry["received"] == len(page_rows)
                    and entry["first"] == page_rows[0]["time"]
                    and entry["last"] == page_rows[-1]["time"]
                    and timestamp(entry["request_started_at"])
                    <= timestamp(entry["response_completed_at"]),
                    "TDX_INTEGRITY",
                    "accepted page chronology mismatch",
                )
                rows.extend(page_rows)
                known.update(labels)
                previous = min(labels)
                offset += len(page_rows)
            require(
                journal["accepted_rows"] == len(rows) and journal["next_offset"] == offset,
                "TDX_INTEGRITY",
                "accepted count or continuation mismatch",
            )
            require(
                journal["stop_reason"] != "requested_count_reached" or len(rows) == config["count"],
                "TDX_INTEGRITY",
                "false request completion",
            )
            return config, journal, sorted(rows, key=lambda r: datetime.fromisoformat(r["time"]))
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            OverflowError,
            RecursionError,
        ) as exc:
            raise DataError("TDX_INTEGRITY", "invalid native bar capture") from exc

    def describe(self):
        config, journal, rows = self._load()
        return self._report(config, journal, rows)

    def _report(self, config, journal, rows):
        dates = {}
        for row in rows:
            key = row["time"][:10]
            dates[key] = dates.get(key, 0) + 1
        return dict(
            source="tdx_native_0x052d",
            schema=SCHEMA,
            request=config,
            sdk=journal["sdk"],
            capture_directory=str(self.directory),
            manifest_sha256=self.manifest_sha256,
            stop_reason=journal["stop_reason"],
            pages=journal["pages"],
            accepted_rows=len(rows),
            next_offset=journal["next_offset"],
            first=rows[0]["time"] if rows else None,
            last=rows[-1]["time"] if rows else None,
            date_counts=dates,
            request_satisfied=journal["stop_reason"] == "requested_count_reached",
            complete_history=False,
            date_coverage_verified=False,
            historical_available_at=None,
            bar_start=None,
            bar_end=None,
            historical_pit=False,
            execution_admitted=False,
            interval_verified=False,
            corporate_actions_verified=False,
            trading_status_verified=False,
            volume_unit="share (source wire value; decoded lots * 100)",
            money_unit="CNY",
            quantity_precision="source float encoding; no odd-lot or exchange-precision guarantee",
            price_basis="adjust=none requested; raw SDK integers preserved",
            receipt_scope="decoded protocol payload, not full TCP capture; symbol/date context from request/SDK",
            network_used=False,
        )

    def get_price(self, *, mode, start=None, end=None, labels=None, allow_partial=False):
        require(mode == ROLE, "TDX_RESEARCH_MODE", "explicit mode='retrospective' required")
        require(type(allow_partial) is bool, "INVALID_ARGUMENT", "allow_partial must be bool")
        low, high, wanted = _bounds(start, end, labels)
        config, journal, rows = self._load()
        report = self._report(config, journal, rows)
        require(
            allow_partial or report["request_satisfied"],
            "TDX_PARTIAL_CAPTURE",
            "capture stopped before requested count; inspect receipt or explicitly allow_partial",
            report,
        )
        selected = [
            r
            for r in rows
            if (low is None or timestamp(r["time"]) >= low)
            and (high is None or timestamp(r["time"]) <= high)
            and (wanted is None or timestamp(r["time"]) in wanted)
        ]
        found = {timestamp(r["time"]) for r in selected}
        report.update(
            mode=ROLE,
            selected_rows=len(selected),
            selection_status="source_rows" if selected else "no_observed_rows",
            missing_requested_labels=None
            if wanted is None
            else [t.isoformat() for t in sorted(wanted - found)],
            start=None if low is None else low.isoformat(),
            end=None if high is None else high.isoformat(),
            selection_basis="inclusive source labels; no inferred calendar or bar interval",
        )
        records = [
            dict(
                time=timestamp(r["time"]),
                **{
                    k: Decimal(r[k + "_price_milli"]) / 1000
                    for k in ("open", "high", "low", "close")
                },
                volume=Decimal(str(r["volume_wire_value"])),
                money=Decimal(str(r["amount"])),
                volume_lots=Decimal(str(r["volume_lots"])),
            )
            for r in selected
        ]
        frame = pd.DataFrame(
            records,
            columns=["time", "open", "high", "low", "close", "volume", "money", "volume_lots"],
        ).set_index("time")
        frame.attrs = report
        return ResearchResult(frame, report)
