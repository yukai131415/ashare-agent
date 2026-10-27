(() => {
  const form=document.getElementById('login'),password=document.getElementById('password'),toggle=document.querySelector('.password-toggle');
  toggle.addEventListener('click',()=>{const visible=password.type==='password';password.type=visible?'text':'password';toggle.setAttribute('aria-label',visible?'隐藏密码':'显示密码');toggle.setAttribute('aria-pressed',String(visible));});
  const clock=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  function tick(){document.getElementById('auth-clock').textContent='北京时间 '+clock.format(new Date());}tick();setInterval(tick,30000);
  form.addEventListener('submit',async event=>{
    event.preventDefault();const button=form.querySelector('[type="submit"]'),label=button.querySelector('.submit-label'),original=label.textContent,error=document.getElementById('error');
    button.disabled=true;button.setAttribute('aria-busy','true');label.textContent='正在验证…';error.textContent='';
    try{const response=await fetch('/api/auth/'+form.dataset.endpoint,{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},body:JSON.stringify(Object.fromEntries(new FormData(form)))});const data=await response.json();if(!response.ok)throw Error(data.error||'暂时无法登录，请稍后重试。');label.textContent='验证成功，正在进入…';location.replace('/');}
    catch(exception){error.textContent=exception instanceof TypeError?'连接失败，请检查服务是否运行。':exception.message;}
    finally{button.disabled=false;button.removeAttribute('aria-busy');label.textContent=original;}
  });
})();
