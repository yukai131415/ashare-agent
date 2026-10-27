"""Bounded provider-neutral context manifest; never interpret characters as tokens."""
from .persistence import fingerprint
import json


def request_manifest(messages,tools,maximum=100000):
    payload={'messages':messages,'tools':tools}
    size=len(json.dumps(payload,ensure_ascii=False,separators=(',',':')))
    # 字符上限只用于上下文保护，不是 Token 计费；直接截断会破坏工具 schema/JSON。
    if size>maximum:raise ValueError(f'模型上下文过大：{size}字符，上限{maximum}；请减少证据摘要或按需查询，不截断JSON')
    return {'request_hash':fingerprint(payload),'input_characters':size,'measurement':'characters_not_tokens'}
