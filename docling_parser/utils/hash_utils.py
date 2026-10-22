"""Утилиты для хеширования файлов (используется для кэширования результатов)."""
import hashlib
from pathlib import Path


def compute_file_sha256(file_path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Вычисляет SHA-256 содержимого файла (поблоково, без загрузки в память)."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
