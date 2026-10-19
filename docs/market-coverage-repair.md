# Market coverage repair and original observations

Immutable Parquet manifests, frozen SQLite metadata and DuckDB readers remain the market storage contract. Every reader fixes one release. Old releases retain their original objects. Accounts continue through the independent retained runtime.

Canonical daily reads combine series 1 and series 5 on disjoint security days. Series 1 preserves exact decimals; series 5 preserves original binary64 values. The logical SQL view uses tagged numeric structs to prevent implicit loss. Canonical minute reads combine HF series 2 and free-stockdb series 4 on disjoint whole security days. TDX series 3 is explicit. No intraday splicing or guessed timestamp shift is permitted.

Explicit Sina/Sohu/Tencent/Eastmoney/BaoStock research queries read the original SQLite source pinned by the release, retaining the prior provider filters and values. Main-series priority must not erase explicit source observations. Source files, WAL, fingerprints and hashes must match. A changed original fails closed. Before modifying a referenced original, retain its consistent snapshot or explicitly retire that release's original-source reproducibility.

Explicit HF queries retain original rows and use restrictions. Six structurally valid price-conflict observations return to canonical facts while preserving execution/fresh_valuation=deny. Nonpositive and structurally invalid OHLC remain excluded from canonical facts. Frozen manifests and review overlays reference the retained market objects.

Early Sohu observations require corroborated whole OHLC, share quantities within original lot precision, and compatible amount/volume envelopes. Original ingestion already converted lots to shares and ten-thousand CNY to CNY; this repair does not scale twice. Free-stockdb raw and adjusted APIs are distinct. Supplements retain original binary64 values and exact integral reported share quantities. Pre-2010 daily supplements additionally require per-record independent corroboration. Unsupported observations retain exact source locators and quarantine reasons.

## Resume and publication

Use the registered market, audit and deployment paths. Complete early daily processing before free-stockdb, then validate and publish. Only staged objects and the checkpoint change during the first two steps. Restart validates the frozen source/rule version and skips complete batches.

```sh
python scripts/repair_market_coverage.py --root MARKET --audit AUDIT --entry ENTRY
python scripts/repair_stockdb_coverage.py --root MARKET --audit AUDIT --entry ENTRY --old-audit ORIGINAL_STOCKDB_AUDIT --tooling MSGPACK_TOOLING
python scripts/audit_coverage_repair.py --root MARKET --audit AUDIT
python scripts/finalize_coverage_repair.py --root MARKET --audit AUDIT --entry ENTRY
python scripts/finalize_coverage_repair.py --root MARKET --audit AUDIT --entry ENTRY --publish
```

The independent input audit checks unique original keys and reconciles each exclusive disposition, recording exact source/year/reason scopes. The finalizer also requires original explicit-source query restoration evidence from the retained acceptance run: all 31,384,115 daily provider-query records, plus all 30,581 previously excluded HF original observations, compared by keys and values while preserving use restrictions.

These are R2 repair scripts for the sealed R1 source inventory, not arbitrary ingestion commands. Publication verifies all changed keys and values in bounded instrument groups, retained old rows and batches, disjoint supplemental days, unique target keys, exhaustive input dispositions, metadata references and source versions. CURRENT changes atomically. Consumers separately pin the new release and manifest SHA in deployment.json. Rollback restores the prior consumer entry and paired runtime; it never deletes objects.

New dates without an evidenced calendar remain queryable with unknown expected coverage. Observed dates are not invented calendar sessions. Uncorroborated early observations, minute interval endpoints, the HF partial final day, rounded reported quantities and strict historical market PIT remain explicit limitations.

All repair audit artifacts, runtimes, test outputs and worktrees are retained in controlled Workspace paths. Preserve originals and every published object. This repair imports no financial samples, starts no persistent service and replays no account orders.
