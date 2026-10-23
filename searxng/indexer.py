"""SQLite documents/FTS index, optional local multilingual-e5-large vectors.

No connection, database creation, model import or download at module import time.
Vector retrieval is an exact scan for a small local corpus, not an ANN index.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from .models import ParsedDocument, now

log = logging.getLogger("alfa.indexer")


def fingerprint(text):
    return sha256(text.encode("utf-8")).hexdigest()


def split_text(text, size=1200, overlap=150):
    return [text[start:start+size] for start in range(0, len(text), size-overlap) if text[start:start+size].strip()]


def normalize_vector(vector):
    values = [float(x) for x in vector]
    length = math.sqrt(sum(x*x for x in values))
    if not values or not all(math.isfinite(x) for x in values) or not length:
        raise ValueError("Invalid embedding")
    return [x/length for x in values]


class E5Embedder:
    model_id = "intfloat/multilingual-e5-large"

    def __init__(self, local_path):
        self.local_path = Path(local_path)
        self._model = None

    @property
    def key(self):
        return self.model_id + ":" + str(self.local_path.resolve())

    def _load(self):
        if self._model is None:
            if not self.local_path.is_dir():
                raise RuntimeError("E5 is not installed locally. No automatic download is allowed.")
            from sentence_transformers import SentenceTransformer
            log.info("Загрузка E5 из локальной папки %s", self.local_path)
            self._model = SentenceTransformer(str(self.local_path), local_files_only=True, trust_remote_code=False)
            log.info("E5 готова: устройство=%s", self._model.device)
        return self._model

    def split(self, text):
        tokenizer = self._load().tokenizer
        offsets = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True,
                            truncation=False)["offset_mapping"]
        return [text[offsets[i][0]:offsets[min(i+448, len(offsets))-1][1]]
                for i in range(0, len(offsets), 384)]

    def passages(self, texts):
        return self._load().encode(["passage: " + t for t in texts],
                                   normalize_embeddings=True, show_progress_bar=False).tolist()

    def query(self, text):
        return self._load().encode(["query: " + text],
                                   normalize_embeddings=True, show_progress_bar=False)[0].tolist()


class Indexer:
    def __init__(self, path: Path, embedder=None):
        self.path, self.embedder = Path(path), embedder
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, content_hash TEXT UNIQUE, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS urls (url TEXT PRIMARY KEY, document_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY, document_id TEXT, ordinal INTEGER, text TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(id UNINDEXED,text);
            CREATE TABLE IF NOT EXISTS vectors (
                chunk_id TEXT, model TEXT, vector TEXT, PRIMARY KEY(chunk_id, model));
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, payload TEXT);
            CREATE TABLE IF NOT EXISTS trace (
                id INTEGER PRIMARY KEY, run_id TEXT, step TEXT, created_at TEXT, payload TEXT);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, run_id, step, payload):
        with self.db() as db:
            db.execute("INSERT INTO trace(run_id,step,created_at,payload) VALUES (?,?,?,?)",
                       (run_id, step, now(), json.dumps(payload, ensure_ascii=False)))

    def save_run(self, payload):
        with self.db() as db:
            db.execute("INSERT INTO runs VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (payload["id"], json.dumps(payload, ensure_ascii=False)))

    def index(self, document: ParsedDocument):
        content_hash = fingerprint(re.sub(r"\s+", " ", document.text).strip())
        doc_id = content_hash[:24]
        with self.db() as db:
            existing = db.execute("SELECT id FROM documents WHERE content_hash=?", (content_hash,)).fetchone()
            if not existing:
                texts = self.embedder.split(document.text) if self.embedder else split_text(document.text)
                db.execute("INSERT INTO documents VALUES (?,?,?)", (doc_id, content_hash, document.model_dump_json()))
                for i, text in enumerate(texts):
                    chunk_id = f"{doc_id}:{i}"
                    db.execute("INSERT INTO chunks VALUES (?,?,?,?)", (chunk_id, doc_id, i, text))
                    db.execute("INSERT INTO chunks_fts VALUES (?,?)", (chunk_id, text))
            db.execute("INSERT INTO urls VALUES (?,?) ON CONFLICT(url) DO UPDATE SET document_id=excluded.document_id",
                       (document.url, doc_id))
        log.info("Индексация %s: %s, фрагментов=%d", doc_id,
                 "повтор документа" if existing else "новый документ", len(self.chunks([doc_id])))
        if self.embedder:
            self.embed_document(doc_id)
        return doc_id

    def chunks(self, document_ids):
        if not document_ids:
            return []
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM chunks WHERE document_id IN (" + ",".join("?" for _ in document_ids) + ") ORDER BY document_id,ordinal",
                list(document_ids))]

    def embed_document(self, doc_id):
        if not self.embedder:
            raise RuntimeError("No embedder configured")
        # Re-chunk legacy lexical documents before vectorizing? Keep IDs stable instead:
        # tokenize each stored chunk and average its subpassages, preserving the source text.
        added = 0
        for row in self.chunks([doc_id]):
            with self.db() as db:
                if db.execute("SELECT 1 FROM vectors WHERE chunk_id=? AND model=?", (row["id"], self.embedder.key)).fetchone():
                    continue
            parts = self.embedder.split(row["text"])
            vectors = self.embedder.passages(parts)
            vector = normalize_vector([sum(v[i] for v in vectors)/len(vectors) for i in range(len(vectors[0]))])
            with self.db() as db:
                db.execute("INSERT OR REPLACE INTO vectors VALUES (?,?,?)", (row["id"], self.embedder.key, json.dumps(vector)))
            added += 1
            # log.debug("Вектор E5: документ=%s, фрагмент=%s", doc_id, row["id"])
        log.info("E5: документ=%s, новых векторов=%d", doc_id, added)

    def search(self, query, limit=10, document_ids=None):
        terms = re.findall(r"\w+", query.lower(), flags=re.UNICODE)[:32]
        if not terms:
            return []
        match = " OR ".join('"' + t + '"' for t in terms)
        with self.db() as db:
            sql = """SELECT c.*,bm25(chunks_fts) AS score FROM chunks_fts
                JOIN chunks c ON c.id=chunks_fts.id WHERE chunks_fts MATCH ?"""
            args = [match]
            if document_ids is not None:
                if not document_ids:
                    return []
                sql += " AND c.document_id IN (" + ",".join("?" for _ in document_ids) + ")"
                args.extend(document_ids)
            rows = db.execute(sql + " ORDER BY score LIMIT ?", [*args, limit]).fetchall()
            return [dict(r) for r in rows]

    def semantic_search(self, query, limit=10, document_ids=None):
        if not self.embedder:
            raise RuntimeError("Semantic search requires locally installed E5")
        q = normalize_vector(self.embedder.query(query))
        with self.db() as db:
            rows = db.execute("""SELECT c.*,v.vector FROM vectors v JOIN chunks c ON c.id=v.chunk_id WHERE v.model=?""",
                              (self.embedder.key,)).fetchall()
        results = []
        for row in rows:
            if document_ids is not None and row["document_id"] not in document_ids:
                continue
            vector = json.loads(row["vector"])
            if len(vector) != len(q):
                raise ValueError("Embedding dimension mismatch")
            results.append({k: row[k] for k in ("id", "document_id", "ordinal", "text")} |
                           {"score": sum(a*b for a, b in zip(q, vector))})
        ranked = sorted(results, key=lambda r: r["score"], reverse=True)[:limit]
        log.debug("Семантический поиск: просмотрено векторов=%d, найдено=%d", len(results), len(ranked))
        return ranked

    def document(self, doc_id):
        with self.db() as db:
            row = db.execute("SELECT payload FROM documents WHERE id=?", (doc_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def document_for_url(self, url):
        """Reuse the stored revision; refreshing already collected URLs is explicit."""
        with self.db() as db:
            row = db.execute("""SELECT d.id, d.payload FROM urls u
                JOIN documents d ON d.id=u.document_id WHERE u.url=?""", (url,)).fetchone()
            if not row:
                return None
            metadata = json.loads(row["payload"])
            metadata.pop("text", None)
            return {"id": row["id"], "metadata": metadata}
