"""Chat onboarding sessions for the UI: one Onboarding per company being added."""
from __future__ import annotations
import logging
import os
from pathlib import Path
import threading
from uuid import uuid4
from profile.db.store import Store, SharedEmbedder
from profile.onboarding import Onboarding
from profile.service import publish
from profile.trace import ProfileTrace, TracedClient, TracedStore
from utils.config import settings
from utils.llm import make_client

log = logging.getLogger('alfa.gui')
# In a container the code directory is read-only for the service user: GUI_RUNS_DIR points at a volume.
RUNS = Path(os.getenv('GUI_RUNS_DIR') or Path(__file__).resolve().parent/'runs')
DEFAULT = object()


class Controller:
    def __init__(self, store=None, client_factory=None, runs_dir=RUNS, embedder=DEFAULT, parse=None):
        self.store = store or Store()
        self.client_factory = client_factory or (lambda: make_client(settings()))
        # None — files are searched by words only (tests, no model at hand).
        self.embedder = SharedEmbedder() if embedder is DEFAULT else embedder
        self.parse = parse
        self.runs_dir = Path(runs_dir)
        self.sessions: dict[str, Onboarding] = {}
        self.lock = threading.RLock()
        self.library_loading = False

    def prepare_library(self):
        """A fresh deployment has no company library: load appendix 1 in the background."""
        from profile.db.prepare import ensure_companies

        def run():
            try:
                log.info('Проверяю библиотеку компаний приложения 1')
                if ensure_companies(self.store.dsn):
                    log.info('Библиотека компаний приложения 1 загружена')
                else:
                    log.info('Библиотека компаний уже готова')
            except Exception:  # noqa: BLE001 — без библиотеки чат работает без набора банка
                log.exception('Библиотека компаний не загружена')
            finally:
                self.library_loading = False

        thread = threading.Thread(target=run, daemon=True, name='company-library')
        self.library_loading = True
        thread.start()
        return thread

    def _library(self):
        if self.library_loading:
            raise ValueError('Библиотека компаний загружается — поиск заработает через минуту')
        ready = getattr(self.store, 'ready', None)
        try:
            ok = ready() if ready else self.store.field_labels() is not None
        except Exception:
            ok = False
        if not ok:
            raise ValueError('Библиотека профиля не подготовлена: docker compose run --rm profile-prepare --companies-only')

    def companies(self, query):
        self._library()
        rows = self.store.companies(query, limit=20)
        return [{'id': r.get('SYNTHETIC_U'), 'industry': r.get('MAINOKVED_NAME', ''),
                 'region': r.get('REGION', ''), 'revenue': r.get('REVENUE_COMPANY', '')} for r in rows]

    def create(self):
        session_id = str(uuid4())
        run_dir = self.runs_dir/session_id
        run_dir.mkdir(parents=True, mode=0o700)
        trace = ProfileTrace(run_dir/'trace.jsonl')
        session = Onboarding(TracedStore(self.store, trace), lambda: TracedClient(self.client_factory(), trace),
                             run_dir, trace=trace, embedder=self.embedder, parse=self.parse)
        with self.lock:
            self.sessions[session_id] = session
        return session

    def get(self, session_id):
        with self.lock:
            if session_id not in self.sessions:
                raise KeyError('Сессия не найдена; начните заново')
            return self.sessions[session_id]

    @staticmethod
    def publish(payload):
        return publish(payload)

    def close(self):
        pass
