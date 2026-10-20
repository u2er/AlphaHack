"""Сюжеты: куда встаёт новая запись — без базы и без модели."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from eventgraph import stories
from eventgraph.stories import Known

T0 = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


def unit(*xs):
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def place(vector, at, known):
    return stories.place(99, vector, at, known, 0.85, 24 * H, 0.97, 48 * H)


def test_a_record_unlike_anything_starts_its_own_story():
    assert place(unit(1, 0), T0, []).story == 99
    assert place(unit(0, 1), T0, [Known(1, 1, unit(1, 0), T0)]).story == 99


def test_a_like_record_joins_the_most_similar_story():
    known = [Known(1, 1, unit(1, 0, 0), T0), Known(2, 2, unit(0.9, 0.44, 0), T0)]
    spot = place(unit(0.99, 0.14, 0), T0 + H, known)
    assert (spot.story, spot.duplicate_of) == (1, None)


def test_a_story_closes_a_day_after_its_first_record():
    # сюжет начат вчера: даже свежая похожая запись в нём не продлевает его
    known = [Known(1, 1, unit(1, 0), T0), Known(2, 1, unit(1, 0.05), T0 + 20 * H)]
    assert place(unit(1, 0.02), T0 + 25 * H, known).story == 99


def test_a_near_copy_of_what_is_in_the_graph_is_a_repeat():
    known = [Known(1, 1, unit(1, 0), T0, event="Событие", title="Заголовок")]
    spot = place(unit(1, 0.01), T0 + 30 * H, known)
    assert spot.duplicate_of is known[0] and spot.story == 1
    # без события в графе — просто тот же сюжет (если он ещё открыт)
    assert place(unit(1, 0.01), T0 + H, [Known(1, 1, unit(1, 0), T0)]).duplicate_of is None
    # слишком давно разобранное — уже не повтор
    assert place(unit(1, 0.01), T0 + 49 * H, known).duplicate_of is None
    # похожее, но не копия — не повтор
    assert place(unit(1, 0.4), T0 + H, known).duplicate_of is None


def test_vectors_survive_the_database():
    v = unit(0.3, 0.4, 0.5)
    assert np.allclose(stories.unpack(stories.pack(v)), v)
