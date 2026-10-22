"""Лёгкие текст-утилиты пакета: без зависимости от docling и torch.

Хранят маркеры [IMAGE]/[TABLE] и функцию прикрепления описаний к ним: движок
отдаёт markdown с маркерами, а описания (если их кто-то сгенерировал —
например, VLM коннектора) подставляются рядом с ними.
"""
from typing import Sequence

# Маркер изображения в markdown (значение image_placeholder, которым движок
# вызывает экспорт — см. docling_parser.py).
IMAGE_MARKER = "[IMAGE]"

# Маркер таблицы: его пишет CustomMarkdownTableSerializer (вместо markdown-таблицы).
TABLE_MARKER = "[TABLE]"


def inject_descriptions(markdown: str, descriptions: Sequence[str | None],
                        marker: str = IMAGE_MARKER) -> str:
    """Прикрепляет описания к маркерам ``marker`` в markdown.

    ``descriptions`` упорядочены по документному порядку маркеров: i-е
    значение относится к i-му вхождению маркера (сериализатор docling
    раскладывает их в том же порядке, что и ``iterate_items``). ``None`` или
    пустая строка — маркер остаётся как есть. Если число маркеров и описаний
    не совпадает, лишние (любой стороны) остаются без описаний: текст не
    ломаем.
    """
    parts = markdown.split(marker)
    marker_count = len(parts) - 1
    count = min(marker_count, len(descriptions))

    out = [parts[0]]
    for i in range(count):
        desc = (descriptions[i] or "").strip()
        out.append(marker + (f" {desc}" if desc else ""))
        out.append(parts[i + 1])
    if marker_count > count:
        out.append(marker + marker.join(parts[count + 1:]))
    return "".join(out)


def inject_image_descriptions(markdown: str, descriptions: Sequence[str | None]) -> str:
    """Прикрепляет описания к маркерам [IMAGE] (см. inject_descriptions)."""
    return inject_descriptions(markdown, descriptions, IMAGE_MARKER)


def inject_table_descriptions(markdown: str, descriptions: Sequence[str | None]) -> str:
    """Прикрепляет описания к маркерам [TABLE] (см. inject_descriptions).

    Описание встаёт на строку маркера: «[TABLE] <описание>»; подпись и
    заголовки таблицы остаются следующими строками.
    """
    return inject_descriptions(markdown, descriptions, TABLE_MARKER)
