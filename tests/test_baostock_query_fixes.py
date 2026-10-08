"""Public cutoff and invalid-metadata controls; all market rows are synthetic."""

import copy
from datetime import datetime, timezone, timedelta
import json

import pytest

from ashare_data import BaoStockSource, DataError, Store
from ashare_data import baostock as api
from ashare_data.cli import main
from test_baostock import BASIC, BASIC_FIELDS, bundle as basic_bundle
from test_baostock_minutes import bundle as minute_bundle, rows_for


@pytest.fixture(params=["5m", "15m", "30m", "60m"])
def minute_view(request, tmp_path):
    blobs = minute_bundle(tmp_path, request.param, days=2)
    store = Store.init(tmp_path / "store")
    return store.baostock(api._publish(store, blobs)), store, request.param


# Counts for a single independently specified session grid, including each endpoint.
@pytest.mark.parametrize(
    "clock,counts",
    [
        ("00:00:00", [0, 0, 0, 0]),
        ("09:30:00", [0, 0, 0, 0]),
        ("09:35:00", [1, 0, 0, 0]),
        ("10:30:00", [12, 4, 2, 1]),
        ("11:30:00", [24, 8, 4, 2]),
        ("11:30:00.001", [24, 8, 4, 2]),
        ("12:59:59", [24, 8, 4, 2]),
        ("13:00:00", [24, 8, 4, 2]),
        ("14:55:00", [47, 15, 7, 3]),
        ("15:00:00", [48, 16, 8, 4]),
        ("23:59:59.999999", [48, 16, 8, 4]),
    ],
)
@pytest.mark.parametrize("day", [2, 3])
@pytest.mark.parametrize("inclusive", [True, False])
def test_public_cutoff_system_matrix(minute_view, clock, counts, day, inclusive):
    view, store, freq = minute_view
    position = ["5m", "15m", "30m", "60m"].index(freq)
    full = view.get_price()
    end = f"2020-01-{day:02d}T{clock}+08:00"
    stamp = datetime.fromisoformat(end)
    expected = counts[position] + ([48, 16, 8, 4][position] if day == 3 else 0)
    if not inclusive and any(
        str(v) == stamp.replace(tzinfo=None).isoformat(timespec="milliseconds")
        for v in full.data.source_label
    ):
        expected -= 1
    result = view.get_price(end=end, end_inclusive=inclusive)
    bound = view.at(end, visibility="source_label", inclusive=inclusive)
    assert len(result.data) == expected == len(bound.get_price().data)
    utc = stamp.astimezone(timezone.utc)
    assert result.to_dict() == view.get_price(end=utc, end_inclusive=inclusive).to_dict()
    assert all(
        (
            v.to_pydatetime().replace(tzinfo=stamp.tzinfo) <= stamp
            if inclusive
            else v.to_pydatetime().replace(tzinfo=stamp.tzinfo) < stamp
        )
        for v in result.data.index
    )
    assert bound.descriptor()["rows"] == bound.coverage()["rows"] == expected
    assert len(bound.lineage()["rows"]) == expected and "receipt" not in bound.lineage()
    assert result.report["quality"]["bar_end"] is None
    assert result.report["quality"]["available_at"] is None
    assert not result.report["quality"]["selection"]["historical_pit_verified"]
    assert not result.report["coverage"]["query_complete"]
    for d in bound.coverage()["minute_labels"]["days"]:
        assert all(
            datetime.fromisoformat(v) <= stamp.replace(tzinfo=None)
            for v in d["missing_hypothesized_labels"]
        )
    assert len(view.get_price().data) == len(
        Store(store.root).baostock(view.descriptor()["capture_id"]).get_price().data
    )


@pytest.mark.parametrize(
    "end",
    [
        "2020-01-01T23:59:59+08:00",
        "2020-01-04T00:00:00+08:00",
        "2020-01-02T23:30:00-08:00",
        "2020-01-03T14:55:00.000001+08:00",
        datetime(2020, 1, 3, 6, 55, tzinfo=timezone.utc),
    ],
)
def test_bound_around_capture_and_timezone(minute_view, end):
    view, _, _ = minute_view
    cutoff = datetime.fromisoformat(end) if isinstance(end, str) else end
    target = cutoff.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    original = view.get_price().data
    expected = original.loc[original.index <= target]
    assert view.get_price(end=end).data.equals(expected)


BAD_ENDS = [
    "",
    "2020-01-02",
    "14:55",
    "2020-01-02T14:55:00",
    "2020-01-02T14:55+08:00",
    "2020-02-30T14:55:00+08:00",
    "2020-01-02T24:00:00+08:00",
    "2020-01-02T14:55:60+08:00",
    "2020-01-02T14:55:00+08:60",
    "2020-01-02T14:55:00+24:00",
    "2020-01-02T14:55:00.0000001Z",
    "20200102T145500Z",
    "２０２０-01-02T14:55:00Z",
    "2020-01-02 14:55:00Z",
    0,
    True,
    [],
    {},
    datetime(2020, 1, 2),
    float("nan"),
]


@pytest.mark.parametrize("end", BAD_ENDS)
def test_invalid_cutoff_before_fetch_and_read(minute_view, monkeypatch, end):
    view, store, freq = minute_view
    source = object.__new__(BaoStockSource)
    monkeypatch.setattr(source, "fetch", lambda *a, **kw: pytest.fail("invalid end started fetch"))
    for fn in [
        lambda: view.get_price(end=end),
        lambda: view.at(end, visibility="source_label"),
        lambda: source.get_price(
            "600000.XSHG",
            store=store,
            start_date="2020-01-02",
            end_date="2020-01-03",
            frequency=freq,
            end=end,
        ),
    ]:
        with pytest.raises(DataError) as e:
            fn()
        assert e.value.code == "INVALID_TIME"


@pytest.mark.parametrize("inclusive", [0, 1, "true", "false", None, [], {}])
def test_invalid_inclusivity(minute_view, inclusive):
    view, _, _ = minute_view
    with pytest.raises(DataError, match="布尔"):
        view.get_price(end="2020-01-02T14:55:00+08:00", end_inclusive=inclusive)
    with pytest.raises(DataError):
        view.at("2020-01-02T14:55:00+08:00", visibility="source_label", inclusive=inclusive)


def test_repeated_bounds_never_widen_and_no_mutation(minute_view):
    view, store, _ = minute_view
    before = view.lineage()
    end = "2020-01-03T14:55:00+08:00"
    bounded = view.at(end, visibility="source_label", inclusive=False)
    expected = bounded.get_price().to_dict()
    assert bounded.at(end, visibility="source_label").get_price().to_dict() == expected
    assert (
        bounded.at("2020-01-04T15:00:00+08:00", visibility="source_label").get_price().to_dict()
        == expected
    )
    assert bounded.get_price(end="2020-01-04T15:00:00+08:00").to_dict() == expected
    result = bounded.get_price()
    result.data.iloc[0]["source_row"]["close"] = "999"
    result.report["coverage"]["selection"]["end"] = "edited"
    bounded.lineage()["rows"][0]["source_row"]["close"] = "999"
    assert bounded.get_price().to_dict() == expected
    assert (
        view.lineage()
        == before
        == Store(store.root).baostock(view.descriptor()["capture_id"]).lineage()
    )
    for mode, code in [("received", "RECEIPT_NOT_VISIBLE"), ("verified", "PIT_UNAVAILABLE")]:
        with pytest.raises(DataError) as e:
            bounded.at("2030-01-01T00:00:00+08:00", visibility=mode)
        assert e.value.code == code


def test_millisecond_and_future_value_perturbation(tmp_path):
    outputs = []
    for variant in range(2):
        root = tmp_path / str(variant)
        root.mkdir()
        rows = rows_for()
        late = copy.deepcopy(rows[-2])
        late[1] = "20200102145500001"
        rows.insert(-1, late)
        if variant:
            for row in rows[-2:]:
                row[3:9] = ["20", "21", "19", "20", "200", "4000"]
        store = Store.init(root / "store")
        view = store.baostock(api._publish(store, minute_bundle(root, rows=rows)))
        selected = view.at("2020-01-02T14:55:00+08:00", visibility="source_label")
        outputs.append(selected.get_price().data)
        assert len(selected.get_price().data) == 47
        assert selected.lineage()["rows"][-1]["source_row"]["time"] == "20200102145500000"
    assert outputs[0].equals(outputs[1])


@pytest.mark.parametrize("kind", ["daily", "basic", "calendar"])
def test_cutoff_rejects_nonminute(tmp_path, kind):
    blobs, _ = basic_bundle(tmp_path, kind)
    view = Store.init(tmp_path / "store")
    view = view.baostock(api._publish(view, blobs))
    with pytest.raises(DataError) as e:
        view.at("2020-01-02T14:55:00+08:00", visibility="source_label")
    assert e.value.code == "UNSUPPORTED_FREQUENCY"


INVALID_META = (
    [
        ("ipoDate", v)
        for v in [
            "not-a-date",
            "2020-02-30",
            "20200101",
            "2020-1-01",
            "0000-01-01",
            "２０２０-01-01",
            "2020-01-01T00:00:00",
        ]
    ]
    + [
        ("outDate", "1900-01-01"),
        ("outDate", "not-a-date"),
        ("outDate", "2020-02-30"),
    ]
    + [
        (field, value)
        for field in ("type", "status")
        for value in ("", "999", "-1", "01", "unknown")
    ]
)


@pytest.mark.parametrize("field,value", INVALID_META)
def test_invalid_metadata_sealed_but_refused(tmp_path, field, value):
    row = BASIC.copy()
    row[BASIC_FIELDS.index(field)] = value
    blobs, _ = basic_bundle(tmp_path, rows=[row])
    store = Store.init(tmp_path / "store")
    sid = api._publish(store, blobs)
    view = Store(store.root).baostock(sid)
    assert view.descriptor()["status"] == "source_schema_invalid"
    assert view.coverage()["source_status"] == "source_schema_invalid"
    assert view.quality()["basic_validation"] == "invalid"
    assert value in json.dumps(view.lineage(), ensure_ascii=False)
    with pytest.raises(DataError) as e:
        view.get_security_info()
    assert e.value.code == "SOURCE_SCHEMA_ERROR" and e.value.details["capture_id"] == sid
    with pytest.raises(DataError):
        view.at("2030-01-01T00:00:00+08:00", visibility="received")


@pytest.mark.parametrize("type_", ["1", "2", "3"])
@pytest.mark.parametrize("status", ["0", "1"])
@pytest.mark.parametrize(
    "dates",
    [("2000-02-29", "2020-02-29"), ("2000-01-01", ""), ("", ""), ("2000-01-01", "2000-01-01")],
)
def test_supported_metadata_and_missing_dates(tmp_path, type_, status, dates):
    row = BASIC.copy()
    row[2:] = [*dates, type_, status]
    blobs, _ = basic_bundle(tmp_path, rows=[row])
    store = Store.init(tmp_path / "store")
    view = store.baostock(api._publish(store, blobs))
    assert view.get_security_info().data.iloc[0].tolist() == row
    assert view.quality()["missing_basic_dates"] == [
        name for name, value in zip(["ipoDate", "outDate"], dates) if not value
    ]
    assert not view.quality()["execution_permission"] and not view.quality()["historical_pit"]


@pytest.mark.parametrize("field", ["ipoDate", "outDate", "type", "status"])
@pytest.mark.parametrize("value", [None, 1, False, []])
def test_nonstring_metadata_refused(field, value):
    row = dict(zip(BASIC_FIELDS, BASIC))
    row[field] = value
    with pytest.raises(DataError):
        api.validate_basic([row], "sh.600000")


@pytest.mark.parametrize(
    "extra,expected",
    [
        (["--end", "2020-01-03T14:55:00+08:00"], [95, 31, 15, 7]),
        (["--end", "2020-01-03T14:55:00+08:00", "--end-exclusive"], [94, 31, 15, 7]),
        (["--as-of", "2020-01-03T06:55:00Z", "--visibility", "source_label"], [95, 31, 15, 7]),
    ],
)
def test_cli_cutoff(minute_view, capsys, extra, expected):
    view, store, freq = minute_view
    assert (
        main(
            [
                "--store",
                str(store.root),
                "baostock-query",
                "price",
                "--capture",
                view.descriptor()["capture_id"],
                *extra,
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert len(result["rows"]) == expected[["5m", "15m", "30m", "60m"].index(freq)]


@pytest.mark.parametrize(
    "extra",
    [
        ["--end-exclusive"],
        ["--visibility", "source_label"],
        ["--end", "2020-01-03"],
        ["--as-of", "2020-01-03T14:55:00+08:00"],
        ["--as-of", "2020-01-03T14:55:00+08:00", "--visibility", "verified"],
    ],
)
def test_cli_errors(minute_view, capsys, extra):
    view, store, _ = minute_view
    assert (
        main(
            [
                "--store",
                str(store.root),
                "baostock-query",
                "price",
                "--capture",
                view.descriptor()["capture_id"],
                *extra,
            ]
        )
        == 2
    )
    assert '"error"' in capsys.readouterr().err


def test_explicit_acquisition_projects_after_fetch(minute_view, monkeypatch):
    view, store, freq = minute_view
    source = object.__new__(BaoStockSource)
    calls = []

    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return view.descriptor()["capture_id"]

    monkeypatch.setattr(source, "fetch", fetch)
    result = source.get_price(
        "600000.XSHG",
        store=store,
        start_date="2020-01-02",
        end_date="2020-01-03",
        frequency=freq,
        end="2020-01-03T14:55:00+08:00",
    )
    assert result.data.equals(view.get_price(end="2020-01-03T14:55:00+08:00").data)
    assert result.report["network_used"] is True  # explicit acquisition route; stub sends nothing
    assert len(calls) == 1 and calls[0]["end_date"] == "2020-01-03" and "end" not in calls[0]


@pytest.mark.parametrize("mode", ["", "assumed", "closed", None, True, [], {}])
def test_invalid_visibility_is_explicit(minute_view, mode):
    with pytest.raises(DataError) as e:
        minute_view[0].at("2020-01-03T14:55:00+08:00", visibility=mode)
    assert e.value.code == "INVALID_ARGUMENT"


def test_missing_clock_and_exclusive_without_end(minute_view):
    view = minute_view[0]
    for fn in [
        lambda: view.at(None, visibility="source_label"),
        lambda: view.get_price(end_inclusive=False),
        lambda: view.at("2020-01-03T14:55:00+08:00", inclusive=False),
    ]:
        with pytest.raises(DataError):
            fn()
