"""Degrees of connection and shortest paths over recorded relationships.

Distances are minimum numbers of steps between people, where one step is any
included relationship record between two people. Stored seed/networkDegree
fields are never used.

Two traversal rules are available:

* any-direction (default): a relationship can be walked in either direction.
  Every step still reports the stored direction and meaning.
* recorded-direction: one-way relationship types (mentor, postdoc, committee)
  can be walked only from their `from` person to their `to` person, which the
  data dictionary defines as their meaning (mentor -> mentee, postdoc ->
  supervisor, committee member -> student). Mutual types (coauthor, grant,
  collaborator, labmate, met, event, irta_colleague) can be walked both ways,
  because the relationship holds in both directions. Training records are
  left out: the packet defines no direction for them and their stored order
  is mixed (trainee -> PI in some records, mentor -> trainee in others).
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from .model import Notebook

# Display precedence. A pair with records of several kinds is shown as the first kind in this order:
# supervision, then research, then association.
TYPE_ORDER = ["mentor", "postdoc", "committee", "coauthor", "grant", "collaborator", "training",
              "labmate", "irta_colleague", "met", "event"]
ONE_WAY = {"mentor", "postdoc", "committee"}
MUTUAL = {"coauthor", "grant", "collaborator", "labmate", "met", "event", "irta_colleague"}
UNSPECIFIED_DIRECTION = {"training"}

# Training-program records sit with associations: most of them record co-listing on a program roster and
# state that individual supervision is not established.
TYPE_GROUPS = {
    "supervision": ["mentor", "postdoc", "committee"],
    "research": ["coauthor", "grant", "collaborator"],
    "association": ["training", "labmate", "irta_colleague", "met", "event"],
}
GROUP_PRIORITY = ["supervision", "research", "association"]
TYPE_GROUP_OF = {t: g for g, ts in TYPE_GROUPS.items() for t in ts}

ALL_TYPES = frozenset(TYPE_ORDER)
ALL_PROVENANCE = frozenset(["public", "reported", "firsthand"])
ALL_CONFIDENCE = frozenset(["confirmed", "likely", "unverified"])
DEFAULT_STATUS = frozenset(["reviewed"])

MODES = ("any", "directed")


@dataclass(frozen=True)
class TieFilter:
    types: FrozenSet[str] = ALL_TYPES
    provenance: FrozenSet[str] = ALL_PROVENANCE
    confidence: FrozenSet[str] = ALL_CONFIDENCE
    status: FrozenSet[str] = DEFAULT_STATUS

    def allows(self, e: Dict[str, Any]) -> bool:
        return (e.get("type") in self.types and e.get("provenance") in self.provenance
                and e.get("confidence") in self.confidence and e.get("status") in self.status)

    def describe(self) -> Dict[str, Any]:
        return {
            "types": [t for t in TYPE_ORDER if t in self.types],
            "provenance": sorted(self.provenance),
            "confidence": sorted(self.confidence),
            "status": sorted(self.status),
            "isDefault": self == TieFilter(),
        }

    @classmethod
    def from_params(cls, params: Dict[str, str]) -> "TieFilter":
        def pick(name, allowed, default):
            raw = params.get(name)
            if raw is None or raw == "":
                return default
            vals = frozenset(v for v in raw.split(",") if v in allowed)
            return vals
        status_allowed = {"reviewed", "proposed", "rejected"}
        return cls(
            types=pick("types", ALL_TYPES, ALL_TYPES),
            provenance=pick("provenance", ALL_PROVENANCE, ALL_PROVENANCE),
            confidence=pick("confidence", ALL_CONFIDENCE, ALL_CONFIDENCE),
            status=pick("status", status_allowed, DEFAULT_STATUS),
        )


def pair(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def can_walk(e: Dict[str, Any], u: str, v: str, mode: str) -> bool:
    """May relationship record e be walked from u to v under the traversal rule?"""
    if {e["from"], e["to"]} != {u, v}:
        return False
    if mode == "any":
        return True
    t = e["type"]
    if t in MUTUAL:
        return True
    if t in ONE_WAY:
        return e["from"] == u and e["to"] == v
    return False


class Graph:
    """Person-level graph for one relationship filter."""

    def __init__(self, nb: Notebook, tie_filter: TieFilter = TieFilter()):
        self.nb = nb
        self.filter = tie_filter
        self.edges: Dict[str, Dict[str, Any]] = {}
        self.pair_edges: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        self.nbrs: Dict[str, Set[str]] = defaultdict(set)
        self.excluded_count = 0
        contacts = nb.records["contacts"]
        arch_c = nb.archived["contacts"]
        for eid, e in nb.active("edges"):
            a, b = e.get("from"), e.get("to")
            if a not in contacts or b not in contacts or a in arch_c or b in arch_c or a == b:
                continue
            if not tie_filter.allows(e):
                self.excluded_count += 1
                continue
            self.edges[eid] = e
            self.pair_edges[pair(a, b)].append(eid)
            self.nbrs[a].add(b)
            self.nbrs[b].add(a)
        self._out: Dict[str, Set[str]] = {}

    # -- adjacency -------------------------------------------------------------

    def records_between(self, a: str, b: str) -> List[Dict[str, Any]]:
        return [self.edges[eid] for eid in self.pair_edges.get(pair(a, b), [])]

    def walkable(self, u: str, v: str, mode: str) -> List[Dict[str, Any]]:
        return [e for e in self.records_between(u, v) if can_walk(e, u, v, mode)]

    def out_nbrs(self, u: str, mode: str) -> Set[str]:
        if mode == "any":
            return self.nbrs.get(u, set())
        if u not in self._out:
            self._out[u] = {v for v in self.nbrs.get(u, set()) if self.walkable(u, v, mode)}
        return self._out[u]

    # -- breadth-first search ----------------------------------------------------

    def bfs(self, sources: Iterable[str], mode: str = "any",
            max_depth: Optional[int] = None) -> Tuple[Dict[str, int], Dict[str, List[str]]]:
        """Distances from the nearest source and every shortest-path predecessor."""
        dist: Dict[str, int] = {}
        preds: Dict[str, List[str]] = {}
        q = deque()
        for s in sources:
            if s in self.nb.records["contacts"] and s not in dist:
                dist[s] = 0
                preds[s] = []
                q.append(s)
        while q:
            u = q.popleft()
            if max_depth is not None and dist[u] >= max_depth:
                continue
            for v in self.out_nbrs(u, mode):
                if v not in dist:
                    dist[v] = dist[u] + 1
                    preds[v] = [u]
                    q.append(v)
                elif dist[v] == dist[u] + 1:
                    preds[v].append(u)
        for v in preds:
            preds[v].sort()
        return dist, preds

    def lab_distances(self) -> Tuple[Dict[str, int], Dict[str, List[str]]]:
        return self.bfs(self.nb.root_ids, "any")

    # -- shortest paths ------------------------------------------------------------

    def shortest_paths(self, sources: Iterable[str], target: str, mode: str = "any",
                       limit: int = 25) -> Dict[str, Any]:
        sources = [s for s in sources if s in self.nb.records["contacts"]]
        if target not in self.nb.records["contacts"]:
            return {"status": "unknown-person", "distance": None, "paths": [], "pathCount": 0}
        if target in sources:
            return {"status": "same-person", "distance": 0, "paths": [[target]], "pathCount": 1}
        dist, preds = self.bfs(sources, mode)
        if target not in dist:
            return {"status": "no-path", "distance": None, "paths": [], "pathCount": 0}
        counts: Dict[str, int] = {}

        def count(v):
            if v in counts:
                return counts[v]
            counts[v] = 1 if dist[v] == 0 else sum(count(p) for p in preds[v])
            return counts[v]

        total = count(target)
        paths: List[List[str]] = []

        def walk(v, suffix):
            if len(paths) >= limit:
                return
            if dist[v] == 0:
                paths.append([v] + suffix)
                return
            for p in preds[v]:
                walk(p, [v] + suffix)

        walk(target, [])
        return {"status": "path", "distance": dist[target], "paths": paths, "pathCount": total,
                "truncated": total > len(paths)}

    def path_steps(self, path: List[str], mode: str) -> List[Dict[str, Any]]:
        """For each step u->v, every included record that may be walked u->v."""
        steps = []
        for u, v in zip(path, path[1:]):
            recs = self.walkable(u, v, mode)
            steps.append({"from": u, "to": v, "records": recs})
        return steps
