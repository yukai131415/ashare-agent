(() => {
  const escapeText = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const numeric = value => value === null || value === undefined || value === '' || !Number.isFinite(Number(value)) ? null : Number(value);
  const fixed = (value, places=2) => numeric(value) === null ? '—' : Number(value).toFixed(places);
  const panel = document.createElement('section');
  panel.className='quant-stage'; panel.setAttribute('aria-label','选股链路动态监控');
  document.querySelector('.grid').after(panel);
  let signature='';
  let scoreData=null;
  const scoreDialog=document.createElement('dialog');scoreDialog.className='score-details-dialog';scoreDialog.id='score-details-dialog';scoreDialog.setAttribute('aria-labelledby','score-details-title');
  scoreDialog.innerHTML='<header class="score-dialog-header"><div><small>RESEARCH SCORE / 保密版</small><h2 id="score-details-title">评分流程与结果</h2></div><button type="button" class="score-dialog-close" aria-label="关闭评分说明">×</button></header><div id="score-details-body"></div><footer><span>只展示已保存结果，不重新计算或选股。</span><button type="button" class="score-dialog-done">知道了</button></footer>';
  document.body.append(scoreDialog);
  const closeScore=()=>scoreDialog.close();
  scoreDialog.querySelector('.score-dialog-close').onclick=closeScore;scoreDialog.querySelector('.score-dialog-done').onclick=closeScore;
  scoreDialog.addEventListener('click',event=>{if(event.target!==scoreDialog)return;const bounds=scoreDialog.getBoundingClientRect();if(event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<bounds.top||event.clientY>bounds.bottom)closeScore();});
  function renderScoreDetails(){
    const data=scoreData||{},rec=data.recommendation||{},roles=data.workflow?.roles||{},technical=roles.technical||{};
    const leaders=technical.top5||[],position=leaders.findIndex(row=>row.code===rec.code&&numeric(row.score)!==null&&numeric(rec.score)!==null&&Math.abs(Number(row.score)-Number(rec.score))<.000001);
    const generated=rec.created_at||data.workflow?.finished_at||technical.finished_at;
    const facts=[['推荐日期',data.selected_date||'未记录'],['目标日',rec.target_date||'未记录'],['候选数量',technical.candidate_count===undefined?'未记录':technical.candidate_count+' 只'],['已存前五名位置',position<0?'暂无可核验记录':'第 '+(position+1)+' 位'],['结果生成时间',generated?String(generated).replace('T',' '):'未记录']];
    const stages=[['data_acquisition','数据核验','核对已采集的数据快照及可用状态。'],['technical','评分计算','由服务器内部策略计算综合评分，参数和计算明细不公开。'],['risk','风险审查','执行已有风险审查流程；审查通过不代表投资无风险。'],['decision','结果保存','汇总研究结果，保存推荐和评分供页面读取。']];
    const state={success:'已完成',failed:'失败',skipped:'已跳过'};
    document.getElementById('score-details-body').innerHTML=`<div class="score-result"><div><span>${escapeText(rec.code||'暂无股票')}</span><h3>${escapeText(rec.name||'等待有效推荐')}</h3><small>综合评分 · 已保存的模型输出</small></div><strong>${fixed(rec.score,3)}</strong></div><p class="score-precision">归档原始评分：${numeric(rec.score)===null?'未记录':escapeText(String(rec.score))}。圆环显示保留三位小数；评分不是上涨概率或预期收益率。</p><dl class="score-facts">${facts.map(([key,value])=>`<div><dt>${escapeText(key)}</dt><dd>${escapeText(value)}</dd></div>`).join('')}</dl><h3 class="score-flow-title">流程概述</h3><ol class="score-flow">${stages.map(([key,title,description],i)=>`<li><span class="score-flow-number">${String(i+1).padStart(2,'0')}</span><div><strong>${title}</strong><p>${description}</p></div><small data-state="${escapeText(roles[key]?.status||'missing')}">${state[roles[key]?.status]||'未记录'}</small></li>`).join('')}</ol><div class="score-private-note">策略保密：不展示真实模型名称、因子权重、公式、阈值或评分贡献。这里是流程说明，不是可复刻的逐项计算明细；缺失信息不会推算补造。</div>`;
  }
  function openScoreDetails(){renderScoreDetails();if(!scoreDialog.open)scoreDialog.showModal();}
  const schedulerItem=document.createElement('div');schedulerItem.className='monitor-item';
  schedulerItem.innerHTML='<button type="button" id="agent-control-trigger" aria-haspopup="dialog" aria-controls="agent-control-dialog" title="查看主 Agent 运行与修复记录" style="background:transparent;border:0;padding:0;width:100%;text-align:left;color:inherit;font:inherit"><span style="display:block">主 Agent</span><strong id="agent-control-counts">修复 — 次 / 成功 — 次</strong></button>';
  document.querySelector('.monitor-grid').append(schedulerItem);
  window.renderQuantMonitor = data => {
    scoreData=data;
    if(scoreDialog.open)renderScoreDetails();
    const next = JSON.stringify([data.selected_date,data.recommendation,data.workflow,data.history]);
    if(signature===next)return;
    signature=next;
    const rec=data.recommendation,roles=data.workflow?.roles || {},technical=roles.technical || {};
    const leaders=Array.isArray(technical.top5)?technical.top5.slice(0,5):[];
    const history=[...(data.history || [])].sort((a,b)=>String(b.date).localeCompare(String(a.date)));
    panel.dataset.active=String(Boolean(rec && data.workflow?.ok));
    const candidates=leaders.map((row,index)=>`<div class="quant-candidate ${rec?.code===row.code?'is-selected':''}" style="--delay:${index*.7}s"><small>${String(index+1).padStart(2,'0')}</small><div>${escapeText(row.name)}<small>${escapeText(row.code)} · ¥${fixed(row.price)}</small></div><b>${fixed(row.score,3)}</b></div>`).join('');
    // Paths show relationships in the saved report; they are not trading events.
    const paths=[38,89,140,191,242].map(y=>`M 250 ${y} C 330 ${y}, 330 140, 420 140`);
    paths.push('M 580 140 C 660 140,660 65,750 65','M 580 140 C 660 140,660 140,750 140','M 580 140 C 660 140,660 215,750 215');
    const steps=['data_acquisition','fundamental','technical','risk','decision'].map(key=>{
      const role=roles[key],names={data_acquisition:'数据获取',fundamental:'基本面分析',technical:'技术面分析',risk:'风控审查',decision:'决策汇总'};
      const status=role?.status || 'missing';
      return `<div class="quant-step" data-status="${escapeText(status)}"><i aria-hidden="true"></i>${names[key]}<small>${escapeText(({success:'已完成',failed:'失败',skipped:'已跳过',missing:'无记录'})[status] || status)}</small></div>`;
    }).join('');
    const feed=history.slice(0,10).map(row=>{const value=numeric(row.return_pct);return `<div class="quant-feed-row"><time>${escapeText(row.date)}</time><b>${escapeText(row.name)} <small>${escapeText(row.code)}</small></b><span class="${value===null?'waiting':value<0?'loss':value>0?'profit':'flat'}">${value===null?'待回测':(value>0?'+':'')+fixed(value)+'%'}</span></div>`;}).join('');
    const samples=history.filter(row=>numeric(row.return_pct)!==null).slice(0,12).reverse();
    let trend='<div class="quant-empty">暂无已完成回测，等待有效结果</div>';
    if(samples.length){
      const values=samples.map(row=>Number(row.return_pct)),low=Math.min(0,...values),high=Math.max(0,...values),range=high-low||1;
      const x=i=>samples.length===1?260:38+i*444/(samples.length-1),y=v=>77-(v-low)/range*57;
      const points=values.map((v,i)=>`${x(i)},${y(v)}`).join(' '),zero=y(0);
      trend=`<svg viewBox="0 0 520 105" role="img" aria-label="${escapeText(samples.map(row=>row.date+' 模拟收益 '+fixed(row.return_pct)+'%').join('；'))}"><defs><linearGradient id="quant-area" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#75c6ed" stop-opacity=".2"/><stop offset="1" stop-color="#75c6ed" stop-opacity="0"/></linearGradient></defs><path d="M ${x(0)} 80 L ${points.replaceAll(' ',' L ')} L ${x(values.length-1)} 80 Z" fill="url(#quant-area)"/><path d="M 30 ${zero} H 490" stroke="#47606e" stroke-dasharray="3 5"/><text x="0" y="${zero+3}">0%</text><polyline class="trend-stroke" points="${points}"/>${values.map((v,i)=>`<circle cx="${x(i)}" cy="${y(v)}" r="2.5" fill="${v<0?'var(--loss)':v>0?'var(--profit)':'var(--muted)'}"><title>${escapeText(samples[i].date)}：${fixed(v)}%</title></circle>`).join('')}<text x="30" y="101">${escapeText(samples[0].date)}</text><text x="488" y="101" text-anchor="end">${escapeText(samples[samples.length-1].date)}</text></svg>`;
    }
    panel.innerHTML=`<div class="quant-heading"><div><div class="quant-eyebrow">RESEARCH SIGNAL FLOW</div><h2>选股链路 · 动态观察台</h2></div><div><span class="tag">${escapeText(data.selected_date || '未选择日期')} · 已保存快照</span><div class="quant-caption">流光为展示动效，非实时行情或交易信号</div></div></div><div class="quant-grid"><svg class="quant-wires" viewBox="0 0 1000 280" preserveAspectRatio="none" aria-hidden="true">${paths.map(path=>`<path class="quant-wire" d="${path}"/><path class="quant-packet" d="${path}"/>`).join('')}</svg><div class="quant-pane"><div class="quant-pane-head"><span>候选前五 · 模型评分</span><small>候选 ${escapeText(technical.candidate_count ?? '—')} 只</small></div>${candidates || '<div class="quant-empty">该日无候选排名快照<br>不会用其他日期的数据填充</div>'}</div><div class="quant-pane quant-hub"><div class="quant-orbit"><span>精选模型评分</span><strong>${fixed(rec?.score,3)}</strong></div><div><h3>${escapeText(rec?.model_label || '等待有效推荐')}</h3><p>原有多因子评分 · 新闻不计分</p><p>采集快照 ${escapeText(roles.data_acquisition?.market_rows ?? '—')} 条</p></div></div><div class="quant-pane">${steps}<div class="quant-final"><div class="quant-caption">所选日期推荐 · ${escapeText(rec?.code || '—')}</div><strong>${escapeText(rec?.name || '暂无有效推荐')}</strong><span>参考价 ¥${fixed(rec?.reference_price)} · 目标日 ${escapeText(rec?.target_date || '—')}</span></div></div></div><div class="quant-bottom"><div class="quant-trend"><div class="quant-caption">最近 ${samples.length} 笔单次模拟收益（%） · 非股价走势</div>${trend}</div><div class="quant-feed"><div class="quant-caption">历史推荐轮播 · 悬停暂停 · 非实时成交</div><div class="quant-feed-window" tabindex="0" aria-label="历史推荐滚动列表，聚焦暂停"><div class="${history.length>3?'quant-feed-track':''}"><div>${feed || '<div class="quant-empty">暂无推荐记录</div>'}</div>${history.length>3?`<div aria-hidden="true">${feed}</div>`:''}</div></div></div></div>`;
    const orbit=panel.querySelector('.quant-orbit');
    const trigger=document.createElement('button');trigger.type='button';trigger.className='quant-orbit score-details-trigger';trigger.setAttribute('aria-haspopup','dialog');trigger.setAttribute('aria-controls','score-details-dialog');trigger.setAttribute('aria-label','查看评分流程与结果（保密版）');trigger.title='点击查看评分流程与结果（保密版）';
    trigger.disabled=numeric(rec?.score)===null;trigger.append(...orbit.childNodes);orbit.replaceWith(trigger);trigger.onclick=openScoreDetails;
    const hint=document.createElement('small');hint.className='score-open-hint';hint.textContent=trigger.disabled?'等待有效评分':'点击圆环查看评分说明';trigger.after(hint);
  };
  const observer=new IntersectionObserver(entries=>document.body.classList.toggle('quant-offscreen',!entries[0].isIntersecting));
  observer.observe(panel);
  const visibility=()=>document.body.classList.toggle('quant-hidden',document.hidden);
  document.addEventListener('visibilitychange',visibility);visibility();
})();
