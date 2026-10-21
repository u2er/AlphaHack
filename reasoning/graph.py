from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from reasoning.profiles import DEMO_PROFILE


class GraphSnapshot:
    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"Graph JSON file not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
            raise ValueError(f"Invalid graph JSON: expected an object with a nodes array ({path})")
        if not isinstance(payload.get("edges"), list):
            raise ValueError(f"Invalid graph JSON: expected an edges array ({path})")
        self.nodes = {node["id"]: node for node in payload["nodes"] if node.get("id")}
        self.edges = payload["edges"]
        self.demo_profile = DEMO_PROFILE

    @staticmethod
    def _meta(node: dict[str, Any]) -> dict[str, Any]:
        value = node.get("meta")
        return value if isinstance(value, dict) else {}

    def find_company(self, company_name: str) -> dict[str, Any]:
        query = " ".join(company_name.split()).casefold()
        if not query:
            raise ValueError("company_name must not be empty")
        companies = [
            node for node in self.nodes.values()
            if self._meta(node).get("type") == "org"
            and self._meta(node).get("ekind") in {
                "company", "компания", "банк", "bank", "corporation",
                "корпорация", "авиакомпания", "группа компаний",
            }
        ]
        exact = [
            node for node in companies
            if node.get("name", "").casefold() == query
            or any(str(alias).casefold() == query for alias in self._meta(node).get("aliases", []))
        ]
        matches = exact or [
            node for node in companies
            if query in node.get("name", "").casefold()
            or any(query in str(alias).casefold() for alias in self._meta(node).get("aliases", []))
        ]
        matches.sort(key=lambda node: node.get("name", "").casefold())
        demo_names = [self.demo_profile["name"], *self.demo_profile["aliases"]]
        demo_match = any(
            query == name.casefold() or query in name.casefold()
            for name in demo_names
        )
        if demo_match:
            profile_match = {
                "id": self.demo_profile["id"],
                "name": self.demo_profile["name"],
                "meta": self.demo_profile,
                "degree": 0,
                "synthetic": True,
            }
            return {"ok": True, "matches": [profile_match], "ambiguous": False}
        if not matches:
            return {"ok": False, "error": f"Company not found: {company_name}", "matches": []}
        return {
            "ok": True,
            "matches": [
                {
                    "id": node["id"],
                    "name": node.get("name", ""),
                    "meta": self._meta(node),
                    "degree": sum(
                        1 for edge in self.edges
                        if edge.get("source_id") == node["id"] or edge.get("target_id") == node["id"]
                    ),
                }
                for node in matches[:10]
            ],
            "ambiguous": len(matches) > 1,
        }

    def company_timeline(
        self,
        company_id: str,
        since: str | None = None,
        until: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        if company_id == self.demo_profile["id"]:
            return self._profile_timeline(since, until, limit)
        company = self.nodes.get(company_id)
        if company is None or self._meta(company).get("type") != "org":
            return {"ok": False, "error": f"Organization node not found: {company_id}"}
        start = date.fromisoformat(since) if since else None
        end = date.fromisoformat(until) if until else None
        limit = max(1, min(int(limit), 100))
        linked_ids = {
            edge["target_id"] if edge.get("source_id") == company_id else edge.get("source_id")
            for edge in self.edges
            if edge.get("name") == "involves"
            and company_id in (edge.get("source_id"), edge.get("target_id"))
        }
        events = []
        for event_id in linked_ids:
            node = self.nodes.get(event_id)
            if node is None or self._meta(node).get("type") != "event":
                continue
            meta = self._meta(node)
            event_date = meta.get("date")
            if event_date:
                parsed_date = date.fromisoformat(event_date)
                if start and parsed_date < start:
                    continue
                if end and parsed_date > end:
                    continue
            events.append(
                {
                    "id": event_id,
                    "name": node.get("name", ""),
                    "date": event_date,
                    "summary": meta.get("summary", ""),
                    "importance": meta.get("importance"),
                    "sources_count": meta.get("sources_count"),
                    "first_seen": meta.get("first_seen"),
                    "last_seen": meta.get("last_seen"),
                    "sources": self._event_sources(event_id),
                }
            )
        events.sort(key=lambda event: (event["date"] or "", event["name"]))
        event_ids = {event["id"] for event in events}
        relations = []
        for edge in self.edges:
            if edge.get("name") not in {"develops", "contradicts"}:
                continue
            if edge.get("source_id") in event_ids and edge.get("target_id") in event_ids:
                source = self.nodes[edge["source_id"]]
                target = self.nodes[edge["target_id"]]
                relations.append(
                    {
                        "relation": edge["name"],
                        "source": source.get("name", ""),
                        "target": target.get("name", ""),
                    }
                )
        return {
            "ok": True,
            "company": {
                "id": company_id,
                "name": company.get("name", ""),
                "meta": self._meta(company),
            },
            "events": events[:limit],
            "total_events": len(events),
            "event_relations": relations,
            "chronology": "oldest_to_newest",
        }

    def _profile_timeline(
        self, since: str | None, until: str | None, limit: int
    ) -> dict[str, Any]:
        start = date.fromisoformat(since) if since else None
        end = date.fromisoformat(until) if until else None
        limit = max(1, min(int(limit), 100))
        ranked_events: list[tuple[int, dict[str, Any]]] = []
        weak_matches = 0
        for node in self.nodes.values():
            meta = self._meta(node)
            if meta.get("type") != "event":
                continue
            event_date = meta.get("date")
            if event_date:
                parsed_date = date.fromisoformat(event_date)
                if start and parsed_date < start:
                    continue
                if end and parsed_date > end:
                    continue
            text = " ".join(
                [
                    str(node.get("name", "")),
                    str(meta.get("summary", "")),
                    " ".join(str(topic) for topic in meta.get("topics", [])),
                ]
            ).casefold()
            category_matches: dict[str, dict[str, Any]] = {}
            category_scores = []
            for rule in self.demo_profile["relevance_rules"]:
                strong = [
                    term for term in rule.get("strong_terms", [])
                    if self._matches_term(text, term)
                ]
                contextual = [
                    term for term in rule.get("terms", [])
                    if self._matches_term(text, term)
                ]
                if not strong and not contextual:
                    continue
                weight = rule["weight"] if strong else rule.get("context_weight", rule["weight"])
                category_matches[rule["category"]] = {
                    "strong": strong,
                    "contextual": contextual,
                }
                category_scores.append(weight)
            if not category_matches:
                continue
            score = max(category_scores)
            if isinstance(meta.get("importance"), int):
                score += min(5, meta["importance"])
            if score < 45:
                weak_matches += 1
                continue
            event = {
                "id": node["id"],
                "name": node.get("name", ""),
                "date": event_date,
                "summary": meta.get("summary", ""),
                "importance": meta.get("importance"),
                "sources_count": meta.get("sources_count"),
                "first_seen": meta.get("first_seen"),
                "last_seen": meta.get("last_seen"),
                "sources": self._event_sources(node["id"]),
                "relevance_score": score,
                "relevance_categories": list(category_matches),
                "relevance_matches": category_matches,
                "relevance_basis": "profile keyword match; indirect relevance, not a confirmed company-specific impact",
            }
            ranked_events.append((score, event))
        ranked_events.sort(
            key=lambda pair: (
                -pair[0],
                -(int(pair[1]["date"].replace("-", "")) if pair[1]["date"] else 0),
                pair[1]["name"],
            )
        )
        selected = [event for _, event in ranked_events[:limit]]
        selected.sort(key=lambda event: (event["date"] or "", event["name"]))
        event_ids = {event["id"] for event in selected}
        relations = []
        for edge in self.edges:
            if edge.get("name") not in {"develops", "contradicts"}:
                continue
            if edge.get("source_id") in event_ids and edge.get("target_id") in event_ids:
                source = self.nodes[edge["source_id"]]
                target = self.nodes[edge["target_id"]]
                relations.append(
                    {
                        "relation": edge["name"],
                        "source": source.get("name", ""),
                        "target": target.get("name", ""),
                    }
                )
        return {
            "ok": True,
            "company": {
                "id": self.demo_profile["id"],
                "name": self.demo_profile["name"],
                "meta": self.demo_profile,
                "synthetic": True,
            },
            "events": selected,
            "total_events": len(ranked_events),
            "weak_matches_filtered": weak_matches,
            "event_relations": relations,
            "chronology": "oldest_to_newest",
            "selection_note": (
                "Only profile-related candidate events are returned. Relevance is a "
                "heuristic; verify each causal link before describing company impact."
            ),
        }

    @staticmethod
    def _matches_term(text: str, term: str) -> bool:
        return re.search(rf"(?<![а-яёa-z]){re.escape(term.casefold())}", text) is not None

    def _event_sources(self, event_id: str) -> list[str]:
        sources = []
        for edge in self.edges:
            if edge.get("name") != "reported_by":
                continue
            if event_id not in (edge.get("source_id"), edge.get("target_id")):
                continue
            other_id = edge.get("target_id") if edge.get("source_id") == event_id else edge.get("source_id")
            other = self.nodes.get(other_id)
            if other:
                sources.append(other.get("name", ""))
        return sorted(set(sources))

    def tools(self) -> list[Any]:
        from reasoning.agent import Tool

        return [
            Tool(
                name="find_company",
                description=(
                    "Find a company in the knowledge graph. The synthetic demo company "
                    "ВекторФуд is also available for relevance testing."
                ),
                parameters={
                    "type": "object",
                    "properties": {"company_name": {"type": "string"}},
                    "required": ["company_name"],
                    "additionalProperties": False,
                },
                handler=self.find_company,
            ),
            Tool(
                name="get_company_timeline",
                description=(
                    "Get dated news events directly linked to a graph company, or profile-matched "
                    "candidate news for synthetic ВекторФуд. Events are chronological and include "
                    "relevance categories, summaries, source labels and developments."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "company_id": {"type": "string"},
                        "since": {"type": ["string", "null"], "description": "Inclusive ISO date"},
                        "until": {"type": ["string", "null"], "description": "Inclusive ISO date"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
                    },
                    "required": ["company_id"],
                    "additionalProperties": False,
                },
                handler=self.company_timeline,
            ),
        ]
