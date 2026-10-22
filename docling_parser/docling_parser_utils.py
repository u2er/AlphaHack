"""Кастомные markdown-сериализаторы для docling.

По умолчанию docling при экспорте в markdown всегда рендерит таблицы как
markdown-таблицы. В сложной таблице такое представление часто теряет
структуру (слияния ячеек и т.п.), поэтому здесь:
  - таблицы в тексте заменяются маркером [TABLE] + подписью/заголовками;
    сама таблица передаётся как изображение (см. docling_parser.py);
  - списки, инлайновые группы, key-value и формы оборачиваются парами
    маркеров [START_LIST]...[END_LIST] и т.п.
"""
from docling_core.types.doc.document import (DOCUMENT_TOKENS_EXPORT_LABELS,
                                              DEFAULT_CONTENT_LAYERS,
                                              ListGroup,
                                              InlineGroup,
                                              KeyValueItem,
                                              DoclingDocument,)
from docling_core.transforms.serializer.markdown import (MarkdownTableSerializer,
                                                          MarkdownDocSerializer,
                                                          MarkdownParams,
                                                          MarkdownListSerializer,
                                                          MarkdownInlineSerializer,
                                                          MarkdownKeyValueSerializer,
                                                          MarkdownFormSerializer)
from docling_core.transforms.serializer.base import (BaseTableSerializer,
                                                      BaseInlineSerializer,
                                                      BaseKeyValueSerializer,
                                                      BaseFormSerializer,
                                                      SerializationResult)
from docling_core.transforms.serializer.common import create_ser_result
from docling_core.types.doc import ImageRefMode, TableItem

from typing import Any, Optional


# Переопределяем serializer для таблиц. При сериализации документа в markdown по умолчанию нет возможности НЕ конвертировать таблицы.
# Если талица сложная, то ее markdown представление может получиться не инфомативным.
# Все таблицы сохраняются в виде изображений и передаются в таком виде модели. В тексте остается лишь маркер [TABLE]
class CustomMarkdownTableSerializer(MarkdownTableSerializer):
    def serialize(self, *, item: TableItem, doc_serializer, doc, **kwargs):

        # Не выводим таблицу, если она исключена фильтром по страницам/диапазону
        if item.self_ref in doc_serializer.get_excluded_refs(**kwargs):
            return create_ser_result(text="", span_source=item)
        # Игнорируем вложенные таблицы
        if kwargs.get("_nested_in_table"):
            return create_ser_result(text="", span_source=item)

        caption = item.caption_text(doc) or ""

        # Сбор заголовков столбцов
        col_headers = []
        if item.data.num_rows > 0 and item.data.num_cols > 0:
            num_headers = 0
            for i, row in enumerate(item.data.grid):
                any_header = any(cell.column_header for cell in row)
                if any_header:
                    num_headers += 1
                else:
                    break

            if num_headers > 0:
                col_headers = ["" for _ in range(item.data.num_cols)]
                for i in range(num_headers):
                    for j, cell in enumerate(item.data.grid[i]):
                        cell_text = cell._get_text(doc=doc, **kwargs)
                        if col_headers[j] != "":
                            cell_text = f".{cell_text}"
                        col_headers[j] += cell_text

        # Сбор заголовков строк
        row_headers = []
        if item.data.num_rows > num_headers:
            for row in item.data.grid[num_headers:]:
                # Собираем текст из ячеек, помеченных как row_header
                row_header_cells = [cell._get_text(doc=doc, **kwargs) for cell in row if cell.row_header]
                if row_header_cells:
                    row_headers.append(" ".join(row_header_cells))

        parts = ["[TABLE]"]
        if caption:
            parts.append(f"Подпись к таблице: {caption}")
        if col_headers:
            parts.append(f"Столбцы таблицы: {', '.join(col_headers)}")
        if row_headers:
            parts.append(f"Строки таблицы: {', '.join(row_headers)}")

        text = "\n".join(parts)
        return create_ser_result(text=text, span_source=item)


class CustomMarkdownKeyValueSerializer(MarkdownKeyValueSerializer):
    def serialize(self, *, item: KeyValueItem, doc_serializer, doc, **kwargs) -> SerializationResult:

        if item.self_ref in doc_serializer.get_excluded_refs(**kwargs):
            return create_ser_result(text="", span_source=item)

        base_serialization_result = super().serialize(item=item, doc_serializer=doc_serializer, doc=doc, **kwargs)
        if not base_serialization_result.text:
            return create_ser_result()
        marked_result = f"[START_KV]\n{base_serialization_result.text}\n[END_KV]"
        return SerializationResult(text=marked_result, spans=base_serialization_result.spans)


class CustomMarkdownListSerializer(MarkdownListSerializer):
    def serialize(self, *, item: ListGroup, doc_serializer, doc: DoclingDocument, list_level: int = 0, is_inline_scope: bool = False,
                  visited: Optional[set[str]] = None, **kwargs: Any,) -> SerializationResult:

        if item.self_ref in doc_serializer.get_excluded_refs(**kwargs):
            return create_ser_result(text="", span_source=item)
        base_serialization_result = super().serialize(item=item, doc_serializer=doc_serializer, doc=doc, list_level=list_level, is_inline_scope=is_inline_scope, visited=visited, **kwargs)
        if not base_serialization_result.text:
            return create_ser_result()
        marked_result = f"[START_LIST]\n{base_serialization_result.text}\n[END_LIST]"
        return SerializationResult(text=marked_result, spans=base_serialization_result.spans)


class CustomMarkdownInlineSerializer(MarkdownInlineSerializer):
    def serialize(self, *, item: InlineGroup, doc_serializer, doc: DoclingDocument, list_level: int = 0, is_inline_scope: bool = False,
                  visited: Optional[set[str]] = None, **kwargs: Any,) -> SerializationResult:

        if item.self_ref in doc_serializer.get_excluded_refs(**kwargs):
            return create_ser_result(text="", span_source=item)
        base_serialization_result = super().serialize(item=item, doc_serializer=doc_serializer, doc=doc, list_level=list_level, is_inline_scope=is_inline_scope, visited=visited, **kwargs)
        if not base_serialization_result.text:
            return create_ser_result()
        marked_result = f"[START_INLINE]\n{base_serialization_result.text}\n[END_INLINE]"
        return SerializationResult(text=marked_result, spans=base_serialization_result.spans)


class CustomMarkdownDocSerializer(MarkdownDocSerializer):
    # text_serializer: BaseTextSerializer = MarkdownTextSerializer()
    table_serializer: BaseTableSerializer = CustomMarkdownTableSerializer()
    key_value_serializer: BaseKeyValueSerializer = CustomMarkdownKeyValueSerializer()
    form_serializer: BaseFormSerializer = MarkdownFormSerializer()
    # fallback_serializer: BaseFallbackSerializer = MarkdownFallbackSerializer()

    list_serializer: BaseTableSerializer = CustomMarkdownListSerializer()
    inline_serializer: BaseInlineSerializer = CustomMarkdownInlineSerializer()

    # meta_serializer: BaseMetaSerializer = MarkdownMetaSerializer()
    # annotation_serializer: BaseAnnotationSerializer = MarkdownAnnotationSerializer()


# Обертка для функции export_to_markdown: по умолчанию docling не позволяет
# использовать кастомные сериализаторы, поэтому передаём свои явно.
# Остальные параметры MarkdownParams — значения по умолчанию.
def custom_export_to_markdown(doc,
                              page_no: Optional[int] = None,
                              image_mode = ImageRefMode.PLACEHOLDER,
                              image_placeholder: str = "[IMAGE]",
                              include_annotations: bool = True) -> str:
    """Экспортирует документ (или одну страницу) в markdown кастомными
    сериализаторами: таблицы — маркер [TABLE] + подпись/заголовки, списки и
    другие структурные элементы — пары маркеров [START_...]/[END_...]."""
    # Используем CustomMarkdownDocSerializer, а не MarkdownDocSerializer, как в оригинале
    serializer = CustomMarkdownDocSerializer(
        doc=doc,
        params=MarkdownParams(
            labels=DOCUMENT_TOKENS_EXPORT_LABELS,
            layers=DEFAULT_CONTENT_LAYERS,
            pages={page_no} if page_no is not None else None,
            image_placeholder=image_placeholder,
            image_mode=image_mode,
            include_annotations=include_annotations))
    ser_res = serializer.serialize()
    return ser_res.text
