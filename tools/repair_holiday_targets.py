"""Backup and repair holiday target dates without changing picks or model scores."""
import json
import sqlite3
import shutil
from datetime import date, datetime
from pathlib import Path
from a_stock_agents.trading_calendar import next_weekday
from a_stock_agents.storage import atomic_write_json

root = Path(__file__).resolve().parents[1] / 'runtime'
db = root / 'agents/recommendation/data/recommendations.db'
latest = root / 'agents/recommendation/data/latest.json'
results = root / 'agents/backtest/data/results.jsonl'
backup = root / 'backups' / datetime.now().strftime('holiday_targets_%Y%m%d_%H%M%S')
backup.mkdir(parents=True)
with sqlite3.connect(db) as con:
    with sqlite3.connect(backup / db.name) as dst:
        con.backup(dst)
    for p in (latest, results):
        if p.exists():
            shutil.copy2(p, backup / p.name)
    changes = {}
    for rec_id, rec_day, target in con.execute('SELECT recommendation_id,recommendation_date,target_date FROM recommendation_history').fetchall():
        correct = next_weekday(date.fromisoformat(rec_day)).isoformat()
        if correct != target:
            changes[rec_id] = correct
            for table in ('recommendation_history', 'recommendation_realtime'):
                con.execute(f'UPDATE {table} SET target_date=? WHERE recommendation_id=?', (correct, rec_id))
            print(rec_day, target, '->', correct)
    con.commit()
if latest.exists():
    data = json.loads(latest.read_text(encoding='utf-8'))
    if data.get('recommendation_id') in changes:
        data['target_date'] = changes[data['recommendation_id']]
        atomic_write_json(latest, data)
# Remove only now-invalid backtests; they remain recoverable in the backup.
if results.exists() and changes:
    rows = [json.loads(line) for line in results.read_text(encoding='utf-8').splitlines() if line.strip()]
    kept = [r for r in rows if r.get('recommendation_id') not in changes or r.get('target_date') == changes[r['recommendation_id']]]
    temp = results.with_suffix('.repair.tmp')
    temp.write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in kept), encoding='utf-8')
    temp.replace(results)
    print('Invalid backtests removed:', len(rows)-len(kept))
print('Backup:', backup)
