"""Read-only profile and pgvector retrieval API."""
from __future__ import annotations

from contextlib import contextmanager
import logging
import numpy as np
from .connection import connect, database_url

log = logging.getLogger('alfa.profile.db')


class SharedEmbedder:
    """The EventGraph embedder (EMB_MODEL / EMB_BASE_URL): one model for the graph and the library."""

    def __init__(self, chunk_chars=None):
        self._chunk_chars = chunk_chars

    @property
    def name(self):
        from eventgraph import embed
        return embed.model_name()

    @property
    def chunk_chars(self):
        if self._chunk_chars:
            return self._chunk_chars
        from eventgraph.config import settings
        # E5 reads 512 tokens; most sentence-transformers models stop at 128.
        return 1500 if 'e5' in settings.emb_model.lower() else 500

    def split(self, text):
        """Overlapping windows cut at whitespace so a fragment fits the model's input."""
        size, text = self.chunk_chars, ' '.join(text.split())
        step, chunks, start = size - size // 5, [], 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text) and (cut := text.rfind(' ', start + step // 2, end)) > start:
                end = cut
            chunks.append(text[start:end].strip())
            if end == len(text):
                break
            start = max(end - size // 5, start + 1)
        return [chunk for chunk in chunks if chunk]

    def encode(self, texts, query=False):
        from eventgraph import embed
        return np.asarray(embed.embed_sync(list(texts), passage=not query), dtype='<f4')


class Store:
    def __init__(self, dsn=None, embedder=None):
        self.dsn = database_url(dsn)
        self.embedder = embedder or SharedEmbedder()
        self._index_checked = False

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

    def _check_index(self, c):
        """Vectors of another model are not comparable: refuse instead of returning noise."""
        name = getattr(self.embedder, 'name', None)
        if self._index_checked or not name:
            return
        row = c.execute("SELECT value FROM profile_data.metadata WHERE key='embedding_model'", ()).fetchone()
        if row and row['value'] != name:
            raise ValueError(f'Library was indexed with {row["value"]}, the embedder is {name}: '
                             'rerun python -m profile.db.prepare --replace')
        self._index_checked = True

    def search(self, query, limit=5):
        if not query.strip():
            return []
        vector = self.embedder.encode([query], query=True)[0]
        limit = min(max(int(limit), 1), 10)
        with self.connection(vector=True) as c:
            self._check_index(c)
            rows = c.execute('''SELECT ch.id,ch.text,ch.page,d.filename,d.sha256,d.date_hint,d.extraction,
                1 - (ch.embedding <=> %s) AS score
                FROM profile_data.chunks ch JOIN profile_data.documents d ON ch.document_id=d.id
                ORDER BY ch.embedding <=> %s, ch.id LIMIT %s''', (vector, vector, limit)).fetchall()
        for item in rows:
            item['score'] = float(item['score'])
            item['date_warning'] = 'Дата извлечена автоматически; проверьте её в документе. Материал не подтверждает текущую ситуацию.'
        log.info('Поиск в библиотеке: возвращено %d фрагментов', len(rows))
        return rows
