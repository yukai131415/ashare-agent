(() => {
  const widget=document.createElement('div');widget.className='scheduler-countdown';widget.dataset.state='loading';
  widget.innerHTML='<div id="countdown-value" role="timer" aria-label="推荐任务倒计时" aria-live="off">-- : -- : --</div>';
  const bar=document.querySelector('.auth-userbar'),logout=document.getElementById('auth-logout');
  if(!bar||!logout)return;
  const actions=document.createElement('div');actions.className='auth-header-actions';
  logout.before(actions);actions.append(widget,logout);
  const $=id=>document.getElementById(id);let snapshot=null,received=0;
  function tick(){
    if(!snapshot)return;
    const age=performance.now()-received,stale=age>Math.max(180000,Number(snapshot.dashboard_refresh_seconds||60)*3000);
    const state=stale?'stale':snapshot.countdown_state;widget.dataset.state=state;
    const statuses={needs_review:['待处理','当日任务达到尝试上限，请检查运行记录'],disabled:['已关闭','定时任务未启用'],running:['执行中','正在获取数据并生成推荐'],awaiting_dispatch:['等待调度','已到执行时间，等待后端状态同步'],calendar_unavailable:['待更新日历','交易日历未配置，不推测执行日期'],stale:['状态待同步','服务端状态已过期，请检查连接']};
    if(statuses[state]){$('countdown-value').textContent=statuses[state][0];return;}
    const target=Date.parse(snapshot.next_run_at),server=Date.parse(snapshot.server_now);
    if(!Number.isFinite(target)||!Number.isFinite(server)){$('countdown-value').textContent='等待同步';return;}
    const seconds=Math.max(0,Math.ceil((target-(server+age))/1000));
    if(!seconds){widget.dataset.state='awaiting_dispatch';$('countdown-value').textContent='等待调度';return;}
    const days=Math.floor(seconds/86400),hours=Math.floor(seconds%86400/3600),minutes=Math.floor(seconds%3600/60),remaining=seconds%60;
    $('countdown-value').textContent=(days?days+'天 ':'')+[hours,minutes,remaining].map(n=>String(n).padStart(2,'0')).join(' : ');
  }
  const previous=window.renderQuantMonitor;
  window.renderQuantMonitor=data=>{previous?.(data);snapshot=data.health?.scheduler||null;received=performance.now();tick();};
  tick();setInterval(tick,1000);
})();
