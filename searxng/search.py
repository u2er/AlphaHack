"""Search providers discover URLs; their snippets are never treated as documents."""
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import logging
import ssl
from time import perf_counter
import httpx
from .models import SearchTask, SearchHit, SearchBatch

log = logging.getLogger("alfa.search")


class SearchError(RuntimeError):
    pass


def canonical_url(url: str) -> str:
    p = urlsplit(url)
    if p.scheme not in {"https", "http"} or not p.hostname or p.username or p.password:
        raise ValueError("Invalid article URL")
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k not in {"fbclid", "gclid"}]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", urlencode(sorted(query)), ""))


def allowed_domain(url: str, domains: list[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return not domains or any(host == d.lower() or host.endswith("." + d.lower()) for d in domains)


class SearchProvider(Protocol):
    def search(self, task: SearchTask, limit: int = 8) -> SearchBatch: ...


class SearXNGProvider:
    def __init__(self, base_url="http://127.0.0.1:8080", client=None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=25, verify=False, follow_redirects=False)

    def close(self):
        self.client.close()

    def search(self, task: SearchTask, limit: int = 8) -> SearchBatch:
        started = perf_counter()
        params = {"q": task.query, "format": "json", "language": "ru",
                  "categories": task.category, "pageno": 1}
        if task.time_range:
            params["time_range"] = task.time_range
        try:
            response = self.client.get(self.base_url + "/search", params=params)
            if response.status_code != 200:
                raise SearchError(f"SearXNG HTTP {response.status_code}")
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ValueError()
        except httpx.HTTPError:
            raise SearchError("SearXNG network error") from None
        except ValueError:
            raise SearchError("SearXNG invalid JSON; enable search.formats: [html, json]") from None
        eligible, seen = [], set()
        invalid = duplicates = outside_domains = 0
        for row in data["results"]:
            try:
                url = canonical_url(row["url"])
                hit = SearchHit(url=url, title=str(row.get("title", "")),
                                snippet=str(row.get("content", "")),
                                published_hint=str(row["publishedDate"]) if row.get("publishedDate") else None,
                                engines=[str(e) for e in row.get("engines", [])])
            except (KeyError, TypeError, ValueError):
                invalid += 1
                continue
            if url in seen:
                duplicates += 1
                continue
            seen.add(url)
            if not allowed_domain(url, task.domains):
                outside_domains += 1
                continue
            eligible.append(hit)
        hits = eligible[:limit]
        warnings = ["Some upstream engines failed: " + str(data["unresponsive_engines"])] if data.get("unresponsive_engines") else []
        log.debug("SearXNG: получено=%d, подходящих=%d, отдано=%d (лимит=%d); "
                  "некорректных=%d, повторов=%d, вне доменов=%d; %.2f с",
                  len(data["results"]), len(eligible), len(hits), limit,
                  invalid, duplicates, outside_domains, perf_counter() - started)
        return SearchBatch(hits=hits, warnings=warnings)
