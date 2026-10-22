"""Управление извлечением контента из PDF-файлов.

Капсулирует парсер (движок — docling), кэширует результаты
на диске (ключ — SHA-256 файла), обрабатывает страницы, таблицы и изображения
и возвращает ParsedDocument-объекты.
"""
from pathlib import Path
import logging
import shutil

logger = logging.getLogger(__name__)
from collections import defaultdict

from docling_parser.document import ParsedDocument
from docling_parser.utils.hash_utils import compute_file_sha256



class PdfFileManager:
    """Управляет извлечением контента из PDF-файлов (движок — docling).

    Извлекает текст, таблицы и изображения, сохраняя пути к медиа
    в метаданных.
    """

    def __init__(self, file_path: str,
                 cache_dir: str,
                 cache_key: str | None = None,
                 **parser_kwargs):
        """Инициализирует менеджер с параметрами парсинга и кэширования.

        Args:
            file_path: Путь к PDF-файлу.
            cache_dir: Директория для кэшированных результатов.
            cache_key: Версия рецепта парсинга (промпт описаний, разрешение,
                модель). Если кэш снят другой версией — он перепарсивается:
                иначе в тексте остались бы описания от старых настроек.
            **parser_kwargs: Дополнительные параметры движка docling
                (см. PDFDoclingParser).
        """
        self.file_path = file_path if isinstance(file_path, Path) else Path(file_path)
        self.file_name = self.file_path.name
        self.cache_dir = cache_dir if isinstance(cache_dir, Path) else Path(cache_dir)

        # PDFDoclingParser импортируется лениво, чтобы пакет работал
        # без установленной docling (например, в API-процессе).
        from docling_parser.docling_parser import PDFDoclingParser
        self.parser = PDFDoclingParser(**parser_kwargs)

        self.hash = compute_file_sha256(self.file_path)
        self.content_dir = self.cache_dir / self.hash
        self.cache_key = (cache_key or "").strip() or None

        self.texts = []
        self.text = None
        self.tables = []
        self.images = []

        # Пути к файлам по номеру страницы
        self.page_images_paths = defaultdict(lambda: list())
        self.page_tables_paths = defaultdict(lambda: list())

        logger.info(f'PdfFileManager инициализирован для пути: {self.file_path}')


    def data_exists(self) -> bool:
        """Проверяет наличие кэшированных данных для текущего файла.

        Кэш считается годным, только если его сняли той же версией рецепта
        парсинга (``cache_key``). Иначе описания в тексте были бы от старых
        промптов/разрешения, а текст страниц — единственное, что читается
        из кэша как есть.
        """
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if not self.content_dir.exists():
            return False
        if self.cache_key is None:
            return True
        marker = self._cache_key_path()
        if not marker.exists():
            return False
        return marker.read_text(encoding='utf-8').strip() == self.cache_key

    def _cache_key_path(self) -> Path:
        return self.content_dir / 'cache_key'


    def extract_content(self) -> None:
        """Извлекает контент из PDF-файла или загружает из кэша.

        Если кэш существует — загружает сохранённые результаты.
        Иначе вызывает парсер, обрабатывает изображения, таблицы и текст,
        затем сохраняет результаты в кэш.
        """
        if self.file_path.suffix != '.pdf':
            logger.warning(f'Неожиданный тип файла: {self.file_path}')
        elif self.data_exists():
            self.load_contents()
        else:
            if self.content_dir.exists():
                # Кэш снят другой версией рецепта (промпт/разрешение/модель):
                # старые описания и PNG ему не годятся — перепарсиваем с нуля.
                logger.info(f'Кэш {self.content_dir} снят другой версией — перепарсиваю')
                shutil.rmtree(self.content_dir)
            output = self.parser.extract_content(self.file_path)
            self.text = ParsedDocument(page_content=output["full_text"], metadata={"file": self.file_name})
            # Сначала обрабатываем изображения, чтобы получить пути для их вставки в текст
            self.process_images(output['images'])
            self.process_tables(output['tables'])
            self.process_texts(output['texts'])

            self.save_contents()


    def process_texts(self, texts) -> None:
        """Обрабатывает текстовые чанки по страницам.

        Прикрепляет к каждой странице пути к изображениям и таблицам
        из метаданных. «Нечитаемые» страницы OCR'ит сам docling
        (Tesseract, языки rus/eng) — отдельного fallback здесь нет.
        """
        for i, text in enumerate(texts):
            page = i + 1

            metadata = {"page": page, "file": self.file_name}

            # Прикрепляем пути к изображениям и таблицам, если они есть на этой странице.
            # Используется разделитель |, поскольку Chroma не поддерживает хранение списков в метадате
            if page in self.page_images_paths:
                metadata["images"] = "|".join(self.page_images_paths[page])
            if page in self.page_tables_paths:
                metadata["tables"] = "|".join(self.page_tables_paths[page])

            self.texts.append(ParsedDocument(page_content=text, metadata=metadata))


    def process_tables(self, tables_data) -> None:
        """Обрабатывает извлечённые таблицы: сохраняет в кэш и формирует ParsedDocument."""
        import pandas as pd

        for j, data in enumerate(tables_data):
            try:
                cur_data = data.copy()
                page = cur_data['page']
                file_name = f'{cur_data["n"]}__{page}.png'
                path = str(self.content_dir / "tables" / file_name)

                if page not in self.page_tables_paths:
                    self.page_tables_paths[page] = []
                self.page_tables_paths[page].append(path)

                df = cur_data.get('df', None)
                safe_md = ""
                if isinstance(df, pd.DataFrame) and not df.empty:
                    try:
                        safe_md = "\n" + df.to_markdown(index=False)
                    except Exception:
                        safe_md = "\n" + df.to_csv(index=False)

                base_text = (cur_data.pop("text", "") or "")
                text = f"{base_text}\n{safe_md}"

                cur_data['file'] = self.file_name
                self.tables.append(ParsedDocument(page_content=text, metadata=cur_data))
            except Exception as e:
                logger.warning(f"pdf_parser: обработка таблицы {j + 1} не удалась: {e}")


    def process_images(self, images_data) -> None:
        """Обрабатывает извлечённые изображения: сохраняет в кэш и формирует ParsedDocument."""
        for data in images_data:
            cur_data = data.copy()

            page = cur_data['page']
            file_name = f"{cur_data['n']}__{page}.png"
            path = str(self.content_dir / "images" / file_name)

            if page not in self.page_images_paths:
                self.page_images_paths[page] = []
            self.page_images_paths[page].append(path)

            text = cur_data.pop("text")
            cur_data["file"] = self.file_name

            self.images.append(ParsedDocument(page_content=text, metadata=cur_data))


    def load_contents(self) -> None:
        """Загружает извлечённые данные из кэш-директории.

        Текст страниц читается из 'texts/*.txt' (описания изображений и
        таблиц уже внутри), пути к таблицам и изображениям восстанавливаются
        из имён файлов в 'tables/' и 'images/'; описания — из сайдкар-файлов
        'images/<n>__<page>.txt' и 'tables/<n>__<page>.txt'.
        """
        tables_dir = self.content_dir / 'tables'
        for path in sorted(tables_dir.glob('*.png'), key=lambda p: int(p.stem.split('__')[0])):
            n, page = path.stem.split('__')
            self.page_tables_paths[page].append(str(path))
            # Пиксели и df в кэш не сохраняются — восстанавливаем только
            # метаданные (для статистики и путей к кропам) и описание.
            metadata = {'n': int(n), 'page': int(page), 'file': self.file_name}
            description_path = path.with_suffix('.txt')
            if description_path.exists():
                metadata['description'] = description_path.read_text(encoding='utf-8').strip()
            self.tables.append(ParsedDocument(page_content="", metadata=metadata))

        images_dir = self.content_dir / 'images'
        for path in sorted(images_dir.glob('*.png'), key=lambda p: int(p.stem.split('__')[0])):
            n, page = path.stem.split('__')
            self.page_images_paths[page].append(str(path))
            metadata = {'n': int(n), 'page': int(page), 'file': self.file_name}
            description_path = path.with_suffix('.txt')
            if description_path.exists():
                metadata['description'] = description_path.read_text(encoding='utf-8').strip()
            self.images.append(ParsedDocument(page_content="", metadata=metadata))

        texts_dir = self.content_dir / 'texts'
        for path in sorted(texts_dir.glob('*.txt'), key=lambda p: int(p.stem)):
            with open(str(path), 'r') as file:
                text = file.read()
            page = path.stem
            self.texts.append(ParsedDocument(page_content=text, metadata={'page': int(page),
                                                                    'file': self.file_name,
                                                                    'images': "|".join(self.page_images_paths[page]),
                                                                    'tables': "|".join(self.page_tables_paths[page])}))

        logger.info(f'Содержимое загружено из кэша: {self.content_dir}')


    def save_contents(self) -> None:
        """Сохраняет извлечённые текст, таблицы и изображения в кэш.

        Структура кэша: <cache_dir>/<sha256>/{texts,tables,images}/.
        Сохраняются только данные, которые восстанавливает load_contents:
        постраничные тексты (с описаниями внутри), PNG и описания
        изображений/таблиц (images|tables/<n>__<page>.txt, при наличии);
        полный текст документа при необходимости собирается из страниц.
        """
        self.content_dir.mkdir(parents=True, exist_ok=True)
        for subdir in ('texts', 'tables', 'images'):
            (self.content_dir / subdir).mkdir(parents=True, exist_ok=True)
        if self.cache_key is not None:
            self._cache_key_path().write_text(self.cache_key, encoding='utf-8')

        for doc in self.texts:
            file_name = f'{doc.metadata["page"]}.txt'
            path = str(self.content_dir / 'texts' / file_name)
            with open(path, 'wb') as file:
                file.write(doc.page_content.encode('utf-8'))

        for doc in self.tables:
            file_name = f'{doc.metadata["n"]}__{doc.metadata["page"]}.png'
            path = str(self.content_dir / 'tables' / file_name)
            doc.metadata['image'].save(path)
            description = doc.metadata.get('description')
            if description:
                with open(str(Path(path).with_suffix('.txt')), 'w', encoding='utf-8') as file:
                    file.write(description)

        for doc in self.images:
            file_name = f'{doc.metadata["n"]}__{doc.metadata["page"]}.png'
            path = str(self.content_dir / 'images' / file_name)
            doc.metadata['image'].save(path)
            description = doc.metadata.get('description')
            if description:
                with open(str(Path(path).with_suffix('.txt')), 'w', encoding='utf-8') as file:
                    file.write(description)

        logger.info(f'Содержимое сохранено в: {self.content_dir}')
