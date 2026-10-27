"""A small, polite HTTP JSON client: one request at a time per host, retries, and an on-disk cache.

The cache keeps every response used to build a packet, so a build can be re-run
offline and audited later. Tests pass their own `transport` instead of the network.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

USER_AGENT = "LabNetwork/0.1 (local research-network notebook; https://github.com/)"

# Seconds between requests to the same host (the services' published etiquette, rounded up).
MIN_INTERVAL = {
    "api.openalex.org": 0.12,
    "api.reporter.nih.gov": 1.05,
    "pub.orcid.org": 0.25,
    "api.nsf.gov": 0.5,
    "slack.com": 1.2,
}

Transport = Callable[[str, str, Optional[bytes], Dict[str, str]], Any]

# Hosts that report a per-request cost against a free daily allowance.
METERED = {"api.openalex.org"}


def _cache_url(url: str) -> str:
    """The URL without an api_key parameter, so keys never reach the cache files,
    error messages or the record of sources."""
    parts = urllib.parse.urlsplit(url)
    if "api_key=" not in parts.query:
        return url
    query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                                    if k != "api_key"], safe=":|,/")
    return urllib.parse.urlunsplit(parts._replace(query=query))


class HttpError(Exception):
    def __init__(self, url: str, status: int, message: str):
        url = _cache_url(url)              # errors end up in run records and logs
        super().__init__(f"{status} from {url}: {message}")
        self.url = url
        self.status = status


def _real_transport(method: str, url: str, body: Optional[bytes], headers: Dict[str, str]) -> Any:
    """(data, response headers). Injected test transports may return the data alone."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8")), {k.lower(): v for k, v in resp.headers.items()}


class BudgetReached(HttpError):
    """Raised before a request that would go past the run's spending limit."""

    def __init__(self, url: str, status: int, message: str):
        super().__init__(url, status, message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class Client:
    def __init__(self, cache_dir: Optional[str] = None, mailto: str = "", transport: Optional[Transport] = None,
                 offline: bool = False, log: Callable[[str], None] = lambda m: None,
                 budget_usd: Optional[float] = None):
        self.cache_dir = cache_dir
        self.mailto = mailto
        self.transport = transport or _real_transport
        self.throttle = transport is None   # injected transports (tests) need no politeness delay
        self.offline = offline
        self.log = log
        self.last: Dict[str, float] = {}
        self.requests = 0
        self.cache_hits = 0
        self.sources: list = []
        # OpenAlex reports what each request costs against its free daily allowance (meta.cost_usd), and what is
        # left of today's allowance (x-ratelimit-remaining-usd).
        self.cost_usd = 0.0
        self.budget_usd = budget_usd
        self.remaining_usd: Optional[float] = None
        self.reserve_usd = 0.002      # stop before the day's allowance runs out completely
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    def _cache_path(self, key: str) -> Optional[str]:
        if not self.cache_dir:
            return None
        return os.path.join(self.cache_dir, hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".json")

    def has_cached(self, url: str, method: str = "GET") -> bool:
        path = self._cache_path(method + " " + _cache_url(url))
        return bool(path) and os.path.exists(path)

    def get(self, url: str, params: Optional[Dict[str, Any]] = None, use_cache: bool = True,
            headers: Optional[Dict[str, str]] = None) -> Any:
        """`headers` carry credentials such as an Authorization header; they never reach the cache or the logs."""
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, safe=":|,/")
        return self._request("GET", url, None, use_cache, headers)

    def post(self, url: str, payload: Dict[str, Any], use_cache: bool = True) -> Any:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        return self._request("POST", url, body, use_cache)

    def _request(self, method: str, url: str, body: Optional[bytes], use_cache: bool,
                 extra_headers: Optional[Dict[str, str]] = None) -> Any:
        key = method + " " + _cache_url(url) + (" " + body.decode("utf-8") if body else "")
        path = self._cache_path(key)
        if use_cache and path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            self.cache_hits += 1
            self.sources.append({"url": _cache_url(url), "method": method, "fetchedAt": cached.get("fetchedAt"),
                                 "cached": True})
            return cached["data"]
        if self.offline:
            raise HttpError(url, 0, "offline mode and no cached response")
        host = urllib.parse.urlsplit(url).netloc
        if host in METERED:
            if self.budget_usd is not None and self.cost_usd >= self.budget_usd:
                raise BudgetReached(url, 0, f"stopped at ${self.cost_usd:.4f} to stay within the ${self.budget_usd:.2f} limit")
            if self.remaining_usd is not None and self.remaining_usd < self.reserve_usd:
                raise BudgetReached(url, 0, f"today's OpenAlex allowance is used up (${self.remaining_usd:.4f} left)")
        headers = {"User-Agent": USER_AGENT + (f" mailto:{self.mailto}" if self.mailto else ""),
                   "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        delay = 1.0
        for attempt in range(5):
            wait = MIN_INTERVAL.get(host, 0.2) - (time.time() - self.last.get(host, 0))
            if wait > 0 and self.throttle:
                time.sleep(wait)
            self.last[host] = time.time()
            try:
                result = self.transport(method, url, body, headers)
                data, resp_headers = result if isinstance(result, tuple) and len(result) == 2 else (result, {})
                left = (resp_headers or {}).get("x-ratelimit-remaining-usd")
                if left is not None:
                    try:
                        self.remaining_usd = float(left)
                    except ValueError:
                        pass
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and host in METERED and (exc.headers or {}).get("x-ratelimit-remaining-usd") in ("0", "0.0"):
                    self.remaining_usd = 0.0
                    raise BudgetReached(url, 429, "today's OpenAlex allowance is used up")
                if exc.code in (429, 500, 502, 503, 504) and attempt < 4:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else delay)
                    delay *= 2
                    continue
                raise HttpError(url, exc.code, exc.reason if isinstance(exc.reason, str) else str(exc.reason))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt < 4:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise HttpError(url, 0, str(exc))
        self.requests += 1
        if isinstance(data, dict):
            cost = (data.get("meta") or {}).get("cost_usd") if isinstance(data.get("meta"), dict) else None
            if isinstance(cost, (int, float)):
                self.cost_usd += float(cost)
        fetched = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.sources.append({"url": _cache_url(url), "method": method, "fetchedAt": fetched, "cached": False})
        if path:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"key": key, "fetchedAt": fetched, "data": data}, f, ensure_ascii=False)
            os.replace(tmp, path)
        return data
