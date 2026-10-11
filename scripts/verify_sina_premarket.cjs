// Offline evidence audit; never used as an AQ quote or event-time publisher.
const fs = require('fs');
const vm = require('vm');
const crypto = require('crypto');
const path = require('path');
const root = process.argv[2];
const sha = b => crypto.createHash('sha256').update(b).digest('hex');
const read = n => fs.readFileSync(path.join(root, n));
const raw = read('sina-pingan-daily.js');
const decoder = read('sina-klc-decoder.js');
const frozen = JSON.parse(read('sina-pingan-daily-decoded.json'));
const envelope = raw.toString().match(/^var KLC_K2_sz000001="([A-Za-z0-9+/]+)"/);
if (!envelope || frozen.raw_sha256 !== sha(raw) || frozen.decoder_sha256 !== sha(decoder)) throw Error('lineage');
function decode(source) {
  const context = vm.createContext({encoded: envelope[1]});
  vm.runInContext(source, context, {timeout: 2000});
  return JSON.parse(JSON.stringify(vm.runInContext('d(encoded)', context, {timeout: 5000})));
}
const rows = decode(decoder.toString());
if (JSON.stringify(rows) !== JSON.stringify(frozen.rows)) throw Error('full decoded record mismatch');
const needle = 'a.p && (i.prevclose = P(a.p, r.p_p))';
if (decoder.toString().split(needle).length !== 2) throw Error('instrumentation ambiguous');
// Log the already encoded marker and precision; do not alter the calculation.
const auditRows = decode(decoder.toString().replace(needle,
  'a.p && (i.prevclose = P(a.p, r.p_p), i.audit = {encoded: a.p, precision_code: r.p_p, scale_exponents: T(r.p_p)})'));
const audit = auditRows.find(r => r.date === '2024-06-14T00:00:00.000Z');
console.log(JSON.stringify({all_rows_match: true, count: rows.length,
  raw_sha256: sha(raw), decoder_sha256: sha(decoder),
  selected: rows.filter(r => ['2024-06-13T00:00:00.000Z','2024-06-14T00:00:00.000Z'].includes(r.date))
    .map(r => ({date: r.date, ...(r.prevclose === undefined ? {previous_raw_close: r.close} : {source_prevclose: r.prevclose})})),
  marker_audit: audit.audit,
  explanation: 'The encoded previous-price integer is scaled independently of current-day OHLC. This proves the frozen source marker, not a JQ rounding rule or historical delivery clock.'}, null, 2));
