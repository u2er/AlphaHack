# PDF-документы: каталог с файлами (mode "dir") или прямые ссылки (mode "url").
#
# Настройки (`config` источника в sources.py, приходят в ctx.config):
#   mode             — откуда брать: "dir" (по умолчанию) | "url";
#   path             — каталог с PDF-файлами (mode "dir"); читаются *.pdf;
#   urls             — прямые ссылки на PDF-файлы (mode "url"): список или строка;
#   max_files_per_run — сколько новых файлов разобрать за запуск (3);
#   max_images       — не больше стольких картинок на запись (3);
#   max_tables       — не больше стольких таблиц описать LLM (3);
#   max_file_bytes   — потолок размера одного файла (50 МБ);
#   pdfparser_kwargs — доп. настройки движка, например {"images_scale": 2};
#   source           — подпись источника, уйдёт в data["source"].
#
# Парсинг — движком docling (пакет docling_parser в корне репозитория).
#
# Курсор — ctx.state["seen"]: {имя файла | url: sha256}. Файл, чьё содержимое
# не менялось, повторно не парсится; изменилось — новая запись (external_id —
# SHA-256 файла). В mode "url" адрес считается неизменным, пока курсор не
# сброшен (POST /connectors/{name}/state/reset). Кэш парсера —
# <каталог данных>/pdf_cache/<sha256>: повторный запуск на тот же файл читает
# результат из кэша, не парся документ заново.
#
# Запись: text — markdown всего документа (маркеры [TABLE], [IMAGE], списки),
# image — вычитанные картинки и кропы таблиц (≤ max_images), id — SHA-256 файла;
# в data — url (mode "url"), file_name, source, published (Last-Modified или
# mtime), pages, sha256.
#
# Парсер импортируется лениво в _make_manager: тяжёлые зависимости
# (torch, pandas) не должны тянуться в API-процесс. Парсинг — синхронный и
# CPU-тяжёлый, но collect и так живёт в отдельном потоке воркера, поэтому
# вызывается прямо в нём.
#
# Если LLM настроена и видит картинки (LLM_API_KEY + LLM_VISION), каждое
# изображение (не больше max_images) и каждая таблица (не больше max_tables)
# описывается одним VLM-вызовом по кропу, и в тексте вместо голого [IMAGE] /
# [TABLE] остаётся «[IMAGE] <описание>» / «[TABLE] <описание>» — агенту они
# понятны и без vision. Без ключа — обычные маркеры. Бюджеты независимы:
# таблицы не «съедают» описания картинок и наоборот.

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from eventgraph.config import settings
from eventgraph.connectors.images import MEDIA_TYPES, shrink
from eventgraph.connectors.runtime import Context

# Курсор: сколько увиденных файлов помним.
SEEN_CAP = 1000
DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024

# Длинная сторона изображения, уходящего на описание в VLM. Графики читаются
# по осям и легенде — 1024 px для этого мало, поэтому по умолчанию 1600.
IMAGE_DESC_EDGE = 1600

# Масштаб рендеринга страниц docling (влияет на кропы картинок и таблиц):
# при 2 подписи на графике ~500 px и не читаются.
DEFAULT_IMAGES_SCALE = 3

# Иконки и логотипы мельче этого (по длинной стороне, px кропа): описывать
# нечего, агенту они не нужны — не тратим на них бюджет описаний и запись.
MIN_MEDIA_EDGE = 200

# Как показать описателю подпись и текст вокруг элемента.
_CONTEXT_HEADER = "Контекст из документа:"

_PICTURE_PROMPT = """\
Ты описываешь иллюстрацию из аналитического документа для базы знаний.
Опиши её подробно и по делу — 5–10 предложений — без вступлений и оценок.

Описывай только то, что видно на самом изображении. Числа, диапазоны шкал и
состав легенды бери только с картинки. Контекст из документа (подпись,
окружающий текст) — это фон: он помогает верно прочитать названия, единицы и
обозначения, но если он расходится с изображением, верь изображению. Не
пересказывай контекст и не добавляй серии, оси и значения, которых на
изображении нет.

Если это график, диаграмма или схема, обязательно укажи:
1) тип графика и что он показывает;
2) оси: что отложено по горизонтали и вертикали, единицы измерения,
   диапазон и шаг шкалы;
3) легенду: все серии, их цвет или стиль и что каждая означает;
4) ключевые значения: уровни, пики и минимумы с примерными цифрами и
   периодами, переломы тренда, сравнение серий или годов;
5) вывод, который следует из графика.
Если это фотография или иллюстрация — перечисли, что на ней видно, все
надписи и логотипы. Данных не выдумывай: что не читается, так и скажи.
Ответ — только описание."""

_TABLE_PROMPT = """\
Ты описываешь таблицу из аналитического документа для базы знаний.
Опиши её подробно и по делу — 4–8 предложений — без вступлений.

Описывай таблицу с изображения; markdown с точными значениями — сверка для
цифр, а не замена описания. Числа и структуру бери из таблицы; окружающий
текст документа не пересказывай и не добавляй показателей, которых в таблице
нет.

Обязательно укажи:
1) что показывает таблица и за какой период или в каком разрезе;
2) строки и столбцы: какие показатели и периоды/категории;
3) единицы измерения и базу (%, г/г, шт., руб. и т. п.);
4) ключевые цифры: самые большие и маленькие значения, тренды и
   переломы — с числами из таблицы; приводи значение вместе с его годом
   или периодом, не объединяй числа разных лет в диапазоны и не
   пересчитывай их сам;
5) итоговый вывод.
Если для таблицы передан markdown с точными значениями, опирайся на него.
Данных не выдумывай. Ответ — только описание."""

_DESCRIPTION_PROMPT = _PICTURE_PROMPT
_TABLE_DESCRIPTION_PROMPT = _TABLE_PROMPT


def options(config: dict[str, Any]) -> dict[str, Any]:
    mode = str(config.get("mode") or "dir").strip().lower()
    if mode not in ("dir", "url"):
        raise ValueError(f"mode={mode!r}: допустимо 'dir' или 'url'")

    raw_urls = config.get("urls")
    urls: list[str] = []
    if raw_urls is not None:
        urls = [raw_urls] if isinstance(raw_urls, str) else list(raw_urls)
    urls = [str(u).strip() for u in urls if str(u).strip()]

    path = str(config.get("path") or "").strip()
    if mode == "dir" and not path:
        raise ValueError("path: укажите каталог с PDF-файлами, например /data/pdfs")
    if mode == "url" and not urls:
        raise ValueError("urls: укажите прямые ссылки на PDF-файлы (список или строку)")

    try:
        max_files = int(config.get("max_files_per_run", 3))
        max_images = int(config.get("max_images", 3))
        max_tables = int(config.get("max_tables", 3))
        max_bytes = int(config.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES))
    except (TypeError, ValueError):
        raise ValueError("max_files_per_run, max_images, max_tables и max_file_bytes — целые числа") from None
    if max_files < 1:
        raise ValueError("max_files_per_run: не меньше 1")
    if max_images < 0:
        raise ValueError("max_images: не меньше 0")
    if max_tables < 0:
        raise ValueError("max_tables: не меньше 0")
    if max_bytes < 1:
        raise ValueError("max_file_bytes: не меньше 1")

    kwargs = config.get("pdfparser_kwargs") or {}
    if not isinstance(kwargs, dict):
        raise ValueError("pdfparser_kwargs: должен быть словарём, например {'images_scale': 2}")

    return {
        "mode": mode,
        "path": path,
        "urls": urls,
        "max_files": max_files,
        "max_images": max_images,
        "max_tables": max_tables,
        "max_bytes": max_bytes,
        "kwargs": kwargs,
        "source": str(config.get("source") or "").strip() or None,
    }


# ── кандидаты на парсинг ─────────────────────────────────────────────────────


def _sha256(path: Path) -> str:
    """Как compute_file_sha256 парсера, только без его импорта на уровне модуля."""
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _published_from_mtime(path: Path) -> str | None:
    try:
        at = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat()


def _published_from_http(value: str | None) -> str | None:
    """Last-Modified заголовка в ISO — дата публикации, если сайт её даёт."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _suffix(url: str) -> str:
    tail = str(url).rsplit("/", 1)[-1].split("?", 1)[0]
    dot = tail.rfind(".")
    if 0 <= dot < len(tail) - 1 and tail[dot + 1:].isalnum():
        return "." + tail[dot + 1:].lower()
    return ".pdf"


async def _download(ctx: Context, url: str, dest: Path, max_bytes: int) -> str | None:
    """Файл по ссылке в dest. Возвращает published (Last-Modified) или None."""
    response = await ctx.http.get(url)
    if response.status_code != 200:
        raise RuntimeError(f"{url}: HTTP {response.status_code}")
    data = response.content
    if len(data) > max_bytes:
        raise RuntimeError(f"{url}: файл больше {max_bytes // (1024 * 1024)} МБ")
    if not data.startswith(b"%PDF"):
        raise RuntimeError(f"{url}: это не PDF-файл")
    dest.write_bytes(data)
    return _published_from_http(response.headers.get("last-modified"))


async def _dir_candidates(opts: dict[str, Any], seen: dict[str, str]) -> list[tuple[str, Path, str | None]]:
    """(имя файла, путь, published) тех, чьё содержимое ещё не видено."""
    root = Path(opts["path"])
    if not root.is_dir():
        raise RuntimeError(f"каталог {root} не найден — смонтируйте его в контейнер или поправьте path")
    out = []
    for path in sorted(root.glob("*.pdf")):
        if not path.is_file():
            continue
        sha = await asyncio.to_thread(_sha256, path)
        if seen.get(path.name) == sha:
            continue
        out.append((path.name, path, _published_from_mtime(path)))
    return out


async def _url_candidates(
    opts: dict[str, Any], ctx: Context, tmp: str, seen: dict[str, str]
) -> tuple[list[tuple[str, Path, str | None]], int]:
    """(кандидаты, сколько ссылок не скачались). Кандидат — (url, файл,
    published). Ошибка скачивания не валит запуск: ссылка не помечается
    увиденной и добирается в следующий раз."""
    out, failed = [], 0
    for n, url in enumerate(opts["urls"]):
        if seen.get(url):
            continue  # адрес уже виден: содержимое считаем неизменным
        dest = Path(tmp) / f"dl-{n}{_suffix(url)}"
        try:
            published = await _download(ctx, url, dest, opts["max_bytes"])
        except RuntimeError as exc:
            failed += 1
            ctx.log(f"{url}: {exc} — попробую в следующий раз")
            continue
        out.append((url, dest, published))
    return out, failed


# ── парсинг ──────────────────────────────────────────────────────────────────


def _media_key(path: Path) -> tuple[int, int]:
    """'1__3.png' — картинка/таблица №1 на 3-й странице: по номерам, не по строкам."""
    try:
        n, page = path.stem.split("__")
        return int(n), int(page)
    except ValueError:
        return (10 ** 9, 0)


def _png_size(data: bytes) -> tuple[int, int] | None:
    """Размер PNG из заголовка IHDR — без PIL (картинка может быть любой)."""
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _collect_images(manager: Any, limit: int) -> list[bytes]:
    """Картинки и кропы таблиц из кэша — байтами: сначала картинки, потом таблицы.

    Читаем с диска, а не из памяти: после загрузки результата из кэша в памяти
    остаются только тексты, а PNG лежат в content_dir — и на обоих путях так.
    Иконки и логотипы (мельче MIN_MEDIA_EDGE) пропускаем: агенту они ничего не
    дают, а место в бюджете картинок занимают.
    """
    if limit <= 0:
        return []
    out: list[bytes] = []
    for sub in ("images", "tables"):
        folder = Path(manager.content_dir) / sub
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.png"), key=_media_key):
            try:
                data = path.read_bytes()
            except OSError:
                continue
            size = _png_size(data)
            if size and max(size) < MIN_MEDIA_EDGE:
                continue
            out.append(data)
            if len(out) >= limit:
                return out
    return out


def _make_manager(file_path: Path, cache_dir: Path, kwargs: dict[str, Any]) -> Any:
    """Единственная точка ленивого импорта тяжёлого стека (docling/torch).

    Unit-тесты подменяют её фейком: логика коннектора так проверяется без
    ML-зависимостей; реальный движок покрывает integration-тест.
    """
    from docling_parser.file_manager import PdfFileManager

    return PdfFileManager(file_path=str(file_path), cache_dir=str(cache_dir), **kwargs)


def _cache_key(describer: Any, table_describer: Any, images_scale: Any) -> str:
    """Версия рецепта парсинга: от неё зависит, годится ли старый кэш.

    Кэш — тексты страниц с уже вписанными описаниями, поэтому смена промпта,
    модели или разрешения обязана его инвалидировать; иначе в записи надолго
    останутся описания от старых настроек. Хэш короткий — файл в кэше.
    """
    recipe = "|".join((
        "v2",                                    # версия самой схемы описаний
        _DESCRIPTION_PROMPT if describer else "-",
        _TABLE_DESCRIPTION_PROMPT if table_describer else "-",
        str(settings.llm_model),
        str(settings.llm_base_url),
        f"edge={IMAGE_DESC_EDGE}",
        f"scale={images_scale}",
    ))
    return hashlib.sha256(recipe.encode("utf-8")).hexdigest()[:16]


def _image_data_url(image: Any) -> str:
    """PIL-изображение → data-URL для vision-модели (длинная сторона ≤ IMAGE_DESC_EDGE)."""
    import base64
    import io

    if image is None:
        raise ValueError("изображение недоступно (docling не извлёк его пиксели)")
    alpha = image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info)
    buf = io.BytesIO()
    image.convert("RGBA" if alpha else "RGB").save(
        buf, format="PNG" if alpha else "JPEG", quality=85
    )
    data, ext = shrink(buf.getvalue(), "png" if alpha else "jpg", IMAGE_DESC_EDGE)
    return f"data:{MEDIA_TYPES[ext]};base64,{base64.b64encode(data).decode('ascii')}"


def _context_block(context: dict[str, Any] | None) -> str:
    """Подпись, markdown таблицы и текст страницы — одним блоком для промпта."""
    if not context:
        return ""
    parts: list[str] = []
    if context.get("caption"):
        parts.append(f"Подпись: {context['caption']}")
    if context.get("table_markdown"):
        parts.append("Значения из таблицы (markdown):\n" + str(context["table_markdown"]))
    if context.get("page_text"):
        parts.append("Текст документа рядом:\n" + str(context["page_text"]))
    if not parts:
        return ""
    return _CONTEXT_HEADER + "\n" + "\n\n".join(parts)


def _make_describer(max_items: int, prompt: str) -> Any:
    """Генератор описаний для движка (PIL.Image [, context] → str) — или None.

    Включается, только если LLM настроена и видит картинки (LLM_API_KEY и
    LLM_VISION). Таблицы описываются тем же VLM по кропу области таблицы,
    со своим бюджетом и промптом. Клиент openai создаётся здесь — в
    API-процессе и в unit-тестах (без ключа) он не появляется.

    Описателю движка передаётся контекст элемента (подпись, markdown таблицы,
    текст страницы) — с ним описание графика точнее; описатель принимает
    его вторым, необязательным аргументом.
    """
    if not (settings.llm_api_key and settings.llm_vision) or max_items <= 0:
        return None
    from openai import OpenAI

    client = OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout,
    )
    budget = [max_items]
    extra = dict(settings.llm_extra)
    # Подробное описание графика — это 250–400 токенов; без запаса
    # «думающая» модель может утащить весь бюджет на рассуждение.
    extra.setdefault("max_tokens", 1024)
    # «Думающие» модели тратят на рассуждение 2–6 тыс. токенов и отдают пустой
    # content (finish_reason=length). Описанию рассуждение не нужно, поэтому
    # просим его отключить; если провайдер такого не понимает — без параметра.
    thinking = [not any(k in extra for k in ("reasoning_effort", "reasoning"))]
    # Если провайдер не отключает рассуждение — один повтор с запасом.
    try:
        retry_tokens = max(int(extra["max_tokens"]) * 2, 2048)
    except (TypeError, ValueError):
        retry_tokens = 2048

    def ask(params: dict[str, Any], image: Any, context: dict[str, Any] | None) -> str:
        block = _context_block(context)
        text = f"{prompt}\n\n{block}" if block else prompt
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": _image_data_url(image)}},
            ]}],
            **params,
        )
        return (response.choices[0].message.content or "").strip()

    def complete(image: Any, context: dict[str, Any] | None) -> str:
        """Один вызов с отключённым рассуждением; пустой ответ — повтор."""
        params = {**extra, "reasoning_effort": "none"} if thinking[0] else extra
        try:
            text = ask(params, image, context)
        except Exception:
            if not thinking[0]:
                raise
            # Провайдер не знает reasoning_effort — дальше зовём без него.
            thinking[0] = False
            text = ask(extra, image, context)
        if not text:
            params = {**extra, "max_tokens": retry_tokens}
            if thinking[0]:
                params["reasoning_effort"] = "none"
            text = ask(params, image, context)
        return text

    def describe(image: Any, context: dict[str, Any] | None = None) -> str | None:
        size = getattr(image, "size", None)
        if size and max(size) < MIN_MEDIA_EDGE:
            # Иконка/логотип: описывать нечего, бюджет не тратим.
            return None
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        return complete(image, context) or None

    return describe


async def _parse_file(
    ctx: Context, path: Path, opts: dict[str, Any], url: str | None, published: str | None
) -> tuple[dict[str, Any] | None, str]:
    """Один PDF → (запись, sha256). Запись None — документ распарсился,
    но нести нечего: ни текста, ни картинок."""
    sha = _sha256(path)
    cache_dir = settings.data_dir / "pdf_cache"
    kwargs = dict(opts["kwargs"])
    describer = _make_describer(opts["max_images"], _DESCRIPTION_PROMPT)
    if describer is not None:
        kwargs["image_describer"] = describer
        ctx.log(f"{path.name}: изображения описываются LLM (до {opts['max_images']})")
    table_describer = _make_describer(opts["max_tables"], _TABLE_DESCRIPTION_PROMPT)
    if table_describer is not None:
        kwargs["table_describer"] = table_describer
        ctx.log(f"{path.name}: таблицы описываются LLM (до {opts['max_tables']})")
    # Разрешение вырезов: описание графика держится на осях и легенде, при
    # масштабе 2 подписи на кропе ~500 px уже не читаются.
    kwargs.setdefault("images_scale", DEFAULT_IMAGES_SCALE)
    # Версия рецепта: сменился промпт, разрешение или модель — кэш перепарсим,
    # иначе в тексте остались бы описания от старых настроек.
    kwargs["cache_key"] = _cache_key(describer, table_describer, kwargs["images_scale"])
    manager = _make_manager(path, cache_dir, kwargs)
    cached = await asyncio.to_thread(manager.data_exists)
    await asyncio.to_thread(manager.extract_content)

    if manager.text is not None:
        text = (manager.text.page_content or "").strip()
    else:
        # путь через кэш не восстанавливает полный текст документа — соберём из страниц
        text = "\n\n".join(doc.page_content for doc in manager.texts if doc.page_content).strip()
    images = _collect_images(manager, opts["max_images"])
    if not text and not images:
        ctx.log(f"{path.name}: в документе нет ни текста, ни картинок — не берётся")
        return None, sha

    record: dict[str, Any] = {
        "id": sha,
        "text": text,
        "file_name": path.name,
        "pages": len(manager.texts),
        "sha256": sha,
    }
    if url:
        record["url"] = url
    if opts["source"]:
        record["source"] = opts["source"]
    if published:
        record["published"] = published
    if images:
        record["image"] = images
    ctx.log(
        f"{path.name}: {len(manager.texts)} стр., {len(images)} картинок"
        + (" (из кэша)" if cached else "")
    )
    return record, sha


def _mark(seen: dict[str, str], key: str, sha: str) -> None:
    seen.pop(key, None)
    seen[key] = sha


# ── запуск ───────────────────────────────────────────────────────────────────


async def collect(ctx: Context) -> list[dict[str, Any]]:
    opts = options(ctx.config)
    seen: dict[str, str] = dict(ctx.state.get("seen") or {})
    tmp: tempfile.TemporaryDirectory | None = None
    try:
        if opts["mode"] == "url":
            tmp = tempfile.TemporaryDirectory(prefix="eventgraph-pdf-")
            candidates, failed = await _url_candidates(opts, ctx, tmp.name, seen)
        else:
            candidates, failed = await _dir_candidates(opts, seen), 0

        candidates = candidates[: opts["max_files"]]
        if not candidates:
            if failed:
                # ссылки заданы, но ни один файл не скачался — молча молчать нельзя
                raise RuntimeError(f"ни один из {failed} файлов не скачался по ссылкам")
            ctx.log(f"{opts['mode']}: новых PDF нет")
            return []

        records: list[dict[str, Any]] = []
        failures = 0
        for key, path, published in candidates:
            url = key if opts["mode"] == "url" else None
            try:
                parsed, sha = await _parse_file(ctx, path, opts, url, published)
            except Exception as exc:  # noqa: BLE001 — сбой одного файла не валит запуск
                failures += 1
                ctx.log(f"{key}: парсинг не удался ({type(exc).__name__}: {exc}) — попробую в следующий раз")
                continue
            if parsed is None:
                _mark(seen, key, sha)  # пустой документ: повторять не зачем
                continue
            _mark(seen, key, sha)
            records.append(parsed)

        if failures == len(candidates):
            raise RuntimeError(f"не удалось разобрать ни один из {len(candidates)} файлов PDF")
        ctx.state["seen"] = dict(list(seen.items())[-SEEN_CAP:])
        ctx.log(
            f"{opts['mode']}: файлов {len(candidates)}, записей {len(records)}"
            + (f", ошибок {failures}" if failures else "")
        )
        return records
    finally:
        if tmp is not None:
            tmp.cleanup()
