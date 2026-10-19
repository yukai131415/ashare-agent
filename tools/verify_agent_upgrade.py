"""Compare the actual compiled graph against the pre-upgrade, frozen full rankings."""
import hashlib
from pathlib import Path
from a_stock_agents.config import load_settings
from a_stock_agents.contracts import iso_now
from a_stock_agents.storage import read_csv, read_json, atomic_write_json
from a_stock_agents.workflow import AgentWorkflow


def main():
    settings=load_settings()
    baseline=read_json(settings.runtime/'upgrade_verification/before.json')
    if not baseline:
        raise SystemExit('Missing pre-upgrade baseline; never generate a baseline from the upgraded implementation.')
    comparisons=[]
    for i,case in enumerate(baseline['cases']):
        path=settings.root/case['snapshot']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==case['snapshot_sha256'], 'Snapshot changed'
        workflow=AgentWorkflow(settings)
        report,ranked=workflow.invoke(f'verify_{i}',replay_rows=read_csv(path),model=case['model'],persist=False)
        actual=[{'code':r['code'],'score':score,'price':float(r['price'])} for score,r in ranked]
        expected=case['ranked']
        passed=actual==expected and (report['ok'] if expected else not report['ok'])
        comparisons.append({'snapshot':case['snapshot'],'model':case['model'],'candidate_count':len(expected),'equal':passed,
                            'before_top1':expected[0] if expected else None,'after_top1':actual[0] if actual else None})
        if i%30==0:print(f'{i}/{len(baseline["cases"])}',flush=True)
    hashes={name:hashlib.sha256((settings.root/'src/a_stock_agents'/name).read_bytes()).hexdigest() for name in ('models.py','agents/analysis.py')}
    unchanged=all(hashes[n]==baseline['source_sha256'][n] for n in hashes)
    result={'verified_at':iso_now(),'engine':'compiled_langgraph','baseline_captured_at':baseline['captured_at'],
            'cases':len(comparisons),'ranked_rows':sum(c['candidate_count'] for c in comparisons),
            'passed':sum(c['equal'] for c in comparisons),'model_and_feature_files_unchanged':unchanged,
            'ok':all(c['equal'] for c in comparisons) and unchanged,'comparisons':comparisons}
    atomic_write_json(settings.root/'docs/verification/recommendation_parity.json',result)
    print({k:v for k,v in result.items() if k!='comparisons'},flush=True)
    return 0 if result['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
