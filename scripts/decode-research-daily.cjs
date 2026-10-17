// Reproduce bounded projections from a pinned pure KLC decoder. Never execute responses.
// Offline Node tool owned by AD daily research; dependencies and invocation: docs/development.md.
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const path = require('node:path');
const root = process.argv[2];
const verify = process.argv[3] === '--verify';
if (!root || process.argv.length > 4 || (process.argv[3] && !verify)) throw Error('Usage: decode-research-daily.cjs <package> [--verify]');
const sha = b => crypto.createHash('sha256').update(b).digest('hex');
const decoder = fs.readFileSync(path.join(root, 'raw/sina-klc-decoder.js'));
if (sha(decoder) !== '39a599c94dde4df1c2eb0882d4bff9560160cfd52ead0797cebb04b3122c2f52') throw Error('Decoder mismatch');
const contract = JSON.parse(fs.readFileSync(path.join(root, 'strategy-manifest.json')));
for (const security of contract.securities) {
  if (!/^\d{6}\.X(SHE|SHG)$/.test(security)) throw Error('Invalid security');
  const code = (security.endsWith('XSHE') ? 'sz' : 'sh') + security.slice(0, 6);
  const raw = fs.readFileSync(path.join(root, 'raw', code + '-daily.js'));
  const envelope = raw.toString().match(new RegExp('^var KLC_K2_' + code + '="([A-Za-z0-9+/]+)";\\s*(?:/\\*[A-Za-z0-9+/\\s]+\\*/\\s*)?$'));
  if (!envelope) throw Error('Unexpected envelope: ' + code);
  const context = vm.createContext({input: envelope[1]}, {codeGeneration:{strings:false,wasm:false}});
  const rows = JSON.parse(JSON.stringify(vm.runInContext(decoder + '; d(input);', context, {timeout:5000})));
  if (!Array.isArray(rows) || !rows.length || rows.length > 20000) throw Error('Row bound');
  const selected = rows.filter(r => [['2019-07-01', '2020-05-31'], ['2021-08-01', '2022-06-30'], ['2023-07-01', '2024-04-30']].some(([a,b]) => a <= r.date.slice(0,10) && r.date.slice(0,10) <= b));
  const value = {security, raw_sha256:sha(raw), decoder_sha256:sha(decoder), source_row_count:rows.length,
    source_first_date:rows[0].date, source_last_date:rows.at(-1).date, rows:selected};
  const result = Buffer.from(JSON.stringify(value) + '\n');
  const target = path.join(root, 'derived', code + '-daily.json');
  if (verify) {
    if (!fs.readFileSync(target).equals(result)) throw Error('Derived bytes differ: ' + security);
  } else {
    fs.writeFileSync(target, result, {flag:'wx'});
  }
  console.log(JSON.stringify({security, total:rows.length, selected:selected.length, verified:verify, derived_sha256:sha(result)}));
}
