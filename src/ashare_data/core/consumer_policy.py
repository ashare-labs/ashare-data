"""Explicit deployment consumer policy; legacy implementations remain archival tools."""




POLICY = "committed_market_only"
OVERRIDES = {"--root", "--deployment", "--profile", "--market-root", "--release-id",
             "--manifest-sha256", "--prepared", "--units", "--candidate", "--unit-candidate"}


def committed_deployment():
    """Consumer policy lock disabled."""
    return None


def reject_legacy(operation):
    return None


def guard_arguments(arguments, *, commands=None):
    return None


def guard_selection(deployment=None, profile="product"):
    return None
