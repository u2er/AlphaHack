"""Вид коннектора PDF (движок — docling).

Логика коннектора проверяется через фейк менеджера парсера: поддельный
`_make_manager` отдаёт объект с тем же маленьким контрактом, что и
`PdfFileManager` (`extract_content()`, `.text`, `.texts`, `.content_dir`),
поэтому unit-тесты обходятся без torch/docling. Реальный движок покрывает
`tests/integration/test_pdf_docling.py` (пропускается без docling).
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import httpx
import pytest

from eventgraph.config import settings
from eventgraph.connectors.kinds import pdf
from eventgraph.connectors.runtime import Context


# ── генератор тестовых PDF ───────────────────────────────────────────────────


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


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── фейк менеджера парсера ───────────────────────────────────────────────────

PNG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 16
PNG_B = b"\x89PNG\r\n\x1a\n" + b"B" * 16
PNG_C = b"\x89PNG\r\n\x1a\n" + b"C" * 16


def png_bytes(width: int, height: int, fill: bytes = b"") -> bytes:
    """PNG с настоящим заголовком IHDR: коннектор читает из него размер."""
    header = (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
              + width.to_bytes(4, "big") + height.to_bytes(4, "big")
              + b"\x08\x02\x00\x00\x00" + b"\x00" * 4)
    return header + fill


class FakeDoc:
    """То, что ParsedDocument: текстовый фрагмент и метаданные."""

    def __init__(self, page_content: str = ""):
        self.page_content = page_content
        self.metadata: dict = {}


class FakeManager:
    """Двойник PdfFileManager с тем же контрактом, что использует коннектор:
    `data_exists()`, `extract_content()`, `.text`, `.texts`, `.content_dir`."""

    def __init__(self, file_path: Path, cache_dir: Path, kwargs: dict, spec: dict):
        self.file_path = Path(file_path)
        self.cache_dir = Path(cache_dir)
        self.hash = sha256(self.file_path.read_bytes())
        self.content_dir = self.cache_dir / self.hash
        self.text = None
        self.texts = []
        self.kwargs = kwargs
        self._spec = spec

    def data_exists(self) -> bool:
        return bool(self._spec.get("cached"))

    def extract_content(self) -> None:
        spec = self._spec
        if spec.get("error"):
            raise RuntimeError(spec["error"])
        # полный текст; None — эмуляция пути «загружено из кэша»
        if spec.get("text") is not None:
            self.text = FakeDoc(spec["text"])
        self.texts = [FakeDoc(page) for page in spec.get("pages", [])]
        # «сохраняем» медиа в кэш, как это делает PdfFileManager
        for sub, media in (("images", spec.get("images", [])), ("tables", spec.get("tables", []))):
            folder = self.content_dir / sub
            folder.mkdir(parents=True, exist_ok=True)
            for n, data in enumerate(media, start=1):
                (folder / f"{n}__1.png").write_bytes(data)


def install_fake(monkeypatch, prepared: dict[str, dict]) -> None:
    """Подменяет ленивый `_make_manager`: sha256 байтов файла → спека результата.

    Ключ по содержимому (а не по имени) работает в обоих режимах: в mode
    "dir" файл лежит в каталоге под своим именем, в mode "url" скачанная
    копия — dl-N.pdf.
    """

    def factory(file_path: Path, cache_dir: Path, kwargs: dict):
        name = Path(file_path).name
        digest = sha256(Path(file_path).read_bytes())
        assert digest in prepared, f"фейк не знает файл {name} (sha256 {digest[:12]}…)"
        return FakeManager(file_path, cache_dir, kwargs, prepared[digest])

    monkeypatch.setattr(pdf, "_make_manager", factory)


# ── подставной интернет и запуск ────────────────────────────────────────────


class Web:
    """Подставной интернет для mode "url": адрес → PDF-байты или (статус, тело, заголовки)."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.requested: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        route = self.routes.get(url)
        if route is None:
            return httpx.Response(404, content=b"no such file")
        if isinstance(route, tuple):
            status, body, headers = route
            return httpx.Response(status, content=body, headers=headers)
        return httpx.Response(200, content=route, headers={"content-type": "application/pdf"})


async def run(
    config: dict, state: dict | None = None, web: Web | None = None
) -> tuple[list[dict], Context]:
    ctx = Context(config=config, state=state if state is not None else {})
    if web is not None:
        await ctx.http.aclose()
        ctx.http = httpx.AsyncClient(transport=httpx.MockTransport(web.handler), follow_redirects=True)
    try:
        return await pdf.collect(ctx), ctx
    finally:
        await ctx.aclose()


# ── импорт ───────────────────────────────────────────────────────────────────


def test_import_does_not_load_heavy_dependencies():
    """Модуль не должен тащить тяжёлый стек в API-процесс.

    Первый в файле: до него ещё ни один тест не запускал collect(), который
    и импортирует пакет парсера (лениво, в _make_manager).
    """
    import sys

    for module in ("torch", "docling", "docling_parser", "pandas"):
        assert module not in sys.modules, module


# ── конфигурация ─────────────────────────────────────────────────────────────


async def test_unknown_mode_rejected():
    with pytest.raises(ValueError, match="mode"):
        await run({"mode": "ftp"})


async def test_dir_mode_requires_path():
    with pytest.raises(ValueError, match="path"):
        await run({"mode": "dir"})


async def test_url_mode_requires_urls():
    with pytest.raises(ValueError, match="urls"):
        await run({"mode": "url"})


# ── mode "dir" ───────────────────────────────────────────────────────────────


async def test_dir_mode_parses_and_moves_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    first, second = make_pdf("Test report 12345"), make_pdf("Test report 12345 revised")
    install_fake(monkeypatch, {
        sha256(first): {"text": "Test report 12345", "pages": ["Test report 12345"]},
        sha256(second): {
            "text": "Test report 12345 revised",
            "pages": ["Test report 12345 revised"],
        },
    })
    docs = tmp_path / "docs"
    docs.mkdir()
    report = docs / "report.pdf"
    report.write_bytes(first)

    records, ctx = await run({"mode": "dir", "path": str(docs)})

    assert len(records) == 1
    record = records[0]
    assert record["id"] == sha256(report.read_bytes())
    assert record["text"] == "Test report 12345"
    assert record["file_name"] == "report.pdf"
    assert record["pages"] == 1
    assert record["published"]
    assert ctx.state["seen"] == {"report.pdf": record["id"]}
    # менеджер «сохранил» результат в кэш парсера
    assert (tmp_path / "data" / "pdf_cache" / record["id"]).is_dir()

    # файл не изменился — повторный запуск пустой
    records, ctx = await run({"mode": "dir", "path": str(docs)}, ctx.state)
    assert records == []

    # содержимое изменилось — новая запись с новым external_id
    report.write_bytes(second)
    records, ctx = await run({"mode": "dir", "path": str(docs)}, ctx.state)
    assert len(records) == 1
    assert records[0]["id"] != record["id"]
    assert set(ctx.state["seen"]) == {"report.pdf"}


async def test_dir_mode_cache_path_rebuilds_full_text(tmp_path, monkeypatch):
    """Путь через кэш: менеджер не восстанавливает .text — полный текст
    собирается из постраничных фрагментов."""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    install_fake(monkeypatch, {sha256(make_pdf("cache path")): {
        "text": None,
        "pages": ["первая страница", "вторая страница"],
    }})
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "report.pdf").write_bytes(make_pdf("cache path"))

    records, _ = await run({"mode": "dir", "path": str(docs)})

    assert records[0]["text"] == "первая страница\n\nвторая страница"


async def test_dir_mode_collects_images_and_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    spec = {
        "text": "с картинками",
        "pages": ["с картинками"],
        "images": [PNG_A, PNG_B],
        "tables": [PNG_C],
    }
    first, second = make_pdf("images"), make_pdf("images v2")
    install_fake(monkeypatch, {sha256(first): spec, sha256(second): spec})
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.pdf").write_bytes(first)

    # лимит 3: сначала картинки, потом кропы таблиц
    records, ctx = await run({"mode": "dir", "path": str(docs)})
    assert records[0]["image"] == [PNG_A, PNG_B, PNG_C]

    # лимит 1: только первая картинка
    (docs / "doc.pdf").write_bytes(second)
    records, _ = await run({"mode": "dir", "path": str(docs), "max_images": 1}, ctx.state)
    assert records[0]["image"] == [PNG_A]


async def test_icons_not_attached_and_not_described(tmp_path, monkeypatch):
    """Логотипы/иконки мельче MIN_MEDIA_EDGE не попадают в запись."""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    icon = png_bytes(74, 107, b"I")
    chart = png_bytes(740, 476, b"C")
    install_fake(monkeypatch, {
        sha256(make_pdf("icons")): {
            "text": "иконки", "pages": ["иконки"], "images": [icon, chart],
        },
    })
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.pdf").write_bytes(make_pdf("icons"))

    records, _ = await run({"mode": "dir", "path": str(docs)})
    assert records[0]["image"] == [chart]


async def test_dir_mode_missing_directory(tmp_path):
    with pytest.raises(RuntimeError, match="не найден"):
        await run({"mode": "dir", "path": str(tmp_path / "nope")})


async def test_dir_mode_bad_file_does_not_kill_run(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    install_fake(monkeypatch, {
        sha256(make_pdf("Good 1")): {"text": "Good 1", "pages": ["Good 1"]},
        sha256(b"not a pdf at all"): {"error": "не удалось распарсить"},
    })
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "good.pdf").write_bytes(make_pdf("Good 1"))
    (docs / "bad.pdf").write_bytes(b"not a pdf at all")

    records, ctx = await run({"mode": "dir", "path": str(docs)})

    assert len(records) == 1
    assert "good.pdf" in ctx.state["seen"]
    assert "bad.pdf" not in ctx.state["seen"]


async def test_dir_mode_all_bad_files_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    install_fake(monkeypatch, {sha256(b"not a pdf at all"): {"error": "не удалось распарсить"}})
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "bad.pdf").write_bytes(b"not a pdf at all")

    with pytest.raises(RuntimeError, match="ни один из 1 файлов"):
        await run({"mode": "dir", "path": str(docs)})


async def test_dir_mode_empty_document_is_skipped_and_remembered(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    install_fake(monkeypatch, {sha256(make_pdf("")): {"text": "", "pages": [""]}})
    docs = tmp_path / "docs"
    docs.mkdir()
    empty = docs / "empty.pdf"
    empty.write_bytes(make_pdf(""))

    records, ctx = await run({"mode": "dir", "path": str(docs)})

    assert records == []
    assert ctx.state["seen"] == {"empty.pdf": sha256(empty.read_bytes())}


async def test_dir_mode_max_files_per_run(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    prepared = {
        sha256(make_pdf(f"Doc {n}")): {"text": f"Doc {n}", "pages": [f"Doc {n}"]}
        for n in range(1, 4)
    }
    install_fake(monkeypatch, prepared)
    docs = tmp_path / "docs"
    docs.mkdir()
    for n in range(1, 4):
        (docs / f"doc{n}.pdf").write_bytes(make_pdf(f"Doc {n}"))

    records, ctx = await run({"mode": "dir", "path": str(docs), "max_files_per_run": 2})
    assert len(records) == 2
    assert len(ctx.state["seen"]) == 2
    # до остальных дойдём следующим запуском
    records, _ = await run(
        {"mode": "dir", "path": str(docs), "max_files_per_run": 2}, ctx.state
    )
    assert len(records) == 1


# ── mode "url" ───────────────────────────────────────────────────────────────

GOOD = "https://example.org/docs/plan.pdf"


async def test_url_mode_downloads_parses_and_remembers(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    body = make_pdf("Remote document 777")
    install_fake(monkeypatch, {sha256(body): {
        "text": "Remote document 777",
        "pages": ["Remote document 777"],
    }})
    web = Web(
        {
            GOOD: (
                200,
                body,
                {
                    "content-type": "application/pdf",
                    "last-modified": "Wed, 21 Oct 2026 10:30:00 GMT",
                },
            )
        }
    )

    records, ctx = await run({"mode": "url", "urls": [GOOD]}, web=web)

    assert len(records) == 1
    record = records[0]
    assert record["text"] == "Remote document 777"
    assert record["url"] == GOOD
    assert record["published"].startswith("2026-10-21T10:30:00")
    assert ctx.state["seen"] == {GOOD: record["id"]}
    assert web.requested == [GOOD]

    # ссылка уже видна — ни докачка, ни повторный парсинг
    records, _ = await run({"mode": "url", "urls": [GOOD]}, ctx.state, web=web)
    assert records == []
    assert web.requested == [GOOD]


async def test_url_mode_one_bad_link_does_not_kill_run(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    body = make_pdf("Fine file 1")
    install_fake(monkeypatch, {sha256(body): {"text": "Fine file 1", "pages": ["Fine file 1"]}})
    web = Web({"https://example.org/ok.pdf": body})

    records, ctx = await run(
        {"mode": "url", "urls": ["https://example.org/ok.pdf", "https://example.org/missing.pdf"]},
        web=web,
    )

    assert len(records) == 1
    assert records[0]["text"] == "Fine file 1"
    assert "https://example.org/ok.pdf" in ctx.state["seen"]
    assert "https://example.org/missing.pdf" not in ctx.state["seen"]


async def test_url_mode_all_links_bad_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    with pytest.raises(RuntimeError, match="не скачался"):
        await run(
            {"mode": "url", "urls": ["https://example.org/a.pdf"]},
            web=Web({}),
        )


async def test_url_mode_non_pdf_body_is_not_marked_seen(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    body = make_pdf("Good file")
    install_fake(monkeypatch, {sha256(body): {"text": "Good file", "pages": ["Good file"]}})
    web = Web(
        {
            "https://example.org/ok.pdf": body,
            "https://example.org/not-pdf.pdf": b"this is not a pdf",
        }
    )

    records, ctx = await run(
        {"mode": "url", "urls": ["https://example.org/ok.pdf", "https://example.org/not-pdf.pdf"]},
        web=web,
    )

    assert len(records) == 1
    assert "https://example.org/ok.pdf" in ctx.state["seen"]
    assert "https://example.org/not-pdf.pdf" not in ctx.state["seen"]


# ── описания изображений (VLM) ───────────────────────────────────────────────


def _install_recording_fake(monkeypatch, prepared: dict) -> dict:
    """Фейк менеджера, который запоминает kwargs, переданные движку."""
    seen: dict = {}

    def factory(file_path: Path, cache_dir: Path, kwargs: dict):
        seen.clear()
        seen.update(kwargs)
        digest = sha256(Path(file_path).read_bytes())
        return FakeManager(file_path, cache_dir, kwargs, prepared[digest])

    monkeypatch.setattr(pdf, "_make_manager", factory)
    return seen


async def test_dir_mode_llm_vision_enables_image_describer(tmp_path, monkeypatch):
    """LLM_API_KEY + LLM_VISION → движок получает image_describer и table_describer."""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_vision", True)
    monkeypatch.setattr(settings, "llm_base_url", "http://llm.test/v1")
    seen = _install_recording_fake(monkeypatch, {
        sha256(make_pdf("with describer")): {"text": "with describer", "pages": ["with describer"]},
    })
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.pdf").write_bytes(make_pdf("with describer"))

    records, _ = await run({"mode": "dir", "path": str(docs)})

    assert len(records) == 1
    describer = seen.get("image_describer")
    assert callable(describer)
    # таблицы получают свой описатель (независимый бюджет)
    assert callable(seen.get("table_describer"))
    # сам вызов описателя фейк-менеджер не делает — только передача в движок


async def test_dir_mode_max_tables_zero_disables_table_describer(tmp_path, monkeypatch):
    """max_tables=0 → таблицы не описываются, картинки — описываются."""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_vision", True)
    monkeypatch.setattr(settings, "llm_base_url", "http://llm.test/v1")
    seen = _install_recording_fake(monkeypatch, {
        sha256(make_pdf("no table desc")): {"text": "no table desc", "pages": ["no table desc"]},
    })
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.pdf").write_bytes(make_pdf("no table desc"))

    await run({"mode": "dir", "path": str(docs), "max_tables": 0})

    assert "image_describer" in seen
    assert "table_describer" not in seen


async def test_options_reject_negative_max_tables():
    with pytest.raises(ValueError, match="max_tables"):
        await run({"mode": "dir", "path": "/tmp", "max_tables": -1})


async def test_options_default_max_tables():
    assert pdf.options({"mode": "dir", "path": "/tmp"})["max_tables"] == 3


def test_cache_key_changes_with_prompt_and_scale():
    """Смена промпта/разрешения/модели инвалидирует кэш, а цифры бюджета — нет."""
    base = pdf._cache_key(object(), object(), 3)
    assert base == pdf._cache_key(object(), object(), 3)   # стабилен
    assert pdf._cache_key(object(), object(), 2) != base   # другое разрешение
    assert pdf._cache_key(None, object(), 3) != base       # отключили описания


async def test_parse_file_sets_scale_and_cache_key(tmp_path, monkeypatch):
    """Движку передаются масштаб кропов и версия рецепта (для инвалидации кэша)."""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    seen = _install_recording_fake(monkeypatch, {
        sha256(make_pdf("scale")): {"text": "scale", "pages": ["scale"]},
    })
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.pdf").write_bytes(make_pdf("scale"))

    await run({"mode": "dir", "path": str(docs)})

    assert seen["images_scale"] == pdf.DEFAULT_IMAGES_SCALE
    assert seen["cache_key"]                       # версия рецепта ушла в кэш


async def test_parse_file_keeps_explicit_images_scale(tmp_path, monkeypatch):
    """pdfparser_kwargs переопределяет масштаб по умолчанию."""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    seen = _install_recording_fake(monkeypatch, {
        sha256(make_pdf("explicit")): {"text": "explicit", "pages": ["explicit"]},
    })
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.pdf").write_bytes(make_pdf("explicit"))

    await run({"mode": "dir", "path": str(docs), "pdfparser_kwargs": {"images_scale": 4}})

    assert seen["images_scale"] == 4


class _FakeCompletions:
    """Клиент-заглушка: первый ответ пустой (кончился бюджет), второй — текст."""

    def __init__(self, fail_reasoning: bool = False):
        self.calls: list[dict] = []
        self.fail_reasoning = fail_reasoning

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_reasoning and "reasoning_effort" in kwargs:
            raise TypeError("unexpected keyword argument 'reasoning_effort'")
        text = "" if len(self.calls) == 1 else "описание таблицы"

        class _Message:
            content = text

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        return _Response()


def _fake_client(monkeypatch, completions):
    fake_client = type("C", (), {"chat": type("Chat", (), {"completions": completions})()})()
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: fake_client)
    monkeypatch.setattr(pdf, "_image_data_url", lambda image: "data:image/jpeg;base64,AA==")


def test_describer_disables_reasoning(monkeypatch):
    """Рассуждение отключается: иначе «думающая» модель отдаёт пустой content."""
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_vision", True)
    monkeypatch.setattr(settings, "llm_params", "")

    completions = _FakeCompletions()
    _fake_client(monkeypatch, completions)

    describe = pdf._make_describer(3, pdf._PICTURE_PROMPT)
    assert describe(object()) == "описание таблицы"      # первый ответ пуст — повтор
    assert all(call["reasoning_effort"] == "none" for call in completions.calls)
    assert [call["max_tokens"] for call in completions.calls] == [1024, 2048]


def test_describer_falls_back_when_reasoning_effort_unsupported(monkeypatch):
    """Провайдер не знает reasoning_effort — запрос уходит без него."""
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_vision", True)
    monkeypatch.setattr(settings, "llm_params", "")

    completions = _FakeCompletions(fail_reasoning=True)
    _fake_client(monkeypatch, completions)

    describe = pdf._make_describer(3, pdf._PICTURE_PROMPT)
    assert describe(object()) == "описание таблицы"
    assert "reasoning_effort" not in completions.calls[-1]


def test_describer_respects_explicit_reasoning_setting(monkeypatch):
    """LLM_PARAMS с своим reasoning_effort не перебиваем."""
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_vision", True)
    monkeypatch.setattr(settings, "llm_params", '{"reasoning_effort": "low"}')

    completions = _FakeCompletions()
    _fake_client(monkeypatch, completions)

    describe = pdf._make_describer(3, pdf._PICTURE_PROMPT)
    assert describe(object()) == "описание таблицы"
    assert all(call["reasoning_effort"] == "low" for call in completions.calls)


def test_describer_skips_icons_without_spending_budget(monkeypatch):
    """Иконка — без вызова LLM, и бюджет на неё не тратится."""
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_vision", True)
    monkeypatch.setattr(settings, "llm_params", "")

    completions = _FakeCompletions()
    _fake_client(monkeypatch, completions)

    class Image:
        def __init__(self, size):
            self.size = size

    describe = pdf._make_describer(1, pdf._DESCRIPTION_PROMPT)
    assert describe(Image((74, 107))) is None      # логотип, LLM не звали
    assert completions.calls == []
    assert describe(Image((740, 476))) == "описание таблицы"   # бюджет цел
    assert describe(Image((740, 476))) is None                  # бюджет исчерпан


def test_describer_retries_with_bigger_budget_on_empty_answer(monkeypatch):
    """Пустой content (модель потратила бюджет на рассуждение) — один повтор."""
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_vision", True)
    monkeypatch.setattr(settings, "llm_params", '{"max_tokens": 512}')

    completions = _FakeCompletions()
    _fake_client(monkeypatch, completions)

    describe = pdf._make_describer(3, pdf._TABLE_DESCRIPTION_PROMPT)
    assert describe(object()) == "описание таблицы"
    assert [call["max_tokens"] for call in completions.calls] == [512, 2048]


def test_describer_budget_limits_calls(monkeypatch):
    """Бюджет max_items: лишние элементы не описываются (без вызова LLM)."""
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_vision", True)
    monkeypatch.setattr(settings, "llm_params", "")

    completions = _FakeCompletions()
    _fake_client(monkeypatch, completions)

    describe = pdf._make_describer(1, pdf._DESCRIPTION_PROMPT)
    assert describe(object()) == "описание таблицы"   # первый — пустой ответ + повтор
    assert describe(object()) is None                  # бюджет исчерпан
    assert len(completions.calls) == 2


async def test_dir_mode_describer_flag_follows_llm_settings(tmp_path, monkeypatch):
    """image_describer и table_describer передаются движку только при ключе И vision."""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    spec = {"text": "plain", "pages": ["plain"]}
    docs = tmp_path / "docs"
    docs.mkdir()

    # кейс «ключ + vision» покрыт test_dir_mode_llm_vision_enables_image_describer
    for key, vision, enabled in (("", True, False), ("test-key", False, False)):
        monkeypatch.setattr(settings, "llm_api_key", key)
        monkeypatch.setattr(settings, "llm_vision", vision)
        seen = _install_recording_fake(monkeypatch, {sha256(make_pdf("plain")): spec})
        (docs / "doc.pdf").write_bytes(make_pdf("plain"))
        await run({"mode": "dir", "path": str(docs)})
        assert ("image_describer" in seen) is enabled, (key, vision)
        assert ("table_describer" in seen) is enabled, (key, vision)


# ── провязка с sources.py ────────────────────────────────────────────────────


def test_pdf_source_is_registered():
    from eventgraph.sources import BY_NAME

    source = BY_NAME["pdf_docs"]
    assert source.kind == "pdf"
    assert source.images is True
    assert source.timeout is not None
    assert source.config["mode"] == "dir"
    assert source.config["path"] == "/data/pdfs"
