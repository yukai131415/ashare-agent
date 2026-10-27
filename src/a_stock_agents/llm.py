from __future__ import annotations

import json
import os
import time
import uuid
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from tempfile import NamedTemporaryFile
from typing import Any

from .config import Settings
from .storage import atomic_write_json, read_json
from .console_log import console_event, log_scope


OPENAI_API_BASE_URL = "https://api.openai.com/v1"


class LLMHTTPError(ValueError):
    """大模型服务返回了无需换传输方式重试的 HTTP 错误。"""


def normalize_base_url(value: object) -> str:
    """将用户常见的 OpenAI 网页地址或完整端点规范化为 API 基础地址。"""
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return OPENAI_API_BASE_URL
    if "://" not in raw and raw.lower().split("/", 1)[0] in {
        "api.openai.com", "platform.openai.com", "www.platform.openai.com",
    }:
        raw = f"https://{raw}"
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("接口地址必须是完整的 http:// 或 https:// URL。")
    hostname = (parsed.hostname or "").lower()
    if hostname in {"api.openai.com", "platform.openai.com", "www.platform.openai.com"}:
        return OPENAI_API_BASE_URL
    if parsed.query or parsed.fragment:
        raise ValueError("接口地址不能包含查询参数或片段。")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def llm_config(settings: Settings) -> dict[str, Any]:
    configured = dict(settings.values.get("llm", {}))
    saved_path = settings.runtime / "state" / "llm_config.json"
    try:
        saved = read_json(saved_path, {})
    except Exception:
        saved = {}
    if isinstance(saved, dict):
        configured.update(saved)
    env_name = str(configured.get("api_key_env", "ASTOCK_LLM_API_KEY"))
    configured["api_key"] = str(configured.get("api_key") or os.getenv(env_name, ""))
    configured["api_key_env"] = env_name
    configured.setdefault("base_url", OPENAI_API_BASE_URL)
    try:
        configured["base_url"] = normalize_base_url(configured.get("base_url"))
    except ValueError:
        # 兼容历史配置：保留原值，让连接测试返回可读的配置错误。
        configured["base_url"] = str(configured.get("base_url", "")).strip().rstrip("/")
    configured.setdefault("model", "gpt-4o-mini")
    configured.setdefault("enabled", False)
    configured.setdefault("proxy_url", "")
    configured.setdefault('agent_explanations_enabled',bool(settings.values.get('workflow',{}).get('llm_enabled',False)))
    return configured


def public_config(settings: Settings) -> dict[str, Any]:
    config = llm_config(settings)
    profiles = read_json(settings.runtime / 'state/llm_profiles.json', {})
    from hashlib import sha256
    active_id = sha256((str(config.get('base_url'))+'\n'+str(config.get('model'))).encode()).hexdigest()[:20]
    available = dict(profiles) if isinstance(profiles, dict) else {}
    available.setdefault(active_id, config)
    return {
        "active_profile_id": active_id,
        "profiles": [{"id": key, "model": str(value.get('model','')), "base_url": str(value.get('base_url','')),
            "api_key_configured": bool(value.get('api_key'))} for key,value in available.items() if isinstance(value,dict)],
        "enabled": bool(config.get("enabled")),
        "base_url": str(config.get("base_url", "")),
        "model": str(config.get("model", "")),
        "proxy_url": str(config.get("proxy_url", "")),
        "api_key_env": str(config.get("api_key_env", "")),
        "api_key_configured": bool(config.get("api_key")),
        "last_test": read_json(settings.runtime / 'state' / 'llm_connection_status.json', {}),
        "agent_explanations_enabled": bool(config.get('agent_explanations_enabled')),
    }


def save_config(settings: Settings, values: dict[str, Any]) -> dict[str, Any]:
    current = llm_config(settings)
    from hashlib import sha256
    profile_path=settings.runtime/'state/llm_profiles.json'
    profiles=read_json(profile_path,{})
    if not isinstance(profiles,dict):
        profiles={}
    profile_id=lambda item:sha256((str(item.get('base_url'))+'\n'+str(item.get('model'))).encode()).hexdigest()[:20]
    profiles[profile_id(current)]=dict(current)
    if values.get('select_profile_id'):
        chosen=profiles.get(str(values['select_profile_id']))
        if not isinstance(chosen,dict):
            raise ValueError('所选模型不存在，请刷新模型列表。')
        # Only switch connection details; global enable switches retain their state.
        current.update({key:chosen.get(key,'') for key in ('base_url','model','api_key','api_key_env','proxy_url')})
        values={}
    elif values.get('add_profile'):
        if not str(values.get('base_url','')).strip() or not str(values.get('model','')).strip():
            raise ValueError('添加模型时必须填写接口地址和模型名称。')
        # A new provider must never inherit another provider's saved secret.
        current['api_key']=''
        current['api_key_env']=''
        current['proxy_url']=''
    for key in ("enabled", "base_url", "model", "proxy_url", "agent_explanations_enabled"):
        if key in values:
            current[key] = values[key]
    # 空字符串表示“保持原密钥”，避免用户只改模型时意外清除密钥。
    if str(values.get("api_key", "")):
        current["api_key"] = str(values["api_key"])
    current["base_url"] = normalize_base_url(current.get("base_url"))
    current["model"] = str(current.get("model", "")).strip()
    current["proxy_url"] = str(current.get("proxy_url", "")).strip()
    current["enabled"] = bool(current.get("enabled"))
    current['agent_explanations_enabled']=bool(current.get('agent_explanations_enabled'))
    profiles[profile_id(current)]=dict(current)
    atomic_write_json(profile_path,profiles)
    atomic_write_json(
        settings.runtime / "state" / "llm_config.json",
        {key: current[key] for key in ("enabled", "base_url", "model", "api_key", "api_key_env", "proxy_url", "agent_explanations_enabled")},
    )
    _log_event(settings, "config_saved", _safe_config_summary(current))
    atomic_write_json(settings.runtime/'state/llm_connection_status.json',{'ok':None,'status':'待测试','message':'配置已保存，请测试连接。'})
    return public_config(settings)


def answer_question(
    settings: Settings,
    question: str,
    context: str,
    local_answer: str,
) -> tuple[str, str, str]:
    config = llm_config(settings)
    from .token_usage import usage_call, finish_usage
    request_id=uuid.uuid4().hex
    with usage_call(request_id,config.get('model','')), log_scope(request_id=request_id, component='llm', secrets=(str(config.get('api_key') or ''),)):
        started = time.monotonic()
        try:
            result=_answer_question(settings, question, context, local_answer, config)
            finish_usage(result[1])
            return result
        finally:
            console_event('llm_call_finished', duration_ms=round((time.monotonic()-started)*1000, 1))


def _answer_question(settings, question, context, local_answer, config):
    if not config.get("enabled") or not config.get("api_key"):
        _log_event(settings, "answer_local", {"reason": "disabled_or_missing_key", **_safe_config_summary(config)})
        return local_answer, "local", "规则解释"
    base_url = str(config.get("base_url", "")).rstrip("/")
    prompt = (
        "你是 A 股多 Agent 投研解释助手。只解释提供的已保存推荐和系统快照，不得重新选股或改变模型排序，不要编造新闻、价格或财务事实。"
        "回答使用简洁中文，先给完整结论，再列依据和风险，尽量控制在900个中文字以内；明确区分事实、模型推断和不确定性。\n\n"
        f"系统快照：\n{context}\n\n用户问题：{question}"
    )
    try:
        endpoint, payload, provider = _build_request(base_url, prompt, config)
    except ValueError as exc:
        _log_event(settings, "answer_fallback", {"provider": "unknown", "config_error": _short_error(exc)})
        return f"大模型配置无效（{exc}）。\n\n{local_answer}", "fallback", "本地规则解释"
    _log_event(
        settings,
        "answer_started",
        {
            **_safe_config_summary(config),
            "provider": provider,
            "endpoint": endpoint,
            "question_chars": len(question),
            "context_chars": len(context),
            "payload_bytes": len(payload),
        },
    )
    urllib_error: Exception | None = None
    try:
        answer = _urllib_completion(endpoint, payload, config, provider)
        _log_event(settings, "answer_succeeded", {"transport": "urllib", "provider": provider, "answer_chars": len(answer)})
        return answer, "llm", str(config.get("model", ""))
    except LLMHTTPError as exc:
        _log_event(settings, "answer_fallback", {"provider": provider, "http_error": _short_error(exc)})
        return f"大模型请求失败（{exc}）。\n\n{local_answer}", "fallback", "本地规则解释"
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, IndexError, TypeError, ValueError) as exc:
        urllib_error = exc
        _log_event(settings, "answer_transport_failed", {"transport": "urllib", "provider": provider, "error": _short_error(exc)})
    try:
        answer = _curl_completion(endpoint, payload, config, provider)
        _log_event(settings, "answer_succeeded", {"transport": "curl", "provider": provider, "answer_chars": len(answer)})
        return answer, "llm", f"{config.get('model', '')}（curl）"
    except (OSError, subprocess.SubprocessError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        message = f"Python 请求失败：{urllib_error}；curl 备用请求失败：{exc}"
        _log_event(
            settings,
            "answer_fallback",
            {
                "provider": provider,
                "urllib_error": _short_error(urllib_error),
                "curl_error": _short_error(exc),
            },
        )
        return f"大模型暂时不可用（{message}）。\n\n{local_answer}", "fallback", "本地规则解释"


def test_connection(settings: Settings) -> dict[str, Any]:
    config = llm_config(settings)
    _log_event(settings, "test_started", _safe_config_summary(config))
    if not config.get("enabled"):
        _log_event(settings, "test_finished", {"ok": False, "reason": "disabled"})
        return _remember_connection(settings,{"ok": False, "status": "未启用", "message": "请先勾选“启用大模型回答”。"})
    if not config.get("api_key"):
        _log_event(settings, "test_finished", {"ok": False, "reason": "missing_api_key"})
        return _remember_connection(settings,{"ok": False, "status": "缺少 API Key", "message": "请填写 API Key 后保存。"})
    answer, source, model = answer_question(
        settings,
        "只回复：连接成功",
        "{}",
        "所选日期没有推荐记录，暂时无法解释选股原因。",
    )
    ok = source == "llm"
    message = "大模型连接成功。" if ok else answer.split("\n\n", 1)[0]
    config = llm_config(settings)
    _log_event(settings, "test_finished", {"ok": ok, "source": source, "model": model})
    return _remember_connection(settings,{
        "ok": ok,
        "status": "连接成功" if ok else "连接失败",
        "message": message,
        "source": source,
        "model": model,
        "proxy_url": str(config.get("proxy_url", "")),
        "tested_at": date.today().isoformat(),
    })


def _remember_connection(settings,result):
    result={**result,'tested_at':datetime.now().astimezone().isoformat(timespec='seconds')}
    atomic_write_json(settings.runtime/'state/llm_connection_status.json',result)
    return result


def _build_request(base_url: str, prompt: str, config: dict[str, Any]) -> tuple[str, bytes, str]:
    base_url = normalize_base_url(base_url)
    model = config.get("model", "gpt-4o-mini")
    if _is_dashscope_native(base_url):
        endpoint = f"{base_url}/services/aigc/multimodal-generation/generation"
        payload = {
            "model": model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": prompt}],
                    }
                ]
            },
            "parameters": {
                "temperature": 0.2,
                "max_tokens": int(config.get("max_tokens", 700)),
            },
        }
        return endpoint, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "dashscope_native"
    if _is_openai_official(base_url):
        endpoint = f"{base_url}/responses"
        payload = {
            "model": model,
            "instructions": "你是严谨、克制的量化研究助理。只依据输入的系统快照回答。",
            "input": prompt,
            "max_output_tokens": int(config.get("max_tokens", 700)),
        }
        return endpoint, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "openai_responses"
    endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": int(config.get("max_tokens", 700)),
        "messages": [
            {"role": "system", "content": "你是严谨、克制的量化研究助理。"},
            {"role": "user", "content": prompt},
        ],
    }
    # Interactive explanations do not need a long hidden thinking phase. Qwen's
    # non-streaming thinking can otherwise exhaust the request's read timeout.
    hostname=urllib.parse.urlsplit(base_url).hostname or ''
    if model.lower().startswith('qwen3') and hostname.endswith('.aliyuncs.com'):
        payload['enable_thinking']=bool(config.get('enable_thinking',False))
    return endpoint, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "openai_compatible"


def _is_dashscope_native(base_url: str) -> bool:
    return "dashscope.aliyuncs.com/api/v1" in base_url


def _is_openai_official(base_url: str) -> bool:
    return (urllib.parse.urlsplit(base_url).hostname or "").lower() == "api.openai.com"


def _extract_answer(response_data: dict[str, Any], provider: str) -> str:
    answer=_extract_answer_text(response_data,provider)
    choices=(response_data.get('output') or {}).get('choices',[]) if provider=='dashscope_native' else response_data.get('choices',[])
    reason=choices[0].get('finish_reason') if choices else None
    incomplete=response_data.get('incomplete_details') or {}
    if reason in ('length','max_tokens') or response_data.get('status')=='incomplete' or incomplete.get('reason')=='max_output_tokens':
        answer+='\n\n> ⚠ 回答达到模型输出上限，以上内容尚未完整生成。请缩小问题范围后重新提问；这不是页面隐藏了正文。'
    return answer


def _extract_answer_text(response_data: dict[str, Any], provider: str) -> str:
    if provider == "dashscope_native":
        content = response_data["output"]["choices"][0]["message"]["content"]
        if isinstance(content, list):
            return "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict)).strip()
        return str(content)
    if provider == "openai_responses":
        parts: list[str] = []
        for output in response_data.get("output", []):
            if not isinstance(output, dict) or output.get("type") != "message":
                continue
            for content in output.get("content", []):
                if isinstance(content, dict) and content.get("type") == "output_text":
                    parts.append(str(content.get("text", "")))
        answer = "\n".join(part for part in parts if part).strip()
        if not answer:
            raise ValueError("OpenAI 响应中没有可显示的文本。")
        return answer
    content=response_data["choices"][0]["message"].get("content")
    if isinstance(content,list):content='\n'.join(str(item.get('text','')) for item in content if isinstance(item,dict))
    if not isinstance(content,str) or not content.strip():raise ValueError('模型未返回可显示文本，可能只有思考内容或输出额度不足。')
    return content.strip()


def _urllib_completion(endpoint: str, payload: bytes, config: dict[str, Any], provider: str) -> str:
    started = time.monotonic()
    console_event('llm_request', transport='urllib', provider=provider, endpoint=endpoint, request=json.loads(payload))
    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    proxy_url = str(config.get("proxy_url", "")).strip()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    ) if proxy_url else urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=float(config.get("timeout_seconds", 30))) as response:
            response_data = json.loads(response.read().decode("utf-8"))
            from .token_usage import report_usage
            report_usage(response_data,provider,'urllib')
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except OSError:
            body = ""
        raise LLMHTTPError(_describe_http_error(exc.code, body,config.get('base_url',''))) from exc
    console_event('llm_response', transport='urllib', provider=provider,
                  duration_ms=round((time.monotonic()-started)*1000, 1),
                  response=response_data, usage=response_data.get('usage'))
    return _extract_answer(response_data, provider)


def _curl_completion(endpoint: str, payload: bytes, config: dict[str, Any], provider: str) -> str:
    started = time.monotonic()
    proxy_url = str(config.get("proxy_url", "")).strip()
    timeout = str(int(float(config.get("timeout_seconds", 30))))
    config_path = ""
    payload_path = ""
    try:
        with NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".curl") as handle:
            handle.write('header = "Content-Type: application/json"\n')
            handle.write('header = "Accept: application/json"\n')
            handle.write(f'header = "Authorization: Bearer {config["api_key"]}"\n')
            config_path = handle.name
        with NamedTemporaryFile("wb", delete=False, suffix=".json") as handle:
            handle.write(payload)
            payload_path = handle.name
        command = [
            "curl.exe",
            "--silent",
            "--show-error",
            "--fail-with-body",
            "--connect-timeout",
            timeout,
            "--max-time",
            timeout,
            "--config",
            config_path,
            "--data-binary",
            f"@{payload_path}",
            endpoint,
        ]
        if proxy_url:
            command[1:1] = ["--proxy", proxy_url]
        console_event('llm_request', transport='curl', provider=provider, endpoint=endpoint,
                      proxy_configured=bool(proxy_url), request=json.loads(payload))
        completed = subprocess.run(command, capture_output=True, text=True, timeout=float(timeout) + 5)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise OSError(detail or f"curl 退出码 {completed.returncode}")
        response_data = json.loads(completed.stdout)
        from .token_usage import report_usage
        report_usage(response_data,provider,'curl')
        console_event('llm_response', transport='curl', provider=provider,
                      duration_ms=round((time.monotonic()-started)*1000, 1),
                      response=response_data, usage=response_data.get('usage'))
        return _extract_answer(response_data, provider)
    finally:
        for path in (config_path, payload_path):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass


def _safe_config_summary(config: dict[str, Any]) -> dict[str, Any]:
    api_key = str(config.get("api_key", ""))
    return {
        "enabled": bool(config.get("enabled")),
        "base_url": str(config.get("base_url", "")),
        "model": str(config.get("model", "")),
        "provider_hint": _provider_hint(str(config.get("base_url", "")).rstrip("/")),
        "proxy_configured": bool(str(config.get("proxy_url", "")).strip()),
        "api_key_configured": bool(api_key),
        "api_key_length": len(api_key),
        "api_key_env": str(config.get("api_key_env", "")),
    }


def _short_error(exc: object) -> str:
    text = str(exc)
    return text if len(text) <= 900 else f"{text[:900]}..."


def _provider_hint(base_url: str) -> str:
    if _is_dashscope_native(base_url):
        return "dashscope_native"
    if _is_openai_official(base_url):
        return "openai_responses"
    return "openai_compatible"


def _describe_http_error(status: int, body: str, base_url: str = '') -> str:
    detail = ""
    try:
        payload = json.loads(body)
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message", "")).strip()
            elif error:
                detail = str(error).strip()
    except json.JSONDecodeError:
        detail = ""
    host=(urllib.parse.urlsplit(base_url).hostname or '').lower()
    provider='阿里云百炼/千问' if host.endswith('.aliyuncs.com') else 'OpenAI' if host=='api.openai.com' else '当前模型服务商'
    hints = {
        401: f"API Key 无效或已失效，请检查{provider}对应的密钥和接口地址。",
        403: "当前 API Key、项目或地区无权访问该接口。",
        404: "接口地址或模型名称不存在。",
        429: "请求额度、余额或速率限制已达到上限。",
    }
    message = hints.get(status, "大模型服务拒绝了请求。")
    return f"HTTP {status}：{message}" + (f" 服务详情：{detail}" if detail else "")


def _log_event(settings: Settings, event: str, details: dict[str, Any]) -> None:
    entry = console_event(event, component='llm', **details)
    path = settings.runtime / "agents" / "llm" / "logs" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
