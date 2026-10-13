"""Read a pinned native-minute capture offline; never connects or installs an SDK."""
import argparse

from ashare_data import TDXBarView

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("capture_directory")
parser.add_argument("manifest_sha256")
parser.add_argument("--label", action="append", help="Exact aware minute label; repeatable")
parser.add_argument("--allow-partial", action="store_true")
args = parser.parse_args()
view = TDXBarView(args.capture_directory, expected_sha256=args.manifest_sha256)
result = view.get_price(mode="retrospective", labels=args.label, allow_partial=args.allow_partial)
print(result.data)
print({key: result.report[key] for key in ("stop_reason", "first", "last", "selected_rows",
                                         "missing_requested_labels", "historical_pit")})
