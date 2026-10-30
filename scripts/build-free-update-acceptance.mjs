// Update the established local workbook from one verified ledger. Historical
// workbooks remain intact; the destination is the single latest entry.
import fs from 'node:fs/promises';
import path from 'node:path';
import { FileBlob, SpreadsheetFile } from '@oai/artifact-tool';

const [source, previousLedger, currentLedger, destination, evidence, previewRows] = process.argv.slice(2);
if (![source, previousLedger, currentLedger, destination, evidence].every(Boolean)) {
  throw new Error('usage: source.xlsx previous.json current.json destination.xlsx evidence-directory');
}
const previous = JSON.parse(await fs.readFile(previousLedger, 'utf8'));
const data = JSON.parse(await fs.readFile(currentLedger, 'utf8'));
const old = new Map(previous.items.map(i => [i.excel_row, i]));
if (data.items.length !== 198 || new Set(data.items.map(i => i.excel_row)).size !== 198) throw new Error('198 unique rows required');
const wb = await SpreadsheetFile.importXlsx(await FileBlob.load(source));
const sheet = wb.worksheets.getItem('198项验收总表');
const matrix = sheet.getRange('A10:S207').values;
const changed = [];
for (let index = 0; index < data.items.length; index++) {
  const item = data.items[index];
  const values = matrix[index];
  const prior = old.get(item.excel_row) ?? {};
  const beforeText = JSON.stringify([values[4],values[9],values[10],values[17],values[18]]);
  const differs = key => JSON.stringify(prior[key]) !== JSON.stringify(item[key]);
  if (Number(values[0]) !== item.excel_row) throw new Error('source workbook row mapping changed');
  for (const [column, key] of [[1,'category'],[2,'item'],[12,'candidate_status'],[13,'candidate_source'],[14,'note'],[15,'source_url']]) {
    if ((values[column] ?? '') !== (item[key] ?? '')) throw new Error(`original field changed: row ${item.excel_row} ${key}`);
  }
  if (JSON.stringify(old.get(item.excel_row)) === JSON.stringify(item)) continue;
  changed.push(item.excel_row);
  if (!['可运行','待验证','来源问题','继续实施','用户取消'].includes(item.user_status)) throw new Error('missing reviewed user status');
  values[3] = item.user_status;
  if (differs('freshness_display') || differs('acceptance_dimensions')) values[4] = item.freshness_display ?? item.acceptance_dimensions.freshness_label;
  values[5] = item.implementation_verified ? '已验证' : '待验证';
  values[6] = item.coverage_verified ? '已验证' : '待验证';
  values[7] = item.daily_deployment ? '已启用' : '未启用';
  values[8] = item.formal_published ? '已发布' : '未发布';
  if (item.scope_clarification && differs('scope_clarification')) values[9] = item.scope_clarification;
  if (item.latest_period_display && differs('latest_period_display')) values[10] = item.latest_period_display;
  else if (!item.latest_period_display && item.latest_source_dates?.length && differs('latest_source_dates')) values[10] = [...new Set(item.latest_source_dates)].sort().join('；');
  const received = [...new Set(item.live_collected_at ?? [])].sort().at(-1);
  if (received) values[11] = new Date(received);
  values[16] = item.evidence_binding?.version ?? values[16];
  if (differs('next_action_display') || differs('remaining_acceptance')) values[17] = item.next_action_display ?? item.remaining_acceptance?.join('；') ?? values[17];
  if (item.frequency_display && differs('frequency_display')) values[18] = item.frequency_display;
  sheet.getRange(`A${index+10}:S${index+10}`).values = [values];
  sheet.getRange(`K${index+10}`).format.wrapText = true;
  sheet.getRange(`D${index+10}`).format.fill = {'可运行':'#E1F2E8','待验证':'#FFF3D6','来源问题':'#FBE9E7','继续实施':'#FFF3D6','用户取消':'#ECEFF1'}[item.user_status];
  sheet.getRange(`D${index+10}`).format.font.color = {'可运行':'#17633E','待验证':'#805B16','来源问题':'#A52620','继续实施':'#805B16','用户取消':'#455A64'}[item.user_status];
  if (beforeText !== JSON.stringify([values[4],values[9],values[10],values[17],values[18]])) sheet.getRange(`A${index+10}:S${index+10}`).format.autofitRows();
}
sheet.getRange('D9').values = [['当前结论']];
sheet.getRange('T9:T207').copyFrom(sheet.getRange('F9:F207'),'all');
sheet.getRange('T9').values = [['有效真实样本']];
sheet.getRange('T10:T207').values = data.items.map(i => [i.status === '真实小样本通过' ? '有' : '无']);
sheet.getRange('T:T').format.columnWidth = 18;
const narrowed = Boolean(data.scope_decision);
const pendingLabel = narrowed ? '继续实施' : '待验证';
const stoppedLabel = narrowed ? '用户取消' : '来源问题';
const userCounts = data.items.reduce((all,i) => ({...all,[i.user_status]:(all[i.user_status] ?? 0)+1}),{});
sheet.getRange('A4:I4').values = [['范围','可运行（已验范围）',pendingLabel,stoppedLabel,'实现已验证','来源窗口已验','有效真实样本','日常启用','正式发布']];
sheet.getRange('A5:I5').formulas = [[
  '=COUNTA(A10:A207)', '=COUNTIF(D10:D207,"可运行")', `=COUNTIF(D10:D207,"${pendingLabel}")`, `=COUNTIF(D10:D207,"${stoppedLabel}")`,
  '=COUNTIF(F10:F207,"已验证")', '=COUNTIF(G10:G207,"已验证")', '=COUNTIF(T10:T207,"有")',
  '=COUNTIF(H10:H207,"已启用")', '=COUNTIF(I10:I207,"已发布")']];
sheet.getRange('A6').values = [[narrowed ? `目标：CMES历史底库 + 免费源当期增量。${userCounts['可运行'] ?? 0}项已实现，${userCounts[pendingLabel] ?? 0}项继续，${userCounts[stoppedLabel] ?? 0}项用户取消。取消项保留原行和证据，不再研究、实现或重试。` : `目标：CMES 历史底库 + 免费源当期增量。当前快照和短窗口可持续留存；历史空档单列补数。可运行按表内范围验收；定时已启用 ${data.daily_deployed} 项，正式发布 ${data.formal_published} 项。`]];
sheet.getRange('A7').values = [[`核验时间：${data.generated_at.replace('T',' ').slice(0,19)} UTC。原表字段逐字保留；历史证据仍绑定各自代码版本。`]];
const trial = data.items.findIndex(i => i.user_status === pendingLabel)+10;
sheet.getRange(`D${trial}`).values = [['可运行']];
wb.recalculate();
if (Number(sheet.getRange('B5').values[0][0]) !== userCounts['可运行']+1 || Number(sheet.getRange('C5').values[0][0]) !== userCounts[pendingLabel]-1) throw new Error('status formulas do not react');
sheet.getRange(`D${trial}`).values = [[pendingLabel]];
wb.recalculate();
const actual = sheet.getRange('A5:I5').values[0].map(Number);
const expected = [198, userCounts['可运行'], userCounts[pendingLabel], userCounts[stoppedLabel], data.completed,
  data.coverage_verified_count, data.status_counts['真实小样本通过'], data.daily_deployed, data.formal_published];
if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error('workbook summary disagrees with ledger');
const checks = await wb.inspect({kind:'region',sheetId:sheet.name,range:'A4:I5',maxChars:3500,tableMaxRows:2,tableMaxCols:9});
const errors = await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#N/A|#NAME\\?',options:{useRegex:true,maxResults:50},maxChars:3000});
await fs.mkdir(evidence,{recursive:true});
await fs.writeFile(path.join(evidence,'workbook-inspection.json'),JSON.stringify({checks,errors,summary:actual,changed_rows:changed,status_recalculation_test_passed:true},null,2));
const ranges = [['opening','A1:I16']];
const selectedPreviews = previewRows ? previewRows.split(',').map(Number) : changed.slice(0, 8);
if (selectedPreviews.some(number => !changed.includes(number))) throw new Error('preview row is not part of this update');
for (const number of selectedPreviews) {
  const row = data.items.findIndex(i => i.excel_row === number)+10;
  ranges.push([`scope-${number}`,`J${row}:L${row}`]);
}
for (const [name,range] of ranges) {
  const blob = await wb.render({sheetName:sheet.name,range,scale:1,format:'png'});
  await fs.writeFile(path.join(evidence,`workbook-${name}.png`),new Uint8Array(await blob.arrayBuffer()));
}
await fs.mkdir(path.dirname(destination),{recursive:true});
await (await SpreadsheetFile.exportXlsx(wb)).save(destination);
console.log(JSON.stringify({destination,rows:198,summary:actual,changed_rows:changed}));
