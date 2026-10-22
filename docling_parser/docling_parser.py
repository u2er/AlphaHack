"""Парсер PDF на основе docling.

Извлекает текст (markdown с маркерами элементов), таблицы
(dataframe + markdown + кроп изображения) и изображения.
"""
from typing import Dict, Any
import inspect
import logging

logger = logging.getLogger(__name__)

from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractOcrOptions, TableFormerMode, TableStructureOptions
from docling.datamodel.base_models import InputFormat
from docling_core.types.doc import ImageRefMode, PictureItem, TableItem

from docling_parser.docling_parser_utils import custom_export_to_markdown
from docling_parser.utils.markdown_utils import (IMAGE_MARKER, TABLE_MARKER,
                                                 inject_descriptions)

# Сколько контекста отдаём описателю: markdown таблицы с точными значениями
# важнее картинки, а для рисунков контекст — окно вокруг подписи.
TABLE_CONTEXT_CHARS = 4000



def recompute_coord_for_pil(left, top, right, bottom, page_h, scale):
    """Пересчитывает координаты docling (PDF-система, от верхнего левого угла,
    нормализованные к высоте страницы) в систему координат Pillow."""
    pil_left = left*scale
    pil_top  = page_h - top*scale
    pil_right  = right*scale
    pil_bottom  = page_h - bottom*scale
    return pil_left, pil_top, pil_right, pil_bottom


def _accepts_context(describer) -> bool:
    """Принимает ли описатель второй аргумент (контекст)?

    Простые описатели (`lambda image: …`, тесты) оставляем с одним
    аргументом; контекст — опциональное расширение.
    """
    try:
        parameters = inspect.signature(describer).parameters.values()
    except (TypeError, ValueError):
        return False
    positional = [p for p in parameters
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return any(p.kind == p.VAR_POSITIONAL for p in parameters) or len(positional) >= 2


def _is_small(image, edge: int = 200) -> bool:
    """Иконка или логотип: по такой картинке описывать нечего, контекст лишний."""
    try:
        width, height = image.size
    except (AttributeError, TypeError):
        return False
    return max(width, height) < edge


def _page_text_window(page_texts: list[str], page_no: int, caption: str | None = None,
                      around: int = 700) -> str | None:
    """Окно текста вокруг подписи элемента — фон для описателя.

    Контекст нужен, чтобы верно прочитать названия и единицы на картинке.
    Без подписи фона не даём вовсе: «первая страница целиком» сбивала модель,
    и она описывала соседние графики вместо того, что на кропе.
    """
    if not caption or not 1 <= page_no <= len(page_texts):
        return None
    raw = page_texts[page_no - 1] or ""
    lines = [line.strip() for line in raw.splitlines()]
    useful = "\n".join(line for line in lines
                       if line and not line.startswith(("[", "Подпись к таблице", "Столбцы таблицы",
                                                        "Строки таблицы", "##")))
    at = useful.find(caption.strip())
    if at < 0:
        return None
    start = max(0, at - around)
    return useful[start:at + len(caption) + around].strip() or None


class PDFDoclingParser:
    """Извлекает тексты, таблицы и изображения через docling.

    Поддерживаемые kwargs:
        generate_picture_images: bool = True
            Генерировать изображения картинок/рисунков документа.
        image_describer: Callable[[PIL.Image.Image, dict | None], str | None] | None
            Произвольный генератор текстовых описаний изображений
            (например, VLM-вызов из коннектора). Второй аргумент — контекст
            ``{"kind", "page", "n", "caption", "page_text"}``: подпись,
            текст страницы вокруг элемента (описателю он помогает понять
            график). Описатель может принимать и один аргумент — движок
            вызовет его без контекста. Если описатель задан, маркеры [IMAGE]
            в постраничном и полном markdown получают описание рядом —
            ``[IMAGE] <описание>``, — а описания дублируются в поле
            ``description`` записей ``images``. Ошибки описателя не ломают
            парсинг: изображение остаётся с голым маркером [IMAGE].
        table_describer: Callable[[PIL.Image.Image, dict | None], str | None] | None
            То же для таблиц: описания (по кропу области таблицы) получают
            маркеры ``[TABLE]`` — ``[TABLE] <описание>``, — и поле
            ``description`` записей ``tables``. В контексте — подпись,
            markdown таблицы (``table_markdown``) и текст страницы.
        images_scale: int = 2
            Масштаб рендеринга страниц (влияет на разрешение вырезов).
    """

    def __init__(self, **kwargs):
        self.generate_picture_images = kwargs.pop("generate_picture_images", True)
        self.image_describer = kwargs.pop("image_describer", None)
        self.table_describer = kwargs.pop("table_describer", None)
        self.images_scale = kwargs.pop("images_scale", 2)
        self.generate_page_images = True

        if self.image_describer is not None and not callable(self.image_describer):
            raise Exception("image_describer: должна быть функцией (PIL.Image -> str или None)")
        if self.table_describer is not None and not callable(self.table_describer):
            raise Exception("table_describer: должна быть функцией (PIL.Image -> str или None)")
        if len(kwargs):
            raise Exception(f"Переданные аргументы не поддерживаются: {list(kwargs.keys())}")

    def _build_pipeline_options(self) -> PdfPipelineOptions:
        """Собирает настройки пайплайна docling."""
        return PdfPipelineOptions(
            generate_picture_images=self.generate_picture_images,
            images_scale=self.images_scale,
            generate_page_images=self.generate_page_images,
            ocr_options=TesseractOcrOptions(lang=["rus", "eng"]),
            table_structure_options=TableStructureOptions(do_cell_matching=False, mode=TableFormerMode.ACCURATE))

    def extract_content(self, file_path: str) -> Dict[str, Any]:
        """Парсит документ и возвращает извлечённый контент.

        Returns:
            dict с ключами:
                texts:     list[str] — markdown по страницам (индекс = page_no - 1)
                full_text: str — markdown всего документа
                tables:    list[dict] — {'df', 'text', 'image', 'page', 'n', 'caption'}
                images:    list[dict] — {'image', 'text', 'page', 'n', 'caption', 'description'}
                                 (description — только если задан image_describer)
        """
        logger.info(f"Использование PDFDoclingParser для парсинга документа")
        pipeline_options = self._build_pipeline_options()

        converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)})

        conversion_result = converter.convert(source=file_path)

        markdown_pages = []
        tables_data = []
        images_data = []
        table_counter = 0
        image_counter = 0

        for page_no, page in conversion_result.document.pages.items():
            # ВАЖНО! при вызове custom_export_to_markdown производится анализ всего дерева документа, несмотря на то, что указана конкретная страниа! (особенность docling)
            # При большом количестве страниц может замедлять время выолнения.
            # Из альтернатив - производить конвертацию страныцы (converter.convert(page_range=(num,num))) вместо всего документа. Может ухудшиться качетство парсинга, поскольку пропадет связь между элементами на разных страницах.
            # docling по умолчанию анализирует документ целиком
            page_markdown = custom_export_to_markdown(doc=conversion_result.document,
                                                  page_no=page_no,
                                                  image_mode=ImageRefMode.PLACEHOLDER,
                                                  image_placeholder=IMAGE_MARKER,
                                                  include_annotations=True)

            page_image = page.image.pil_image
            _, page_h = page_image.size

            for element, _level in conversion_result.document.iterate_items(page_no=page_no):
                if isinstance(element, TableItem):
                    table_counter += 1
                    # Использование element.get_image() на таблицах не всегда работает
                    # Используется подход с определением bbox элемента и дальшейшим вырезанием области из изображения всей странцы
                    bbox = element.prov[0].bbox
                    cropped_image = page_image.crop((recompute_coord_for_pil(bbox.l, bbox.t, bbox.r, bbox.b, page_h, self.images_scale)))

                    df = element.export_to_dataframe(conversion_result.document)
                    tables_data.append({'df': df, 'text': element.export_to_markdown(conversion_result.document), 'image': cropped_image,
                        'page': page_no, 'n': table_counter, 'caption': element.caption_text(conversion_result.document),
                        'description': None})

                if isinstance(element, PictureItem):
                    image_counter += 1
                    images_data.append({'image': element.get_image(conversion_result.document), 'text': element.caption_text(conversion_result.document),
                                        'page': page_no, 'n': image_counter, 'caption': element.caption_text(conversion_result.document),
                                        'description': None})
            markdown_pages.append(page_markdown)
        logger.info(f"Извлечено {len(tables_data)} таблиц, {len(markdown_pages)} страниц и {len(images_data)} изображений из документа {file_path}")

        if self.image_describer is not None:
            self._describe(images_data, self.image_describer, markdown_pages, "image")
        if self.table_describer is not None:
            self._describe(tables_data, self.table_describer, markdown_pages, "table")

        # Описания в документном порядке == порядку маркеров в тексте.
        for page_no in range(1, len(markdown_pages) + 1):
            for marker, data, describer in (
                (IMAGE_MARKER, images_data, self.image_describer),
                (TABLE_MARKER, tables_data, self.table_describer),
            ):
                if describer is None:
                    continue
                descriptions = [e["description"] for e in data if e["page"] == page_no]
                if not any(descriptions):
                    continue
                text = markdown_pages[page_no - 1]
                if text.count(marker) != len(descriptions):
                    logger.warning(f"Страница {page_no}: маркеров {marker} {text.count(marker)}, "
                                   f"элементов {len(descriptions)} — описания привязаны по порядку")
                markdown_pages[page_no - 1] = inject_descriptions(text, descriptions, marker)

        full_markdown = custom_export_to_markdown(doc=conversion_result.document,
                                                  image_mode=ImageRefMode.PLACEHOLDER,
                                                  image_placeholder=IMAGE_MARKER,
                                                  include_annotations=True)
        for marker, data, describer in (
            (IMAGE_MARKER, images_data, self.image_describer),
            (TABLE_MARKER, tables_data, self.table_describer),
        ):
            if describer is None or not data:
                continue
            if full_markdown.count(marker) != len(data):
                logger.warning(f"Маркеров {marker} {full_markdown.count(marker)}, "
                               f"элементов {len(data)} — описания привязаны по порядку")
            full_markdown = inject_descriptions(full_markdown, [e["description"] for e in data], marker)

        return {'texts': markdown_pages, 'full_text': full_markdown, 'tables': tables_data, 'images': images_data}

    def _describe(self, elements: list, describer, page_texts: list[str], kind: str) -> None:
        """Запрашивает у описателя описания элементов (в порядке документа).

        Описателю передаётся контекст элемента (подпись, markdown таблицы,
        окно текста страницы) — по нему он точнее понимает график. Описатель
        может принимать и один аргумент: тогда вызывается без контекста.

        Сбой на одном элементе не прерывает парсинг: запись остаётся
        без описания, а в тексте под ней — голый маркер (например, [IMAGE]).
        """
        accepts_context = _accepts_context(describer)
        for entry in elements:
            caption = (entry.get("caption") or "").strip() or None
            page_text = _page_text_window(page_texts, entry["page"], caption)
            # Иконка/логотип: фон страницы только сбивает — по нему модель
            # описывает соседний график вместо самой картинки.
            if kind == "image" and _is_small(entry.get("image")):
                page_text = None
            context = {
                "kind": kind,
                "page": entry["page"],
                "n": entry["n"],
                "caption": caption,
                "page_text": page_text,
            }
            if kind == "table":
                context["table_markdown"] = (entry.get("text") or "")[:TABLE_CONTEXT_CHARS] or None
            try:
                description = (describer(entry["image"], context) if accepts_context
                               else describer(entry["image"]))
            except Exception as exc:  # noqa: BLE001 — описание опционально
                logger.warning(f"Не удалось описать {kind} {entry['n']} (стр. {entry['page']}): {exc}")
                description = None
            # Описание живёт в строке маркера — переводы строк ломали бы markdown.
            entry["description"] = " ".join((description or "").split()) or None
