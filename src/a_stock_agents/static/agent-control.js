(() => {
  const panel=document.getElementById('view-overview');if(!panel)return;
  const trigger=document.getElementById('agent-control-trigger');if(!trigger)return;
  const dialog=document.createElement('dialog');dialog.id='agent-control-dialog';dialog.className='model-dialog';dialog.style.width='min(1100px,calc(100vw - 28px))';dialog.setAttribute('aria-labelledby','agent-control-title');
  const header=document.createElement('header');header.className='dialog-header';
  const title=document.createElement('h2');title.id='agent-control-title';title.textContent='主 Agent · Worker 运行控制台';
  const close=document.createElement('button');close.type='button';close.className='dialog-close';close.textContent='×';close.setAttribute('aria-label','关闭运行控制台');header.append(title,close);dialog.append(header);document.body.append(dialog);
  const card=document.createElement('article');card.className='card';card.style.gridColumn='1 / -1';
  card.innerHTML='<p data-note class="workspace-note"></p><p class="workspace-note">次数为累计记录：每次启动受限重试计一次修复；该 Worker 后续真实执行成功才计成功，多次重试后成功只计一次。明细展示最近100条事件。</p><div data-workers class="workspace-kpis"></div><details open><summary>实时执行 / 恢复记录（每3秒同步）</summary><div class="table-wrap" style="max-height:280px;overflow:auto"><table><thead><tr><th>时间</th><th>Agent</th><th>状态</th><th>动作 / 诊断</th></tr></thead><tbody></tbody></table></div></details>';
  dialog.append(card);
  close.onclick=()=>dialog.close();
  dialog.addEventListener('click',event=>{if(event.target!==dialog)return;const b=dialog.getBoundingClientRect();if(event.clientX<b.left||event.clientX>b.right||event.clientY<b.top||event.clientY>b.bottom)dialog.close();});
  trigger.onclick=()=>{if(!dialog.open)dialog.showModal();load();};
  const names={master:'主 Agent',pipeline:'正式执行链',market:'行情 Worker',news:'新闻 Worker',analysis:'分析 Worker',backtest:'回测 Worker',recommendation:'推荐 Worker',data_acquisition:'数据获取',fundamental:'基本面',technical:'技术面',risk:'风险审查',decision:'决策汇总','shadow.data':'影子 · 数据准备','shadow.fundamental':'影子 · 基本面','shadow.technical':'影子 · 技术面','shadow.risk':'影子 · 风控','shadow.decision':'影子 · 决策'};
  const labels={running:'执行中',started:'执行中',success:'已完成',completed:'已完成',role_started:'执行中',role_finished:'阶段结束',dispatched:'已分发',diagnosing:'主Agent诊断中',retry_wait:'自动恢复 / 等待重试',needs_attention:'需要处理',failed_attempt:'本次尝试失败',pipeline_started:'链路已启动',pipeline_finished:'链路结束',pipeline_skipped:'休市跳过',usage:'模型用量已记录',unconfirmed:'状态待确认'};
  let loading=false;
  function status(row){return row.ok===false?'失败':labels[row.status]||row.status;}
  function color(row){return ['needs_attention','failed_attempt','unconfirmed'].includes(row.status)||row.ok===false?'#ff6b91':['running','started','diagnosing','retry_wait','role_started'].includes(row.status)?'#ffd166':'#45e8bf';}
  async function load(){
    // 轮询请求未结束时不叠加请求；后台标签页暂停拉取，降低接口与页面负担。
    if(loading||document.hidden)return;loading=true;
    try{
      const response=await fetch('/api/agent-control',{cache:'no-store'}),data=await response.json();if(!response.ok)throw Error(data.error||'读取失败');
      const stats=data.recovery_statistics;
      if(!stats||!Number.isInteger(stats.attempts)||!Number.isInteger(stats.successes))throw Error('后端尚未提供修复统计，请重启现有后端服务后刷新页面');
      document.getElementById('agent-control-counts').textContent=`修复 ${stats.attempts} 次 / 成功 ${stats.successes} 次`;trigger.title='点击查看主 Agent 运行与修复记录';
      card.querySelector('[data-note]').textContent=data.notice;
      if(data.harness){
        // 管理员计划/任务由后端权限控制；动态文字使用 textContent，避免把服务端
        // 错误、模型输出或运行编号当 HTML 执行。前端显示条件不能替代后端鉴权。
        let section=card.querySelector('[data-harness]');
        if(!section){section=document.createElement('details');section.dataset.harness='';const summary=document.createElement('summary');summary.textContent='Harness 执行计划 / 最近任务（管理员）';section.append(summary,document.createElement('div'));card.append(section);}
        const body=section.querySelector('div');body.replaceChildren();
        for(const plan of data.harness.plans){
          const heading=document.createElement('h3');heading.textContent=`${({formal:'正式选股',shadow:'影子研究',intraday:'当日分析'})[plan.task_type]} · ${plan.version}`;body.append(heading);
          const list=document.createElement('ul');
          for(const step of plan.steps){const item=document.createElement('li');item.textContent=step.title+'；验收：'+step.acceptance.join('、');list.append(item);}body.append(list);
        }
        const heading=document.createElement('h3');heading.textContent='最近持久任务';body.append(heading);
        for(const run of data.harness.runs.slice(0,10)){const item=document.createElement('p');item.style.overflowWrap='anywhere';item.textContent=`${run.id} · ${run.kind} · ${run.status} · 恢复 ${run.recoveries} 次`;body.append(item);}
        if(!data.harness.runs.length){const empty=document.createElement('p');empty.textContent='暂无 Harness 新任务；历史业务结果不会被伪造为新运行记录。';body.append(empty);}
      }
      const workers=card.querySelector('[data-workers]');workers.replaceChildren();
      for(const row of data.workers){const box=document.createElement('div'),label=document.createElement('small'),value=document.createElement('strong'),time=document.createElement('small');label.textContent=names[row.worker]||row.worker;value.textContent=status(row);value.style.color=color(row);time.textContent=new Date(row.at).toLocaleTimeString('zh-CN');box.append(label,value,time);workers.append(box);}
      if(!data.workers.length)workers.textContent='暂无新运行记录，下一次任务执行后自动显示。';
      const body=card.querySelector('tbody');body.replaceChildren();
      for(const row of data.events.filter(r=>r.status!=='usage')){const tr=document.createElement('tr');for(const text of [new Date(row.at).toLocaleString('zh-CN'),names[row.worker]||row.worker,status(row),[row.action==='retry'?'受限重试':row.action==='stop'?'安全停止':row.action==='dispatch'?'按依赖分发':'',row.error_type,row.reason,row.source==='model'?'大模型协调':row.source==='rules'?'安全规则兜底':''].filter(Boolean).join(' · ')]){const td=document.createElement('td');td.textContent=text;tr.append(td);}tr.children[2].style.color=color(row);body.append(tr);}
    }catch(error){card.querySelector('[data-note]').textContent='监控连接异常：'+error.message;trigger.title='状态同步失败，显示的可能是上次结果；点击查看';}finally{loading=false;}
  }
  document.addEventListener('workspace-view',e=>{if(e.detail==='overview')load();});setInterval(load,3000);load();
})();
