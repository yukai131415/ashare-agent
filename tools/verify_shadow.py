"""Run one bounded real shadow experiment, verifying protected production files."""
import hashlib
import json
from a_stock_agents.config import load_settings
from a_stock_agents.shadow import freeze_input,execute,history
from a_stock_agents.storage import atomic_write_json
from a_stock_agents.contracts import iso_now


if __name__=='__main__':
    settings=load_settings()
    paths=['agents/recommendation/data/recommendations.db','agents/recommendation/data/latest.json','state/model_state.json','state/health.json']
    def hashes():return {name:hashlib.sha256((settings.runtime/name).read_bytes()).hexdigest() for name in paths if (settings.runtime/name).exists()}
    before=hashes();day,baseline,pool,cutoff=freeze_input(settings)
    execute(settings,day,baseline,pool,cutoff)
    report=history(settings)[0];after=hashes()
    result={'verified_at':iso_now(),'date':day,'production_unchanged':before==after,'protected_files':list(before),
            'shadow_status':report['status'],'prospective':report['prospective'],
            'selected_code':(report.get('selection') or {}).get('code'),'roles':list(report.get('reviews',{})),
            'tool_calls':report.get('tool_calls',[]),'usage':report.get('usage',{}),'error_type':report.get('error_type')}
    atomic_write_json(settings.root/'docs/verification/shadow_smoke.json',result)
    print('SHADOW_VERIFICATION '+json.dumps(result,ensure_ascii=False),flush=True)
