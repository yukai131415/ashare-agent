"""Public market enrichment isolated from the recommendation engine and its history."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
import hashlib
import math
import json
import subprocess
import re
from html import unescape
import threading
from urllib.parse import urlencode

from .console_log import console_event
from .contracts import iso_now
from .http import HttpClient
from .recommendation_store import recommendation_store
from .research_dashboard import number
from .scheduler import SHANGHAI, shanghai_now
from .storage import atomic_write_json, read_json
from .trading_calendar import is_trading_weekday, next_weekday

_job_lock=threading.Lock()
_running=set()


def folder(settings):return settings.runtime/'research_dashboard'


def http(settings):return HttpClient(12,settings.section('http')['user_agent'],0)


def public_json(settings,url,params):
    """Use the same public endpoint through curl if urllib's TLS transport fails."""
    try:return http(settings).get_json(url,params)
    except Exception as exc:
        console_event('public_json_fallback',level='WARN',endpoint=url,parameters=params,error_type=type(exc).__name__,message=str(exc),transport='curl')
        result=subprocess.run(['curl.exe','--silent','--show-error','--fail-with-body','--connect-timeout','8','--max-time','18',
                               url+'?'+urlencode(params)],capture_output=True,timeout=20)
        console_event('public_json_response',endpoint=url,returncode=result.returncode,response=result.stdout[:8000].decode('utf-8',errors='replace'),stderr=result.stderr[:1000].decode('utf-8',errors='replace'))
        if result.returncode not in (0,56):raise OSError('公开接口传输失败：'+result.stderr.decode('utf-8',errors='replace')[:200])
        # Some public servers omit TLS close_notify after a complete JSON response.
        # Accept only a fully parseable document; truncated bodies still fail here.
        return json.loads(result.stdout.decode('utf-8'))


def parse_candles(payload,symbol):
    if payload.get('code')!=0:raise ValueError('日线接口返回错误')
    records=payload.get('data',{}).get(symbol,{}).get('day',[])
    candles={}
    for row in records:
        day=date.fromisoformat(str(row[0])).isoformat()
        if day in candles:raise ValueError('日线日期重复')
        op,close,high,low=map(float,row[1:5])
        if not all(math.isfinite(v) and v>0 for v in (op,close,high,low)) or high<max(op,close,low) or low>min(op,close,high):
            raise ValueError('日线价格不合法')
        candles[day]={'open':op,'close':close,'high':high,'low':low}
    if not candles:raise ValueError('日线为空')
    return candles


def fetch_candles(settings,symbol,start,end):
    if not (len(symbol)==8 and symbol[:2] in ('sh','sz') and symbol[2:].isdigit()):raise ValueError('无效证券代码')
    key=hashlib.sha256(f'{symbol}:{start}:{end}'.encode()).hexdigest()[:20]
    path=folder(settings)/'daily'/f'{symbol}_{key}.json'
    cached=read_json(path,{})
    now=shanghai_now()
    stamp=str(cached.get('fetched_at',''))
    if cached.get('candles') and stamp[:10]==now.date().isoformat() and (end<now.date().isoformat() or stamp[11:16]>='15:10'):return cached
    url='https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
    params={'param':f'{symbol},day,{start},{end},640,'}
    payload=http(settings).get_json(url,params)
    candles=parse_candles(payload,symbol)
    result={'symbol':symbol,'source':url,'params':params,'fetched_at':iso_now(),'adjustment':'none','candles':candles}
    atomic_write_json(path,{**result,'raw':payload})
    return result


def equity_metrics(returns):
    equity=peak=1.;drawdown=0.;streak=max_streak=0
    for value in returns:
        equity*=1+value/100
        peak=max(peak,equity);drawdown=min(drawdown,(equity/peak-1)*100)
        streak=streak+1 if value<0 else 0;max_streak=max(max_streak,streak)
    return {'compound_return_pct':round((equity-1)*100,4),'max_drawdown_pct':round(drawdown,4),'max_losing_streak':max_streak,'current_losing_streak':streak,
            'win_rate':round(sum(v>0 for v in returns)/len(returns)*100,2) if returns else None}


def performance(records,series,buy_cost_pct=0.,sell_cost_pct=0.,now=None):
    if not all(math.isfinite(v) and 0<=v<=5 for v in (buy_cost_pct,sell_cost_pct)):raise ValueError('成本假设需在0至5%之间')
    now=now or shanghai_now()
    seen=set();trades=[];due=0
    for rec in sorted(records,key=lambda row:str(row.get('recommendation_date') or row.get('date') or row.get('created_at',''))):
        entry=str(rec.get('recommendation_date') or rec.get('date') or rec.get('created_at',''))[:10]
        target=str(rec.get('target_date',''))[:10]
        if entry in seen:continue
        seen.add(entry)
        row={'date':entry,'target_date':target,'code':rec.get('code'),'name':rec.get('name'),'status':'pending'}
        try:
            entry_day,target_day=date.fromisoformat(entry),date.fromisoformat(target)
            if not is_trading_weekday(entry_day) or next_weekday(entry_day)!=target_day:
                row.update(status='invalid_dates',reason='推荐日期或目标交易日不符合下一交易日规则');trades.append(row);continue
            if target_day>now.date() or (target_day==now.date() and (now.hour,now.minute)<(15,10)):
                row['reason']='等待目标日收盘确认（15:10后）';trades.append(row);continue
            due+=1
            symbol=('sh' if str(rec['code']).startswith('6') else 'sz')+str(rec['code'])
            candles=series.get(symbol,{}).get('candles',{})
            index=series.get('sh000300',{}).get('candles',{})
            if entry not in candles or target not in candles or entry not in index or target not in index:
                row.update(status='missing',reason='缺少股票或沪深300对应日期的真实收盘价');trades.append(row);continue
            confirmed=True
            for snapshot in (series[symbol],series['sh000300']):
                try:
                    stamp=datetime.fromisoformat(snapshot['fetched_at']).astimezone(SHANGHAI)
                    if stamp>now+timedelta(minutes=5) or stamp.date()<target_day or (stamp.date()==target_day and (stamp.hour,stamp.minute)<(15,10)):
                        confirmed=False
                except (KeyError,ValueError,TypeError):confirmed=False
            if not confirmed:
                row.update(status='missing_close',reason='日线采集于目标日收盘确认前或无采集时间，等待刷新');trades.append(row);continue
            e,x=candles[entry],candles[target]
            if e['high']==e['low'] or x['high']==x['low']:
                row.update(status='unverified_fill',reason='存在一字价日，未确认成交条件，未纳入收益');trades.append(row);continue
            gross=x['high']/e['close']-1-.01
            net=(1+gross)*(1-sell_cost_pct/100)/(1+buy_cost_pct/100)-1
            benchmark=index[target]['close']/index[entry]['close']-1
            row.update(status='ok',entry_close=e['close'],exit_high=x['high'],gross_return_pct=round(gross*100,6),net_return_pct=round(net*100,6),benchmark_return_pct=round(benchmark*100,6),source=series[symbol].get('source'),fetched_at=series[symbol].get('fetched_at'))
        except (ValueError,KeyError,TypeError) as exc:
            row.update(status='invalid',reason=str(exc))
        trades.append(row)
    matched=[row for row in trades if row['status']=='ok']
    strategy=index=1.;curve=[]
    for row in matched:
        strategy*=1+row['net_return_pct']/100;index*=1+row['benchmark_return_pct']/100
        curve.append({'date':row['target_date'],'strategy_pct':round((strategy-1)*100,4),'benchmark_pct':round((index-1)*100,4)})
    stats=equity_metrics([row['net_return_pct'] for row in matched]) if matched else None
    if stats:
        stats.update(benchmark_return_pct=round((index-1)*100,4),excess_percentage_points=round((strategy-index)*100,4),samples=len(matched))
    return {'status':'ok' if matched else 'unavailable','method':'自定义口径：单笔模拟收益＝（目标日最高价÷前一交易日收盘价−1）×100%−1个百分点；累计收益＝逐笔累乘（1＋单笔收益率）−1。成本假设默认0，可另行设置。',
            'buy_cost_pct':buy_cost_pct,'sell_cost_pct':sell_cost_pct,'benchmark':'沪深300，同一已匹配持有窗口，指数价格收益未扣费',
            'notes':['最高涨幅减1个百分点是自定义事后模拟，不代表能按该价格卖出；复利假设每笔投入全部模拟资金。','不复权价格收益，未计分红和公司行动；额外费用默认为0，并非实际券商费率。','未验证成交、涨跌停排队、滑点及整手约束；一字价日排除。','缺失不按零处理；策略与指数使用相同已匹配样本窗口复利，不代表完整账户净值。'],
            'due':due,'matched':len(matched),'coverage_pct':round(len(matched)/due*100,2) if due else None,'summary':stats,'curve':curve,'trades':trades,
            'start_date':matched[0]['date'] if matched else None}


def collect_performance(settings):
    records=recommendation_store(settings.runtime).history()
    now=shanghai_now();valid=[]
    for rec in records:
        try:
            entry=date.fromisoformat(str(rec.get('recommendation_date') or rec['created_at'])[:10])
            target=date.fromisoformat(rec['target_date'][:10])
            if is_trading_weekday(entry) and target<=now.date():valid.append(rec)
        except (ValueError,KeyError):continue
    series={};errors=[]
    if valid:
        start=min(str(row.get('recommendation_date') or row['created_at'])[:10] for row in valid)
        end=min(max(row['target_date'][:10] for row in valid),now.date().isoformat())
        symbols={'sh000300'}|{('sh' if row['code'].startswith('6') else 'sz')+row['code'] for row in valid}
        with ThreadPoolExecutor(max_workers=3,thread_name_prefix='dashboard-daily') as pool:
            futures={pool.submit(fetch_candles,settings,symbol,start,end):symbol for symbol in symbols}
            for future in as_completed(futures):
                try:series[futures[future]]=future.result()
                except Exception as exc:errors.append({'symbol':futures[future],'error':str(exc)[:250]})
    result={'generated_at':iso_now(),'records':records,'series':series,'errors':errors,'default':performance(records,series,now=now)}
    atomic_write_json(folder(settings)/'performance.json',result)
    return {'matched':result['default']['matched'],'due':result['default']['due'],'errors':errors}


def _collect_eastmoney_sectors(settings):
    bases=settings.section('market').get('base_urls',['https://push2.eastmoney.com/api/qt/clist/get'])[:3]
    all_rows={};total=0;source=None;fetched=iso_now()
    for page in range(1,11):
        params={'pn':page,'pz':100,'po':1,'np':1,'fltt':2,'invt':2,'fid':'f3','fs':'m:90+t:2','fields':'f12,f14,f2,f3,f6,f104,f105,f124'}
        error=None
        for base in bases:
            try:
                payload=public_json(settings,base,params);data=payload.get('data')
                if not isinstance(data,dict) or not isinstance(data.get('diff'),(list,dict)):raise ValueError('板块接口无有效数据')
                source=base
                atomic_write_json(folder(settings)/'raw'/f"sectors_{fetched[:19].replace(':','').replace('-','')}_{page}.json",{'source':base,'params':params,'fetched_at':iso_now(),'payload':payload})
                break
            except Exception as exc:error=exc;data=None
        if data is None:raise error
        total=int(data.get('total',0));diff=data['diff'];diff=list(diff.values()) if isinstance(diff,dict) else diff
        for row in diff:
            code=str(row.get('f12',''));change=number(row.get('f3'));stamp=number(row.get('f124'))
            if not code.startswith('BK') or change is None or not stamp:continue
            quote=datetime.fromtimestamp(stamp,SHANGHAI)
            if quote>shanghai_now()+timedelta(minutes=5):continue
            all_rows[code]={'code':code,'name':row.get('f14'),'change_pct':change,'amount_yi':round((number(row.get('f6')) or 0)/1e8,2),
                            'up':number(row.get('f104')),'down':number(row.get('f105')),'quote_time':quote.isoformat()}
        if len(all_rows)>=total or not diff:break
    if not all_rows:raise ValueError('板块列表为空')
    result={'status':'ok','source':source,'fetched_at':fetched,'total_reported':total,'covered':len(all_rows),'complete':len(all_rows)>=total,
            'scope':'东方财富行业板块，包含多级行业，分类存在重叠，不可相加作为全市场统计',
            'rows':sorted(all_rows.values(),key=lambda row:row['change_pct'],reverse=True)}
    atomic_write_json(folder(settings)/'sectors.json',result)
    return {'records':len(all_rows),'complete':result['complete']}


def public_text(settings,url):
    try:return http(settings).get_text(url)
    except Exception as exc:
        console_event('public_text_fallback',level='WARN',endpoint=url,error_type=type(exc).__name__,message=str(exc),transport='curl')
        response=subprocess.run(['curl.exe','--silent','--show-error','--fail-with-body','--max-time','18',url],capture_output=True,timeout=20)
        console_event('public_text_response',endpoint=url,returncode=response.returncode,response=response.stdout[:8000].decode('utf-8',errors='replace'),stderr=response.stderr[:1000].decode('utf-8',errors='replace'))
        if response.returncode not in (0,56) or not response.stdout:raise OSError('备用公开来源无有效响应')
        for encoding in ('utf-8','gb18030'):
            try:return response.stdout.decode(encoding)
            except UnicodeDecodeError:pass
        raise ValueError('公开来源编码无法识别')


def parse_sina_sectors(text):
    match=re.search(r'var\s+S_Finance_bankuai_sinaindustry\s*=\s*(\{.*\})\s*;?\s*$',text,re.S)
    if not match:raise ValueError('新浪行业快照格式变化')
    payload=json.loads(match.group(1));rows=[]
    for code,value in payload.items():
        cells=str(value).split(',')
        if len(cells)<8 or cells[0]!=code or number(cells[5]) is None:continue
        rows.append({'code':code,'name':cells[1],'change_pct':float(cells[5]),'company_count':number(cells[2]),
                     'amount_yi':None,'raw_amount':cells[7],'up':None,'down':None,'quote_time':None})
    if not rows:raise ValueError('新浪行业快照为空')
    return rows


def collect_sectors(settings):
    try:return _collect_eastmoney_sectors(settings)
    except Exception as exc:reason=str(exc)[:250]
    url='https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php'
    text=public_text(settings,url);rows=parse_sina_sectors(text);stamp=iso_now()
    result={'status':'ok','provider':'sina','source':url,'fetched_at':stamp,'total_reported':len(rows),'covered':len(rows),'complete':True,
            'scope':'新浪行业分类；上游未给出行情时间，不能确认盘中实时性。金额未统一单位时不展示。','source_switch_reason':reason,
            'rows':sorted(rows,key=lambda row:row['change_pct'],reverse=True)}
    atomic_write_json(folder(settings)/'raw'/f"sina_sectors_{stamp[:19].replace(':','').replace('-','')}.json",{'source':url,'fetched_at':stamp,'text':text})
    atomic_write_json(folder(settings)/'sectors.json',result)
    return {'records':len(rows),'complete':True,'provider':'sina','quote_time_available':False}


def collect_industry(settings):
    rec=recommendation_store(settings.runtime).realtime()
    if not rec:return {'status':'no_recommendation'}
    code=rec['code'];params={'secid':('1.' if code.startswith('6') else '0.')+code,'fields':'f57,f58,f127'}
    error=None
    for base in settings.section('market').get('base_urls',[])[:4]:
        url=base.replace('/clist/get','/stock/get')
        try:
            raw=public_json(settings,url,params);data=raw.get('data') or {}
            if data.get('f57')!=code or not data.get('f127'):raise ValueError('行业归属字段缺失')
            result={'status':'ok','code':code,'name':rec['name'],'industry':data['f127'],'source':url,'fetched_at':iso_now(),'scope':'最新行业归属，不作历史分类回填'}
            atomic_write_json(folder(settings)/'industry'/f'{code}.json',{**result,'raw':raw})
            return result
        except Exception as exc:error=exc
    url=f'https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_CorpOtherInfo/stockid/{code}/menu_num/2.phtml'
    text=public_text(settings,url)
    before=text.split('备注：此为申万行业分类')[0]
    if '此为申万行业分类' not in text:raise error or ValueError('行业归属页未确认分类口径')
    table=before.rsplit('<table',1)[-1];industry=None
    for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>',table,re.S|re.I):
        if '点击查看' not in row:continue
        cells=re.findall(r'<td\b[^>]*>(.*?)</td>',row,re.S|re.I)
        if cells:
            industry=unescape(re.sub(r'<[^>]*>','',cells[0])).strip();break
    if not industry or len(industry)>100:raise ValueError('未找到可核验行业归属')
    result={'status':'ok','code':code,'name':rec['name'],'industry':industry,'source':url,'fetched_at':iso_now(),'scope':'新浪公司资料，申万行业分类，最新归属，不作历史回填'}
    atomic_write_json(folder(settings)/'industry'/f'{code}.json',result)
    return result


def run_sync(settings):
    root=str(settings.runtime.resolve())
    from .scheduler import process_lock
    with process_lock(folder(settings)/'sync.lock') as acquired:
        if not acquired:return
        state={'status':'running','started_at':iso_now(),'parts':{}}
        atomic_write_json(folder(settings)/'sync_state.json',state)
        for name,fn in [('performance',collect_performance),('sectors',collect_sectors),('industry',collect_industry)]:
            try:
                result=fn(settings);state['parts'][name]={'ok':True,**result}
                console_event('dashboard_data_collected',part=name,result=result)
            except Exception as exc:
                state['parts'][name]={'ok':False,'error':str(exc)[:300]}
                console_event('dashboard_data_failed',part=name,error_type=type(exc).__name__,message=str(exc)[:300])
            atomic_write_json(folder(settings)/'sync_state.json',state)
        state.update(status='completed' if all(v['ok'] for v in state['parts'].values()) else 'partial',finished_at=iso_now())
        atomic_write_json(folder(settings)/'sync_state.json',state)


def start_sync(settings):
    key=str(settings.runtime.resolve())
    with _job_lock:
        if key in _running:return {'status':'running'}
        _running.add(key)
    def worker():
        try:run_sync(settings)
        except Exception as exc:
            atomic_write_json(folder(settings)/'sync_state.json',{'status':'failed','error':str(exc)[:300],'finished_at':iso_now()})
        finally:
            with _job_lock:_running.discard(key)
    threading.Thread(target=worker,name='research-dashboard-sync',daemon=True).start()
    return {'status':'started'}


def refresh_after_close_if_due(settings):
    now=shanghai_now()
    if not is_trading_weekday(now.date()) or (now.hour,now.minute)<(15,10):return
    state=read_json(folder(settings)/'sync_state.json',{})
    try:
        stamp=datetime.fromisoformat(state['started_at']).astimezone(SHANGHAI)
        if now-stamp<timedelta(minutes=5):return
    except (KeyError,ValueError,TypeError):pass
    saved=read_json(folder(settings)/'performance.json',{})
    result=performance(saved.get('records',[]),saved.get('series',{}),now=now)
    if not saved or any(row['status'] in ('missing','missing_close') for row in result['trades']):
        start_sync(settings)


def enrichment_view(settings,data,buy=0.,sell=0.):
    from .research_dashboard import research_view
    view=research_view(settings,data)
    saved=read_json(folder(settings)/'performance.json',{})
    view['performance']=performance(data.get('history',saved.get('records',[])),saved.get('series',{}),buy,sell)
    view['performance'].update(generated_at=saved.get('generated_at'),errors=saved.get('errors',[]))
    view['sectors']=read_json(folder(settings)/'sectors.json',{'status':'unavailable','rows':[]})
    code=(data.get('recommendation') or {}).get('code')
    view['industry']=read_json(folder(settings)/'industry'/f'{code}.json',{'status':'unavailable'}) if code else {'status':'unavailable'}
    view['sync']=read_json(folder(settings)/'sync_state.json',{'status':'not_started'})
    return view
