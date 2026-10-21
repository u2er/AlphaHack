# Reasoning prototype

Standalone prototype for answering company questions from an exported EventGraph
JSON snapshot. It does not modify or run the news-ingestion pipeline.

## Run

Set `LLM_API_KEY`, `LLM_MODEL`, and `LLM_BASE_URL` in the repository-root
`.env` or the environment. The package reads those exact settings; the root
`.env` is found even when the command is started from another directory. For
example:

```dotenv
LLM_API_KEY=your-key
LLM_MODEL=gpt-6-luna
LLM_BASE_URL=https://api.openai.com/v1
```

For GPT-6 models, the package defaults `reasoning_effort` to `none` so function
tools work with the Chat Completions endpoint. Override it with
`LLM_REASONING_EFFORT` if needed.

The agent keeps conversation and tool history in process memory. When it reaches
20 model steps without completing the turn, the CLI asks whether to continue;
answer `1` or `2`. Clarifying questions include numbered options and allow a
free-form answer. Full prompts, model responses, tool
arguments and results are written to `reasoning/agent.log` by default; override
the path with `REASONING_LOG_FILE`. The log can contain conversation data and
is ignored by Git.

Then run:

```bash
python -m reasoning.cli
```

The default graph is `data/casket-news-graph-2026-10-21.json`. Override it with
`REASONING_GRAPH_JSON=/path/to/graph.json`. The chat history, including tool
calls and results, is kept in memory for the lifetime of this process only.

## Prototype flow

The ReAct agent is instructed to identify the company, use graph tools to find
it and retrieve linked events, read the events in chronological order, infer
the latest known state, and then assess risks. Answers must cite event names
and dates. The graph tools currently provide company lookup and a chronological
list of directly linked events with source labels and `develops`/`contradicts`
relations.

The synthetic `ВекторФуд` profile is available for relevance experiments. It
matches candidate news by profile categories (inputs/costs, logistics/energy,
imports/FX, financing/inflation, industry/regulation, demand, and regions).
Each candidate includes the matching categories and is explicitly marked as an
indirect heuristic match, not proof of impact. The tool returns at most 30
candidates by default (100 maximum), selected by profile relevance and then
presented chronologically; inspect `relevance_matches` and reject weak keyword
matches before drawing conclusions. Low-confidence contextual-only matches are
excluded from the candidate list and counted in `weak_matches_filtered`.

This is an experimental baseline: it uses only the graph snapshot, does not
ingest the PDF research reports, and does not yet guarantee a separate,
machine-validated extraction of state and risk claims.
