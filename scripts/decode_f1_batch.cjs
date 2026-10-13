// Captured response envelopes are data; only the hash-pinned existing decoder runs.
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const path = require('node:path');
const [root, decoderPath, scopePath = 'docs/f1-batch-scope.json'] = process.argv.slice(2);
const sha = b => crypto.createHash('sha256').update(b).digest('hex');
const decoder = fs.readFileSync(decoderPath);
if (sha(decoder) !== '39a599c94dde4df1c2eb0882d4bff9560160cfd52ead0797cebb04b3122c2f52') throw Error('Decoder mismatch');
const scope = JSON.parse(fs.readFileSync(scopePath));
fs.mkdirSync(path.join(root, 'derived'), {recursive:true});
for (const {security} of scope.securities) {
  const code = (security.endsWith('XSHE') ? 'sz' : 'sh') + security.slice(0, 6);
  const raw = fs.readFileSync(path.join(root, 'raw', code + '-daily.js'));
  const envelope = raw.toString().match(new RegExp('^var KLC_K2_' + code + '="([A-Za-z0-9+/]+)";\\s*(?:/\\*[A-Za-z0-9+/\\s]+\\*/\\s*)?$'));
  if (!envelope) throw Error('Unexpected envelope: ' + code);
  const context = vm.createContext({input:envelope[1]}, {codeGeneration:{strings:false,wasm:false}});
  const rows = JSON.parse(JSON.stringify(vm.runInContext(decoder + ';d(input);', context, {timeout:5000})));
  if (!Array.isArray(rows) || !rows.length || rows.length > 20000) throw Error('Row bound');
  const selected = rows.filter(r => scope.input_windows.some(([a,b]) => a <= r.date.slice(0,10) && r.date.slice(0,10) <= b));
  const value = {security, raw_sha256:sha(raw), decoder_sha256:sha(decoder), source_row_count:rows.length, rows:selected};
  fs.writeFileSync(path.join(root,'derived',code+'-daily.json'), JSON.stringify(value)+'\n');
  console.log(JSON.stringify({security, selected:selected.length}));
}
