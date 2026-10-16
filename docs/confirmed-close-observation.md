# Evidence-based close observation

`daily_observation capture --confirmed-close` selects
`ad-current-daily-observation.v2`. The legacy v1 API/CLI remains unchanged for
sealed accounts. Its 17:45 cutoff was a pilot observation window, not a verified
Sina publication SLA; the general `Client.get_price()` also retains its legacy
15:05 software threshold. Neither is used by the opt-in v2 candidate reader.

The [current SSE trading rules](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)
and [SSE's own page script](https://www.sse.com.cn/xhtml/home/2021public/querySearch/search_HQ_2021.js)
are the primary basis. The script's `checkStatus()` maps E to closed, U to
closing auction, and T to continuous trading. A clock threshold alone does not
prove that a provider has published a usable close.

The v2 policy requires all of:

- Actual same-day receipt after 15:00 Asia/Shanghai; the SSE calendar identifies
  the day as open and supplies the next open day, including weekend gaps.
- Exactly the latest 120 consecutive source-calendar daily rows per security,
  with validated raw OHLCV. Missing labels are never filled.
- A freshly fetched SSE one-security snapshot with the exact code, same-day
  timestamp at or after 15:00, no future time and phase E.
- Snapshot open/last/reference close matching the daily open/close/prior close.
- A second fresh HTTP daily read after the SSE snapshot, identical across all
  120 selected rows. It has a separate receipt even when its body hash matches.
- No revisions against previously frozen comparable captures.

First daily read, official confirmation and repeated daily read retain URLs,
raw bytes, hashes, request-start and receipt times. `read()` reconstructs the
checks offline. An interrupted capture can be explicitly recovered; missing
publication, unstable responses and conflicts leave an inspectable blocked
attempt. There is no automatic source fallback, time override, retry loop or
scheduler. A retry is a new explicit invocation.

This is bounded observed readiness, not guaranteed first-publication time or
permanent finality. `source_publication_time_verified`, `finality_verified` and
`execution_permission` remain false. A later revision blocks the new capture;
an earlier settled account is not silently rewritten. Official quote time may
be delayed, so the first successful capture time must be measured on each real
day. The v2 gate can pass shortly after 15:00 in controlled tests; that is not
evidence of a real vendor's publication latency.
