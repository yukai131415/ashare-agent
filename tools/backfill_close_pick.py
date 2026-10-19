"""Explicit historical close reconstruction, never backdate the generation time."""
import argparse
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from a_stock_agents.config import load_settings
from a_stock_agents.storage import read_csv, atomic_write_json
from a_stock_agents.agents.analysis import build_features
from a_stock_agents.agents.market import is_allowed_stock
from a_stock_agents.agents.recommendation import recommendation_basis
from a_stock_agents.contracts import MarketRow, iso_now
from a_stock_agents.models import get_model
from a_stock_agents.recommendation_store import recommendation_store
from a_stock_agents.trading_calendar import next_weekday, is_trading_weekday

p = argparse.ArgumentParser()
p.add_argument('--date', required=True)
p.add_argument('--snapshot', required=True)
a = p.parse_args()
s = load_settings()
day = date.fromisoformat(a.date)
assert is_trading_weekday(day)
rows = read_csv(Path(a.snapshot))
assert len(rows) >= int(s.section('supervisor').get('min_market_rows', 2500)), 'Incomplete universe'
for r in rows:
    if r.get('source') == 'baostock_unadjusted_daily':
        assert r.get('source_date') == a.date, 'Wrong daily bar date'
        assert day < datetime.now().date(), 'Historical bars must be completed'
        assert r.get('volume_ratio_method') == 'daily_volume / preceding_5_trading_sessions_mean'
    else:
        t = datetime.strptime(r.get('quote_time', ''), '%Y%m%d%H%M%S')
        assert t.date() == day and t.hour >= 15, 'Not target-day closing data'
rows = [r for r in rows if is_allowed_stock(MarketRow(code=r['code'], name=r['name'], price=1, change_pct=0), s.section('market'))]
features = build_features(rows, [])
model = get_model('balanced')
cfg = s.section('recommendation')
candidates = [r for r in features if float(r.get('amount') or 0) >= cfg['min_turnover_amount'] and abs(float(r.get('change_pct') or 0)) < cfg['max_daily_change_pct']]
candidates.sort(key=model.score, reverse=True)
assert candidates
r = candidates[0]
rec = dict(recommendation_id=hashlib.sha256(f'close-backfill:{day}:{r["code"]}:{model.version}'.encode()).hexdigest()[:20], recommendation_date=a.date, created_at=iso_now(), target_date=next_weekday(day).isoformat(), code=r['code'], name=r['name'], reference_price=float(r['price']), model=model.name, model_version=model.version, score=round(model.score(r), 6), basis={**recommendation_basis(r), 'calculation_mode': 'historical_close_backfill', 'market_snapshot': str(Path(a.snapshot).resolve()), 'source_trading_date': a.date}, disclaimer='收盘后补算，不是当日14:30实际推荐；仅供研究，不构成投资建议。')
store = recommendation_store(s.runtime)
rec['basis']['historical_provider'] = r.get('source', 'tencent_snapshot')
rec['basis']['volume_ratio_method'] = r.get('volume_ratio_method', 'provider_quote')
assert not any(x.get('recommendation_date') == a.date for x in store.history()), 'Existing date must not be overwritten'
output = s.runtime / 'research_backtests' / f'close_backfill_{a.date}.json'
atomic_write_json(output, {'recommendation': rec, 'universe_count': len(rows), 'candidate_count': len(candidates), 'top3': [{'code': x['code'], 'name': x['name'], 'score': round(model.score(x), 6)} for x in candidates[:3]]})
assert store.insert_history(rec)
print(json.dumps(rec, ensure_ascii=False))
