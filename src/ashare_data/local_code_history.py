"""Issuer-confirmed quote-code lifetimes; separate from issuer IPO dates.

These are identifiers and public notice references, not price data. The catalog
was checked against all 97 CNINFO title results for 证券代码 in 2010-01-01..
2026-10-14. It is a dated research audit, not proof that future changes cannot occur.
"""

from .local_sources import SourceUnavailable

CODE_CHANGES = [
    dict(
        old_symbol="sh600849",
        new_symbol="sh601607",
        old_name="上海医药",
        issuer_ipo="1994-03-24",
        effective_date="2010-03-05",
        registration_date="2010-03-05",
        first_trade_date="2010-03-09",
        url="https://static.cninfo.com.cn/finalpage/2010-03-03/57644871.PDF",
        receipt="02c3aa51877e94c0ce25f16a3dc553fd3fc6d47c5b4124f1211a245186a084fe",
        note="Code change on March 5; March 9 first trade observed in the source, not inferred IPO.",
    ),
    dict(
        old_symbol="sh601313",
        new_symbol="sh601360",
        old_name="江南嘉捷",
        issuer_ipo="2012-01-16",
        effective_date="2018-02-28",
        registration_date="2018-02-23",
        first_trade_date="2018-02-28",
        halt_start="2018-02-22",
        halt_end="2018-02-27",
        url="https://static.cninfo.com.cn/finalpage/2018-02-23/1204428590.PDF",
        receipt="00bec778e95cb844957cf40b25386c2808e92b0af43a3bf76c03e473f7a7bd0d",
        note="Quote code starts February 28; share registration changes February 23 during the announced halt.",
    ),
    dict(
        old_symbol="sz000022",
        new_symbol="sz001872",
        old_name="深赤湾A",
        issuer_ipo="1993-05-05",
        effective_date="2018-12-26",
        registration_date="2018-12-26",
        first_trade_date="2018-12-26",
        halt_start="2018-12-21",
        halt_end="2018-12-25",
        url="https://static.cninfo.com.cn/finalpage/2018-12-26/1205690369.PDF",
        receipt="bf406b1759dd7fda45cc46927e36316840b0b1152bb40677a7b52e11568d4350",
        note="Old code valid through December 25; issuer IPO is not the new code start.",
    ),
    dict(
        old_symbol="sz000043",
        new_symbol="sz001914",
        old_name="中航善达",
        issuer_ipo="1994-09-28",
        effective_date="2019-12-16",
        registration_date="2019-12-16",
        first_trade_date="2019-12-16",
        url="https://static.cninfo.com.cn/finalpage/2019-12-16/1207164397.PDF",
        receipt="46160431c51df23c30b3be802eb140492255128fb166d103ed735a62f303ab05",
        note="Old code valid through December 13 trading session; issuer remains listed.",
    ),
]

LISTING_CORRECTIONS = [
    dict(
        symbol="sh600190",
        name="退市锦港",
        ipo="1999-06-09",
        out="2025-07-25",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2025-07-19/1224216580.PDF",
        receipt="56087fc65f0a9aeb254365479b273da9896733038700400a5205d22aa18d8aa4",
        note="Issuer notice pages 1 and 3 specify July 25 delisting; the current SSE list reports July 28. Both source facts remain stored. Last trade was July 18; suspension starts July 21.",
    ),
    dict(
        symbol="sh600680",
        name="*ST上普",
        ipo="1993-10-18",
        out="2019-05-23",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2019-05-18/1206282884.PDF",
        receipt="c4656b7929c100c5f289a8f8c55a1d9bb9099604d00d6ee559925bc5be9d1bcc",
        note="Issuer notice page 1 specifies May 23 delisting in its 2019 context. SSE and BaoStock both report May 24; source agreement is not proof of the legal exit date.",
    ),
]


LISTING_CORRECTIONS += [
    dict(
        symbol="sh600069",
        name="退市银鸽",
        ipo="1997-04-30",
        out="2020-08-27",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2020-08-21/1208215163.PDF",
        receipt="3ffb80fd1a1f5405e04d85c5ce544add3d20e37a27956158bbba8775bd112b80",
        note="Issuer notice page 1 specifies 2020-08-27 delisting; the current SSE list reports 2020-08-28. Both source facts remain stored.",
    ),
    dict(
        symbol="sh600074",
        name="退市保千",
        ipo="1997-06-23",
        out="2020-06-02",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2020-05-27/1207863178.PDF",
        receipt="500d8cc53fb1da215b6e761f8a77116cdb819d68d9c95b7e484dc37e05c05908",
        note="Issuer notice page 1 specifies 2020-06-02 delisting; the current SSE list reports 2020-06-03. Both source facts remain stored.",
    ),
    dict(
        symbol="sh600086",
        name="退市金钰",
        ipo="1997-06-06",
        out="2021-03-17",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2021-03-11/1209365262.PDF",
        receipt="5d85d0e3d8044284d91c6949f15941a32996dc8190fe7393c70ce1c8770f67fd",
        note="Issuer notice page 1 specifies 2021-03-17 delisting; the current SSE list reports 2021-03-18. Both source facts remain stored.",
    ),
    dict(
        symbol="sh600175",
        name="退市美都",
        ipo="1999-04-08",
        out="2020-08-14",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2020-08-10/1208144856.PDF",
        receipt="977a574055d710fe8625d627bda30b95f7b20055a922ec2cfd263028c6661a52",
        note="Issuer notice page 1 specifies 2020-08-14 delisting; the current SSE list reports 2020-08-17. Both source facts remain stored.",
    ),
    dict(
        symbol="sh600240",
        name="退市华业",
        ipo="2000-06-28",
        out="2020-02-05",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2020-01-31/1207285956.PDF",
        receipt="8d3c1cb510efa29b578b80121c44ec5c77c83f07a3ff9947640e4720806be637",
        note="Issuer notice page 1 specifies 2020-02-05 delisting; the current SSE list reports 2020-02-06. Both source facts remain stored.",
    ),
    dict(
        symbol="sh600401",
        name="退市海润",
        ipo="2003-09-24",
        out="2019-07-12",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2019-07-09/1206433170.PDF",
        receipt="c44c3851c0f562ab884e669f55d960963e4a2d488c4f57ad2d97286459441b32",
        note="Issuer notice page 1 specifies 2019-07-12 delisting; the current SSE list reports 2019-07-15. Both source facts remain stored.",
    ),
    dict(
        symbol="sh600555",
        name="退市海创",
        ipo="2001-03-28",
        out="2022-07-13",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2022-07-07/1213974042.PDF",
        receipt="0d810bf05749026d17d7dea672499ebc056cdc120851759098e42df211e04888",
        note="Issuer notice page 1 specifies 2022-07-13 delisting; the current SSE list reports 2022-07-14. Both source facts remain stored.",
    ),
    dict(
        symbol="sh600614",
        name="退市鹏起",
        ipo="1992-08-28",
        out="2021-07-21",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2021-07-15/1210484693.PDF",
        receipt="af00b6fda315ec9ad369a05661dceaec39a3398b728b58b4b415de4f0aae7edd",
        note="Issuer notice page 1 specifies 2021-07-21 delisting; the current SSE list reports 2021-07-22. Both source facts remain stored.",
    ),
    dict(
        symbol="sh600634",
        name="退市富控",
        ipo="1993-03-04",
        out="2021-07-21",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2021-07-15/1210485822.PDF",
        receipt="1a5af93bc944eb36ff9081a261584451b075526abb59b577864ea6a88ea1b941",
        note="Issuer notice page 1 specifies 2021-07-21 delisting; the current SSE list reports 2021-07-22. Both source facts remain stored.",
    ),
    dict(
        symbol="sh600687",
        name="退市刚泰",
        ipo="1993-11-08",
        out="2021-03-03",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2021-02-25/1209300864.PDF",
        receipt="7d11a775e3825bfd2d91e88c2587fcc7ff9dfd9cd6579f66d98fd7871b7b56fc",
        note="Issuer notice page 1 specifies 2021-03-03 delisting; the current SSE list reports 2021-03-04. Both source facts remain stored.",
    ),
    dict(
        symbol="sh600695",
        name="退市绿庭",
        ipo="1993-11-22",
        out="2022-06-14",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2022-06-08/1213632761.PDF",
        receipt="29c2ee283900573de1b5d1af8da4064f23561306c084d4f74940da13cffefeba",
        note="Issuer notice page 1 specifies 2022-06-14 delisting; the current SSE list reports 2022-06-15. Both source facts remain stored.",
    ),
    dict(
        symbol="sh600701",
        name="退市工新",
        ipo="1996-05-28",
        out="2021-04-30",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2021-04-27/1209818865.PDF",
        receipt="6de9c9f496ad4aaa01a24ca6d1f0809fb81546feab9973c50f42ea28c39e30c9",
        note="Issuer notice page 1 specifies 2021-04-30 delisting; the current SSE list reports 2021-05-06. Both source facts remain stored.",
    ),
    dict(
        symbol="sh600747",
        name="退市大控",
        ipo="1996-09-16",
        out="2019-12-12",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2019-12-09/1207147293.PDF",
        receipt="492ca8a1048dff30ba81091d00758de1ae7dcebaa00c2ee941d6f5f1f5d43367",
        note="Issuer notice page 1 specifies 2019-12-12 delisting; the current SSE list reports 2019-12-13. Both source facts remain stored.",
    ),
    dict(
        symbol="sh601558",
        name="退市锐电",
        ipo="2011-01-13",
        out="2020-07-02",
        board="main",
        source="issuer_exit_notice",
        out_inclusive=False,
        url="https://static.cninfo.com.cn/finalpage/2020-06-24/1207954579.PDF",
        receipt="cd1bcfc756d4ce4ee0a806217d45cd27648fe16e549ac4f6141fe9544e3f0d71",
        note="Issuer notice page 1 specifies 2020-07-02 delisting; the current SSE list reports 2020-07-03. Both source facts remain stored.",
    ),
]


def refresh_code_history(store, http):
    known = store.code_events()
    events, receipts = [], []
    for event in CODE_CHANGES:
        # These are retrospective identifier facts, not facts known as of end.
        # A later code change is still needed to reject a current code that a
        # provider backfills into a historical-only collection window.
        if event in known:
            continue
        body, received = http.get(event["url"])
        if not body.startswith(b"%PDF") or received[-1]["sha256"] != event["receipt"]:
            raise SourceUnavailable("Issuer code-change notice changed; review the new document")
        events.append(event)
        receipts.extend(received)
    # Reconcile old-code instruments even when the event was already known:
    # a historical window can advance past the issuer's IPO on a later run.
    store.save_code_events(CODE_CHANGES, receipts)
    corrections, correction_receipts = [], []
    for record in LISTING_CORRECTIONS:
        if record["out"] > store.meta("end"):
            continue
        known = store.db.execute(
            "SELECT 1 FROM listing_facts WHERE symbol=? AND receipt=? "
            "AND json_extract(body,'$.source')='issuer_exit_notice'",
            (record["symbol"], record["receipt"]),
        ).fetchone()
        if known:
            continue
        body, received = http.get(record["url"])
        if not body.startswith(b"%PDF") or received[-1]["sha256"] != record["receipt"]:
            raise SourceUnavailable("Issuer exit notice changed; review the new document")
        corrections.append(record)
        correction_receipts.extend(received)
    if corrections:
        store.save_universe(
            corrections, correction_receipts, store.meta("start"), store.meta("end")
        )
    return len(events) + len(corrections)
