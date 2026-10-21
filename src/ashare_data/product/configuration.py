"""Select AD asset paths from the shared deployment manifest, without initializing anything."""

import json
import os
from pathlib import Path

from ashare_data.core.model import DataError, digest, require
from ashare_data.product.references import KINDS, normalize


def deployment_path(deployment=None):
    """Explicit path, environment, then shared user configuration; never create it."""
    selected = deployment or os.environ.get("ASHARE_DEPLOYMENT")
    if not selected and os.environ.get("ASHARE_DATA_ROOT"):
        selected = Path(os.environ["ASHARE_DATA_ROOT"]).expanduser() / "deployment.json"
    if selected:
        return Path(selected).expanduser()
    configured = os.environ.get("XDG_CONFIG_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".config"
    if not home.is_absolute():
        home = Path.home() / ".config"
    candidate = home / "ashare" / "deployment.json"
    # A broken link is declared configuration, so consumers must fail closed.
    return candidate if candidate.exists() or candidate.is_symlink() else None


def load_profile(deployment=None, *, profile="product"):
    """Read only the common manifest envelope; consumers resolve their own assets."""
    selected = deployment_path(deployment)
    require(
        selected is not None,
        "PRODUCT_NOT_CONFIGURED",
        "Select --deployment, environment or the user config ashare/deployment.json",
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
        return path, root, value, selected_profile
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise DataError(
            "PRODUCT_CONFIGURATION", "Cannot resolve the selected deployment manifest",
            {"reason": str(exc)},
        ) from exc


def resolve(deployment=None, *, profile="product", reference_set=None):
    path, root, value, selected_profile = load_profile(deployment, profile=profile)
    try:
        result = {"data_root": root, "profile": profile, "deployment": path}
        selected_assets = {}
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
            selected_assets[selected_profile[setting]] = {**asset, "path": str(result[key])}
        result["market_release"] = None
        market = selected_profile.get("market_release")
        if market is not None:
            asset = value["assets"][market["asset"]]
            require(asset["kind"] == "market_release", "PRODUCT_CONFIGURATION", "Select a market_release asset")
            target = Path(asset["path"]).expanduser()
            target = (target if target.is_absolute() else root / target).resolve()
            require(type(market["release_id"]) is int and market["release_id"] > 0,
                    "PRODUCT_CONFIGURATION", "A committed release ID is required")
            result["market_release"] = {"root": str(target), "release_id": market["release_id"],
                                        "manifest_sha256": market["manifest_sha256"]}
            selected_assets[market["asset"]] = {**asset, "path": str(target)}
        sets = selected_profile.get("reference_sets", {})
        require(isinstance(sets, dict), "PRODUCT_CONFIGURATION", "reference_sets must be a mapping")
        chosen = (
            reference_set
            if reference_set is not None
            else selected_profile.get("default_reference_set")
        )
        references, bindings, selection = None, {}, None
        if chosen is not None:
            require(
                isinstance(chosen, str) and chosen in sets,
                "PRODUCT_REFERENCE_CONFIGURATION",
                "Selected reference set is not configured",
            )
            spec = sets[chosen]
            kind = spec["kind"]
            require(
                kind in KINDS, "PRODUCT_REFERENCE_CONFIGURATION", "Unsupported reference set kind"
            )
            references = {"name": chosen, "kind": kind}
            if kind == "market_release":
                require(set(spec) == {"kind"} and result["market_release"] is not None,
                        "PRODUCT_REFERENCE_CONFIGURATION", "Released references use the profile price release")
                references["release"] = result["market_release"]
            roles = {} if kind == "market_release" else {**KINDS[kind], "corporate": "corporate_history"}
            for role, expected in roles.items():
                setting = "corporate_history_asset" if role == "corporate" else role + "_asset"
                name = spec[setting]
                asset = value["assets"][name]
                require(
                    asset["kind"] == expected,
                    "PRODUCT_REFERENCE_CONFIGURATION",
                    "Reference asset kind differs",
                )
                target = Path(asset["path"]).expanduser()
                target = (target if target.is_absolute() else root / target).resolve()
                relative = Path(asset.get("binding", "binding.json"))
                require(
                    not relative.is_absolute()
                    and ".." not in relative.parts
                    and (target / relative).resolve().is_relative_to(target),
                    "PRODUCT_REFERENCE_CONFIGURATION",
                    "Binding must stay within its selected asset",
                )
                references[role] = {
                    "binding_path": str(target / relative),
                    "binding_sha256": asset["binding_sha256"],
                }
                selected_assets[name] = {**asset, "path": str(target)}
            selection, _, bindings = normalize(references)
        result.update(
            references=references,
            reference_sets=sets,
            default_reference_set=selected_profile.get("default_reference_set"),
            selected_assets=selected_assets,
            configuration_sha256=digest(
                {
                    "profile": profile,
                    "daily_root": str(result["daily_root"]),
                    "release_store_root": str(result["release_store_root"]),
                    "reference_set": selection,
                    "market_release": result["market_release"],
                    "source_bindings": bindings,
                }
            ),
        )
        return result
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise DataError(
            "PRODUCT_CONFIGURATION",
            "Cannot resolve the selected deployment manifest",
            {"reason": str(exc)},
        ) from exc
