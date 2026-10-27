(() => {
  const original=window.fetch.bind(window),id=()=>crypto.randomUUID?.()||`${Date.now()}-${Math.random().toString(16).slice(2)}`;
  let sent=0,windowStart=Date.now();
  function report(event,data={}){
    if(Date.now()-windowStart>60000){sent=0;windowStart=Date.now();}if(sent++>=20)return;
    // No request/response body, credentials, query strings, or exception text.
    original('/api/client-events',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event,...data})}).catch(()=>{});
  }
  window.fetch=async(input,options={})=>{
    const url=new URL(typeof input==='string'?input:input.url,location.href);
    if(url.origin!==location.origin)return original(input,options);
    const requestId=id(),started=performance.now(),headers=new Headers(input instanceof Request?input.headers:undefined);
    new Headers(options.headers).forEach((v,k)=>headers.set(k,v));headers.set('X-Request-ID',requestId);
    try{
      const response=await original(input,{...options,headers});
      console.info('[API]',options.method||(input instanceof Request?input.method:'GET'),url.pathname,response.status,response.headers.get('X-Request-ID')||requestId,Math.round(performance.now()-started)+'ms');
      return response;
    }catch(error){
      report('fetch_failed',{path:url.pathname,related_request_id:requestId,duration_ms:Math.round(performance.now()-started)});throw error;
    }
  };
  window.addEventListener('error',e=>report('javascript_error',{path:location.pathname,line:Number(e.lineno)||0}));
  window.addEventListener('unhandledrejection',()=>report('unhandled_rejection',{path:location.pathname}));
})();
