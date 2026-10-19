"""Archive public Eastmoney research excerpts and index real source text."""
from __future__ import annotations
import re
from datetime import datetime,timedelta,timezone
from html.parser import HTMLParser
from .contracts import iso_now
from .http import HttpClient
from .storage import atomic_write_json


class ReportBodyParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth=0;self.parts=[]

    def handle_starttag(self,tag,attrs):
        if not self.depth:
            if tag=='div' and dict(attrs).get('id')=='ctx-content':self.depth=1
        elif tag not in ('br','hr','img','input','meta','link','wbr'):
            self.depth+=1
            if tag in ('p','div','li'):self.parts.append('\n')

    def handle_endtag(self,tag):
        if self.depth and tag not in ('br','hr','img','input','meta','link','wbr'):self.depth-=1

    def handle_data(self,data):
        if self.depth:self.parts.append(data)

    def text(self):
        return '\n'.join(line.strip() for line in ''.join(self.parts).splitlines() if line.strip())


class ResearchCollector:
    def __init__(self,settings,index):
        self.settings,self.index=settings,index
        self.config=settings.values.get('research',{})
        self.http=HttpClient(timeout=float(self.config.get('fetch_timeout_seconds',15)),
            user_agent=settings.section('http')['user_agent'],interval=0.4)

    def sync(self,code,as_of,limit=3):
        if not re.fullmatch(r'\d{6}',code):raise ValueError('Invalid research code')
        cutoff=datetime.fromisoformat(as_of.replace('Z','+00:00'))
        if cutoff.tzinfo is None:raise ValueError('研报截止时间必须含时区')
        cutoff=cutoff.astimezone(timezone(timedelta(hours=8)));end=cutoff.date()
        params={'industryCode':'*','pageSize':min(max(limit*3,10),30),'industry':'*','rating':'*','ratingChange':'*',
            'beginTime':(end-timedelta(days=int(self.config.get('lookback_days',730)))).isoformat(),
            'endTime':end.isoformat(),'pageNo':1,'fields':'','qType':0,'orgCode':'','code':code,'rcode':'','p':1,'pageNum':1,'pageNumber':1}
        url='https://reportapi.eastmoney.com/report/list';payload=self.http.get_json(url,params)
        if not isinstance(payload.get('data'),list):raise ValueError('研报目录结构异常')
        folder=self.settings.runtime/'agents/research/raw'/code;folder.mkdir(parents=True,exist_ok=True)
        atomic_write_json(folder/f'list_{end}.json',{'url':url,'params':params,'fetched_at':iso_now(),'payload':payload})
        documents=[];errors=[]
        for report in payload['data']:
            if len(documents)>=limit:break
            if str(report.get('stockCode'))!=code:continue
            identifier=str(report.get('infoCode',''))
            if not re.fullmatch(r'AP\d+',identifier):continue
            try:
                raw_date=str(report['publishDate'])
                published=datetime.fromisoformat(raw_date).replace(tzinfo=cutoff.tzinfo)
                # Midnight-only metadata is conservatively available at publication day end.
                if published.hour==published.minute==published.second==0:published=published.replace(hour=23,minute=59,second=59)
                if published>cutoff:continue
                detail_url='https://data.eastmoney.com/report/zw_stock.jshtml?infocode='+identifier
                path=folder/(identifier+'.html')
                if path.exists():html=path.read_text(encoding='utf-8')
                else:
                    html=self.http.get_text(detail_url);path.write_text(html,encoding='utf-8')
                parser=ReportBodyParser();parser.feed(html);text=parser.text()
                if len(text)<100:raise ValueError('研报正文不足，未导入')
                name=str(report.get('stockName',''))
                if code not in text and (not name or name not in text):raise ValueError('研报正文与股票不匹配')
                documents.append({'code':code,'title':report['title'],'source':detail_url,
                    'published_at':published.isoformat(),'text':text,'provider_publish_date':raw_date,
                    'organization':report.get('orgSName'),'pdf_url':f'https://pdf.dfcfw.com/pdf/H3_{identifier}_1.pdf'})
            except Exception as exc:errors.append({'report':identifier,'error_type':type(exc).__name__})
        indexed=self.index.ingest(documents) if documents else {'chunks':0}
        result={'status':'ok' if documents else 'no_reports' if not errors else 'unavailable',
            'code':code,'as_of':as_of,'documents':len(documents),'errors':errors,**indexed,
            'sources':[{'title':d['title'],'source':d['source'],'published_at':d['published_at']} for d in documents]}
        atomic_write_json(self.settings.runtime/'agents/research/data'/f'{code}_{end}.json',{'fetched_at':iso_now(),'documents':documents,'result':result})
        return result
