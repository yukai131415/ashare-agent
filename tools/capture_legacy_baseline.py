"""Capture legacy analysis-then-filter ordering using unchanged model/feature files.

The first capture filtered before normalization; retain it for audit and correct that
test-harness error. This script deliberately does not import selection/workflow.
"""
import hashlib
from a_stock_agents.config import load_settings
from a_stock_agents.storage import read_json,read_csv,atomic_write_json
from a_stock_agents.agents.analysis import build_features
from a_stock_agents.agents.market import is_allowed_stock
from a_stock_agents.contracts import MarketRow,iso_now
from a_stock_agents.models import MODELS

s=load_settings()
path=s.runtime/'upgrade_verification/before.json'
original=read_json(path)
assert original, 'Requires baseline captured before upgrade'
for name in ('models.py','agents/analysis.py'):
    assert hashlib.sha256((s.root/'src/a_stock_agents'/name).read_bytes()).hexdigest()==original['source_sha256'][name]
archive=path.with_name('before_filter_first_capture.json')
if not archive.exists():
    atomic_write_json(archive,original)
cases=[]
for old in original['cases']:
    snapshot=s.root/old['snapshot']
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest()==old['snapshot_sha256']
    features=build_features(read_csv(snapshot),[])
    # Legacy RecommendationAgent filters AFTER AnalysisAgent computes z-scores.
    features=[r for r in features if is_allowed_stock(MarketRow(code=r['code'],name=r['name'],price=1,change_pct=0),s.section('market'))]
    model=MODELS[old['model']]
    ranked=[]
    for row in features:
        if float(row.get('amount') or 0)<s.section('recommendation')['min_turnover_amount']:continue
        if abs(float(row.get('change_pct') or 0))>=s.section('recommendation')['max_daily_change_pct']:continue
        ranked.append((model.score(row),row))
    ranked.sort(key=lambda value:value[0],reverse=True)
    cases.append({**old,'ranked':[{'code':r['code'],'score':score,'price':float(r['price'])} for score,r in ranked]})
atomic_write_json(path,{**original,'cases':cases,'capture_corrected_at':iso_now(),
    'capture_correction':'Test harness now applies legacy analysis-before-filter order. Model and feature source SHA256 remain identical to pre-upgrade capture; initial capture retained.'})
print('Captured legacy analysis-before-filter baseline:',len(cases))
