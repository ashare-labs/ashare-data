"""Explicit deployment consumer policy; legacy implementations remain archival tools."""

import json
import os
from pathlib import Path

from ashare_data.core.model import DataError, require


POLICY = "committed_market_only"
OVERRIDES = {"--root", "--deployment", "--profile", "--market-root", "--release-id",
             "--manifest-sha256", "--prepared", "--units", "--candidate", "--unit-candidate"}


def committed_deployment():
    """Inspect the declared default consumer, without opening any source data."""
    selected = os.environ.get("ASHARE_CONSUMER_DEPLOYMENT") or os.environ.get("ASHARE_DEPLOYMENT")
    if not selected and os.environ.get("ASHARE_DATA_ROOT"):
        selected = str(Path(os.environ["ASHARE_DATA_ROOT"]) / "deployment.json")
    if not selected:
        home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        if not home.is_absolute():
            home = Path.home() / ".config"
        selected = str(home / "ashare" / "deployment.json")
    path = Path(selected).expanduser()
    if not path.exists() and not path.is_symlink():
        return None
    try:
        require(path.stat().st_size <= 1024 * 1024, "PRODUCT_CONFIGURATION", "Manifest exceeds 1 MiB")
        value = json.loads(path.read_bytes())
        profile = value.get("profiles", {}).get("product", {})
        if profile.get("consumer_policy") == POLICY:
            require(profile.get("market_release") is not None and not profile.get("daily_asset"),
                    "PRODUCT_CONFIGURATION", "Committed-only consumption requires a market release and no legacy daily asset")
            overrides = [key for key in (
                "ASHARE_QUANT_FACTOR_SOURCES", "ASHARE_QUANT_PYTHON_HISTORY_FACTORS",
                "ASHARE_QUANT_MARKET_STORES", "ASHARE_QUANT_DAILY_STORE", "ASHARE_QUANT_DIAGNOSTICS",
                "AQ_API_URL", "QUANT_SERVICE_URL",
            ) if os.environ.get(key)]
            require(not overrides, "CONSUMER_OVERRIDE_DISABLED",
                    "Remove inherited source/service overrides from the formal consumer environment",
                    {"variables": overrides})
            return path.resolve()
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise DataError("PRODUCT_CONFIGURATION", "Cannot inspect declared consumer policy") from exc
    return None


def reject_legacy(operation):
    require(committed_deployment() is None, "LEGACY_SOURCE_DISABLED",
            "Committed-market consumer disables this legacy entry; use product prices/minute/coverage",
            {"operation": operation, "fallback": False})


def guard_arguments(arguments, *, commands=None):
    if committed_deployment() is None:
        return
    require(not any(arg.split("=", 1)[0] in OVERRIDES for arg in arguments),
            "CONSUMER_OVERRIDE_DISABLED", "Use the deployment-bound committed market; source overrides are disabled")
    if commands is not None:
        require(not arguments or arguments[0] in {*commands, "--help", "-h", "--version"},
                "LEGACY_SOURCE_DISABLED", "This command is outside the committed-market consumer",
                {"allowed_commands": sorted(commands), "fallback": False})


def guard_selection(deployment=None, profile="product"):
    bound = committed_deployment()
    if bound is not None:
        require(profile == "product" and (deployment is None or Path(deployment).expanduser().resolve() == bound),
                "CONSUMER_OVERRIDE_DISABLED", "The formal consumer is bound to its product deployment")
