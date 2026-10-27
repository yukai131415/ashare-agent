(() => {
  const definitions={
    funds:{title:'个股资金观察',caption:'查看个股近期资金流记录，不参与模型评分。',fields:'<label>股票代码<input name="code" aria-label="资金观察股票代码" value="000530" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" required></label>'},
    limitpool:{title:'涨停及强势股观察',caption:'按指定交易日查询涨停或强势股池，不是系统推荐名单。',fields:'<label>交易日期<input type="date" name="date" aria-label="股池交易日期"></label><label>股池<select name="pool"><option>涨停</option><option>强势</option></select></label>'},
    billboard:{title:'龙虎榜事件观察',caption:'展示最近一段时间的个股上榜统计，不代表机构买入建议。',fields:'<label>统计窗口<select name="days"><option value="5">最近5日</option><option value="10">最近10日</option><option value="30">最近30日</option><option value="60">最近60日</option></select></label>'},
    sectorfunds:{title:'板块资金热度',caption:'观察行业、概念或地域板块资金流，上游结果可能仅覆盖部分板块。',fields:'<label>窗口<select name="days"><option>5日</option><option>10日</option><option>今日</option></select></label><label>板块类型<select name="category"><option>行业资金流</option><option>概念资金流</option><option>地域资金流</option></select></label>'}
  };
  const states=new Map();
  function text(tag,value,cls){const node=document.createElement(tag);node.textContent=String(value??'—');if(cls)node.className=cls;return node;}
  function numericStyle(column,value){
    if(!/(涨跌幅|净流|净额)/.test(column))return '';
    const number=Number(String(value).replaceAll(',','').replace('%',''));
    return Number.isFinite(number)?number>0?'positive':number<0?'negative':'':'';
  }
  function render(state){
    const {body,result}=state;body.replaceChildren();
    if(!result||result.status!=='ok'){body.append(text('p',result?.message||'暂无可用数据。','workspace-empty'));return;}
    body.append(text('p',`${result.provider} · ${result.tool} · 查询时间 ${result.fetched_at?.replace('T',' ')}${result.cached?' · 3分钟内共享公开数据缓存':''}`,'workspace-callout'));
    body.append(text('p',result.scope,'workspace-note'));
    body.append(text('p','查询参数：'+JSON.stringify(result.arguments||{}),'workspace-note'));
    body.append(text('p',`数据日期：${result.data_dates?.join('、')||'上游未提供，不能视为今日实时数据'}。${result.freshness_note}`,'workspace-note'));
    for(const line of result.metadata||[])body.append(text('p',line,'workspace-note'));
    const rows=result.rows||[],columns=result.columns||[],pages=Math.max(1,Math.ceil(rows.length/10));state.page=Math.min(state.page,pages);
    const wrap=text('div','','table-wrap'),table=document.createElement('table'),head=document.createElement('thead'),tr=document.createElement('tr');
    for(const col of columns)tr.append(text('th',col));head.append(tr);table.append(head);
    const tbody=document.createElement('tbody');
    for(const row of rows.slice((state.page-1)*10,state.page*10)){const line=document.createElement('tr');for(const col of columns)line.append(text('td',row[col],numericStyle(col,row[col])));tbody.append(line);}
    table.append(tbody);wrap.append(table);body.append(wrap);
    const pager=text('div','','workspace-pager');pager.append(text('span',`第 ${state.page} / ${pages} 页 · 已返回 ${rows.length} 条`));
    for(const [label,page] of [['首页',1],['上一页',state.page-1],['下一页',state.page+1],['最后一页',pages]]){const button=text('button',label);button.type='button';button.disabled=page<1||page>pages||page===state.page;button.onclick=()=>{state.page=page;render(state);};pager.append(button);}body.append(pager);
    body.append(text('p','金额、比例保留上游列名和原始单位；无单位字段不擅自换算。仅供研究观察，非投资建议。','workspace-note'));
  }
  async function load(kind){
    const state=states.get(kind);if(!state||state.loading)return;
    if(!state.form.reportValidity())return;
    state.loading=true;state.button.disabled=true;state.badge.textContent='查询中';state.body.replaceChildren(text('p','正在通过 MCP 查询公开数据，最长约55秒…','workspace-empty'));
    try{
      const params=new URLSearchParams(new FormData(state.form));params.set('kind',kind);
      const response=await fetch('/api/stock-observation?'+params);const result=await response.json();
      if(!response.ok)throw Error(result.error||'请求失败');
      state.result=result;state.page=1;state.badge.textContent=result.status==='ok'?'已获取':'暂不可用';render(state);
    }catch(error){state.result={status:'unavailable',message:error.message};state.badge.textContent='查询失败';render(state);}
    finally{state.loading=false;state.button.disabled=false;}
  }
  for(const [kind,definition] of Object.entries(definitions)){
    const panel=document.getElementById('view-'+kind);if(!panel)continue;
    const article=text('article','','card workspace-full stock-observation');
    const title=text('div','','section-title');title.append(text('h2',definition.title));const badge=text('span','按需查询','tag');title.append(badge);article.append(title,text('p',definition.caption,'workspace-note'));
    const form=document.createElement('form');form.className='workspace-controls';form.innerHTML=definition.fields;
    const button=text('button','查询数据');button.type='submit';form.append(button);article.append(form);
    const body=text('div','点击查询获取外部数据；不会修改推荐或历史回测。','stock-observation-body');body.setAttribute('aria-live','polite');article.append(body);panel.append(article);
    states.set(kind,{form,button,body,badge,page:1,result:null,loading:false});form.onsubmit=event=>{event.preventDefault();load(kind);};
  }
})();
