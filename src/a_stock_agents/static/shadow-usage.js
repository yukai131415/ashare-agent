(() => {
  if(window.APP_AUTH?.role!=='admin')return;
  const host=document.querySelector('#view-usage article');if(!host)return;
  const section=document.createElement('section');
  section.innerHTML='<h3>影子实验 Token（系统共享）</h3><p data-note class="workspace-note"></p><div data-kpis class="workspace-kpis"></div><div class="table-wrap"><table><thead><tr><th>执行时间</th><th>选股日期 / 次数</th><th>模型</th><th>用途</th><th>状态</th><th>发送 Token</th><th>响应 Token</th><th>模型调用数</th><th>未报告响应</th></tr></thead><tbody></tbody></table></div><div data-pager class="workspace-pager"></div>';
  host.querySelector('#usage-kpis').after(section);
  const count=v=>v==null?'未记录':Number(v).toLocaleString('zh-CN');
  const states={completed:'已完成',abstained:'未选股',failed:'失败',running:'进行中',blocked:'已中止'};
  let rows=[],page=1,sequence=0;
  function render(){
    const body=section.querySelector('tbody');body.replaceChildren();
    for(const r of rows.slice((page-1)*10,page*10)){
      const tr=document.createElement('tr');
      for(const value of [r.started_at.replace('T',' '),`${r.date} / ${r.attempt??'—'}`,r.model,'影子实验',states[r.status]||r.status,count(r.input_tokens),count(r.output_tokens),count(r.model_calls),count(r.unreported_responses)]){
        const td=document.createElement('td');td.textContent=value;tr.append(td);
      }body.append(tr);
    }
    if(!rows.length){const tr=body.insertRow(),td=tr.insertCell();td.colSpan=9;td.textContent='所选日期内暂无影子实验。';}
    const pager=section.querySelector('[data-pager]'),pages=Math.max(1,Math.ceil(rows.length/10));pager.replaceChildren();
    const label=document.createElement('span');label.textContent=`第 ${page} / ${pages} 页`;pager.append(label);
    for(const [text,next] of [['上一页',page-1],['下一页',page+1]]){const b=document.createElement('button');b.type='button';b.textContent=text;b.disabled=next<1||next>pages;b.onclick=()=>{page=next;render();};pager.append(b);}
  }
  async function load(){
    const id=++sequence;
    try{
      const params=new URLSearchParams(new FormData(document.getElementById('usage-filter')));
      const response=await fetch('/api/token-usage?'+params,{cache:'no-store'}),data=await response.json();
      if(!response.ok)throw Error(data.error||'读取失败');if(id!==sequence)return;
      const d=data.shadow_usage,t=d.totals;rows=d.details;page=1;
      section.querySelector('[data-note]').textContent=d.note;
      const kpis=section.querySelector('[data-kpis]');kpis.replaceChildren();
      for(const [label,value] of [['影子发送 Token（已报告）',t.input_tokens],['影子响应 Token（已报告）',t.output_tokens],['实验次数（含重跑）',t.experiments],['模型调用数',t.model_calls],['未报告响应',t.unreported_responses],['缺少用量的实验',t.missing_usage_attempts]]){
        const box=document.createElement('div'),small=document.createElement('small'),strong=document.createElement('strong');small.textContent=label;strong.textContent=count(value);box.append(small,strong);kpis.append(box);
      }render();
    }catch(error){if(id===sequence)section.querySelector('[data-note]').textContent='影子用量读取失败：'+error.message;}
  }
  document.getElementById('usage-filter').addEventListener('submit',()=>load());
  document.addEventListener('workspace-view',event=>{if(event.detail==='usage')load();});
  load();
})();
