"""Local E5 relevance ranking. Loading is lazy; model downloads are disabled."""
from pathlib import Path


DEFAULT_MODEL = Path(__file__).resolve().parents[1] / "models" / "multilingual-e5-large"


class E5Ranker:
    def __init__(self, path=DEFAULT_MODEL, *, min_score=0.84, max_score_gap=0.025, min_support_ratio=0.3):
        if not -1 <= min_score <= 1 or max_score_gap < 0:
            raise ValueError("Invalid relevance thresholds")
        if not 0 <= min_support_ratio <= 1:
            raise ValueError("Invalid support ratio")
        self.min_support_ratio = min_support_ratio
        self.min_score, self.max_score_gap = min_score, max_score_gap
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

    def query(self, text):
        return self._load().encode("query: " + text, normalize_embeddings=True,
                                   show_progress_bar=False)

    def rank(self, query_vector, chunks, limit):
        if not chunks:
            return []
        import numpy as np
        passages, owners = [], []
        for i, chunk in enumerate(chunks):
            offsets = self.tokenizer(chunk.text, add_special_tokens=False,
                                     return_offsets_mapping=True, truncation=False)["offset_mapping"]
            for start in range(0, max(1, len(offsets)), 448):
                left = offsets[start][0] if start else 0
                right = offsets[start + 448][0] if start + 448 < len(offsets) else len(chunk.text)
                passages.append("passage: " + chunk.text[left:right])
                owners.append(i)
        encoded = self._load().encode(passages, normalize_embeddings=True,
                                      show_progress_bar=False, batch_size=16)
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
            if any(len(self.tokenizer(t, add_special_tokens=False,
                       return_offsets_mapping=True, truncation=False)["offset_mapping"]) > 448 for t in sentences):
                support = 1.0 if scores[i] >= cutoff else 0.0
            else:
                sentence_vectors = self._load().encode(
                    ["passage: " + sentence for sentence in sentences],
                    normalize_embeddings=True, show_progress_bar=False, batch_size=16)
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
