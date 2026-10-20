from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Long-edge cap. Above this, vision models downscale server-side anyway and the
# extra pixels are pure cost.
MAX_EDGE = 1568
# Per-image ceiling on the *encoded* bytes (before base64, which adds ~33%).
MAX_ENCODED_BYTES = 1_200_000
# A single tool call may not flood the context with pictures.
MAX_IMAGES_PER_RESULT = 4
JPEG_QUALITY = 85

_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
]

_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


class ImageError(ValueError):
    """Raised when an image cannot be read or re-encoded."""


@dataclass
class ToolImage:
    """One picture a tool wants the model to actually look at."""

    data: bytes
    mime: str = "image/png"
    name: str = "image"
    width: int | None = None
    height: int | None = None

    @property
    def size_note(self) -> str:
        dims = f"{self.width}x{self.height}" if self.width and self.height else "unknown size"
        return f"{self.name} ({self.mime}, {dims}, {len(self.data)} bytes)"

    def meta(self) -> dict[str, Any]:
        """Log-safe description — never includes the pixels."""
        return {
            "name": self.name,
            "mime": self.mime,
            "width": self.width,
            "height": self.height,
            "bytes": len(self.data),
        }


@dataclass
class ToolResult:
    """A tool result that carries images alongside its textual payload.

    ``text`` may be any JSON-serialisable value — it goes through the same
    stringification as an ordinary tool result.
    """

    text: Any = ""
    images: list[ToolImage] = field(default_factory=list)


def _pil():
    try:
        from PIL import Image  # noqa: PLC0415 — optional at import time
    except ImportError as exc:  # pragma: no cover - depends on install
        raise ImageError(
            "Pillow is not installed; image tools need the 'pillow' library"
        ) from exc
    return Image


def sniff_mime(data: bytes, name: str = "") -> str | None:
    """Best-effort image MIME type from the magic bytes, falling back to the
    filename. Returns ``None`` when the bytes are not a known image."""
    for sig, mime in _SIGNATURES:
        if data.startswith(sig):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    guessed = mimetypes.guess_type(name)[0] if name else None
    return guessed if (guessed or "").startswith("image/") else None


def is_image_file(path: Path) -> bool:
    """Whether a file on disk looks like an image (signature first, name second)."""
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(32)
    except OSError:
        return False
    return sniff_mime(head, Path(path).name) is not None


def probe(path: Path) -> dict[str, Any]:
    """Cheap metadata for an image file: format, dimensions, byte size.

    Never decodes the full raster, so it is safe to call on huge files.
    """
    path = Path(path)
    info: dict[str, Any] = {"name": path.name, "bytes": path.stat().st_size}
    with path.open("rb") as fh:
        info["mime"] = sniff_mime(fh.read(32), path.name)
    try:
        Image = _pil()
        with Image.open(path) as im:
            info["format"] = im.format
            info["width"], info["height"] = im.size
            info["mode"] = im.mode
    except ImageError:
        pass  # no Pillow — signature-level info is still useful
    except Exception as exc:  # noqa: BLE001 — a corrupt file should not crash a tool
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def load(path: Path, name: str | None = None) -> ToolImage:
    """Read an image file into a (not yet normalised) :class:`ToolImage`."""
    path = Path(path)
    data = path.read_bytes()
    mime = sniff_mime(data, path.name)
    if mime is None:
        raise ImageError(f"'{path.name}' is not a recognised image file")
    return ToolImage(data=data, mime=mime, name=name or path.name)


def normalize(image: ToolImage) -> ToolImage:
    """Return a copy that is safe to send: EXIF-rotated, within :data:`MAX_EDGE`
    and :data:`MAX_ENCODED_BYTES`, in a format every vision API accepts.

    Images with transparency stay PNG; everything else becomes JPEG, which is
    what keeps a photo under the size ceiling.
    """
    import io

    Image = _pil()
    try:
        from PIL import ImageOps

        with Image.open(io.BytesIO(image.data)) as im:
            im = ImageOps.exif_transpose(im) or im
            has_alpha = im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info
            )
            im = im.convert("RGBA" if has_alpha else "RGB")

            longest = max(im.size)
            if longest > MAX_EDGE:
                scale = MAX_EDGE / longest
                im = im.resize(
                    (max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                    Image.LANCZOS,
                )

            data, mime = _encode(im, has_alpha)
            # Still too heavy (a large screenshot with alpha): shrink until it fits.
            for _ in range(4):
                if len(data) <= MAX_ENCODED_BYTES:
                    break
                im = im.resize((max(1, im.width * 3 // 4), max(1, im.height * 3 // 4)), Image.LANCZOS)
                data, mime = _encode(im, has_alpha)
            return ToolImage(
                data=data, mime=mime, name=image.name, width=im.width, height=im.height
            )
    except ImageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ImageError(f"Could not process image '{image.name}': {exc}") from exc


def _encode(im, has_alpha: bool) -> tuple[bytes, str]:
    import io

    buf = io.BytesIO()
    if has_alpha:
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "image/png"
    im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue(), "image/jpeg"


def extension_for(mime: str) -> str:
    return _EXT_BY_MIME.get(mime, ".png")


def data_uri(image: ToolImage) -> str:
    return f"data:{image.mime};base64,{base64.b64encode(image.data).decode('ascii')}"


def content_parts(text: str, images: list[ToolImage]) -> list[dict[str, Any]]:
    """Build an OpenAI multimodal message body: leading text, then the pictures."""
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for image in images:
        parts.append({"type": "image_url", "image_url": {"url": data_uri(image)}})
    return parts


def prepare(images: list[ToolImage]) -> tuple[list[ToolImage], list[str]]:
    """Normalise a tool's images, capping their number.

    Returns the images that are safe to send plus human-readable notes about
    anything that was dropped, so the model is told rather than left guessing.
    """
    notes: list[str] = []
    ready: list[ToolImage] = []
    for image in images[:MAX_IMAGES_PER_RESULT]:
        try:
            ready.append(normalize(image))
        except ImageError as exc:
            notes.append(str(exc))
    dropped = len(images) - MAX_IMAGES_PER_RESULT
    if dropped > 0:
        notes.append(
            f"{dropped} more image(s) were not shown — at most "
            f"{MAX_IMAGES_PER_RESULT} images can be returned by one tool call"
        )
    return ready, notes
