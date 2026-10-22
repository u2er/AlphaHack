"""Парсер PDF на основе docling.

Извлекает текст (markdown с маркерами элементов), таблицы
(dataframe + markdown + кроп изображения) и изображения.
"""
from typing import Dict, Any
import logging

logger = logging.getLogger(__name__)

from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractOcrOptions, TableFormerMode, TableStructureOptions
from docling.datamodel.base_models import InputFormat
from docling_core.types.doc import ImageRefMode, PictureItem, TableItem

from docling_parser.docling_parser_utils import custom_export_to_markdown



def recompute_coord_for_pil(left, top, right, bottom, page_h, scale):
    """Пересчитывает координаты docling (PDF-система, от верхнего левого угла,
    нормализованные к высоте страницы) в систему координат Pillow."""
    pil_left = left*scale
    pil_top  = page_h - top*scale
    pil_right  = right*scale
    pil_bottom  = page_h - bottom*scale
    return pil_left, pil_top, pil_right, pil_bottom


class PDFDoclingParser:
    """Извлекает тексты, таблицы и изображения через docling.

    Поддерживаемые kwargs:
        generate_picture_images: bool = True
            Генерировать изображения картинок/рисунков документа.
        do_picture_description: bool = False
            Подключить VLM для создания текстового описания изображений.
        images_scale: int = 2
            Масштаб рендеринга страниц (влияет на разрешение вырезов).
    """

    def __init__(self, **kwargs):
        self.generate_picture_images = kwargs.pop("generate_picture_images", True)
        self.do_picture_description = kwargs.pop("do_picture_description", False)
        self.images_scale = kwargs.pop("images_scale", 2)
        self.generate_page_images = True

        if len(kwargs):
            raise Exception(f"Переданные аргументы не поддерживаются: {list(kwargs.keys())}")

    def _build_pipeline_options(self) -> PdfPipelineOptions:
        """Собирает настройки пайплайна docling."""
        return PdfPipelineOptions(
            generate_picture_images=self.generate_picture_images,
            do_picture_description=self.do_picture_description,  # Можно сразу подключить VLM для создания текстового описания изображений
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
                images:    list[dict] — {'image', 'text', 'page', 'n', 'caption'}
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
                                                  image_placeholder="[IMAGE]",
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
                        'page': page_no, 'n': table_counter, 'caption': element.caption_text(conversion_result.document)})

                if isinstance(element, PictureItem):
                    image_counter += 1
                    images_data.append({'image': element.get_image(conversion_result.document), 'text': element.caption_text(conversion_result.document),
                                        'page': page_no, 'n': image_counter, 'caption': element.caption_text(conversion_result.document)})
            markdown_pages.append(page_markdown)
        logger.info(f"Извлечено {len(tables_data)} таблиц, {len(markdown_pages)} страниц и {len(images_data)} изображений из документа {file_path}")

        full_markdown = custom_export_to_markdown(doc=conversion_result.document,
                                                  image_mode=ImageRefMode.PLACEHOLDER,
                                                  image_placeholder="[IMAGE]",
                                                  include_annotations=True)

        return {'texts': markdown_pages, 'full_text': full_markdown, 'tables': tables_data, 'images': images_data}
