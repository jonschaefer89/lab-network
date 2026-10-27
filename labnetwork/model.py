"""The current notebook: packet records with every workspace event applied.

Each field of each record carries a version: "packet" for untouched packet
values, otherwise the UUID of the event that last set it. Imports use these
versions to tell a fast-forward (the other copy edited a value we never
touched) from a real conflict (both copies changed it).
"""
from __future__ import annotations

import copy
import re
import unicodedata
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .packet import Packet

COLLECTIONS = ("contacts", "edges", "meetings")
KIND_TO_COLL = {"contact": "contacts", "edge": "edges", "meeting": "meetings"}
BASE_VERSION = "packet"

# Fields that hold lists merged by union rather than replaced.
LIST_FIELDS = {"tags", "sources", "papers", "grants"}


def norm_text(s: str) -> str:
    """Lowercase, accent-free, punctuation-collapsed form used for matching."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def name_key(name: str) -> str:
    """Name form for duplicate detection: drops single-letter initials."""
    parts = [p for p in norm_text(name).split() if len(p) > 1]
    return " ".join(parts)


def paper_key(p: Dict[str, Any]) -> str:
    doi = (p.get("doi") or "").strip().lower()
    if not doi:
        m = re.search(r"doi\.org/(10\.[^\s?#]+)", (p.get("url") or "").lower())
        doi = m.group(1) if m else ""
    if doi:
        return "doi:" + doi.rstrip("/.")
    return "title:" + norm_text(p.get("title", ""))


def grant_key(g: Dict[str, Any]) -> str:
    num = (g.get("grantNumber") or "").strip().upper()
    if num:
        return "award:" + re.sub(r"[^A-Z0-9]", "", num)
    pid = (g.get("projectId") or g.get("awardId") or "").strip().upper()
    if pid:
        return "project:" + re.sub(r"[^A-Z0-9]", "", pid)
    return "title:" + norm_text(g.get("title", "")) + "|" + norm_text(g.get("funder", ""))


def research_item_key(list_name: str, item: Dict[str, Any]) -> str:
    """Identity of one person-paper or person-grant association record."""
    if list_name == "papers":
        return paper_key(item) + "|" + norm_text(item.get("year", ""))
    return grant_key(item) + "|" + norm_text(item.get("projectId", "")) + "|" + norm_text(item.get("role", ""))


def source_key(s: Dict[str, Any]) -> str:
    return (s.get("url") or "").strip() + "|" + norm_text(s.get("note", ""))


def list_item_key(field: str, item: Any) -> str:
    if field == "tags":
        return norm_text(str(item))
    if field == "sources":
        return source_key(item)
    return research_item_key(field, item)


_UNPLACED_ROLE = re.compile(r"current (role|position)[^.;]*\b(pending|unknown|not (identified|verified|confirmed))"
                            r"|^former\b|historical memorial", re.I)


class Notebook:
    def __init__(self, packet: Packet, events: Iterable[Dict[str, Any]], root_ids: List[str]):
        self.packet = packet
        nb = packet.notebook
        self.schema_version = nb.get("schemaVersion", 1)
        self.records: Dict[str, Dict[str, Dict[str, Any]]] = {c: {} for c in COLLECTIONS}
        for coll in COLLECTIONS:
            for rec in nb.get(coll) or []:
                self.records[coll][rec["id"]] = copy.deepcopy(rec)
        self.research = copy.deepcopy(nb.get("research") or [])
        self.applied_seeds = list(nb.get("appliedSeeds") or [])
        self.has_applied_seeds = "appliedSeeds" in nb
        self.coverage = copy.deepcopy(nb.get("coverage")) if "coverage" in nb else None
        self.base_ids = {c: set(self.records[c]) for c in COLLECTIONS}
        self.archived = {c: set() for c in COLLECTIONS}
        self.versions: Dict[Tuple[str, str, str], str] = {}
        self.chains: Dict[Tuple[str, str, str], List[str]] = defaultdict(list)
        self.creators: Dict[Tuple[str, str], str] = {}
        self.history: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        self.aliases: Dict[str, str] = {}
        self.conflicts: Dict[str, Dict[str, Any]] = {}
        self.batches: Dict[str, Dict[str, Any]] = {}
        self.note_meta: Dict[str, Dict[str, Any]] = defaultdict(dict)
        # Whose connections were collected, from collection notes (workspace.note events); the latest wins.
        self.collection: Dict[str, Dict[str, Any]] = {}
        self.collection_runs: List[Dict[str, Any]] = []
        # OpenAlex author records a person marked as not someone's (identity notes); the latest note per record wins.
        self.not_theirs: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.uuids = set()
        self.events: List[Dict[str, Any]] = []
        self.root_ids = list(root_ids)
        self.revision = 0
        self.replay_errors: List[Dict[str, Any]] = []
        for ev in events:
            try:
                self.apply(ev)
            except (KeyError, ValueError, TypeError) as exc:
                # Keep loading; the event stays in the log and is reported, never dropped.
                self.replay_errors.append({"seq": ev.get("seq"), "op": ev.get("op"), "id": ev.get("id"),
                                           "error": f"{type(exc).__name__}: {exc}"})
                if ev.get("uuid"):
                    self.uuids.add(ev["uuid"])

    # -- event application --------------------------------------------------

    def apply(self, ev: Dict[str, Any]) -> None:
        op = ev["op"]
        kind, _, action = op.partition(".")
        rid = ev.get("id")
        data = ev.get("data") or {}
        uid = ev["uuid"]
        if kind in KIND_TO_COLL:
            coll = KIND_TO_COLL[kind]
            if action == "create":
                self.records[coll][rid] = copy.deepcopy(data["record"])
                self.creators[(coll, rid)] = uid
                self.archived[coll].discard(rid)
            elif action == "update":
                rec = self.records[coll][rid]
                for field, value in data["set"].items():
                    if value is None:
                        rec.pop(field, None)
                    else:
                        rec[field] = copy.deepcopy(value)
                    self.versions[(coll, rid, field)] = uid
                    self.chains[(coll, rid, field)].append(uid)
            elif action == "append":
                # Additive change to a list field (tags, sources, papers, grants).
                rec = self.records[coll][rid]
                field = data["field"]
                lst = rec.setdefault(field, [])
                have = {list_item_key(field, x) for x in lst}
                for item in data["items"]:
                    if list_item_key(field, item) not in have:
                        lst.append(copy.deepcopy(item))
                        have.add(list_item_key(field, item))
                self.versions[(coll, rid, field)] = uid
                self.chains[(coll, rid, field)].append(uid)
            elif action == "archive":
                self.archived[coll].add(rid)
            elif action == "restore":
                self.archived[coll].discard(rid)
            elif action == "alias":
                self.aliases[data["alias"]] = rid
            elif action == "meta":
                self.note_meta[rid].update(data.get("set") or {})
            else:
                raise ValueError(f"unknown event op {op}")
            self.history[(coll, rid)].append(ev["seq"])
        elif op == "import.batch":
            info = dict(data)
            info["seq"] = ev["seq"]
            info["ts"] = ev["ts"]
            info["actor"] = ev.get("actor", "")
            self.batches[data["batchId"]] = info
        elif op == "conflict.open":
            c = dict(data)
            c.update({"status": "open", "openedSeq": ev["seq"], "openedAt": ev["ts"]})
            self.conflicts[data["conflictId"]] = c
        elif op == "conflict.resolve":
            c = self.conflicts.get(data["conflictId"])
            if c is not None:
                c.update({"status": data["resolution"], "resolvedSeq": ev["seq"], "resolvedAt": ev["ts"],
                          "resolvedBy": ev.get("actor", ""), "resolutionNote": data.get("note", "")})
        elif op == "workspace.note":
            if data.get("kind") == "collection" and isinstance(data.get("people"), dict):
                run = {k: data.get(k) for k in ("date", "method", "degree", "standard", "batchId")}
                run["people"] = len(data["people"])
                self.collection_runs.append(run)
                for pid, info in data["people"].items():
                    if not isinstance(info, dict):
                        continue
                    prev = self.collection.get(pid) or {}
                    entry = {**info, "date": data.get("date", ""), "method": data.get("method", ""),
                             "standard": data.get("standard") or {}, "batchId": data.get("batchId", "")}
                    if info.get("status") == "collected" and prev.get("status") == "collected":
                        # A later run adds to what earlier runs collected.
                        for k in ("added", "linked"):
                            entry[k] = (prev.get(k) or 0) + (info.get(k) or 0)
                        entry["firstDate"] = prev.get("firstDate") or prev.get("date", "")
                        if prev.get("basis") and str(info.get("basis", "")).startswith("OpenAlex link on"):
                            entry["basis"] = prev["basis"]     # keep how the identity was first established
                    self.collection[pid] = entry
            elif data.get("kind") == "identity" and data.get("person") and data.get("openalexId"):
                marks = self.not_theirs.setdefault(data["person"], {})
                if data.get("notTheirs"):
                    marks[data["openalexId"]] = {"id": data["openalexId"], "name": data.get("name", ""),
                                                 "note": data.get("note", ""), "date": data.get("date", ""),
                                                 "actor": ev.get("actor", ""), "ts": ev.get("ts", "")}
                else:
                    marks.pop(data["openalexId"], None)
                if not marks:
                    del self.not_theirs[data["person"]]
        else:
            raise ValueError(f"unknown event op {op}")
        self.uuids.add(uid)
        self.events.append(ev)
        self.revision += 1

    # -- lookups --------------------------------------------------------------

    def canonical_id(self, rid: str) -> str:
        seen = set()
        while rid in self.aliases and rid not in seen:
            seen.add(rid)
            rid = self.aliases[rid]
        return rid

    def get(self, coll: str, rid: str) -> Optional[Dict[str, Any]]:
        return self.records[coll].get(rid)

    def contact(self, rid: str) -> Optional[Dict[str, Any]]:
        return self.records["contacts"].get(self.canonical_id(rid))

    def openalex_not_theirs(self, rid: str) -> Dict[str, Dict[str, Any]]:
        """The OpenAlex author records marked as not this person's, by record ID."""
        return self.not_theirs.get(self.canonical_id(rid), {})

    def is_active(self, coll: str, rid: str) -> bool:
        return rid in self.records[coll] and rid not in self.archived[coll]

    def active(self, coll: str) -> Iterable[Tuple[str, Dict[str, Any]]]:
        arch = self.archived[coll]
        for rid, rec in self.records[coll].items():
            if rid not in arch:
                yield rid, rec

    def id_taken(self, coll: str, rid: str) -> bool:
        return rid in self.records[coll] or (coll == "contacts" and rid in self.aliases)

    def version(self, coll: str, rid: str, field: str) -> Optional[str]:
        v = self.versions.get((coll, rid, field))
        if v:
            return v
        if rid in self.base_ids[coll]:
            return BASE_VERSION
        return self.creators.get((coll, rid))

    def origin(self, coll: str, rid: str) -> str:
        return "packet" if rid in self.base_ids[coll] else "workspace"

    def edited_fields(self, coll: str, rid: str) -> List[str]:
        return sorted(f for (c, r, f) in self.versions if c == coll and r == rid)

    def record_history(self, coll: str, rid: str) -> List[Dict[str, Any]]:
        seqs = set(self.history.get((coll, rid), []))
        return [ev for ev in self.events if ev["seq"] in seqs]

    # -- person classification -------------------------------------------------

    def doctorate_state(self, c: Dict[str, Any]) -> str:
        """confirmed | not-yet-earned | unknown | not-classified (field absent)."""
        return c.get("doctorateStatus") or "not-classified"

    def placement(self, c: Dict[str, Any]) -> Dict[str, Any]:
        """Whether a current institution and position can be identified."""
        inst = (c.get("institution") or "").strip()
        role = (c.get("role") or "").strip()
        problems = []
        if not inst:
            problems.append("institution not recorded")
        if not role:
            problems.append("position not recorded")
        elif _UNPLACED_ROLE.search(role):
            problems.append("position recorded as former, pending or historical")
        return {"identified": not problems, "problems": problems}

    def is_lab(self, rid: str) -> bool:
        return rid in self.root_ids

    def counts(self) -> Dict[str, Any]:
        active = {coll: sum(1 for _ in self.active(coll)) for coll in COLLECTIONS}
        return {
            "contacts": active["contacts"],
            "edges": active["edges"],
            "meetings": active["meetings"],
            "archived": {coll: len(self.archived[coll]) for coll in COLLECTIONS},
            "createdInWorkspace": {coll: len(set(self.records[coll]) - self.base_ids[coll]) for coll in COLLECTIONS},
            "editedRecords": len({(c, r) for (c, r, _) in self.versions}),
            "openConflicts": sum(1 for c in self.conflicts.values() if c["status"] == "open"),
            "events": len(self.events),
            "importBatches": len(self.batches),
        }

    # -- serialisation ----------------------------------------------------------

    def to_notebook(self, include_archived: bool = False) -> Dict[str, Any]:
        """The current notebook in the packet's own format (schema-compatible)."""
        out: Dict[str, Any] = {"schemaVersion": self.schema_version}
        for coll in ("contacts", "meetings", "edges"):
            out[coll] = [copy.deepcopy(rec) for rid, rec in self.records[coll].items()
                         if include_archived or rid not in self.archived[coll]]
        out["research"] = copy.deepcopy(self.research)
        if self.has_applied_seeds:
            out["appliedSeeds"] = list(self.applied_seeds)
        if self.coverage is not None:
            out["coverage"] = copy.deepcopy(self.coverage)
        return out
