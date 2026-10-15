# Conditional preparation and opening recheck (candidate)

This is an opt-in candidate, not an activation of a calendar plan. The default
collector and all v1–v4 reads retain their old interpretation. No failed v4
capture is promoted. `--conditional` explicitly selects
`ad-calendar-phase-observation.v5`.

The public package requires a separately held, explicitly configured issuer
baseline through `ASHARE_PILOT_EVIDENCE`; see [local evidence setup](calendar-pilot-scope.md#本地证据与公开安装包).
Missing material blocks dependent capture or reread. Production hashes and date
scope are unchanged; synthetic tests do not supply a production baseline.

## Why the old pre-open gate is incorrect

Actual 2026-10-14 08:55 and 08:58 Shanghai captures returned Sina's previous-day
15:34:59 quote. Eastmoney returned same-day 08:30:14/30/28 timestamps and the
three correct session references/limits. The old contract required a current-day
normal quote before 09:00 and rejected the reference/limits after 300 seconds.
These are different facts with different lifetimes.

The [SSE rules effective 2026-07-06](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)
place the opening auction at 09:15–09:25 (2.4.2, 3.3.1), define the ordinary
main-board 10% limits from the previous close and tick rounding (3.3.11,
3.3.13, 3.3.17), and explain ex-right reference prices (4.3.1–3).
Neither these rules nor the vendor code establishes a five-minute expiry for
unchanged session limits. This does not establish a guaranteed vendor
publication time or availability SLA.

[Eastmoney's own page bundle](https://quote.eastmoney.com/newstatic/build/vendor.js)
maps f51/f52 to upper/lower limits, f60 to previous close, and f86 to its displayed
trading time. f86 is not an independently verified exchange publication timestamp.
[Sina's SDK](https://finance.sina.com.cn/sinafinancesdk/js/sf_sdk.js) maps quote
index 32 codes 02/03 to stopped trading. A previous-day 00 is retained as unknown
for the new day. The normal quote observed at 09:13–09:15 on the 14th had zero
open; that observation is neither a fill nor an opening-price receipt.

After three further bounded diagnostics returned Eastmoney EOF (not evidence of
a quota), the official free SSE page endpoint was verified. At 09:33:33 it
returned exact codes, D references/limits and T111 for all three stocks, with
receipts at 09:33:33.238–.384. The
[official page script](https://www.sse.com.cn/xhtml/home/2021public/querySearch/search_HQ_2021.js)
supplies the list endpoint, field names and trade-phase meanings. The candidate
v5 selects this SSE source explicitly instead of Eastmoney for the three session
parameter snapshots. There is no silent provider fallback. Its pre-09:00
availability has not yet been observed and must be measured on the next real day.

## Field and phase contract

| Fact | Preparation, 08:30–09:00 | Opening, 09:30–09:35 |
| --- | --- | --- |
| Previous close, history, calendar | AQ reads AD's unrevised D−1 daily packet, already received before intent; calendar must prove adjacency | Frozen target and calendar; closing cannot rerun the decision |
| Identity, listing board, ST/delisting | Current complete SSE lists, exact security identity | Re-fetch the same sources; changed/unknown or disallowed state blocks |
| Scheduled suspension | Complete dated SSE and Eastmoney snapshots; either positive blocks | Re-fetch both, including positive vetoes |
| Live trading state | May remain null; a current positive halt blocks | Actual D quote, 09:30–09:35, 00, received after source time and within 300 seconds; positive open |
| Reference and limits | Complete one-stock SSE snapshot, exact code, integer D date/time not future; positive reference and rule-consistent 10% rounded limits | Re-fetch; require current continuous phase and an opening price matching Sina; AQ requires exact match to the prepared reference/limits/rule |
| Corporate actions | Full pinned finite issuer corpus and current catalog/text recheck | Repeat the complete check; new/changed/unavailable notices block |
| Actual account/cash/positions | AQ's previous native checkpoint or declared empty genesis | AQ native risk budget, holdings, T+1 and cash checks remain required |

The preparation reference must equal the last raw close in the D−1 daily packet
in AQ. This bounded pilot rejects current price/share/code actions; it never
substitutes an adjusted close silently. Old-day limits, future timestamps, bad
identity, formula mismatches, incomplete snapshots and missing limits still
block. Re-fetching does not promise that vendors are complete; the recorded
receipts and the finite reviewed scope remain the evidence boundary.

## Wire behavior

Both phases collect the full 114-source set. Sina is fetched last so the live
state is the last input before sealing an opening. `read()` verifies raw hashes,
source URLs, receipt times, the real phase window, and recomputes the assessment.
SSE snapshot P/M/N veto both preparation and execution. This bounded pilot only
accepts the actually verified SSE T111 form for opening; other suffixes require review,
with source time 09:30–09:35 and age at most 300 seconds, as well as Sina 00.
Old v4 captures lack these official snapshots and cannot be adopted as v5.

- Admission v5 exposes `preparation_ready`; `suspended` may be null.
- Opening v5 exposes `execution_ready` and `opening_ready`, and carries all
  refreshed admission facts and action proofs alongside the observed opening.
- Both always expose `execution_permission: false`. Neither packet alone
  creates a broker order or a native simulated trade.
- AQ v2 must consume the matching v5 contract. AQ v1 cannot consume it; v2 cannot
  fall back to v4.

Example prospective collector command (not run against the actual account):

```sh
python -m ashare_data.phase_observation --root CANDIDATE_STORE admission --day YYYY-MM-DD --conditional
python -m ashare_data.phase_observation --root CANDIDATE_STORE opening --day YYYY-MM-DD --conditional
```

This patch deliberately retains the reviewed 14–15 action scope. It does not
grant permission for the 16th. A 15–16 activation requires a newly reviewed
baseline, exact calendar evidence and distinct pinned plan/runner. Changing the
old v4 baseline or relabelling an old capture is prohibited by the reader/tests.

## Validation and limitations

Tests cover previous-day quote plus same-day 08:30 limits, positive and unknown
halts, both incomplete suspension lists, ST, missing/old/future/wrong limits,
new issuer notices, full opening recheck, relabelled old packets and late calls.
Fixtures and offline reinterpretation of the failed real capture do not count
as real calendar acceptance. No official daily publication SLA or EOF quota
cause has been established. Network failures still block the dependent phase.
