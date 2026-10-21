"""Реальный движок docling: только в окружении, где он установлен
(docker-образ, полный venv). В лёгком окружении модуль целиком пропускается.

Запуск в докере (модели docling — в томе данных, HF_HOME из compose):
    docker compose run --rm worker python -m pytest tests/integration -m integration
"""

from __future__ import annotations

import io

import pytest

pytestmark = pytest.mark.integration

# Нет docling (и torch/моделей) — пропускаем модуль целиком.
pytest.importorskip("docling")

from eventgraph.config import settings
from eventgraph.connectors.kinds import pdf
from eventgraph.connectors.runtime import Context


def _pdf_bytes(stream: bytes) -> bytes:
    """Минимально валидный PDF: каталог, страница, Helvetica, содержимое — stream."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode("ascii"))
        out.write(body)
        out.write(b"\nendobj\n")
    offsets.append(out.tell())
    out.write(b"5 0 obj\n<< /Length %d >>\nstream\n" % len(stream))
    out.write(stream)
    out.write(b"\nendstream\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % xref)
    return out.getvalue()


def make_pdf(text: str) -> bytes:
    """Одна страница, Helvetica: по одной текстовой линии на строку входа."""

    def esc(line: str) -> str:
        return line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    lines = [line for line in text.splitlines() if line.strip()] or [""]
    stream = "BT /F1 12 Tf 72 720 Td 14 TL\n".encode("latin-1")
    stream += "".join(f"({esc(line)}) Tj T*\n" for line in lines).encode("latin-1")
    stream += b"ET"
    return _pdf_bytes(stream)


async def test_docling_parses_generated_pdf(tmp_path, monkeypatch):
    """Сквозной прогон: collect → реальный PdfFileManager → записи и курсор."""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.pdf").write_bytes(make_pdf("Test report 12345"))

    ctx = Context(config={"mode": "dir", "path": str(docs)}, state={})
    try:
        records = await pdf.collect(ctx)
    finally:
        await ctx.aclose()

    assert len(records) == 1
    assert "Test report 12345" in records[0]["text"]
    assert len(records[0]["id"]) == 64
    assert ctx.state["seen"]

    # повторный запуск читает результат из кэша парсера
    ctx = Context(config={"mode": "dir", "path": str(docs)}, state=ctx.state)
    try:
        assert await pdf.collect(ctx) == []
    finally:
        await ctx.aclose()
