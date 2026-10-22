"""Business-insights data for the UI: EventGraph client feeds plus the bank's analytics.

The browser never sees the EventGraph key: the GUI server calls EventGraph
(EVENTGRAPH_URL, API_KEY) and adds, for every feed item, the closest fragments
of the bank's library (profile_data, the same search the profile agent uses).
The analyst's text is AI interpretation; fragments are the bank's own materials,
and the UI keeps the two apart."""
from __future__ import annotations
import logging
import os
import threading
import httpx

log = logging.getLogger('alfa.gui.insights')
FRAGMENTS = 2
MIN_SCORE = 0.3


class InsightsError(RuntimeError):
    """EventGraph is unreachable or refused the request; the message is safe to show."""


class Insights:
    def __init__(self, store=None, base_url=None, api_key=None, transport=None):
        self.base_url = (base_url or os.getenv('EVENTGRAPH_URL') or 'http://127.0.0.1:8000').rstrip('/')
        self.api_key = api_key if api_key is not None else os.getenv('API_KEY', '')
        self.store = store
        self.transport = transport
        self._fragments: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def _get(self, path, **params):
        if not self.api_key:
            raise InsightsError('API_KEY не задан: GUI не может обратиться к EventGraph')
        try:
            with httpx.Client(base_url=self.base_url, timeout=20, transport=self.transport) as client:
                response = client.get(path, params=params, headers={'X-Api-Key': self.api_key})
        except httpx.HTTPError:
            raise InsightsError(f'EventGraph недоступен по адресу {self.base_url}') from None
        if response.status_code == 404:
            raise KeyError('Профиль не найден в EventGraph')
        if response.status_code != 200:
            raise InsightsError(f'EventGraph ответил HTTP {response.status_code}')
        return response.json()

    def profiles(self):
        return [p for p in self._get('/profiles') if p.get('active')]

    def profile(self, profile_id):
        """Name, confirmed topics with their bank sources, and the user's role."""
        data = self._get(f'/profiles/{profile_id}')
        payload = data.get('payload') or {}
        evidence = payload.get('evidence') or {}
        topics = []
        for topic in (payload.get('monitoring_context') or {}).get('topics', []):
            sources = [{'filename': evidence[s].get('filename'), 'page': evidence[s].get('page'),
                        'date_hint': evidence[s].get('date_hint')} for s in topic.get('source_ids', []) if s in evidence]
            topics.append({'title': topic.get('title'), 'rationale': topic.get('rationale'), 'sources': sources})
        role = (payload.get('user_context') or {}).get('user_role', {}).get('value')
        return {'id': data['id'], 'name': data['name'], 'company_id': data.get('company_id'),
                'updated_at': data.get('updated_at'), 'topics': topics, 'user_role': role,
                'summary': (payload.get('draft_overview') or {}).get('summary')}

    def _bank_fragments(self, item):
        """Closest library fragments to the event: the bank's own analytics, not AI text."""
        if self.store is None:
            return []
        with self._lock:
            if item['uid'] in self._fragments:
                return self._fragments[item['uid']]
        analysis = item.get('analysis') or {}
        query = ' '.join(filter(None, [item.get('title'), analysis.get('headline')])) or item.get('event') or ''
        try:
            hits = self.store.search(query, limit=FRAGMENTS + 2) if query.strip() else []
        except Exception as exc:  # noqa: BLE001 — the feed works without the library
            log.warning('Библиотека аналитики недоступна: %s', str(exc)[:200])
            return []
        fragments, seen = [], set()
        for hit in hits:
            if hit.get('score', 0) < MIN_SCORE or (hit.get('filename'), hit.get('page')) in seen:
                continue
            seen.add((hit.get('filename'), hit.get('page')))
            fragments.append({'filename': hit.get('filename'), 'page': hit.get('page'),
                              'date_hint': hit.get('date_hint'), 'score': round(hit.get('score', 0), 2),
                              'text': ' '.join((hit.get('text') or '').replace('[IMAGE]', ' ').split())[:600]})
            if len(fragments) == FRAGMENTS:
                break
        with self._lock:
            self._fragments[item['uid']] = fragments
        return fragments

    def feed(self, profile_id, min_importance=1, limit=30):
        items = self._get(f'/profiles/{profile_id}/feed', min_importance=min_importance, limit=limit)
        return [item | {'bank': self._bank_fragments(item)} for item in items]
