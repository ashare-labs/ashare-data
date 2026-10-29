"""Merge reviewed row updates without changing the frozen CMES workbook fields.

Updates are explicit version-bound evidence records, never a reason to upgrade
unmentioned rows. The output is a new historical ledger; latest export is separate.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from ashare_data.providers.free_update_acceptance import acceptance_dimensions, user_progress, validate_ledger


def merge(previous, updates):
    validate_ledger(previous)
    rows = {i['excel_row']: i.copy() for i in previous['items']}
    seen = set()
    for update in updates['items']:
        number = update['excel_row']
        if number not in rows or number in seen:
            raise ValueError('unknown or duplicate update row')
        seen.add(number)
        rows[number] = rows[number] | update
    items = [rows[n] for n in sorted(rows)]
    for item in items:
        item['acceptance_dimensions'] = acceptance_dimensions(item)
        item.update(user_progress(item))
        if item.get('publication_reconciliation'):
            item['acceptance_dimensions']['publication_reconciliation'] = item['publication_reconciliation']
    result = previous | {'generated_at':datetime.now(timezone.utc).isoformat(), 'items':items,
        'completed':sum(i['implementation_verified'] for i in items),
        'coverage_verified_count':sum(i['coverage_verified'] for i in items),
        'status_counts':dict(Counter(i['status'] for i in items)),
        'user_status_counts':dict(Counter(i['user_status'] for i in items)),
        'core_counts_by_version':dict(Counter(i['evidence_binding']['version'] for i in items if i['implementation_verified'])),
        'coverage_counts_by_version':dict(Counter(i['evidence_binding']['version'] for i in items if i['coverage_verified'])),
        'daily_deployed':sum(bool(i.get('daily_deployment')) for i in items),
        'formal_published':sum(bool(i.get('formal_published')) for i in items)}
    validate_ledger(result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--previous', type=Path, required=True)
    p.add_argument('--updates', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise ValueError('historical acceptance output already exists; preserve it')
    result = merge(json.loads(args.previous.read_text()), json.loads(args.updates.read_text()))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ['completed','coverage_verified_count','status_counts','daily_deployed','formal_published']},ensure_ascii=False))
