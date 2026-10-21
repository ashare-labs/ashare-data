# Standard minute missing quotes

`LocalMinuteProduct.prices(..., source="auto")` follows the deployment's selected
source. Published minute reads (`source="canonical"`) and the default native
reader represent an existing `price_status="invalid"` observation as a missing
quote. `conflict`, an extreme value, zero activity or a conditional VWAP diagnostic
alone does not establish invalidity and does not trigger this projection.

The time position and source/review identity remain. `open`, `high`, `low`, `close`,
`volume` and `amount` are JSON `null`, with `quote_status="missing"`,
`missing_reason="confirmed_invalid_price"`, `missing_fields` and the original
diagnostics. This is a whole-quote exclusion, not a claim that every field is
individually wrong: an OHLC contradiction cannot identify which price to repair.
Execution and fresh valuation remain denied. No zero, interpolation, forward
fill, previous close or other source's price is substituted.

Existing hard activity diagnostics (negative/nonfinite quantities, noninteger
share volume or null activity) make only the affected `volume`/`amount` field
missing: `quote_status="partial_missing"`, `missing_reason="unavailable_activity"`.
Prices are retained and the existing execution-only denial is unchanged.

For previously excluded HF rows, the reader uses the release-pinned issue ledger
to recover the exact native label. It requires a matching **invalid** review
range, source file SHA256, source row number and frozen source manifest. Reasons
and source locators are returned in `missing_evidence`. Only the requested
security/range's issue records are queried; raw Parquet is not opened for this
recovery. A competing selected whole-day source is checked before adding an old
source's missing slot, including when its labels lie outside the request.

This does not construct a complete session grid, reinterpret unknown quarantines,
or add support for other issue-ledger formats. `report.unavailable_rows` counts
returned rows with missing fields; `repaired_rows` remains zero. `MarketRelease.read` and
explicit `LocalMinuteProduct.prices(..., source="neigezhu")` remain diagnostic
paths: the latter returns original values and evidence. No source file, release
object, quality rule or publication pointer is changed.

The local product JSON is not the `ad-minute-research.v1` backtest input schema.
Store/export still require valid numeric bars and explicitly evidenced missing
coverage; the projection does not relax those import or execution contracts.
AQ's strategy history consumes that existing research contract separately.

集成导出：`ad-minute-research.v1` 可通过覆盖证明中的 `unavailable_quotes` 传递已确认缺失的原因、字段和定位；仅接受全行情为空、price_status=invalid、双 deny 且来源契约哈希一致的 `confirmed_invalid_price`。普通未观察到的缺口仍使用原 `no_quote_in_selected_snapshot`，来源冲突不得伪装成确认错误。
