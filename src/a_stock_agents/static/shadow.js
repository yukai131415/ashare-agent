(() => {
  const panel=document.getElementById('view-shadow');if(!panel)return;
  const admin=window.APP_AUTH?.role==='admin',names={fundamental:'基本面调查',technical:'技术研究',risk:'风险复核',decision:'影子决策'};
  function node(tag,value='',cls){const e=document.createElement(tag);e.textContent=String(value??'');if(cls)e.className=cls;return e;}
  const article=node('article','','card workspace-full shadow-panel'),heading=node('div','','section-title');heading.append(node('h2','多 Agent 影子选股实验'),node('span','不影响正式推荐','tag'));article.append(heading);
  const controls=node('div','','workspace-controls'),refresh=node('button','刷新状态');refresh.onclick=load;controls.append(refresh);
  let modelSelect=null;
  if(admin){const label=node('label','影子模型 ');modelSelect=node('select');modelSelect.setAttribute('aria-label','影子实验模型');label.append(modelSelect);controls.prepend(label);
    modelSelect.onchange=()=>{modelSelect.dataset.userSelected='true';};
    refresh.title='使用所选模型重新研究，会消耗Token；自动同步仅查询状态';
    refresh.onclick=()=>{if(!modelSelect.value){message.textContent='请先选择已配置的模型';return;}post('/api/shadow/refresh',{profile_id:modelSelect.value});};}
  let start,toggle,recheck,enabled=true,financialRecheck=null;
  const message=node('p','','workspace-note');message.setAttribute('role','status');
  if(admin){start=node('button','启动最近一期影子实验');start.onclick=()=>refresh.click();toggle=node('button','关闭自动实验');toggle.onclick=()=>post('/api/shadow/config',{enabled:!enabled});recheck=node('button','复核财务连接');recheck.onclick=()=>post('/api/shadow/financial-check',{});controls.append(start,toggle,recheck);}
  const content=node('div','','shadow-content');article.append(controls,message,content);panel.append(article);
  const flow=node('section','','shadow-flow');flow.setAttribute('aria-label','影子研究执行链路');
  const poolCard=node('div','','shadow-signal-pool'),poolTitle=node('div','','shadow-signal-caption'),poolRows=node('div');poolCard.append(poolTitle,poolRows);
  const core=node('div','','shadow-signal-core'),orbit=node('div','','shadow-orbit'),coreCaption=node('span','研究进度'),coreValue=node('strong','0 / 5'),coreLabel=node('h3','等待启动'),coreNote=node('p','按真实阶段状态同步');orbit.append(coreCaption,coreValue);core.append(orbit,coreLabel,coreNote);
  const right=node('div','','shadow-signal-result'),steps=node('div'),finalBox=node('div','','shadow-final'),finalCaption=node('small','影子实验结果'),finalName=node('strong','等待研究'),finalMeta=node('span','尚未生成决策');finalBox.append(finalCaption,finalName,finalMeta);right.append(steps,finalBox);
  const stageNodes=new Map(),wires=[];
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 1000 320');svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('aria-hidden','true');svg.classList.add('shadow-signal-wires');
  for(let i=0;i<5;i++){for(const d of [`M 270 ${52+i*53} C 315 ${52+i*53} 320 160 360 160`,`M 640 160 C 685 160 685 ${30+i*36} 730 ${30+i*36}`]){const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',d);path.style.animationDelay=`${i*-.4}s`;svg.append(path);wires.push(path);}}
  for(const [key,title] of [['data','数据准备'],...Object.entries(names)]){const item=node('div','','shadow-step');const name=node('strong',title),status=node('span','等待');item.append(name,status);item.dataset.state='pending';stageNodes.set(key,{item,status});steps.append(item);}
  flow.append(svg,poolCard,core,right);
  article.insertBefore(flow,content);
  function updateFlow(){const report=reports[0];let completed=0,index=0;
    flow.dataset.state=report?.status||'idle';
    for(const [key,{item,status}] of stageNodes){const stage=report?.stages?.[key];let state=stage?.status||'pending';if(!stage&&report?.reviews?.[key])state='completed';if(!stage&&report?.active_role===key&&report?.status==='failed')state='failed';if(!stage&&report?.active_role===key&&report?.status==='running')state='running';if(state==='pending'&&report?.status==='failed')state='blocked';if(state==='completed')completed++;if(item.dataset.state!==state)item.dataset.state=state;status.textContent=({pending:'等待',running:'执行中',completed:'已完成',failed:'失败',blocked:'未执行'})[state]||state;if(key==='data'&&stage?.queried!=null)status.textContent+=` ${stage.queried}/${stage.total}`;wires[index*2+1].dataset.state=state;index++;}
    if(report?.market_scope==='on_demand'&&report?.stages?.data?.status==='completed')stageNodes.get('data').status.textContent=`快照就绪 ${report.universe_count}只`;
    const pool=report?.candidate_pool||[],selectedCode=report?.selection?.code;
    poolTitle.textContent=`冻结候选池 · ${pool.length}只`;poolRows.replaceChildren();
    const suggested=new Set(report?.reviews?.[report?.active_role]?.candidates||[]);
    const visible=[...pool].sort((a,b)=>(b.code===selectedCode)-(a.code===selectedCode)||Number(suggested.has(b.code))-Number(suggested.has(a.code))).slice(0,5);
    poolTitle.textContent=`候选摘录 ${visible.length}/${pool.length} · 非评分排名`;
    if(report?.market_scope==='on_demand')poolTitle.textContent=`已查阅 ${report.observed_count} / 可检索 ${report.universe_count} · 摘录${visible.length}只`;
    visible.forEach((stock,i)=>{const row=node('div','','shadow-signal-stock');row.dataset.selected=String(stock.code===selectedCode);row.append(node('small',String(i+1).padStart(2,'0')),node('strong',stock.name||stock.code),node('span',`${stock.code} · ¥${Number(stock.price||0).toFixed(2)}`));poolRows.append(row);wires[i*2].dataset.state=stock.code===selectedCode?'completed':'pending';});
    if(!visible.length)poolRows.append(node('p','等待冻结候选快照'));
    coreCaption.textContent=report?.status==='completed'?'最终影子选择':'研究阶段进度';coreValue.textContent=report?.status==='completed'?selectedCode:`${completed} / 5`;
    coreLabel.textContent=({completed:'决策已生成',abstained:'本轮弃选',failed:'链路中止',running:names[report?.active_role]||'数据准备'})[report?.status]||'等待启动';
    coreNote.textContent=report?.status==='failed'?(report.message||'执行失败，请查看详细研究记录').split('。')[0]:(report?.model||'模型与证据驱动 · 非模拟进度');
    coreNote.title=report?.message||report?.model||'';
    finalName.textContent=report?.selection?.name||(report?.status==='abstained'?'本轮不选股':report?.status==='failed'?'未生成决策':'等待决策汇总');
    finalMeta.textContent=report?.selection?`${selectedCode} · 参考价 ¥${Number(report.selection.price||0).toFixed(2)} · 目标日 ${report.target_date}`:'仅完成决策后展示最终股票';
  }
  if(admin){const repair=node('button','补齐证据后重新研究');repair.onclick=()=>{if(window.confirm('将调用大模型并消耗 Token，旧结论归档，新结果为事后研究且不计前瞻收益。仅允许修复一次，是否继续？'))post('/api/shadow/repair',{});};controls.append(repair);}
  let timer=null,page=1,reports=[],selected=null,loading=false,running=false;
  const pct=v=>typeof v==='number'?(v>0?'+':'')+v.toFixed(2)+'%':'—';
  async function post(url,data){try{refresh.disabled=true;if(modelSelect)modelSelect.disabled=true;if(start)start.disabled=true;if(recheck)recheck.disabled=true;const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const d=await r.json();if(!r.ok)throw Error(d.error);await load();message.textContent=(d.message||'配置已保存')+' '+message.textContent;}catch(e){message.textContent=e.message;}finally{refresh.disabled=admin&&running;if(modelSelect)modelSelect.disabled=running;if(start)start.disabled=running;if(recheck)recheck.disabled=false;}}
  function details(report){
    const area=node('section','','shadow-reviews');area.append(node('h3',report.date+' 角色研究与证据'));
    if(report.late_note)area.append(node('p',report.late_note,'workspace-callout'));
    if(report.model)area.append(node('p','本次研究模型：'+report.model,'workspace-note'));
    if(report.message)area.append(node('p',`历史运行结果（${report.started_at||report.date} · ${report.model||'旧记录未保存模型'}），不代表当前所选模型的连接状态：`+report.message,'workspace-callout'));
    const exploration=report.exploration_coverage;
    if(exploration){
      area.append(node('p',`多维覆盖：${exploration.dimensions.length}/4 个维度 · 可检索 ${exploration.universe} 只 · 去重候选 ${exploration.candidate_codes.length} 只 · 财务核实 ${exploration.financial_ok.length} 只 · 财务待核实 ${exploration.financial_missing.length} 只`,'workspace-callout'));
      area.append(node('p',exploration.dimensions.map(d=>`${d.label}：有效${d.valid}只，取${d.codes.length}只${d.order==='asc'?'（从低到高）':'（从高到低）'}`).join('；'),'workspace-note'));
      area.append(node('p','数据完整性风险检查 '+exploration.risk_checks.length+' 只；不代表这些股票无风险，最终由风控Agent复核。','workspace-note'));
    }
    const coverage=report.financial_coverage;
    if(coverage)area.append(node('p',`原运行实际查询财务 ${coverage.queried}/${coverage.pool} 只，曾失败 ${coverage.failed} 只，未查询 ${coverage.not_queried} 只。单股查询失败不代表全部候选无财报；下方为当时模型意见，不是接口覆盖率事实。`,'workspace-callout'));
    if(financialRecheck?.experiment_day===report.date){
      area.append(node('p',`修复后复核（${financialRecheck.checked_at}）：`+financialRecheck.checks.map(c=>`${c.code} ${c.status==='ok'?'可获取 '+c.report_count+' 份已披露财务记录':'暂不可用或无披露记录'}`).join('；'),'workspace-callout'));
      area.append(node('p',financialRecheck.note,'workspace-note'));
    }
    for(const [key,label] of Object.entries(names)){
      const review=report.reviews?.[key],card=node('article','','shadow-review');card.append(node('h3',label));
      if(!review){card.append(node('p',report.status==='running'?'等待执行':'未生成有效结论'));area.append(card);continue;}
      card.append(node('p',review.summary),node('p','候选：'+(review.candidates.join('、')||'无')+'；否决：'+(review.vetoes.join('、')||'无'),'workspace-note'));
      for(const risk of review.risks)card.append(node('p','风险：'+risk,'workspace-note'));
      for(const id of review.evidence_ids){const ev=report.evidence?.[id];card.append(node('p','证据 '+id+(ev?' · '+JSON.stringify(ev):' · 证据元信息待归档'),'workspace-note'));}
      area.append(card);
    }
    const tools=report.tool_calls||[];area.append(node('p','工具记录：'+(tools.map(x=>`${x.name} ${x.code||''} ${x.status}`).join('；')||'未追加查询，依据冻结快照研究'),'workspace-note'));
    if(admin&&report.usage)area.append(node('p',`系统任务用量：发送 ${report.usage.input_tokens||0} / 响应 ${report.usage.output_tokens||0} Token；模型请求 ${report.usage.model_calls||0}；用量未报告响应 ${report.usage.unreported_responses||0}`,'workspace-note'));
    return area;
  }
  function table(){
    const container=node('div','','table-wrap'),table=node('table'),head=node('thead'),row=node('tr');
    ['日期','正式推荐','影子选择','状态','实验类型','正式模拟收益','影子模拟收益','操作'].forEach(x=>row.append(node('th',x)));head.append(row);table.append(head);const body=node('tbody');
    const statuses={running:'运行中',completed:'完成',abstained:'弃选',failed:'失败'};
    for(const r of reports.slice((page-1)*10,page*10)){
      const tr=node('tr'),outcome=r.outcome||{};
      for(const value of [r.date,(r.baseline?.name||'—')+' '+(r.baseline?.code||''),r.status==='abstained'?'不选':(r.selection?.name||'—')+' '+(r.selection?.code||''),statuses[r.status]||r.status,r.prospective?'前瞻':'事后，不计前瞻',pct(outcome.baseline_return_pct),pct(outcome.shadow_return_pct)])tr.append(node('td',value));
      const cell=node('td'),button=node('button','查看研究');button.onclick=()=>{selected=r.date;render();};cell.append(button);tr.append(cell);body.append(tr);
    }
    table.append(body);container.append(table);return container;
  }
  function render(){
    content.replaceChildren();content.append(node('p','管理员可从系统已配置模型中独立选择影子模型，点击“刷新状态”重新获取推荐并消耗Token；自动同步不重算。普通用户仅查看状态。正式推荐不受影响。','workspace-note'));
    if(!reports.length){content.append(node('p','暂无影子实验。管理员可启动最近一期；交易日正式流水线成功后自动启动。','workspace-empty'));return;}
    content.append(table());const pages=Math.max(1,Math.ceil(reports.length/10)),pager=node('div','','workspace-pager');pager.append(node('span',`第 ${page}/${pages} 页 · ${reports.length} 期`));
    for(const [label,target] of [['首页',1],['上一页',page-1],['下一页',page+1],['最后一页',pages]]){const b=node('button',label);b.disabled=target<1||target>pages||target===page;b.onclick=()=>{page=target;render();};pager.append(b);}content.append(pager);
    const report=reports.find(r=>r.date===selected)||reports[0];content.append(details(report));
    content.append(node('p','模拟口径：目标日最高涨幅减1个百分点；默认额外成本0，收盘后确认。缺日线保持待回测，非实际成交收益。','workspace-note'));
  }
  async function load(){
    if(loading)return;loading=true;
    try{const r=await fetch('/api/shadow'),d=await r.json();if(!r.ok)throw Error(d.error||'读取失败');reports=d.reports;enabled=d.config.enabled;running=d.running;financialRecheck=d.financial_recheck;
      if(modelSelect){const previous=modelSelect.value;modelSelect.replaceChildren();for(const p of d.model_options||[]){const option=node('option',p.model+(p.id===d.active_model_profile_id?'（当前启用）':'')+(p.configured?'':'（未配置密钥）'));option.value=p.id;option.disabled=!p.configured;modelSelect.append(option);}modelSelect.value=modelSelect.dataset.userSelected==='true'&&(d.model_options||[]).some(p=>p.id===previous)?previous:d.active_model_profile_id;modelSelect.disabled=running;refresh.disabled=running;}
      if(toggle)toggle.textContent=enabled?'关闭自动实验':'开启自动实验';if(start)start.disabled=d.running;
      const s=d.statistics;message.textContent=d.notice+` 自动实验${enabled?'开启':'关闭'}。前瞻配对样本 ${s.samples}；影子复利 ${s.samples?pct(s.shadow.compound_return_pct):'暂无'}，正式同窗口复利 ${s.samples?pct(s.baseline.compound_return_pct):'暂无'}。${s.scope}。`;
      render();updateFlow();
      if(admin&&d.system_usage){let note=document.getElementById('shadow-system-usage');if(!note&&document.getElementById('view-usage')){note=node('p','','workspace-callout');note.id='shadow-system-usage';document.querySelector('#view-usage article')?.prepend(note);}if(note)note.textContent=`系统影子任务累计（独立于登录用户筛选）：发送 ${d.system_usage.input_tokens} / 响应 ${d.system_usage.output_tokens} Token；请求 ${d.system_usage.model_calls}；用量未报告 ${d.system_usage.unreported_responses}。`;}
    }catch(e){message.textContent=e.message;}finally{loading=false;}
  }
  document.addEventListener('workspace-view',e=>{clearInterval(timer);if(e.detail==='shadow'){load();timer=setInterval(load,2000);}else if(e.detail==='usage'&&admin)load();});
  if(!panel.hidden){load();timer=setInterval(load,2000);}
})();
