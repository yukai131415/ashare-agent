"""Deterministic error classification and validated policy, outside model control."""
import re
import urllib.error
from pydantic import BaseModel,ConfigDict,Field,field_validator
from ..errors import SourceBlockedError


class RecoveryPolicy(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    max_worker_retries:int=Field(default=2,ge=0,le=2)
    max_run_recoveries:int=Field(default=6,ge=0,le=20)
    backoff_seconds:float=Field(default=1,ge=0,le=30)
    retry_http_statuses:list[int]=Field(default_factory=lambda:[408,429,500,502,503,504])

    @field_validator('retry_http_statuses')
    @classmethod
    def validate_statuses(cls,value):
        if not set(value)<={408,429,500,502,503,504}:raise ValueError('不能把授权或业务错误加入自动恢复白名单')
        return sorted(set(value))

    def can_retry(self,exc):
        classification=classify_error(exc)
        if classification['category']=='http':return classification['http_status'] in set(self.retry_http_statuses)&{408,429,500,502,503,504}
        return classification['category'] in ('timeout','connection')


def classify_error(exc):
    # 沿显式 cause 找原始网络错误；seen 防止异常链意外成环。
    # 未识别错误默认不可重试，不能让模型凭错误文本自行扩大白名单。
    seen=set();current=exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current,urllib.error.HTTPError):return {'category':'http','http_status':current.code}
        # Wrapped public-source errors retain explicit status, never infer all unknown errors retryable.
        if isinstance(current,SourceBlockedError):
            match=re.search(r'(?i)\bHTTP\s*[:=]?\s*(\d{3})\b',str(current))
            if match:return {'category':'http','http_status':int(match[1])}
        if isinstance(current,TimeoutError):return {'category':'timeout','http_status':None}
        if isinstance(current,ConnectionError):return {'category':'connection','http_status':None}
        if isinstance(current,urllib.error.URLError):
            if isinstance(current.reason,TimeoutError):return {'category':'timeout','http_status':None}
            return {'category':'connection','http_status':None}
        current=current.__cause__
    return {'category':'non_retryable','http_status':None}


def policy(settings):
    # 旧 supervisor 配置作为默认值，harness.recovery 显式配置覆盖后再统一校验。
    old=settings.values.get('supervisor',{})
    return RecoveryPolicy.model_validate({'max_worker_retries':min(2,max(0,int(old.get('max_retries',2)))),
        'backoff_seconds':old.get('retry_backoff_seconds',1),**settings.values.get('harness',{}).get('recovery',{})})


def transient(exc):
    return RecoveryPolicy().can_retry(exc)
