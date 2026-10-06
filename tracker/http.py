"""Small HTTP client: rate limiting, retries with backoff, a per-run request budget.

Standard library only so the GitHub Actions job needs no installs. A fake
`transport` can be injected for tests.
"""
from __future__ import annotations

import http.client
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request


def _redact(url: str) -> str:
    return re.sub(r"(key|token|apikey)=[^&]+", r"\1=REDACTED", url, flags=re.I)


class HttpError(Exception):
    def __init__(self, url: str, status: int, body: str = ""):
        url = _redact(url)
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.url = url
        self.status = status
        self.body = body


class BudgetExceeded(Exception):
    """Request budget or wall-clock deadline reached; callers stop and save what they have."""


def _urllib_transport(url: str, headers: dict, timeout: float):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() or b""


class Http:
    def __init__(self, user_agent: str, interval: float = 0.5, budget: int = 3000,
                 transport=None, sleep=time.sleep, timeout: float = 30.0, max_retries: int = 4,
                 deadline_sec: float | None = None):
        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self.interval = interval
        self.budget = budget
        self.transport = transport or _urllib_transport
        self.sleep = sleep
        self.timeout = timeout
        self.max_retries = max_retries
        self.deadline = time.monotonic() + deadline_sec if deadline_sec else None
        self.count = 0
        self._last = 0.0

    def out_of_time(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def _wait_turn(self):
        gap = time.monotonic() - self._last
        if gap < self.interval:
            self.sleep(self.interval - gap)
        self._last = time.monotonic()

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None):
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(clean)}"
        hdrs = dict(self.headers)
        if headers:
            hdrs.update(headers)
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            if self.count >= self.budget:
                raise BudgetExceeded(f"request budget of {self.budget} used up")
            if self.out_of_time():
                raise BudgetExceeded("run deadline reached")
            self._wait_turn()
            self.count += 1
            try:
                status, resp_headers, body = self.transport(url, hdrs, self.timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as e:
                status, resp_headers, body = 0, {}, str(e).encode()
            if 200 <= status < 300:
                try:
                    return json.loads(body.decode("utf-8") or "null")
                except ValueError as e:
                    raise HttpError(url, status, f"invalid JSON: {e}") from e
            retryable = status in (0, 429, 500, 502, 503, 504)
            if not retryable or attempt == self.max_retries:
                raise HttpError(url, status, body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body))
            retry_after = None
            for k, v in (resp_headers or {}).items():
                if k.lower() == "retry-after":
                    try:
                        retry_after = float(v)
                    except ValueError:
                        pass
            wait = retry_after if retry_after is not None else delay + random.uniform(0, delay / 2)
            self.sleep(min(wait, 30.0))
            delay = min(delay * 2, 30.0)
        raise HttpError(url, -1, "unreachable")
