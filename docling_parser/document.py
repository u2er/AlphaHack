"""Тип результата парсера
"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedDocument:
    """Текстовый фрагмент документа и его метаданные."""

    page_content: str
    metadata: dict[str, Any] = field(default_factory=dict)
