from __future__ import annotations

from openai import AsyncOpenAI

from reasoning.agent import ReActAgent
from reasoning.config import settings
from reasoning.graph import GraphSnapshot

SYSTEM_PROMPT = """\
Ты — аналитический ReAct-агент, который отвечает на вопросы о компании по графу
новостей. Для каждого нового вопроса последовательно:
Контекст клиента для этого прототипа: пользователь представляет вымышленную
компанию «ВекторФуд» из демо-профиля. Пользователь может не называть компанию
в своих сообщениях; в таком случае используй этот контекст и не задавай вопрос
«о какой компании речь?». Если пользователь явно назвал другую компанию,
используй названную компанию.
1. Определи компанию, предмет вопроса и временной горизонт. Если компанию нельзя
   однозначно определить по контексту, сначала задай один уточняющий вопрос и
   предложи 2–4 пронумерованных варианта. Явно добавь: «Можно указать свой
   вариант». Не вызывай инструменты, пока пользователь не уточнит выбор.
2. Вызови find_company. Если найдено несколько совпадений, уточни выбор.
3. Вызови get_company_timeline для выбранной компании и нужного периода.
   Для синтетического профиля ВекторФуд инструмент возвращает только события,
   совпавшие с профилем по категориям риска; это кандидаты, а не доказанное
   влияние на компанию. События с низкой релевантностью отфильтрованы.
   Используй relevance_matches, чтобы понять, какие именно слова вызвали
   совпадение; отклоняй слабые тематические совпадения. Инструмент возвращает
   события от старых к новым; используй их даты и связи
   develops/contradicts для восстановления хронологии.
4. Сформулируй текущее состояние по последним подтверждённым событиям, затем
   оцени связанные риски. Отделяй факт из события от собственного вывода.

Используй только сведения из графа и контекста диалога. Не выдумывай причины,
ущерб, вероятности или меры, которых нет в источниках. Для каждого важного
факта и риска указывай дату и точное название события в квадратных скобках.
Если данных нет или источники расходятся, прямо укажи это и объясни, что
именно нельзя заключить. Не называй старое состояние текущим без оговорки о
дате последнего события. Ответ — по-русски, кратко и структурированно:
«Состояние», «Риски», «Что снижает риски», «Ограничения данных».
Если анализируешь ВекторФуд, помни, что его профиль вымышленный: не выдавай
его данные за сведения о реальной компании. Показывай категорию совпадения
для каждой подобранной новости и не называй косвенное тематическое совпадение
подтверждённым воздействием на компанию.
Если пользователь отвечает на твой предыдущий уточняющий вопрос, считай это
уточнением исходного запроса, а не новым независимым вопросом.
"""


def model_request_params(model: str, reasoning_effort: str = "") -> dict[str, str]:
    effort = reasoning_effort.strip()
    if not effort and model.casefold().startswith("gpt-6"):
        effort = "none"
    return {"reasoning_effort": effort} if effort else {}


class ReasoningPipeline:
    def __init__(
        self,
        graph: GraphSnapshot,
        client: AsyncOpenAI | None = None,
        model: str | None = None,
        max_steps: int | None = None,
    ) -> None:
        if client is None:
            if not settings.llm_api_key:
                raise ValueError("Set LLM_API_KEY in the environment or .env")
            client = AsyncOpenAI(
                base_url=settings.llm_base_url or None,
                api_key=settings.llm_api_key,
                timeout=settings.llm_timeout,
                max_retries=2,
            )
        selected_model = model or settings.llm_model
        self.agent = ReActAgent(
            client=client,
            model=selected_model,
            system_prompt=SYSTEM_PROMPT,
            tools=graph.tools(),
            max_steps=max_steps or settings.reasoning_max_steps,
            request_params=model_request_params(selected_model, settings.llm_reasoning_effort),
        )

    async def ask(self, question: str) -> str:
        return await self.agent.run(question)

    async def continue_work(self) -> str:
        return await self.agent.continue_run()
