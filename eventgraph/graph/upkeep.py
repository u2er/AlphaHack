from __future__ import annotations

import datetime


def as_int(value, fallback=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def clip(value, chars):
    if isinstance(value, str) and len(value) > chars:
        return value[:chars].rstrip() + "…"
    return value


# -- graph upkeep ------------------------------------------------------------
# A news graph only grows: duplicates under other spellings, entities nobody
# links to, a week of minor events nobody will ask about. The upkeep tools give
# a maintenance agent the candidates; the agent decides, the tools write.

# Edges that say something about the world. `about` (→ topic) and
# `reported_by` (→ outlet) tie every event to a hub, so through them the whole
# graph is one component and nothing ever looks isolated.
UPKEEP_LINKS = ("involves", "in", "develops", "contradicts", "works_at", "leads", "part_of")
HUB_TYPES = ("topic", "outlet")
ENTITY_TYPES = ("person", "org", "place")
# An event linked this way is part of a story and stays.
STORY_LINKS = ("develops", "contradicts")
# Junk edges: a relation name that means nothing.
JUNK_EDGES = ("noop", "none", "null", "")
# A node marked checked is not offered as an island again for this long.
CHECKED_DAYS = 30
UPKEEP_TEXT = 300


def parse_day(value):
    """A date from `YYYY-MM-DD…` (a field or an event name); None if there is none."""
    text = str(value or "").strip()[:10]
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def event_day(node):
    meta = node.get("meta") or {}
    return parse_day(meta.get("date")) or parse_day(node.get("name"))


def ntype(node):
    return str((node.get("meta") or {}).get("type") or "")


def upkeep_side(node, degree=None):
    """One node of a candidate pair or an island, as much as a decision needs."""
    meta = node.get("meta") or {}
    out = {"id": node.get("id", ""), "name": node.get("name", ""), "type": ntype(node) or None,
           "degree": (degree or {}).get(node.get("id"), node.get("degree", 0))}
    for key in ("date", "importance", "sources_count", "role", "ekind", "aliases"):
        if meta.get(key) not in (None, "", []):
            out[key] = meta[key]
    text = meta.get("summary") or meta.get("about") or node.get("brief") or ""
    if text:
        out["text"] = clip(" ".join(str(text).split()), UPKEEP_TEXT)
    return out


def degrees(edges):
    degree = {}
    for e in edges:
        for end in (e.get("source_id"), e.get("target_id")):
            if end:
                degree[end] = degree.get(end, 0) + 1
    return degree


def islands(dump, max_size=3, today=None):
    """Small pieces of the graph that hang on nothing but topics and outlets.

    Components over the meaningful edges (UPKEEP_LINKS); hubs are left out.
    A piece of at most `max_size` nodes is an island, unless every node in it
    was checked within CHECKED_DAYS. Most news is a small piece of its own —
    an event with its participants — so the pieces come worst first: nodes
    with no edge at all, entities that no event holds, events without
    participants, then the rest; oldest first within each."""
    today = today or datetime.date.today()
    nodes = {n["id"]: n for n in dump.get("nodes") or [] if ntype(n) not in HUB_TYPES}
    edges = dump.get("edges") or []
    parent = {i: i for i in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    inner = []
    for e in edges:
        a, b = e.get("source_id"), e.get("target_id")
        if e.get("name") in UPKEEP_LINKS and a in nodes and b in nodes:
            parent[find(a)] = find(b)
            inner.append(e)
    groups = {}
    for i in nodes:
        groups.setdefault(find(i), []).append(i)

    def checked(i):
        day = parse_day((nodes[i].get("meta") or {}).get("checked"))
        return day is not None and (today - day).days < CHECKED_DAYS

    degree = degrees(edges)
    hubs = {}
    names = {n["id"]: n.get("name", "") for n in dump.get("nodes") or []}
    for e in edges:
        if e.get("name") == "about" and e.get("source_id") in nodes:
            hubs.setdefault(e["source_id"], []).append(names.get(e.get("target_id"), ""))

    islands = []
    for members in groups.values():
        if len(members) > max_size or all(checked(i) for i in members):
            continue
        member_set = set(members)
        sides = []
        for i in members:
            side = upkeep_side(nodes[i], degree)
            if hubs.get(i):
                side["topics"] = hubs[i]
            sides.append(side)
        links = [
            f"{names.get(e['source_id'])} —{e.get('name')}→ {names.get(e['target_id'])}"
            for e in inner if e["source_id"] in member_set and e["target_id"] in member_set
        ]
        kinds = {ntype(nodes[i]) for i in members}
        if any(degree.get(i, 0) == 0 for i in members):
            rank, why = 0, "no edges at all"
        elif "event" not in kinds:
            rank, why = 1, "entities that no event holds"
        elif kinds == {"event"}:
            rank, why = 2, "events without participants"
        else:
            rank, why = 3, "a piece of its own"
        first = min((nodes[i].get("created_at") or "") for i in members)
        islands.append(((rank, first), {"why": why, "nodes": sides, **({"links": links} if links else {})}))
    islands.sort(key=lambda pair: pair[0])
    return [island for _, island in islands]


def prune_plan(dump, older_than_days=30, max_importance=2, max_sources=1, protect_topics=(), today=None,
                only_events=None):
    """What a prune removes, computed from the graph alone — no model involved.

    - events older than `older_than_days` (by their date), of importance at most
      `max_importance` and from at most `max_sources` outlets, that no story
      edge (develops / contradicts) touches and that are not about a protected
      topic;
    - entities (person / org / place / untyped) whose every edge led to those
      events — nothing would hold them any more;
    - junk: edges with a meaningless name or from a node to itself, and nodes
      with neither a type nor an edge.
    An event without an importance is never removed: nobody rated it.
    `only_events` narrows the events to these ids (the ones a reviewer kept on
    the list); orphans are then counted against what is really removed."""
    today = today or datetime.date.today()
    nodes = {n["id"]: n for n in dump.get("nodes") or []}
    edges = dump.get("edges") or []
    protect = {str(t).strip().casefold() for t in protect_topics if str(t).strip()}
    by_node = {}
    for e in edges:
        for end in {e.get("source_id"), e.get("target_id")}:
            if end:
                by_node.setdefault(end, []).append(e)

    junk_edges = [
        e for e in edges
        if str(e.get("name") or "").strip().lower() in JUNK_EDGES or e.get("source_id") == e.get("target_id")
    ]
    junk_ids = {e["id"] for e in junk_edges}

    events = []
    for i, n in nodes.items():
        if ntype(n) != "event":
            continue
        meta = n.get("meta") or {}
        day = event_day(n)
        importance = as_int(meta.get("importance"), None)
        if day is None or importance is None or (today - day).days <= older_than_days:
            continue
        if importance > max_importance:
            continue
        own = [e for e in by_node.get(i, []) if e["id"] not in junk_ids]
        sources = as_int(meta.get("sources_count"), None)
        if sources is None:
            sources = sum(1 for e in own if e.get("name") == "reported_by")
        if sources > max_sources:
            continue
        if any(e.get("name") in STORY_LINKS for e in own):
            continue
        if protect and any(
            e.get("name") == "about"
            and str((nodes.get(e.get("target_id")) or {}).get("name", "")).strip().casefold() in protect
            for e in own
        ):
            continue
        events.append(n)
    if only_events is not None:
        wanted = {str(i) for i in only_events}
        events = [n for n in events if n["id"] in wanted]
    gone = {n["id"] for n in events}

    orphans, empty = [], []
    for i, n in nodes.items():
        kind = ntype(n)
        if i in gone or kind in HUB_TYPES or kind == "event":
            continue
        own = [e for e in by_node.get(i, []) if e["id"] not in junk_ids]
        if not own:
            if not kind:
                empty.append(n)
            continue
        others = {e.get("target_id") if e.get("source_id") == i else e.get("source_id") for e in own}
        if others <= gone:
            orphans.append(n)
    return {"events": events, "orphans": orphans, "empty": empty, "junk_edges": junk_edges}


# -- periods ---------------------------------------------------------------
# The graph read by time: what happened between two dates, who and where was
# at the centre of it, which stories moved on.

# How much of an event a period listing carries: the gist; graph_node gives
# the rest.
PERIOD_TEXT = 260
PERIOD_ENTITIES = 12
PERIOD_STORIES = 10
PERIOD_HEADLINES = 60
PERIOD_MAX_DAYS = 400


def period(dump, since, until, min_importance=1, topics=(), entity="", limit=30):
    """Events dated within [since, until] and what they add up to.

    - `events`: the heaviest first — importance, then how many outlets covered
      it — with topics, places, participants and story links; the next ones as
      bare headlines in `more_events`;
    - `people` / `orgs` / `places`: who and where was at the centre — counted
      over every event of the period that passed the filters, with roles and
      the events that put them there; `new` — the node appeared in the period;
    - `stories`: chains of develops / contradicts touching the period, with the
      earlier links that lead into it;
    - `topics`, `by_day`, `by_importance`: counts.
    An event without importance counts as 1. `entity` keeps the events that
    involve or happen in a node with this name or alias."""
    nodes = {n["id"]: n for n in dump.get("nodes") or []}
    edges = dump.get("edges") or []
    wanted_topics = {str(t).strip().casefold() for t in topics or () if str(t).strip()}
    who = str(entity or "").strip().casefold()

    def named(node, name):
        meta = node.get("meta") or {}
        aliases = meta.get("aliases") or []
        if isinstance(aliases, str):
            aliases = aliases.split(",")
        return name in {str(node.get("name", "")).strip().casefold()} | {str(a).strip().casefold() for a in aliases}

    events = {}
    for i, n in nodes.items():
        if ntype(n) != "event":
            continue
        day = event_day(n)
        if day is not None and since <= day <= until:
            events[i] = {"node": n, "day": day, "topics": [], "places": [], "involves": [],
                         "develops": [], "developed_by": [], "outlets": 0}
    for e in edges:
        a, b, name = e.get("source_id"), e.get("target_id"), e.get("name")
        other = nodes.get(b) or {}
        if a in events:
            ev = events[a]
            if name == "about":
                ev["topics"].append(other.get("name", ""))
            elif name == "in":
                ev["places"].append(b)
            elif name == "involves":
                ev["involves"].append((b, str((e.get("meta") or {}).get("role") or "").strip()))
            elif name == "reported_by":
                ev["outlets"] += 1
            elif name in STORY_LINKS and b in nodes:
                ev["develops"].append(b)
        if b in events and name in STORY_LINKS and a in nodes:
            events[b]["developed_by"].append(a)

    def weight(ev):
        meta = ev["node"].get("meta") or {}
        importance = as_int(meta.get("importance"), 1)
        sources = as_int(meta.get("sources_count"), 0) or ev["outlets"]
        return importance, min(sources, 50)

    kept = []
    for i, ev in events.items():
        importance, _ = weight(ev)
        if importance < min_importance:
            continue
        if wanted_topics and not {t.casefold() for t in ev["topics"]} & wanted_topics:
            continue
        if who and not any(named(nodes.get(x) or {}, who) for x in ev["places"] + [p for p, _ in ev["involves"]]):
            continue
        kept.append(i)
    kept.sort(key=lambda i: (weight(events[i]), events[i]["day"].isoformat()), reverse=True)

    def label(node_id):
        return (nodes.get(node_id) or {}).get("name", "")

    def event_out(i):
        ev, meta = events[i], events[i]["node"].get("meta") or {}
        importance, sources = weight(ev)
        out = {"name": ev["node"].get("name", ""), "date": ev["day"].isoformat(),
               "importance": importance, "sources": sources}
        for key, value in (
            ("topics", ev["topics"]),
            ("places", [label(p) for p in ev["places"]]),
            ("involves", [f"{label(p)} ({role})" if role else label(p) for p, role in ev["involves"]]),
            ("develops", [label(x) for x in ev["develops"]]),
            ("developed_by", [label(x) for x in ev["developed_by"]]),
        ):
            if value:
                out[key] = value
        summary = meta.get("summary") or ""
        if summary:
            out["summary"] = clip(" ".join(str(summary).split()), PERIOD_TEXT)
        return out

    limit = max(1, int(limit))
    tally = {}
    for i in kept:
        ev = events[i]
        importance, _ = weight(ev)
        for node_id, role in [(p, "") for p in ev["places"]] + ev["involves"]:
            node = nodes.get(node_id)
            kind = ntype(node or {})
            if kind not in ENTITY_TYPES:
                continue
            entry = tally.setdefault(node_id, {"kind": kind, "events": [], "weight": 0, "roles": []})
            entry["events"].append(i)
            entry["weight"] += importance
            if role and role not in entry["roles"]:
                entry["roles"].append(role)

    def entity_out(node_id, entry):
        node = nodes[node_id]
        meta = node.get("meta") or {}
        out = {"name": node.get("name", ""), "events": len(entry["events"])}
        about = meta.get("role") or meta.get("about") or meta.get("ekind")
        if about:
            out["about"] = clip(str(about), 120)
        if entry["roles"]:
            out["roles"] = entry["roles"][:5]
        out["top_events"] = [events[i]["node"].get("name", "") for i in entry["events"][:3]]
        created = parse_day(node.get("created_at"))
        if created is not None and since <= created <= until:
            out["new"] = True
        return out

    centre = {}
    for kind, key in (("person", "people"), ("org", "orgs"), ("place", "places")):
        ranked = sorted(
            ((node_id, entry) for node_id, entry in tally.items() if entry["kind"] == kind),
            key=lambda pair: (pair[1]["weight"], len(pair[1]["events"])),
            reverse=True,
        )
        centre[key] = [entity_out(node_id, entry) for node_id, entry in ranked[:PERIOD_ENTITIES]]

    # Stories: components over the story links of the kept events, with the
    # earlier events they lead from.
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    kept_set = set(kept)
    for i in kept:
        for other in events[i]["develops"] + events[i]["developed_by"]:
            parent[find(i)] = find(other)
    chains = {}
    for x in list(parent):
        chains.setdefault(find(x), []).append(x)
    stories = []
    for members in chains.values():
        inside = [m for m in members if m in kept_set]
        if len(members) < 2 or not inside:
            continue
        steps = []
        for m in members:
            if m not in nodes:
                continue
            day = event_day(nodes[m]) or since
            step = {"date": day.isoformat(), "name": nodes[m].get("name", "")}
            if day < since:
                step["before"] = True
            elif day > until:
                step["after"] = True
            steps.append(step)
        steps.sort(key=lambda s: s["date"])
        stories.append((sum(weight(events[m])[0] for m in inside), steps))
    stories.sort(key=lambda pair: pair[0], reverse=True)

    counts_topics, by_day, by_importance = {}, {}, {}
    for i in kept:
        ev = events[i]
        for topic in ev["topics"]:
            counts_topics[topic] = counts_topics.get(topic, 0) + 1
        by_day[ev["day"].isoformat()] = by_day.get(ev["day"].isoformat(), 0) + 1
        imp = str(weight(ev)[0])
        by_importance[imp] = by_importance.get(imp, 0) + 1

    out = {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "events_total": len(kept),
        "events": [event_out(i) for i in kept[:limit]],
    }
    more = [events[i]["node"].get("name", "") for i in kept[limit:limit + PERIOD_HEADLINES]]
    if more:
        out["more_events"] = more
    if len(kept) > limit + PERIOD_HEADLINES:
        out["not_listed"] = len(kept) - limit - PERIOD_HEADLINES
    out.update(centre)
    out["stories"] = [steps for _, steps in stories[:PERIOD_STORIES]]
    out["topics"] = dict(sorted(counts_topics.items(), key=lambda pair: -pair[1]))
    out["by_day"] = dict(sorted(by_day.items()))
    out["by_importance"] = dict(sorted(by_importance.items(), reverse=True))
    return out
