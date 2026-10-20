import sys
from pathlib import Path

# Пакет лежит рядом с тестами, а не ставится в окружение.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
