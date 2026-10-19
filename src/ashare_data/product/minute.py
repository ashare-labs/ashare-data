"""Explicit registered minute sources. Reads never collect, repair, or fall back."""

import hashlib
import json
import re
from datetime import timedelta
from pathlib import Path

from ashare_data.core.model import DataError, digest, require, timestamp
from ashare_data.product.configuration import load_profile
from ashare_data.providers.neigezhu import NeigezhuMinuteView


class LocalMinuteProduct:
    """Portable deployment registry; only the selected source is opened and verified."""

    def __init__(self, deployment=None, *, profile="product"):
        self.entry, self.root, self.document, selected = load_profile(deployment, profile=profile)
        self.profile = profile
        self.market_release = selected.get("market_release")
        self.frozen_market_config = None
        if self.market_release is not None:
            from ashare_data.product.configuration import resolve

            self.frozen_market_config = resolve(self.entry, profile=profile)
            require(
                self.frozen_market_config["market_release"]["release_id"]
                == self.market_release["release_id"]
                and self.frozen_market_config["market_release"]["manifest_sha256"]
                == self.market_release["manifest_sha256"],
                "PRODUCT_CONFIGURATION_CHANGED",
                "Deployment changed while opening minute reader",
            )
        self.default_source = selected.get("default_minute_source", "tdx")
        self.registry = selected.get("minute_sources", {})
        if self.frozen_market_config is not None:
            from ashare_data.product.market_release import open_release

            release = open_release(self.frozen_market_config)
            with release.metadata() as db:
                if db.execute(
                    "select 1 from sqlite_master where type='table' and name='release_contracts'"
                ).fetchone():
                    frozen = db.execute(
                        "select body from release_contracts where name='source_observation_readers'"
                    ).fetchone()
                    if frozen:
                        readers = json.loads(frozen[0])
                        self.registry = {**self.registry, **readers["minute_sources"]}
                        self.document["assets"].update(readers["assets"])
        require(
            isinstance(self.registry, dict),
            "PRODUCT_MINUTE_CONFIGURATION",
            "minute_sources must be a mapping",
        )

    def _path(self, value):
        target = Path(value).expanduser()
        return (target if target.is_absolute() else self.root / target).resolve()

    def _binding(self, source):
        require(
            isinstance(source, str) and source in self.registry,
            "PRODUCT_MINUTE_SOURCE_UNKNOWN",
            "Select a registered minute source; no fallback",
        )
        try:
            item = self.registry[source]
            reader = item["reader"]
            require(
                reader in {"tdx-session", "neigezhu-native"},
                "PRODUCT_MINUTE_CONFIGURATION",
                "Unsupported registered minute reader",
            )
            asset = self.document["assets"][item["asset"]]
            require(
                asset["kind"] == "reference",
                "PRODUCT_MINUTE_CONFIGURATION",
                "Minute readers require a read-only reference asset",
            )
            bindings = {
                "source": source,
                "asset": item["asset"],
                "reader": reader,
                "path": str(self._path(asset["path"])),
                "coverage": item["coverage"],
                "limits": item["limits"],
                "historical_pit": False,
                "strict_execution_admitted": False,
            }
            keys = ("manifest", "review") if reader == "neigezhu-native" else ("content",)
            for key in keys:
                evidence = item[key]
                require(
                    isinstance(evidence["sha256"], str)
                    and re.fullmatch(r"[a-f0-9]{64}", evidence["sha256"]),
                    "PRODUCT_MINUTE_CONFIGURATION",
                    "Evidence requires an external SHA256 pin",
                )
                bindings[key] = {
                    "path": str(self._path(evidence["path"]))
                    if key != "content"
                    else bindings["path"],
                    "sha256": evidence["sha256"],
                }
            if reader == "neigezhu-native":
                require(
                    item["asset"] == "minute-hf-download",
                    "PRODUCT_MINUTE_CONFIGURATION",
                    "The native reader uses the existing minute-hf-download asset",
                )
            return bindings
        except (KeyError, TypeError, ValueError) as exc:
            raise DataError(
                "PRODUCT_MINUTE_CONFIGURATION",
                "Invalid minute source registration",
                {"source": source, "reason": str(exc)},
            ) from exc

    def sources(self):
        rows = []
        if self.market_release is not None:
            selected = self.market_release
            path = self._path(self.document["assets"][selected["asset"]]["path"])
            rows = [
                {
                    "source": source,
                    "reader": "market-release",
                    "series_id": series,
                    "path": str(path),
                    "release_id": selected["release_id"],
                    "manifest_sha256": selected["manifest_sha256"],
                    "status": "registered_unopened",
                    "historical_pit": False,
                    "strict_execution_admitted": False,
                }
                for source, series in (("canonical", 2), ("free_stockdb", 4), ("tdx", 3))
            ]
        for source in (
            ["neigezhu"]
            if self.market_release is not None and "neigezhu" in self.registry
            else self.registry
            if self.market_release is None
            else []
        ):
            try:
                rows.append({**self._binding(source), "status": "registered_unopened"})
            except DataError as exc:
                rows.append(
                    {"source": source, "status": "registration_error", "error": exc.as_dict()}
                )
        return {
            "schema": "ad-local-minute-sources.v1",
            "default_source": self.default_source,
            "market_release": self.market_release,
            "profile": self.profile,
            "data_root": str(self.root),
            "registry_sha256": digest(
                {
                    "data_root": str(self.root),
                    "sources": rows,
                    "default_source": self.default_source,
                }
            ),
            "sources": rows,
            "coverage_basis": "Registered audit scope; not a promise of every security/day",
            "query_bounds": {"securities": 1, "max_days": 60, "max_rows": 15000},
            "verification": "Selected content is checked on query; discovery does not scan raw files",
        }

    def prices(self, security, start, end, *, source="auto"):
        a, b = timestamp(start, minute=True), timestamp(end, minute=True)
        require(
            a <= b and b - a <= timedelta(days=60),
            "PRODUCT_MINUTE_QUERY_BOUND",
            "Choose one security and at most 60 days",
        )
        source = self.default_source if source == "auto" else source
        if self.market_release is not None and source in {"canonical", "tdx", "free_stockdb"}:
            from ashare_data.product.market_release import minute_prices

            return minute_prices(self.frozen_market_config, security, a, b, source)
        binding = self._binding(source)
        try:
            if binding["reader"] == "neigezhu-native":
                view = NeigezhuMinuteView(
                    entry=self.entry,
                    source_root=binding["path"],
                    manifest=binding["manifest"]["path"],
                    expected_manifest_sha256=binding["manifest"]["sha256"],
                    review=binding["review"]["path"],
                    expected_review_sha256=binding["review"]["sha256"],
                )
                require(
                    view.root == Path(binding["path"]),
                    "PRODUCT_MINUTE_CONFIGURATION",
                    "Deployment source changed while opening the reader",
                )
                result = view.get_price(security, mode="retrospective", start=a, end=b).to_dict()
            else:
                path = Path(binding["path"])
                require(
                    path.stat().st_size <= 64 * 1024 * 1024,
                    "PRODUCT_MINUTE_QUERY_BOUND",
                    "Registered TDX session exceeds 64 MiB",
                )
                raw = path.read_bytes()
                require(
                    hashlib.sha256(raw).hexdigest() == binding["content"]["sha256"],
                    "PRODUCT_MINUTE_INTEGRITY",
                    "Registered TDX session changed",
                )
                value = json.loads(raw)
                require(
                    value["schema"] == "ad-tdx-minute-session.v1",
                    "PRODUCT_MINUTE_INTEGRITY",
                    "Unexpected TDX session schema",
                )
                rows = [
                    row
                    for row in value["minute"]
                    if row["security"] == security
                    and a <= timestamp(row["source_label"], minute=True) <= b
                ]
                require(len(rows) <= 15000, "PRODUCT_MINUTE_QUERY_BOUND", "Narrow the interval")
                result = {
                    "rows": rows,
                    "report": {
                        "schema": "ad-tdx-session-read.v1",
                        "rows": len(rows),
                        "range_inclusive": [a.isoformat(), b.isoformat()],
                        "historical_pit": False,
                        "execution_admitted": False,
                        "native_labels_preserved": True,
                        "repaired_rows": 0,
                        "limits": binding["limits"],
                    },
                }
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DataError(
                "PRODUCT_MINUTE_UNAVAILABLE",
                "Selected minute source is unavailable",
                {"source": source, "reason": str(exc)},
            ) from exc
        return {
            "schema": "ad-local-minute-prices.v1",
            "source": source,
            "binding": binding,
            **result,
        }
