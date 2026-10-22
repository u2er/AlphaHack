"""Read-only profile and pgvector retrieval API."""
from __future__ import annotations

from contextlib import contextmanager
import logging
from pathlib import Path
import numpy as np
from .connection import ROOT, connect, database_url

DEFAULT_MODEL = ROOT / 'models' / 'multilingual-e5-large'
log = logging.getLogger('alfa.profile.db')


class E5:
    def __init__(self, path=DEFAULT_MODEL):
        self.path = Path(path)
        self._model = None

    @property
    def model(self):
        if self._model is None:
            if not self.path.is_dir():
                raise FileNotFoundError('Local E5 model is missing')
            import torch
            from sentence_transformers import SentenceTransformer
            torch.set_num_threads(4)
            self._model = SentenceTransformer(str(self.path), local_files_only=True,
                                             trust_remote_code=False, device='cpu')
        return self._model

    def split(self, text):
        offsets = self.model.tokenizer(text, add_special_tokens=False,
                                      return_offsets_mapping=True)['offset_mapping']
        return [text[offsets[i][0]:offsets[min(i + 440, len(offsets)) - 1][1]]
                for i in range(0, len(offsets), 380)]

    def encode(self, texts, query=False):
        prefix = 'query: ' if query else 'passage: '
        return np.asarray(self.model.encode([prefix + t for t in texts], batch_size=8,
                          normalize_embeddings=True, show_progress_bar=False), dtype='<f4')


class Store:
    def __init__(self, dsn=None, embedder=None):
        self.dsn = database_url(dsn)
        self.embedder = embedder or E5()

    @contextmanager
    def connection(self, *, vector=False):
        with connect(self.dsn, vector=vector) as connection:
            yield connection

    def companies(self, query='', limit=20):
        limit = min(max(int(limit), 1), 100)
        escaped = query.replace('!', '!!').replace('%', '!%').replace('_', '!_')
        pattern = f'%{escaped}%'
        with self.connection() as c:
            rows = c.execute('''SELECT payload FROM profile_data.companies
                WHERE id ILIKE %s ESCAPE '!' OR industry ILIKE %s ESCAPE '!'
                OR region ILIKE %s ESCAPE '!'
                ORDER BY (id = %s) DESC, id LIMIT %s''',
                (pattern, pattern, pattern, query, limit)).fetchall()
        return [row['payload'] for row in rows]

    def field_labels(self):
        with self.connection() as c:
            rows = c.execute('''SELECT field_key,label,description,section,sort_order
                FROM profile_data.field_labels ORDER BY sort_order''').fetchall()
        return {row['field_key']: row for row in rows}

    def field_value_labels(self):
        with self.connection() as c:
            rows = c.execute('''SELECT field_key,raw_value,label FROM profile_data.field_value_labels
                ORDER BY field_key,raw_value''').fetchall()
        result = {}
        for row in rows:
            result.setdefault(row['field_key'], {})[row['raw_value']] = row['label']
        return result

    def company(self, company_id):
        with self.connection() as c:
            row = c.execute('SELECT payload FROM profile_data.companies WHERE id=%s', (company_id,)).fetchone()
        if row is None:
            raise ValueError('Company not found')
        return row['payload']

    def signals(self, company_id, limit=10):
        limit = min(max(int(limit), 1), 100)
        with self.connection() as c:
            payments = c.execute('''SELECT payer,beneficiary,SUM(amount)::double precision AS amount,
                SUM(payment_count)::bigint AS count FROM profile_data.payments
                WHERE payer=%s OR beneficiary=%s GROUP BY payer,beneficiary
                ORDER BY amount DESC LIMIT %s''', (company_id, company_id, limit)).fetchall()
            links = c.execute('''SELECT DISTINCT b.company_id FROM profile_data.person_links a
                JOIN profile_data.person_links b ON a.person_id=b.person_id
                WHERE a.company_id=%s AND b.company_id<>%s
                ORDER BY b.company_id LIMIT %s''', (company_id, company_id, limit)).fetchall()
        return {'payments': payments, 'shared_person_companies': links,
                'status': 'unconfirmed_relationships',
                'limitations': ['Тип отношений и роль физлица неизвестны.',
                                'Период, валюта и назначение платежей не указаны; полнота покрытия неизвестна.',
                                'Суммы не равны выручке или доле зависимости компании.']}

    def search(self, query, limit=5):
        if not query.strip():
            return []
        vector = self.embedder.encode([query], query=True)[0]
        limit = min(max(int(limit), 1), 10)
        with self.connection(vector=True) as c:
            rows = c.execute('''SELECT ch.id,ch.text,ch.page,d.filename,d.sha256,d.date_hint,d.extraction,
                1 - (ch.embedding <=> %s) AS score
                FROM profile_data.chunks ch JOIN profile_data.documents d ON ch.document_id=d.id
                ORDER BY ch.embedding <=> %s, ch.id LIMIT %s''', (vector, vector, limit)).fetchall()
        for item in rows:
            item['score'] = float(item['score'])
            item['date_warning'] = 'Дата извлечена автоматически; проверьте её в документе. Материал не подтверждает текущую ситуацию.'
        log.info('Поиск в библиотеке: возвращено %d фрагментов', len(rows))
        return rows
