"""Relevance ranking of page fragments against the query.

E5Ranker — local multilingual-e5-large (lazy, downloads disabled).
EmbedderRanker — the EventGraph embedder (EMB_MODEL / EMB_BASE_URL): no second
model in the image; this is what the agents use (eventgraph/agents/web.py).
"""
import re
from pathlib import Path


DEFAULT_MODEL = Path(__file__).resolve().parents[1] / "models" / "multilingual-e5-large"


class BaseRanker:
    """Ranking over `_encode`; subclasses supply the model, its tokenizer and window."""

    window = 448

    def __init__(self, *, min_score=0.84, max_score_gap=0.025, min_support_ratio=0.3):
        if not -1 <= min_score <= 1 or max_score_gap < 0:
            raise ValueError("Invalid relevance thresholds")
        if not 0 <= min_support_ratio <= 1:
            raise ValueError("Invalid support ratio")
        self.min_support_ratio = min_support_ratio
        self.min_score, self.max_score_gap = min_score, max_score_gap

    @property
    def tokenizer(self):
        raise NotImplementedError

    def _encode(self, texts, passage):
        """Normalized vectors, one row per text."""
        raise NotImplementedError

    def query(self, text):
        return self._encode([text], passage=False)[0]

    def _tokens(self, text):
        return self.tokenizer(text, add_special_tokens=False, return_offsets_mapping=True,
                              truncation=False)["offset_mapping"]

    def rank(self, query_vector, chunks, limit):
        if not chunks:
            return []
        import numpy as np
        window = self.window
        passages, owners = [], []
        for i, chunk in enumerate(chunks):
            offsets = self._tokens(chunk.text)
            for start in range(0, max(1, len(offsets)), window):
                left = offsets[start][0] if start else 0
                right = offsets[start + window][0] if start + window < len(offsets) else len(chunk.text)
                passages.append(chunk.text[left:right])
                owners.append(i)
        encoded = self._encode(passages, passage=True)
        vectors = np.zeros((len(chunks), encoded.shape[1]))
        for owner, vector in zip(owners, encoded):
            vectors[owner] += vector
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors /= np.maximum(norms, 1e-12)
        scores = vectors @ query_vector
        order = sorted(range(len(chunks)), key=lambda i: (-float(scores[i]), i))
        cutoff = max(self.min_score, float(scores[order[0]]) - self.max_score_gap)
        eligible = [i for i in order if float(scores[i]) >= cutoff]
        # Require query relevance across a substantial part of the passage,
        # rather than admitting a long passage on the strength of one sentence.
        from .parser import sentence_ends
        selected = []
        for i in eligible:
            boundaries = [0, *sentence_ends(chunks[i].text)]
            sentences = [chunks[i].text[a:b] for a, b in zip(boundaries, boundaries[1:])]
            # Long sentences were already scored through all their windows above.
            if any(len(self._tokens(t)) > window for t in sentences):
                support = 1.0 if scores[i] >= cutoff else 0.0
            else:
                sentence_vectors = self._encode(sentences, passage=True)
                support = sum(len(t) for t, score in zip(sentences, sentence_vectors @ query_vector)
                              if score >= self.min_score) / max(1, sum(map(len, sentences)))
            if support < self.min_support_ratio:
                continue
            words = set(chunks[i].text.lower().split())
            if any((chunks[i].start < c.end and c.start < chunks[i].end) or
                   len(words & set(c.text.lower().split())) / max(1, len(words | set(c.text.lower().split()))) > 0.6
                   for c, _ in selected):
                continue
            selected.append((chunks[i], float(scores[i])))
            if len(selected) == limit:
                break
        return selected


class E5Ranker(BaseRanker):
    def __init__(self, path=DEFAULT_MODEL, *, min_score=0.84, max_score_gap=0.025, min_support_ratio=0.3):
        super().__init__(min_score=min_score, max_score_gap=max_score_gap, min_support_ratio=min_support_ratio)
        self.path = Path(path)
        self._model = None

    def _load(self):
        if self._model is None:
            if not self.path.is_dir():
                raise FileNotFoundError(f"Local E5 model is missing: {self.path}")
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(
                str(self.path), local_files_only=True, trust_remote_code=False)
        return self._model

    @property
    def tokenizer(self):
        return self._load().tokenizer

    def _encode(self, texts, passage):
        prefix = "passage: " if passage else "query: "
        return self._load().encode([prefix + t for t in texts], normalize_embeddings=True,
                                   show_progress_bar=False, batch_size=16)


_WORD = re.compile(r"\w+|[^\w\s]")


def word_tokenizer(text, **_):
    """Words and punctuation as tokens, with offsets — same shape as a HF tokenizer's
    output. Works for any embedder, local or remote; a Russian word is ~2 model tokens."""
    return {"offset_mapping": [m.span() for m in _WORD.finditer(text)]}


class EmbedderRanker(BaseRanker):
    """Fragments of ~160 words: paraphrase-mpnet is trained on 128 tokens, and a
    page's fragment then fits the agent's context. Its cosine scores sit much
    lower than E5's, hence the thresholds."""

    window = 160

    def __init__(self, encode=None, *, min_score=0.45, max_score_gap=0.12, min_support_ratio=0.3):
        super().__init__(min_score=min_score, max_score_gap=max_score_gap, min_support_ratio=min_support_ratio)
        self._encode_fn = encode

    @property
    def tokenizer(self):
        return word_tokenizer

    def _encode(self, texts, passage):
        if self._encode_fn is not None:
            return self._encode_fn(texts, passage)
        from eventgraph import embed
        return embed.embed_sync(texts, passage=passage)
