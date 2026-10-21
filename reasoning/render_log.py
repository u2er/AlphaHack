from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path
from typing import Any


def read_records(path: Path) -> list[dict[str, Any]]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split(" ", 4)
        if len(parts) != 5:
            continue
        timestamp = f"{parts[0]} {parts[1]}"
        marker, _, payload = parts[4].partition(" ")
        try:
            data = json.loads(payload or "{}")
        except json.JSONDecodeError:
            data = {"raw": payload}
        records.append({"timestamp": timestamp, "kind": marker, "data": data})
    return records


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _markdown(source: str) -> str:
    chunks: list[str] = []
    paragraph: list[str] = []
    in_list = False

    def inline(text: str) -> str:
        safe = _escape(text)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)

    def flush_paragraph() -> None:
        if paragraph:
            chunks.append("<p>" + "<br>".join(inline(line) for line in paragraph) + "</p>")
            paragraph.clear()

    for line in source.splitlines():
        if line.startswith("## "):
            flush_paragraph()
            if in_list:
                chunks.append("</ul>")
                in_list = False
            chunks.append(f"<h3>{inline(line[3:])}</h3>")
        elif line.startswith("- "):
            flush_paragraph()
            if not in_list:
                chunks.append("<ul>")
                in_list = True
            chunks.append(f"<li>{inline(line[2:])}</li>")
        elif not line.strip():
            flush_paragraph()
            if in_list:
                chunks.append("</ul>")
                in_list = False
        else:
            if in_list:
                chunks.append("</ul>")
                in_list = False
            paragraph.append(line)
    flush_paragraph()
    if in_list:
        chunks.append("</ul>")
    return "".join(chunks)


def _card(record: dict[str, Any]) -> str:
    kind, data = record["kind"], record["data"]
    timestamp = _escape(record["timestamp"])
    if kind == "user.message":
        return (
            f'<article class="bubble user"><div class="label">Пользователь</div>'
            f'<div>{_escape(data.get("content", ""))}</div><time>{timestamp}</time></article>'
        )
    if kind == "llm.request":
        messages = data.get("messages", [])
        return (
            f'<div class="step">Запрос модели · шаг {_escape(data.get("step", ""))} · '
            f'{_escape(data.get("model", ""))} · {len(messages)} сообщений'
            f'<details><summary>Полный контекст запроса</summary>'
            f'<pre>{_escape(_json(messages))}</pre></details></div>'
        )
    if kind == "llm.response":
        calls = data.get("tool_calls") or []
        if calls:
            rendered = []
            for call in calls:
                function = call.get("function", {})
                raw = function.get("arguments") or "{}"
                try:
                    arguments = json.loads(raw)
                except json.JSONDecodeError:
                    arguments = raw
                rendered.append(
                    f'<article class="bubble action"><div class="label">Агент вызывает инструмент</div>'
                    f'<strong>{_escape(function.get("name", "unknown"))}</strong>'
                    f'<details><summary>Аргументы</summary><pre>{_escape(_json(arguments))}</pre></details>'
                    f'<time>{timestamp}</time></article>'
                )
            return "".join(rendered)
        content = data.get("content")
        if content:
            return (
                f'<article class="bubble assistant"><div class="label">Агент</div>'
                f'<div class="answer">{_markdown(content)}</div><time>{timestamp}</time></article>'
            )
    if kind == "tool.result":
        tool_name = data.get("tool", "")
        result = data.get("result", {})
        if tool_name == "find_company":
            match = (result.get("matches") or [{}])[0]
            profile = match.get("meta", {})
            preview = (
                f'<b>{_escape(match.get("name", result.get("error", "Компания не найдена")))}</b>'
                f' · синтетический профиль: {"да" if match.get("synthetic") else "нет"}'
                f'<p>{_escape(profile.get("about", ""))}</p>'
            )
        else:
            company = result.get("company", {})
            events = result.get("events", [])
            preview = (
                f'<b>{_escape(company.get("name", ""))}</b>'
                f' · кандидатов: {_escape(result.get("total_events", len(events)))}'
                f' · слабых совпадений отфильтровано: {_escape(result.get("weak_matches_filtered", 0))}'
                f' · получено: {len(events)}<ol class="events">'
            )
            for event in events[:20]:
                categories = ", ".join(event.get("relevance_categories", []))
                preview += (
                    f'<li><time>{_escape(event.get("date", "без даты"))}</time> '
                    f'<b>{_escape(event.get("name", ""))}</b>'
                    f'<small>{_escape(categories)} · score {event.get("relevance_score", "—")}</small>'
                    f'<p>{_escape(event.get("summary", ""))}</p></li>'
                )
            if len(events) > 20:
                preview += f"<li>Ещё {len(events) - 20} событий доступны в полном JSON ниже.</li>"
            preview += "</ol>"
        return (
            f'<section class="tool-result"><div class="label">Результат инструмента · '
            f'{_escape(tool_name)}</div><div>{preview}</div>'
            f'<details><summary>Полный результат инструмента</summary>'
            f'<pre>{_escape(_json(result))}</pre></details><time>{timestamp}</time></section>'
        )
    return ""


def render(records: list[dict[str, Any]], source: Path) -> str:
    cards = "".join(_card(record) for record in records)
    steps = sum(record["kind"] == "llm.request" for record in records)
    calls = sum(record["kind"] == "tool.call" for record in records)
    first_request = next(
        (record["data"] for record in records if record["kind"] == "llm.request"),
        {},
    )
    system_prompt = next(
        (
            message.get("content", "")
            for message in first_request.get("messages", [])
            if message.get("role") == "system"
        ),
        "",
    )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Тестовый диалог — ВекторФуд</title>
<style>
:root{{--ink:#192333;--muted:#667085;--line:#e5eaf0;--blue:#eef5ff;--green:#edf8f2;--amber:#fff7e8}}
*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f9;color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:1050px;margin:36px auto;padding:0 18px 60px}}header{{background:#fff;border:1px solid var(--line);border-radius:16px;padding:24px 28px;margin-bottom:22px;box-shadow:0 4px 18px #1822300a}}
h1{{margin:0 0 5px;font-size:25px}}.sub,time{{color:var(--muted)}}.sub{{font-size:13px}}.notice{{margin-top:16px;padding:12px 14px;border-radius:10px;background:var(--amber);font-size:14px}}
.chat{{display:flex;flex-direction:column;gap:14px}}.bubble,.tool-result{{max-width:92%;border:1px solid var(--line);border-radius:14px;padding:16px 18px;background:#fff;box-shadow:0 3px 12px #18223008}}
.user{{align-self:flex-end;background:var(--blue);border-color:#d8e7ff}}.assistant{{align-self:flex-start;background:var(--green);border-color:#d9efe3;width:100%;max-width:100%}}
.action{{align-self:flex-end;background:#f4f0ff;border-color:#e5dcff;min-width:48%}}.tool-result{{align-self:center;width:94%;max-width:94%}}
.label{{text-transform:uppercase;letter-spacing:.07em;font-size:11px;font-weight:700;color:var(--muted);margin-bottom:6px}}
time{{display:block;font-size:11px;margin-top:10px}}.step{{color:var(--muted);text-align:center;font-size:12px;padding:2px 0}}.step details{{max-width:800px;margin:6px auto;text-align:left}}
.answer h3{{margin:18px 0 8px;font-size:17px}}.answer h3:first-child{{margin-top:0}}.answer p{{margin:8px 0}}.answer li{{margin:6px 0}}
details{{margin-top:10px}}summary{{cursor:pointer;color:#315a88;font-size:13px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;max-height:520px;overflow:auto;border-radius:9px;background:#101828;color:#e5e7eb;padding:14px;font:12px/1.5 ui-monospace,monospace}}
.events{{padding-left:22px}}.events li{{margin:12px 0}}.events small{{display:block;color:var(--muted)}}.events p{{font-size:13px;color:#475467;margin:5px 0}}
footer{{color:var(--muted);font-size:12px;text-align:center;margin-top:20px}}@media(max-width:700px){{main{{margin:14px auto}}.bubble,.tool-result{{max-width:100%;width:100%}}}}
</style></head><body><main>
<header><h1>Тестовый диалог: аналитика для ВекторФуд</h1>
<div class="sub">Чат, шаги модели, вызовы инструментов и результаты · {steps} шагов модели · {calls} вызова инструмента</div>
<div class="notice"><b>Контекст агента:</b> компания задана в системном контексте, а не сообщении пользователя.
Новости-кандидаты подобраны эвристически и не доказывают фактическое воздействие на компанию.</div>
<details><summary>Показать инструкции и контекст агента</summary><pre>{_escape(system_prompt)}</pre></details></header>
<div class="chat">{cards}</div>
<footer>Источник: {_escape(source.name)} · полные запросы и результаты доступны в раскрывающихся блоках.</footer>
</main></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a reasoning agent log as an HTML chat")
    parser.add_argument("log", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    document = render(read_records(args.log), args.log)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(document, encoding="utf-8")
    print(f"Rendered {args.output} ({args.output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
