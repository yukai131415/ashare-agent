(() => {
  const panel=document.getElementById('view-intraday');if(!panel)return;
  const admin=window.APP_AUTH?.role==='admin';
  const el=(tag,text='',cls='')=>{const n=document.createElement(tag);n.textContent=String(text??'');if(cls)n.className=cls;return n;};
  const card=el('article','','card workspace-full intraday-panel'),head=el('div','','section-title');
  head.append(el('h2','当日分析 · 盘中观察'),el('span','10分钟 / 次','tag'));card.append(head);
  const note=el('p','仅跟踪前一交易日选出、目标日为所选日期的股票。9:30开始，午休暂停；同步页面不调用大模型。','workspace-note');card.append(note);
  const controls=el('div','','workspace-controls'),date=document.createElement('input');date.type='date';date.setAttribute('aria-label','当日分析日期');
  const refresh=el('button','同步已保存结果'),toggle=el('button','暂停自动分析'),state=el('span','','workspace-note');
  controls.append(date,refresh);if(admin)controls.append(toggle);controls.append(state);card.append(controls);
  const clock=el('div','','workspace-callout'),stocks=el('div','','intraday-stocks'),disclaimer=el('p','','workspace-callout'),timeline=el('div','','intraday-timeline'),more=el('button','显示更早记录');
  card.append(clock,stocks,disclaimer,timeline,more);panel.append(card);
  let data=null,timer=null,clockTimer=null,loading=false,limit=20;
  const time=value=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';
  const number=(v,n=2)=>typeof v==='number'&&Number.isFinite(v)?v.toFixed(n):'—';
  const tone=v=>typeof v==='number'?(v>0?'intraday-up':v<0?'intraday-down':''):'';
  function updateClock(){if(!data)return;const next=data.next_slot?new Date(data.next_slot):null;
    clock.textContent=!data.config.enabled?'自动分析已暂停；历史记录继续保留。':next?`下个调度时刻：${time(data.next_slot)}（北京时间） · 模型 ${data.config.model||'未配置'}`:'交易日历暂不可用，请检查配置。';
  }
  function sparkline(rows){
    const quotes=[...rows].reverse().filter(r=>r.quote&&typeof r.quote.price==='number');
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 320 65');svg.setAttribute('role','img');svg.setAttribute('aria-label','已采集行情快照价格走势，非完整分时线');svg.classList.add('intraday-spark');
    if(quotes.length<2){const empty=el('p','至少两个有效快照后显示价格变化','workspace-note');return empty;}
    const values=quotes.map(r=>r.quote.price),low=Math.min(...values),high=Math.max(...values),span=high-low||1;
    const line=document.createElementNS(svg.namespaceURI,'polyline');line.setAttribute('points',values.map((v,i)=>`${8+i/(values.length-1)*304},${55-(v-low)/span*45}`).join(' '));line.setAttribute('fill','none');line.setAttribute('stroke',values.at(-1)>=values[0]?'#ff6989':'#3de0aa');line.setAttribute('stroke-width','2');svg.append(line);return svg;
  }
  function render(){
    stocks.replaceChildren();timeline.replaceChildren();disclaimer.textContent=data.disclaimer;
    state.textContent=`${data.date} · ${data.watchlist.length}只跟踪 · ${data.records.length}条记录 · ${data.config.enabled?'自动分析开启':'已暂停'}`;
    toggle.textContent=data.config.enabled?'暂停自动分析':'启用自动分析';
    if(!data.watchlist.length)stocks.append(el('p','该日期没有符合条件的前一交易日推荐，不会调用行情或模型。','workspace-empty'));
    for(const stock of data.watchlist){const tile=el('article','','intraday-stock'),rows=data.records.filter(r=>r.code===stock.code),latest=rows.find(r=>r.quote),q=latest?.quote;
      tile.append(el('small',stock.sources.join(' + ')+' · '+stock.code),el('h3',stock.name||stock.code));
      const price=el('strong',q?`¥${number(q.price)}  ${number(q.change_pct)}%`:'等待盘中行情',tone(q?.change_pct));tile.append(price);
      tile.append(el('p',`推荐参考价 ¥${number(stock.reference_price)}（非持仓成本） · 目标日 ${stock.target_date}`,'workspace-note'),sparkline(rows));
      if(q)tile.append(el('small',`行情时间 ${q.quote_time} · 仅已采集快照，不是逐笔分时线`));stocks.append(tile);
    }
    const statuses={running:'采集 / 分析中',completed:'分析完成',analysis_failed:'模型分析失败',quote_failed:'行情不可用 · 未调用模型',interrupted:'上次任务中断'};
    if(!data.records.length)timeline.append(el('p','暂无盘中分析。服务需保持运行；错过时段不补造历史实时行情。','workspace-empty'));
    for(const record of data.records.slice(0,limit)){
      const item=el('article','','intraday-record');item.dataset.status=record.status;
      const title=el('div','','section-title');title.append(el('h3',`${record.target.name||record.code} · ${time(record.slot)}`),el('span',statuses[record.status]||record.status,'tag'));item.append(title);
      if(record.quote){const q=record.quote;item.append(el('p',`现价 ¥${number(q.price)} · 涨跌 ${number(q.change_pct)}% · 累计成交额 ${typeof q.amount==='number'?number(q.amount/1e8)+'亿元':'—'} · 换手 ${number(q.turnover_pct)}%`,tone(q.change_pct)));
        item.append(el('small',`实际采集 ${time(record.captured_at)} · 行情 ${q.quote_time} · ${record.model||'等待模型'}`));
        if(record.derived?.price_change_since_previous_pct!=null)item.append(el('p',`相对上次快照 ${number(record.derived.price_change_since_previous_pct)}% · 区间成交额 ${typeof record.derived.interval_turnover_amount==='number'?number(record.derived.interval_turnover_amount/1e4)+'万元':'无可比数据'}（不是资金净流入）`,'workspace-note'));
      }
      if(record.message)item.append(el('p',record.message,'workspace-callout'));
      const a=record.analysis;
      if(a){item.append(el('p',a.summary),el('small',`模型风险判断：${({low:'低',medium:'中',high:'高',unknown:'无法判断'})[a.risk_level]}（非保证）`));
        for(const signal of a.signals){const block=el('div','','intraday-signal');block.append(el('p','观察：'+signal.observation),el('p','可能解释（推测）：'+signal.interpretation),el('p','其他可能：'+signal.alternative),el('small','证据：'+signal.evidence_ids.join('、')));item.append(block);}
        for(const point of a.watchpoints)item.append(el('p','继续观察：'+point,'workspace-note'));
        for(const point of a.limitations)item.append(el('p','局限：'+point,'workspace-note'));
      }
      if(admin&&record.usage)item.append(el('small',`Token：发送 ${record.usage.input_tokens??'未报告'} / 响应 ${record.usage.output_tokens??'未报告'}`));timeline.append(item);
    }
    more.hidden=data.records.length<=limit;updateClock();
    if(admin&&data.system_usage){let usage=document.getElementById('intraday-system-usage');if(!usage){usage=el('p','','workspace-callout');usage.id='intraday-system-usage';document.querySelector('#view-usage article')?.prepend(usage);}if(usage)usage.textContent=`当日分析系统任务（${data.date}，独立于个人筛选）：发送 ${data.system_usage.input_tokens} / 响应 ${data.system_usage.output_tokens} Token。`;}
  }
  async function load(){if(loading)return;loading=true;try{const response=await fetch('/api/intraday'+(date.value?'?date='+encodeURIComponent(date.value):''),{cache:'no-store'});const value=await response.json();if(!response.ok)throw Error(value.error||'读取失败');data=value;if(!date.value)date.value=data.date;render();}catch(error){state.textContent=error.message;}finally{loading=false;}}
  toggle.onclick=async()=>{if(!data)return;if(!data.config.enabled&&!window.confirm('启用后在交易时段自动查询行情并调用系统模型，会消耗Token。是否启用？'))return;toggle.disabled=true;try{const response=await fetch('/api/intraday/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!data.config.enabled})});const result=await response.json();if(!response.ok)throw Error(result.error);await load();}catch(error){state.textContent=error.message;}finally{toggle.disabled=false;}};
  refresh.onclick=load;date.onchange=()=>{limit=20;load();};more.onclick=()=>{limit+=20;render();};
  document.addEventListener('workspace-view',event=>{clearInterval(timer);clearInterval(clockTimer);if(event.detail==='intraday'){load();timer=setInterval(load,15000);clockTimer=setInterval(updateClock,1000);}else if(event.detail==='usage'&&admin)load();});
  if(!panel.hidden){load();timer=setInterval(load,15000);clockTimer=setInterval(updateClock,1000);}
})();
