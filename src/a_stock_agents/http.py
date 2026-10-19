from __future__ import annotations

import json
import http.client
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .errors import SourceBlockedError


@dataclass(slots=True)
class HttpClient:
    timeout: float
    user_agent: str
    interval: float = 0.3
    default_headers: dict[str, str] = field(default_factory=dict)
    _last_request: float = 0.0

    def _wait(self) -> None:
        remaining = self.interval - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)

    def get_bytes(self, url: str, params: dict[str, Any] | None = None) -> bytes:
        self._wait()
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        if "eastmoney.com" in url:
            referer = "https://quote.eastmoney.com/center/gridlist.html"
        elif "gtimg.cn" in url or "linkdiary.cn" in url:
            referer = "https://stock.qq.com/"
        elif "sina.com.cn" in url:
            referer = "https://finance.sina.com.cn/"
        else:
            referer = "https://www.baidu.com/"
        headers = {
            "User-Agent": self.user_agent,
            "Referer": referer,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Connection": "close",
            **self.default_headers,
        }
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                if response.status in (403, 429):
                    raise SourceBlockedError(f"数据源返回 HTTP {response.status}: {url}")
                return payload
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429):
                raise SourceBlockedError(f"数据源拒绝访问 HTTP {exc.code}: {url}") from exc
            raise SourceBlockedError(f"上游 HTTP {exc.code}: {url}") from exc
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", str(exc))
            raise SourceBlockedError(f"无法连接数据源 {url}: {reason}") from exc
        finally:
            self._last_request = time.monotonic()

    def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        payload = self.get_bytes(url, params)
        for encoding in ("utf-8", "gb18030"):
            try:
                return payload.decode(encoding)
            except UnicodeDecodeError:
                continue
        return payload.decode("utf-8", errors="replace")

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return json.loads(self.get_text(url, params))
