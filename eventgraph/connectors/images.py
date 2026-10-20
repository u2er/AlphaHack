from __future__ import annotations

import base64
import binascii
import io
from datetime import datetime
from pathlib import Path

import httpx

# Сигнатура → расширение. Только то, что понимают мультимодальные модели.
_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
]

MEDIA_TYPES = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}


class ImageError(ValueError):
    pass


def sniff(data: bytes) -> str | None:
    for signature, ext in _SIGNATURES:
        if data.startswith(signature):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


async def load_image(value: object, http: httpx.AsyncClient, max_bytes: int) -> tuple[bytes, str]:
    """Байты картинки и расширение — или ImageError с понятной причиной."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
    elif isinstance(value, str):
        data = await _from_string(value.strip(), http, max_bytes)
    else:
        raise ImageError(f"image должен быть bytes, base64, data-URL или ссылкой, а не {type(value).__name__}")

    if len(data) > max_bytes:
        raise ImageError(f"картинка больше {max_bytes // (1024 * 1024)} МБ")
    ext = sniff(data)
    if ext is None:
        raise ImageError("не похоже на jpeg/png/gif/webp")
    return data, ext


async def _from_string(value: str, http: httpx.AsyncClient, max_bytes: int) -> bytes:
    if value.startswith(("http://", "https://")):
        try:
            response = await http.get(value)
        except httpx.HTTPError as exc:
            raise ImageError(f"не скачалась {value[:120]}: {exc}") from exc
        if response.status_code != 200:
            raise ImageError(f"не скачалась {value[:120]}: HTTP {response.status_code}")
        return response.content
    if value.startswith("data:"):
        _, _, value = value.partition(",")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageError("строка в image — не ссылка и не base64") from exc


def write_images(root: Path, uid: str, when: datetime, images: list[tuple[bytes, str]]) -> list[str]:
    """Пишет оригиналы и возвращает пути относительно `root` (каталога данных)."""
    day = Path("items") / f"{when:%Y/%m/%d}"
    (root / day).mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for n, (data, ext) in enumerate(images):
        rel = day / f"{uid}-{n}.{ext}"
        (root / rel).write_bytes(data)
        paths.append(rel.as_posix())
    return paths


def shrink(data: bytes, ext: str, edge: int) -> tuple[bytes, str]:
    """Копия для модели: длинная сторона не больше `edge`.

    Картинку модель получает на каждом шаге рана, и цена её — по пикселям;
    новостной иллюстрации, графику или подписи хватает 512 px. Прозрачная
    уходит PNG, остальные JPEG; анимация — первым кадром. Что не открылось или
    уже не больше `edge` — как есть.
    """
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as im:
            if max(im.size) <= edge:
                return data, ext
            im = ImageOps.exif_transpose(im) or im
            alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
            im = im.convert("RGBA" if alpha else "RGB")
            im.thumbnail((edge, edge), Image.LANCZOS)
            out = io.BytesIO()
            if alpha:
                im.save(out, format="PNG", optimize=True)
                return out.getvalue(), "png"
            im.save(out, format="JPEG", quality=85)
            return out.getvalue(), "jpg"
    except (OSError, ValueError, Image.DecompressionBombError):
        return data, ext
