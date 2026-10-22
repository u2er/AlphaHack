"""Ручной smoke-тест.

Запуск:
    python docling_parser/examples/run_example.py /path/to/doc.pdf
    python docling_parser/examples/run_example.py doc.pdf --cache-dir ./cache

Парсит один PDF через PdfFileManager (так же, как коннектор eventgraph),
печатает статистику (страницы/таблицы/изображения) и сохраняет полный
markdown в <out-dir>/<stem>_full.md (по умолчанию <cache-dir>).

Переменные окружения (опционально):
    HF_HOME          — кэш HuggingFace с моделями docling
    TESSDATA_PREFIX  — каталог с tesseract traineddata (rus/eng)
"""
import argparse
import sys
from pathlib import Path

# Корень репозитория: examples/ лежит внутри пакета docling_parser/.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docling_parser import PdfFileManager


def main():
    ap = argparse.ArgumentParser(description="Парсит PDF через docling_parser")
    ap.add_argument("pdf", help="путь к PDF-файлу")
    ap.add_argument("--cache-dir", default=".cache_parser")
    ap.add_argument("--out-dir", default=None,
                    help="куда сохранить полный markdown (по умолчанию <cache-dir>)")
    args = ap.parse_args()

    manager = PdfFileManager(file_path=args.pdf, cache_dir=args.cache_dir)
    manager.extract_content()

    print(f"Страниц: {len(manager.texts)}, таблиц: {len(manager.tables)}, изображений: {len(manager.images)}")
    if manager.texts:
        first = manager.texts[0]
        preview = first.page_content[:500].replace("\n", " ")
        print(f"Предпросмотр страницы {first.metadata.get('page')}: {preview} ...")

    out_dir = Path(args.out_dir) if args.out_dir else Path(args.cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{Path(args.pdf).stem}_full.md"
    if manager.text is not None:
        full = manager.text.page_content
    else:
        # путь через кэш не восстанавливает полный текст — соберём из страниц
        full = "\n\n".join(d.page_content for d in manager.texts if d.page_content)
    out.write_text(full, encoding="utf-8")
    print(f"Полный текст сохранён: {out}")


if __name__ == "__main__":
    main()
