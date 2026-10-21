from __future__ import annotations

import argparse
import asyncio
import logging
import os
from logging import FileHandler, Formatter
from pathlib import Path

from reasoning.agent import AgentStepLimitReached
from reasoning.config import settings
from reasoning.graph import GraphSnapshot
from reasoning.pipeline import ReasoningPipeline

logger = logging.getLogger("reasoning")


def configure_logging() -> None:
    log_path = Path(os.getenv("REASONING_LOG_FILE", str(settings.reasoning_log_file))).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("reasoning")
    log.setLevel(logging.INFO)
    log.propagate = False
    handler = FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(handler)


async def _chat() -> None:
    graph = GraphSnapshot(settings.reasoning_graph_json)
    pipeline = ReasoningPipeline(graph)
    print(f"Reasoning prototype. Подробный лог: {os.getenv('REASONING_LOG_FILE', settings.reasoning_log_file)}")
    print("Демо-профиль компании: ВекторФуд (вымышленный).")
    print("Type 'exit' to quit.")
    while True:
        try:
            question = input("\nВы: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question.casefold() in {"exit", "quit", "выход"}:
            break
        if not question:
            continue
        try:
            answer = await pipeline.ask(question)
            print(f"\nАгент: {answer}")
        except AgentStepLimitReached as exc:
            print(f"\nАгент долго работает (уже {exc.steps} шагов). Продолжить?")
            print("1. Да, продолжить")
            print("2. Нет, остановить")
            while True:
                choice = input("Выберите 1 или 2: ").strip().casefold()
                if choice in {"1", "да", "yes", "y"}:
                    logger.info("agent.continue_by_user")
                    try:
                        answer = await pipeline.continue_work()
                        print(f"\nАгент: {answer}")
                    except AgentStepLimitReached:
                        print("\nАгент всё ещё работает.")
                        continue
                    break
                if choice in {"2", "нет", "no", "n"}:
                    logger.info("agent.continue_declined_by_user")
                    pipeline.agent.history.append(
                        {"role": "assistant", "content": "Работу остановили по запросу пользователя."}
                    )
                    logger.info("agent.stopped_by_user")
                    print("Остановлено.")
                    break
                print("Ответьте «1» или «2». Чтобы остановить, введите 2.")
        except Exception as exc:
            logger.exception("chat.request_failed")
            print(f"\nОшибка: {type(exc).__name__}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the graph-reasoning prototype")
    parser.parse_args()
    configure_logging()
    asyncio.run(_chat())


if __name__ == "__main__":
    main()
