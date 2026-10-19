"""Fetch unadjusted historical bars; reconstruct close factors with provenance."""
import csv
import json
import socket
import sys
from datetime import datetime
from pathlib import Path
import baostock as bs

OUT = Path('runtime/research_backtests/close_inputs_20261015_16')
OUT.mkdir(parents=True, exist_ok=True)
socket.setdefaulttimeout(20)

def query(result):
    rows = []
    while result.error_code == '0' and result.next():
        rows.append(dict(zip(result.fields, result.get_row_data())))
    if result.error_code != '0':
        raise RuntimeError(result.error_msg)
    return rows

def eligible(code):
    return code.startswith(('sh.600', 'sh.601', 'sh.603', 'sh.605', 'sz.000', 'sz.001', 'sz.002', 'sz.003'))

assert bs.login().error_code == '0'
try:
    universe_path = OUT/'universes.json'
    universes = json.loads(universe_path.read_text(encoding='utf-8')) if universe_path.exists() else {day: query(bs.query_all_stock(day=day)) for day in ('2026-10-15','2026-10-16')}
    (OUT/'universes.json').write_text(json.dumps(universes,ensure_ascii=False),encoding='utf-8')
    codes = sorted({r['code'] for rows in universes.values() for r in rows if eligible(r['code'])})
    fields = 'date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,peTTM,isST,tradestatus'
    results = {day: [] for day in universes}
    failures = []
    for i, code in enumerate(codes):
        if len(sys.argv)>1 and i%4 != int(sys.argv[1]):
            continue
        try:
            path = OUT/(code+'.json')
            if path.exists():
                bars = json.loads(path.read_text(encoding='utf-8'))['bars']
            else:
                bars = query(bs.query_history_k_data_plus(code, fields, start_date='2026-09-25',end_date='2026-10-16',frequency='d',adjustflag='3'))
                path.write_text(json.dumps({'source':'baostock','adjustflag':'3','fetched_at':datetime.now().astimezone().isoformat(),'bars':bars}),encoding='utf-8')
            for day, universe in universes.items():
                names = {r['code']:r['code_name'] for r in universe}
                if code not in names:
                    continue
                matches = [b for b in bars if b['date']==day]
                if not matches:
                    raise ValueError(f'missing {day}')
                b = matches[0]
                if b['tradestatus']!='1' or b['isST']=='1':
                    continue
                prior = [float(x['volume']) for x in bars if x['date']<day and x['tradestatus']=='1'][-5:]
                if len(prior)!=5 or sum(prior)<=0:
                    raise ValueError(f'missing five-day volume {day}')
                prev=float(b['preclose'])
                results[day].append(dict(code=code.split('.')[1],name=names[code],price=b['close'],change_pct=b['pctChg'],amount=b['amount'],volume_ratio=float(b['volume'])/(sum(prior)/5),turnover_pct=b['turn'],pe_ratio=b['peTTM'],amplitude_pct=(float(b['high'])-float(b['low']))/prev*100,source_date=day,source='baostock_unadjusted_daily',volume_ratio_method='daily_volume / preceding_5_trading_sessions_mean'))
        except Exception as exc:
            failures.append({'code':code,'error':str(exc)})
        if i%100==0: print(f'{i}/{len(codes)} failures={len(failures)}',flush=True)
    if len(sys.argv)>1:
        print('Shard complete',sys.argv[1],len(failures),flush=True)
        sys.exit(0)
    report={'source':'baostock','universe':len(codes),'counts':{d:len(r) for d,r in results.items()},'failures':failures}
    (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
    if failures:
        raise RuntimeError(f'{len(failures)} incomplete symbols; not publishing snapshots')
    for day, rows in results.items():
        assert len(rows)>=2500
        with (OUT/(day+'.csv')).open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    print(json.dumps(report),flush=True)
finally:
    bs.logout()
