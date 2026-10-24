"""Готовые векторы: эмбеддер считает только тексты, которых нет в файле."""

from __future__ import annotations

import numpy as np

from eventgraph import embed, embed_cache


def unit(*values):
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def use(monkeypatch, path):
    monkeypatch.setattr(embed_cache.settings, "emb_cache", str(path))
    embed_cache._table.cache_clear()


async def test_cached_texts_are_not_embedded_again(tmp_path, monkeypatch):
    path = tmp_path / "embeddings.npz"
    embed_cache.save(path, {embed_cache.key(embed._prefix() + "Банк России"): unit(3, 4, 0)})
    use(monkeypatch, path)
    asked = []

    async def create(texts):
        asked.extend(texts)
        return np.stack([unit(0, 0, 1)] * len(texts))

    monkeypatch.setattr(embed, "_create", create)
    vectors = await embed.embed(["Банк России", "Минфин"])
    assert asked == ["Минфин"]
    assert np.allclose(vectors[0], unit(3, 4, 0), atol=1e-2) and np.allclose(vectors[1], unit(0, 0, 1))
    asked.clear()
    assert np.allclose(await embed.embed_one("Банк России"), unit(3, 4, 0), atol=1e-2) and not asked
    embed_cache._table.cache_clear()


def test_a_file_of_another_model_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "embeddings.npz"
    embed_cache.save(path, {embed_cache.key("x"): unit(1, 0)})
    use(monkeypatch, path)
    monkeypatch.setattr(embed_cache.settings, "emb_model", "other-model")
    embed_cache._table.cache_clear()
    assert embed_cache.lookup(["x"]) == [None]
    embed_cache._table.cache_clear()
