# Current-calendar phase receipts

`python -m ashare_data.phase_observation --root PATH admission --day YYYY-MM-DD`
collects one bounded set of public responses at actual Shanghai 08:30–09:00.
`opening --day YYYY-MM-DD` collects a separate Sina quote at actual 09:30–09:35.
Only the fixed pilot pool 600000.XSHG, 600036.XSHG, 601318.XSHG is supported.
No future/backdated collection, cache fallback, scheduler, credentials, alternate
account or automatic network retry is used.

The admission diagnostic preserves 12 responses: Sina quotes; SSE suspension,
action-calendar, risk-warning and delisting lists; Eastmoney suspension, three
dividend histories and three current quote responses. Each source retains the
exact URL, local start/receipt time, raw body hash, and HTTP/error outcome. HTTP
200 with an API error or incomplete pagination is not an empty valid list.

Sina's own `datas/hq.js` interprets quote index 32: `02` and `03` are stop-day
states. Only a quote dated D and actually received before the decision can be a
D diagnostic. Names without ST do not certify ST absence. The Eastmoney quote
fields f51/f52 are reported upper/lower limits; f86 is retained and rendered as
a timestamp but does not independently prove which trading day's limits apply.
Raw reported values remain visible even while admission is refused.

The current reader intentionally grants no execution permission. The inspected
public list responses lack an explicit effective-through D guarantee; dividend
history is not all-action coverage, includes proposals with null implementation
dates, and has no complete payment-date/cash-availability contract. The SSE
action-calendar page code removes its dividend UI; an empty response is not
proof that no split, rights issue, cash action or transformation applies.
Consequently eligibility, ST, effective limits and complete action absence remain
unverified. That is a concrete unresolved owner contract, not a claim that these
facts cannot be obtained for free.

An opening packet can be `opening_ready` when D's timestamped `00` quote and a
positive open are received within the opening window. Its row projection omits
high/low/close/volume. It still has `execution_permission: false`; AQ also needs
its separately frozen, fully admitted pre-open intent.

`read --capture SHA` checks all raw source hashes/times/URLs and recomputes every
projection. Rehashing a packet with a manually changed permission flag does not
pass. `recover` reconciles saved packets after collector interruption or marks
an unfinished attempt interrupted. The caller must explicitly retry in the
remaining window. Interrupted collectors never authorize a native order.

`tests/test_phase_observation.py` uses clearly synthetic local clocks/HTTP
responses. Real observations and public endpoint probes live outside this repo
and must be inspected separately. No real pre-open claim follows from these tests.
