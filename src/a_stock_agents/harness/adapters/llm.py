"""Native tool-calling transport for shadow experiments; never falls back to rules."""
import json
import re
import time
import urllib.request
import urllib.error
import uuid
from urllib.parse import urlsplit

from ...llm import normalize_base_url, _is_openai_official, _is_dashscope_native, LLMHTTPError, _describe_http_error
from ...console_log import console_event, log_scope


def build_payload(config, messages, tools, force_submit=False):
    base=normalize_base_url(config['base_url'])
    if _is_dashscope_native(base):
        raise ValueError('影子实验需要支持原生Function Calling的OpenAI兼容地址；当前原生接口尚不支持')
    if _is_openai_official(base):
        items=[]
        for message in messages:
            if message['role']=='assistant':items.extend(message.get('response_items',[]))
            elif message['role']=='tool':items.append({'type':'function_call_output','call_id':message['tool_call_id'],'output':message['content']})
            else:items.append({'role':message['role'],'content':message['content']})
        functions=[{'type':'function',**tool['function']} for tool in tools]
        return base+'/responses', {'model':config['model'],'input':items,'tools':functions,
            'tool_choice':{'type':'function','name':'submit_review'} if force_submit else 'auto',
            'parallel_tool_calls':False,'max_output_tokens':2400}, 'responses'
    payload={'model':config['model'],'messages':messages,'tools':tools,
             'tool_choice':{'type':'function','function':{'name':'submit_review'}} if force_submit else 'auto',
             'parallel_tool_calls':False,'temperature':.2,'max_tokens':2400}
    if config['model'].lower().startswith('qwen3') and (urlsplit(base).hostname or '').endswith('.aliyuncs.com'):
        payload['enable_thinking']=False
    return base if base.endswith('/chat/completions') else base+'/chat/completions',payload,'chat'


def complete(config, messages, tools, force_submit=False, timeout=40):
    from ..gateway import model_boundary
    with model_boundary(config) as tracking:
        return _complete(config,messages,tools,force_submit,timeout,tracking)


def _complete(config, messages, tools, force_submit, timeout, tracking):
    from ..context import request_manifest
    tracking.update(request_manifest(messages,tools))
    endpoint,payload,provider=build_payload(config,messages,tools,force_submit)
    started=time.monotonic()
    with log_scope(component='shadow_llm',request_id=uuid.uuid4().hex,secrets=(config['api_key'],)):
        console_event('shadow_llm_request',endpoint=endpoint,method='POST',timeout_seconds=timeout,request=payload)
        request=urllib.request.Request(endpoint,data=json.dumps(payload,ensure_ascii=False).encode(),
            headers={'Authorization':'Bearer '+config['api_key'],'Content-Type':'application/json','Accept':'application/json'})
        proxy=str(config.get('proxy_url') or '').strip()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({'http':proxy,'https':proxy} if proxy else {}))
        try:
            tracking['_mark_sent']()
            with opener.open(request,timeout=timeout) as response:
                raw=response.read(1_000_001)
                if len(raw)>1_000_000:raise ValueError('模型响应过大')
                body=json.loads(raw)
                tracking.update(status='received',provider_request_id=body.get('id') or body.get('request_id'),usage=body.get('usage'))
        except urllib.error.HTTPError as exc:
            tracking.update(status='rejected',http_status=exc.code)
            detail=exc.read(4096).decode('utf-8',errors='replace').replace(config['api_key'],'[REDACTED]')
            detail=re.sub(r'sk-[A-Za-z0-9_*\-]+','[REDACTED]',detail)
            console_event('shadow_llm_failed',http_status=exc.code,response=detail,error_type=type(exc).__name__,duration_ms=round((time.monotonic()-started)*1000))
            raise LLMHTTPError(_describe_http_error(exc.code,detail,config.get('base_url',''))) from exc
        except (urllib.error.URLError,TimeoutError,OSError) as exc:
            console_event('shadow_llm_failed',error_type=type(exc).__name__,message=str(exc),duration_ms=round((time.monotonic()-started)*1000))
            raise ValueError('模型网络连接失败，请检查代理或网络；'+type(exc).__name__) from exc
        if config.get('_usage_sink'):config['_usage_sink'](body.get('usage') or {})
        console_event('shadow_llm_response',response=body,duration_ms=round((time.monotonic()-started)*1000))
        if provider=='responses' and body.get('status') in ('incomplete','failed','cancelled'):
            raise ValueError('模型响应未完成：'+str((body.get('incomplete_details') or {}).get('reason') or body.get('status')))
    usage=body.get('usage') or {}
    if provider=='responses':
        items=body.get('output',[])
        calls=[{'id':item['call_id'],'type':'function','function':{'name':item['name'],'arguments':item['arguments']}} for item in items if item.get('type')=='function_call']
        message={'role':'assistant','response_items':items}
    else:
        choice=body['choices'][0]
        if choice.get('finish_reason')=='length':raise ValueError('模型响应被截断，未生成有效决策')
        original=choice['message']
        calls=original.get('tool_calls') or []
        message={'role':'assistant','content':original.get('content'),'tool_calls':calls}
    if not calls:raise ValueError('模型未返回原生工具调用，接口不兼容或未遵守结构化输出要求')
    if len(calls)!=1:raise ValueError('单轮只允许一个工具调用')
    return message,calls[0],usage
