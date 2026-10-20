from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx

from eventgraph.connectors.images import ImageError, load_image

RESERVED = ("id", "text", "image")


class ResultError(ValueError):
    """Коннектор вернул не то, что обещал контракт."""


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} байт>"
    return str(value)


def jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))


@dataclass
class Draft:
    """Запись до сохранения. Картинки грузятся лениво — дубли их не качают."""

    text: str
    data: dict[str, Any]
    image_refs: list[Any]
    external_id: str | None = None
    images: list[tuple[bytes, str]] = field(default_factory=list)
    image_errors: list[str] = field(default_factory=list)
    loaded: bool = False

    async def load_images(self, http: httpx.AsyncClient, max_bytes: int) -> None:
        """Грузит картинки. Битая картинка не роняет запись: она теряет
        только эту картинку, а причина остаётся в `image_errors`."""
        if self.loaded:
            return
        for ref in self.image_refs:
            try:
                self.images.append(await load_image(ref, http, max_bytes))
            except ImageError as exc:
                self.image_errors.append(str(exc))
        self.loaded = True

    def drop_images(self) -> None:
        """Запись без картинок: не качать и не хранить."""
        self.image_refs, self.images, self.image_errors = [], [], []
        self.loaded = True

    def content_hash(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.text.encode())
        for data, _ in self.images:
            digest.update(hashlib.sha256(data).digest())
        digest.update(json.dumps(self.data, sort_keys=True, ensure_ascii=False).encode())
        return "sha256:" + digest.hexdigest()

    def preview(self) -> dict[str, Any]:
        return {
            "id": self.external_id,
            "text": self.text,
            "images": [{"bytes": len(data), "format": ext} for data, ext in self.images],
            "image_errors": self.image_errors,
            "data": self.data,
        }


def parse(result: Any) -> list[Draft]:
    """Проверка формы без сети: что пришло и где ошибка."""
    if result is None:
        return []
    if isinstance(result, dict):
        rows = [result]
    elif isinstance(result, (list, tuple)):
        rows = list(result)
    else:
        raise ResultError(f"collect вернула {type(result).__name__}, ожидался dict или list[dict]")

    drafts: list[Draft] = []
    for n, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ResultError(f"элемент №{n} — {type(row).__name__}, ожидался dict")
        text = row.get("text")
        text = "" if text is None else str(text)
        image = row.get("image")
        if image is None:
            refs: list[Any] = []
        elif isinstance(image, (list, tuple)):
            refs = [ref for ref in image if ref is not None]
        else:
            refs = [image]
        raw_id = row.get("id")
        external_id = None if raw_id is None or raw_id == "" else str(raw_id)[:512]
        data = jsonable({str(k): v for k, v in row.items() if k not in RESERVED})
        drafts.append(Draft(text=text, data=data, image_refs=refs, external_id=external_id))
    return drafts


async def finalize_ids(drafts: list[Draft], http: httpx.AsyncClient, max_bytes: int) -> list[Draft]:
    """Даёт каждой записи ключ и убирает повторы внутри одного результата.

    Записям без `id` картинки нужны уже здесь — они входят в хеш.
    """
    out: list[Draft] = []
    seen: set[str] = set()
    for draft in drafts:
        if draft.external_id is None:
            await draft.load_images(http, max_bytes)
            draft.external_id = draft.content_hash()
        if draft.external_id in seen:
            continue
        seen.add(draft.external_id)
        out.append(draft)
    return out
