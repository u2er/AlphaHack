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
import time
import httpx

log = logging.getLogger('alfa.gui.insights')
FRAGMENTS = 2
GRAPH_CACHE_SECONDS = 60
CONNECTOR_ACTIONS = {'run': 'run', 'test': 'test', 'pause': 'pause', 'resume': 'resume', 'reset': 'state/reset'}
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

    def _call(self, method, path, params=None, body=None, timeout=20):
        if not self.api_key:
            raise InsightsError('API_KEY не задан: GUI не может обратиться к EventGraph')
        try:
            with httpx.Client(base_url=self.base_url, timeout=timeout, transport=self.transport) as client:
                response = client.request(method, path, params=params, json=body, headers={'X-Api-Key': self.api_key})
        except httpx.HTTPError:
            raise InsightsError(f'EventGraph недоступен по адресу {self.base_url}') from None
        if response.status_code == 404:
            raise KeyError('Не найдено в EventGraph')
        if response.status_code == 422:
            detail = response.json().get('detail') if 'json' in response.headers.get('content-type', '') else ''
            raise ValueError(detail if isinstance(detail, str) else 'EventGraph отклонил запрос')
        if response.status_code not in (200, 201, 202):
            raise InsightsError(f'EventGraph ответил HTTP {response.status_code}')
        return response.json()

    def _get(self, path, **params):
        return self._call('GET', path, params=params)

    # ── коннекторы ──────────────────────────────────────────────────────────
    def connectors(self):
        return [{k: c.get(k) for k in ('name', 'kind', 'description', 'interval_seconds', 'enabled', 'paused',
                                       'next_run_at', 'last_run_at', 'last_status', 'last_error', 'items_count', 'custom')}
                for c in self._get('/connectors')]

    def create_connector(self, kind, title, target, interval_seconds):
        return self._call('POST', '/connectors', body={'kind': kind, 'title': title, 'target': target,
                                                      'interval_seconds': interval_seconds})

    def connector_action(self, name, action):
        if action == 'delete':
            return self._call('DELETE', f'/connectors/{name}')
        if action not in CONNECTOR_ACTIONS:
            raise KeyError('Нет такого действия')
        return self._call('POST', f'/connectors/{name}/{CONNECTOR_ACTIONS[action]}')

    def connector_runs(self, name, limit=10):
        return [{k: r.get(k) for k in ('id', 'kind', 'status', 'items_total', 'items_new', 'items_dup', 'error',
                                       'created_at', 'finished_at', 'log', 'preview')}
                for r in self._get(f'/connectors/{name}/runs', limit=limit)]

    def set_sources(self, profile_id, sources):
        return self._call('PUT', f'/profiles/{profile_id}/sources', body={'sources': list(sources)})

    # ── граф ────────────────────────────────────────────────────────────────
    def _graph_name(self):
        if not getattr(self, '_graph', None):
            self._graph = self._get('/health').get('graph') or 'news'
        return self._graph

    def graph(self):
        """The whole graph without items, cached briefly: 8 thousand elements are a few megabytes."""
        with self._lock:
            cached = getattr(self, '_dump', None)
            if cached and time.monotonic() - cached[0] < GRAPH_CACHE_SECONDS:
                return cached[1]
        dump = self._call('GET', f'/graphs/{self._graph_name()}', timeout=60)
        out = {'graph': dump.get('graph'),
               'nodes': [{'id': n['id'], 'name': n['name'], 'meta': n.get('meta') or {}} for n in dump.get('nodes', [])],
               'edges': [{'id': e['id'], 'name': e['name'], 'source': e.get('source_id'), 'target': e.get('target_id')}
                         for e in dump.get('edges', [])]}
        with self._lock:
            self._dump = (time.monotonic(), out)
        return out

    def graph_element(self, kind, element_id):
        if kind not in ('nodes', 'edges'):
            raise KeyError('kind — nodes или edges')
        return self._get(f'/graphs/{self._graph_name()}/{kind}/{element_id}')

    def graph_search(self, query):
        found = self._call('POST', f'/graphs/{self._graph_name()}/search',
                           body={'query': query, 'limit': 20, 'kinds': ['node']})
        return [{'id': r['element']['id'], 'name': r['element']['name'], 'score': round(r.get('score', 0), 3),
                 'type': (r['element'].get('meta') or r['element'].get('attrs') or {}).get('type')}
                for r in found.get('results', []) if r.get('element')]

    def profiles(self):
        return [p for p in self._get('/profiles') if p.get('active')]

    def publish_profile(self, payload):
        """Persist a confirmed onboarding profile immediately in EventGraph."""
        return self._call('PUT', '/profiles', body=payload)

    def profile_edit_proposal(self, profile_id, instruction):
        return self._call('POST', f'/profiles/{profile_id}/edit-proposal',
                          body={'instruction': instruction}, timeout=600)

    def save_profile(self, profile_id, payload):
        if payload.get('profile_id') != profile_id:
            raise ValueError('Идентификатор профиля в черновике не совпадает')
        return self.publish_profile(payload)

    def summary(self, profile_id, min_importance, direction=''):
        return self._call('GET', f'/ai/summaries/{profile_id}',
                          params={'min_importance': min_importance, 'direction': direction})

    def create_summary(self, profile_id, min_importance, direction=''):
        return self._call('POST', f'/ai/summaries/{profile_id}',
                          body={'min_importance': min_importance, 'direction': direction}, timeout=600)

    def chats(self, profile_id):
        return self._call('GET', f'/ai/chats/{profile_id}')

    def chat(self, profile_id, item_uid):
        return self._call('GET', f'/ai/chats/{profile_id}/{item_uid}')

    def send_chat(self, profile_id, item_uid, message):
        return self._call('POST', f'/ai/chats/{profile_id}/{item_uid}',
                          body={'message': message}, timeout=1800)

    def graph_chat(self, message, history):
        return self._call('POST', '/ai/graph-chat',
                          body={'message': message, 'history': history}, timeout=600)

    def profile(self, profile_id):
        """Name, role, the main page and the table of contents of a paged profile."""
        from profile.contract import load_profile, table_of_contents
        data = self._get(f'/profiles/{profile_id}')
        doc = load_profile(data.get('payload') or {})
        return {'id': data['id'], 'name': data['name'], 'company_id': data.get('company_id'),
                'updated_at': data.get('updated_at'), 'user_role': doc.user_role,
                'main': doc.pages[0].content, 'toc': table_of_contents(doc), 'news_sources': doc.news_sources,
                'files': [f.model_dump() for f in doc.files]}

    def page(self, profile_id, page_id, offset=0):
        return self._get(f'/profiles/{profile_id}/pages/{page_id}', offset=max(0, offset), limit=20000)

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

    def event_importance(self, profile_id):
        return self._get(f'/profiles/{profile_id}/event-importance')

    def feed(self, profile_id, min_importance=1, limit=30):
        items = self._get(f'/profiles/{profile_id}/feed', min_importance=min_importance, limit=limit)
        return [item | {'bank': self._bank_fragments(item)} for item in items]
