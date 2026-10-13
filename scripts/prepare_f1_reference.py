"""Prepare four manual notebook cells; this script never calls JoinQuant."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    request = json.loads((args.root / "results/reference-request.json").read_text())
    destination = args.root / "reference-preparation"
    destination.mkdir(exist_ok=True)
    for day in request["dates"]:
        code = f"""# Prepared, NOT executed. Parent must release shared browser first.
# Check current points in UI before this cell; run each date ONCE.
# Check points after this cell BEFORE the next date; any fee/deduction: STOP.
import json as _f1json, math as _f1math, struct as _f1struct
import jqfactor as _f1factor
_f1_securities = {request["securities"]!r}
_f1_fields = {request["fields"]!r}
_f1_day = {day!r}
if "_f1_batch_attempted" not in globals():
    _f1_batch_attempted = set()
assert _f1_day not in _f1_batch_attempted, "Do not retry an uncertain call"
_f1_batch_attempted.add(_f1_day)
_f1_data = _f1factor.get_factor_values(securities=_f1_securities, factors=_f1_fields, end_date=_f1_day, count=1)
# Count all returned cells, including missing values, before validating shape.
_f1_returned = sum(int(frame.size) for frame in _f1_data.values())
print("AD_F1_BATCH_RETURNED", _f1_day, _f1_returned)
assert set(_f1_data) == set(_f1_fields)
_f1_rows = []
for _f1_name in _f1_fields:
    _f1_frame = _f1_data[_f1_name]
    assert _f1_frame.shape == (1, 12)
    assert set(_f1_frame.columns) == set(_f1_securities)
    assert str(_f1_frame.index[0])[:10] == _f1_day
    for _f1_security in _f1_securities:
        _f1_value = float(_f1_frame.loc[_f1_frame.index[0], _f1_security])
        _f1_missing = _f1math.isnan(_f1_value)
        assert _f1_missing or _f1math.isfinite(_f1_value)
        _f1_rows.append(dict(security=_f1_security,date=_f1_day,field=_f1_name,
                            value=None if _f1_missing else format(_f1_value,".17g"),
                            missing_kind="NaN" if _f1_missing else "none",provider="JoinQuant",
                            float64_bits_be=None if _f1_missing else _f1struct.pack(">d",_f1_value).hex()))
print("AD_F1_BATCH_RESULT " + _f1json.dumps(dict(date=_f1_day,returned_values=_f1_returned,rows=_f1_rows),sort_keys=True))
"""
        (destination / (day + ".py.txt")).write_text(code)
    (destination / "README.md").write_text(
        "Prepared only; no JQ execution occurred. Four date cells, maximum 624 values. "
        "Prior confirmed spend 333; remaining 667; after complete matrix 957/1000. "
        "Current points are unverified. Parent must coordinate browser release, inspect points before/after "
        "each cell, stop on any charge/deduction or uncertain result, and retain accounting plus raw outputs. "
        "No get_price probes, tuning, credentials, exports, or retries. "
        "Flatten the captured rows to a JSON array and pass --reference to run_f1_batch.py; "
        "the capture/accounting is reviewed separately. Original exact failures stay unchanged.\n"
    )
    print(destination)


if __name__ == "__main__":
    main()
