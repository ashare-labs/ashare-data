"""Select AD asset paths from the shared deployment manifest, without initializing anything."""

import json
import os
from pathlib import Path

from ashare_data.core.model import DataError, require


def resolve(deployment=None, *, profile="product"):
    selected = deployment or os.environ.get("ASHARE_DEPLOYMENT")
    if selected is None and os.environ.get("ASHARE_DATA_ROOT"):
        selected = Path(os.environ["ASHARE_DATA_ROOT"]).expanduser() / "deployment.json"
    require(
        selected is not None,
        "PRODUCT_NOT_CONFIGURED",
        "Select --deployment, ASHARE_DEPLOYMENT or ASHARE_DATA_ROOT/deployment.json",
    )
    path = Path(selected).expanduser().resolve()
    try:
        require(
            path.stat().st_size <= 1024 * 1024, "PRODUCT_CONFIGURATION", "Manifest exceeds 1 MiB"
        )
        value = json.loads(path.read_bytes())
        require(
            value["schema"] == "ashare-deployment.v1",
            "PRODUCT_CONFIGURATION",
            "Unknown manifest schema",
        )
        root = Path(value["data_root"]).expanduser()
        root = (root if root.is_absolute() else path.parent / root).resolve()
        selected_profile = value["profiles"][profile]
        require(
            selected_profile["kind"] == "product",
            "PRODUCT_CONFIGURATION",
            "Select a product profile; evaluation data is not a product fallback",
        )
        result = {"data_root": root, "profile": profile, "deployment": path}
        for setting, key, kind in (
            ("daily_asset", "daily_root", "local_daily"),
            ("release_store_asset", "release_store_root", "store"),
        ):
            asset = value["assets"][selected_profile[setting]]
            require(
                asset["kind"] == kind,
                "PRODUCT_CONFIGURATION",
                "Selected asset kind is incompatible",
            )
            target = Path(asset["path"]).expanduser()
            result[key] = (target if target.is_absolute() else root / target).resolve()
        return result
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise DataError(
            "PRODUCT_CONFIGURATION",
            "Cannot resolve the selected deployment manifest",
            {"reason": str(exc)},
        ) from exc
