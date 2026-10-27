"""Operations behind the HTTP API and the command line.

Every change goes through this module: it validates the edit against the
packet schema and the data-dictionary rules, writes one event to the change
log, and applies it to the in-memory notebook.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import threading
import time
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import describe
from .graph import (ALL_TYPES, GROUP_PRIORITY, MODES, TYPE_GROUP_OF, TYPE_ORDER, Graph, TieFilter)
from .model import BASE_VERSION, COLLECTIONS, KIND_TO_COLL, Notebook, name_key, norm_text
from .packet import (check_contact, check_edge, check_meeting, entity_schema, is_http_url,
                     validate_rules, validate_schema, validation_report)
from .search import load_aliases, search_all as run_search
from .store import Workspace, today, utc_now

APP_VERSION = "0.1.0"

CONTACT_READONLY = {"id", "createdAt", "seed", "networkDegree", "expansionStatus", "papers", "grants", "sources"}
EDGE_EDITABLE = {"evidence", "sourceTitle", "sourceUrl", "confidence", "status", "checkedAt", "relationshipStart",
                 "relationshipEnd", "relationshipStatus", "relationshipStatusNote", "relationshipLabel",
                 "attributionScope", "projectId", "projectTitle", "provenance"}
MEETING_EDITABLE = {"contactId", "metBy", "date", "context", "learning", "followUp", "recordedBy"}
META_FIELDS = {"followUpStatus"}


class ServiceError(Exception):
    status = 400

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(ServiceError):
    status = 404


class EditConflict(ServiceError):
    status = 409


def slugify(text: str, fallback: str = "person") -> str:
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:60].rstrip("-")) or fallback


def _short() -> str:
    return uuid.uuid4().hex[:6]


@dataclass(frozen=True)
class PeopleFilter:
    max_degree: Optional[int] = 2       # None means no limit (includes people with no path)
    doctorate: str = "confirmed"        # "confirmed" or "any"
    identified: bool = True             # require identified institution and position

    @classmethod
    def from_params(cls, params: Dict[str, str]) -> "PeopleFilter":
        md = params.get("maxDegree", "2")
        max_degree = None if md in ("all", "any", "") else max(0, min(int(md), 9)) if md.isdigit() else 2
        doct = params.get("doctorate", "confirmed")
        doct = doct if doct in ("confirmed", "any") else "confirmed"
        ident = params.get("identified", "1") not in ("0", "false", "no")
        return cls(max_degree, doct, ident)

    def describe(self) -> Dict[str, Any]:
        return {"maxDegree": self.max_degree, "doctorate": self.doctorate, "identified": self.identified,
                "isDefault": self == PeopleFilter()}


def _drop_beyond(res: Dict[str, Any], limit: int) -> Dict[str, Any]:
    """Remove topic matches for people beyond the degree limit and say how many were left out.

    Name and affiliation matches are kept, so a person further out can still be looked up by name.
    """
    beyond = lambda x: "beyond" in (x.get("hiddenBy") or [])
    left_out = set()
    for k in ("profiles", "notes", "events"):
        left_out.update(x["personId"] for x in res[k] if beyond(x))
        res[k] = [x for x in res[k] if not beyond(x)]
    for k in ("papers", "grants"):
        kept = []
        for item in res[k]:
            left_out.update(a["personId"] for a in item["associations"] if beyond(a))
            item["associations"] = [a for a in item["associations"] if not beyond(a)]
            if item["associations"]:
                item["shownAssociations"] = sum(1 for a in item["associations"] if a.get("shown"))
                kept.append(item)
        res[k] = kept
    named = {x["personId"] for x in res["people"]}
    res["peopleSummary"] = [p for p in res["peopleSummary"] if not beyond(p) or p["personId"] in named]
    for p in res["peopleSummary"]:
        if beyond(p):
            for k in ("profiles", "papers", "grants", "notes", "events"):
                p[k] = 0
    res["counts"].update({"profiles": len(res["profiles"]), "papers": len(res["papers"]),
                          "paperAssociations": sum(len(e["associations"]) for e in res["papers"]),
                          "grants": len(res["grants"]),
                          "grantAssociations": sum(len(e["associations"]) for e in res["grants"]),
                          "notes": len(res["notes"]), "events": len(res["events"])})
    res["beyondLimit"] = {"limit": limit, "people": len(left_out - named)}
    return res


def _tie_order(t: Dict[str, Any]) -> Tuple[Any, ...]:
    """Group, then section, then: most shared records first for research, most recent first otherwise.

    Undated relationships follow the dated ones; family name breaks ties.
    """
    year = t["latestYear"]
    by_date = (0, -year) if year is not None else (1, 0)
    first = (-t["sectionRecords"],) + by_date if t["group"] == "research" else by_date + (0,)
    return (GROUP_PRIORITY.index(t["group"]) if t["group"] in GROUP_PRIORITY else 9, t["section"]) + first + \
        describe.surname_key(t["person"]["name"])


def _quote(arg: str) -> str:
    """A command-line argument quoted for this system's shell (paths may hold spaces, ampersands or quotes).

    On Windows anything but plain path characters goes in double quotes, which both cmd and PowerShell keep as typed.
    """
    if os.name == "nt":
        return _win_quote(arg)
    import shlex
    return shlex.quote(arg)


def _win_quote(arg: str) -> str:
    return arg if re.fullmatch(r"[A-Za-z0-9_.:\\/-]+", arg or "") else '"' + (arg or "").replace('"', '""') + '"'


class Service:
    def __init__(self, workspace: Workspace):
        self.ws = workspace
        self.packet = workspace.load_packet()
        self.report = validation_report(self.packet)
        self.lock = threading.RLock()
        self.nb = Notebook(self.packet, workspace.read_events(), self.packet.root_ids)
        self.aliases = load_aliases(workspace.path)
        self.pending_imports: Dict[str, Dict[str, Any]] = {}
        self._cache: Dict[Any, Any] = {}
        self._library: Optional[Tuple[float, Dict[str, Any]]] = None
        self._latest: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._live_client = None
        self._conferences: Optional[Tuple[Any, List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]] = None

    # ------------------------------------------------------------------ helpers

    def conferences(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Imported conference programs matched to the notebook (see conferences.py), and one search item per
        matched appearance. Reread when an import rewrites the file or the notebook changes."""
        from . import conferences as conf
        p = conf.path(self.ws)
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            return [], []
        key = (mtime, self.nb.revision)
        if self._conferences is None or self._conferences[0] != key:
            data = conf.load(self.ws)
            confs = conf.matched(self.nb, data)
            self._conferences = (key, confs, conf.items(confs), data)
        return self._conferences[1], self._conferences[2]

    def conference_list(self, params: Dict[str, str]) -> Dict[str, Any]:
        """The lab's conference list, each with its imported programs and how many people in the network are on them.

        With `series`, that conference across its years (conferences.breakdown), each person with their card and
        degree under the current filters. With `id`, one program's appearances, matched or not.
        """
        from . import conferences as conf
        with self.lock:
            confs, items = self.conferences()
            data = self._conference_data()
            cid, sid = params.get("id") or "", params.get("series") or ""
            if cid:
                c = next((c for c in confs if c["id"] == cid), None)
                if c is None:
                    raise NotFound(f"No imported conference program with the ID {cid}")
                names = self.names()
                return {"conferences": [{**{k: v for k, v in c.items() if k != "appearances"}, "appearances": [
                    {**a, "personName": names.get(a["person"], "") if a["person"] else "",
                     "candidateNames": [names.get(x, x) for x in a.get("candidates") or []]} for a in c["appearances"]]}]}
            listed = data.get("series", [])
            if sid:
                ser = next((x for x in listed if x["id"] == sid), None)
                if ser is None:
                    raise NotFound(f"{sid} is not on the lab's conference list")
                b = conf.breakdown(confs, sid)
                tf = TieFilter.from_params(params)
                pf = self.people_filter(params)
                dist, preds = self.lab_dist(tf)
                names = self.names()
                limit = self.degree_limit()
                rows, beyond = [], 0
                for p in b["people"]:
                    if not self.nb.is_active("contacts", p["personId"]):
                        continue
                    f = self.person_flags(p["personId"], dist, pf)
                    if limit is not None and "beyond" in f["hiddenBy"]:
                        beyond += 1
                        continue
                    card = self._card(p["personId"], f)
                    card["via"] = [names.get(x, x) for x in preds.get(p["personId"], [])][:3]
                    other = sorted({i["conference"].rsplit(" ", 1)[0] for i in items
                                    if i["personId"] == p["personId"] and i["series"] != sid})
                    rows.append({**p, **card, "otherConferences": other})
                return {**b, **ser, "series": sid, "people": rows, "beyondLimit": {"limit": limit, "people": beyond}}
            out = []
            for ser in listed:
                progs = sorted((c for c in confs if c.get("series") == ser["id"]), key=lambda c: -c["year"])
                people = {a["person"] for c in progs for a in c["appearances"] if a["person"]}
                out.append({**ser, "programs": [{k: v for k, v in c.items() if k != "appearances"} for c in progs],
                            "inNetwork": len(people)})
            listed_ids = {x["id"] for x in listed}
            return {"series": out,
                    "offList": sum(1 for c in data.get("conferences", []) if c.get("series") not in listed_ids)}

    def _conference_data(self) -> Dict[str, Any]:
        """conferences.json as last read (the list and the raw programs)."""
        self.conferences()
        return self._conferences[3] if self._conferences else {"series": [], "conferences": []}

    def conference_series(self, payload: Dict[str, Any], actor: str, remove: str = "") -> Dict[str, Any]:
        """Add a conference to the lab's list (`short`, `name`, `url`, `frequency`), or take one off (`remove`)."""
        from . import conferences as conf
        with self.lock:
            try:
                if remove:
                    kept = conf.series_remove(self.ws, remove)
                    res = {"removed": remove, "programsKept": kept}
                else:
                    res = conf.series_add(self.ws, str(payload.get("short") or ""), str(payload.get("name") or ""),
                                          str(payload.get("url") or ""), str(payload.get("frequency") or ""), actor)
            except KeyError:
                raise NotFound(f"{remove} is not on the lab's conference list")
            except ValueError as err:
                raise ServiceError(str(err))
            self._conferences = None
            return res

    def _cached(self, key, fn):
        k = (self.nb.revision,) + tuple(key)
        if k not in self._cache:
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[k] = fn()
        return self._cache[k]

    def names(self) -> Dict[str, str]:
        return self._cached(("names",), lambda: {rid: c["name"] for rid, c in self.nb.records["contacts"].items()})

    def graph(self, tf: TieFilter = TieFilter()) -> Graph:
        return self._cached(("graph", tf), lambda: Graph(self.nb, tf))

    def lab_dist(self, tf: TieFilter = TieFilter()) -> Tuple[Dict[str, int], Dict[str, List[str]]]:
        return self._cached(("labdist", tf), lambda: self.graph(tf).lab_distances())

    def origin_of_edge(self, eid: str) -> str:
        return self.nb.origin("edges", eid)

    def _require_contact(self, pid: str) -> Tuple[str, Dict[str, Any]]:
        cid = self.nb.canonical_id(pid)
        c = self.nb.records["contacts"].get(cid)
        if c is None:
            raise NotFound(f"No person with ID '{pid}'")
        return cid, c

    def person_flags(self, pid: str, dist: Dict[str, int], pf: PeopleFilter) -> Dict[str, Any]:
        c = self.nb.records["contacts"][pid]
        d = dist.get(pid)
        is_lab = self.nb.is_lab(pid)
        doct = self.nb.doctorate_state(c)
        plc = self.nb.placement(c)
        reasons = []
        if not is_lab:
            if pf.doctorate == "confirmed" and doct != "confirmed":
                reasons.append("doctorate")
            if pf.identified and not plc["identified"]:
                reasons.append("placement")
        limit = self.degree_limit()
        if limit is not None and not is_lab and (d is None or d > limit):
            reasons.append("beyond")
        elif pf.max_degree is not None and (d is None or d > pf.max_degree):
            reasons.append("degree")
        if pid in self.nb.archived["contacts"]:
            reasons.append("archived")
        return {"degree": d, "isLab": is_lab, "doctorate": doct, "identified": plc["identified"],
                "placementProblems": plc["problems"], "shown": not reasons, "hiddenBy": reasons}

    def degree_limit(self) -> Optional[int]:
        """The furthest degree the app shows (settings.degreeLimit), or None for no limit.

        Set it to the deepest degree whose connections were collected, so no partly collected ring is drawn.
        """
        v = (self.ws.config.get("settings") or {}).get("degreeLimit")
        if isinstance(v, bool) or v in (None, "", "none"):
            return None
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            return None

    def people_filter(self, params: Dict[str, str]) -> PeopleFilter:
        pf = PeopleFilter.from_params(params)
        limit = self.degree_limit()
        if limit is not None and (pf.max_degree is None or pf.max_degree > limit):
            pf = PeopleFilter(limit, pf.doctorate, pf.identified)
        return pf

    def info_fn(self, tf: TieFilter, pf: PeopleFilter):
        dist, _ = self.lab_dist(tf)

        def info(pid):
            if pid not in self.nb.records["contacts"]:
                return {"degree": None, "shown": False, "hiddenBy": ["unknown"]}
            f = self.person_flags(pid, dist, pf)
            return {"degree": f["degree"], "shown": f["shown"], "hiddenBy": f["hiddenBy"], "isLab": f["isLab"]}
        return info

    def _card(self, pid: str, flags: Dict[str, Any]) -> Dict[str, Any]:
        c = self.nb.records["contacts"][pid]
        return {
            "id": pid, "name": c["name"], "institution": c.get("institution", ""), "role": c.get("role", ""),
            "photoUrl": c.get("photoUrl", ""), "photoConfidence": c.get("photoConfidence", ""),
            "photoSourceUrl": c.get("photoSourceUrl", ""), "photoAttribution": c.get("photoAttribution", ""),
            "lab": c.get("labName") or c.get("lab", ""),
            **flags,
        }

    # ------------------------------------------------------------------ read APIs

    def state(self) -> Dict[str, Any]:
        with self.lock:
            dist, _ = self.lab_dist()
            pf = PeopleFilter()
            roots = []
            for rid in self.nb.root_ids:
                if rid in self.nb.records["contacts"]:
                    roots.append(self._card(rid, self.person_flags(rid, dist, pf)))
            type_counts = {t: 0 for t in TYPE_ORDER}
            for _, e in self.nb.active("edges"):
                type_counts[e["type"]] = type_counts.get(e["type"], 0) + 1
            return {
                "app": {"name": "Lab Network", "version": APP_VERSION},
                "lab": {"name": self.ws.config.get("labName") or "", "roots": roots},
                "packet": {
                    "sha256": self.packet.sha256, "bytes": self.packet.size,
                    "datasetStatus": self.report.get("datasetStatus"), "savedAt": self.report.get("savedAt"),
                    "ok": self.report["ok"], "checks": self.report["checks"], "counts": self.report["counts"],
                },
                "counts": self.nb.counts(),
                "types": [{"type": t, "label": describe.TYPE_LABELS[t], "group": TYPE_GROUP_OF[t],
                           "direction": ("one-way" if t in ("mentor", "postdoc", "committee") else
                                         "unspecified" if t == "training" else "mutual"),
                           "count": type_counts.get(t, 0)} for t in TYPE_ORDER],
                "provenanceLabels": describe.PROVENANCE_LABELS,
                "defaults": self.default_filters(),
                "degreeLimit": self.degree_limit(),
                "refresh": {"lastRefresh": (self.ws.config.get("settings") or {}).get("lastRefresh")},
                "workspace": {"events": len(self.nb.events), "quarantined": self.ws.quarantined,
                              "labName": self.ws.config.get("labName", "")},
                "coverage": self.nb.coverage,
                "appliedSeeds": self.nb.applied_seeds,
                # A lab's shared read-only copy (site.py): when it was made and where changes go instead.
                "sharedCopy": (self.ws.config.get("settings") or {}).get("sharedCopy"),
            }

    def default_filters(self) -> Dict[str, str]:
        d = (self.ws.config.get("settings") or {}).get("defaultFilters") or {}
        deg = str(d.get("maxDegree", "2"))
        limit = self.degree_limit()
        if limit is not None and (not deg.isdigit() or int(deg) > limit):
            deg = str(limit)
        return {"deg": deg, "doct": d.get("doctorate", "confirmed"),
                "ident": "0" if str(d.get("identified", "1")) in ("0", "false") else "1"}

    def digests(self) -> Dict[str, Any]:
        """Digests of refresh, expand and library runs (newest first), the library's last run and its schedule."""
        from . import schedule
        ddir = os.path.join(self.ws.path, "digests")
        out = []
        if os.path.isdir(ddir):
            for name in sorted(os.listdir(ddir), reverse=True):
                if name.endswith(".json"):
                    with open(os.path.join(ddir, name), "r", encoding="utf-8") as f:
                        d = json.load(f)
                    d.setdefault("kind", "expand" if name.startswith("expand") else "refresh")
                    d.setdefault("date", name[:-5].split("-", 1)[-1][:10])
                    md_path = os.path.join(ddir, name[:-5] + ".md")
                    if os.path.exists(md_path):
                        with open(md_path, encoding="utf-8") as f:
                            d["markdown"] = f.read()
                    else:
                        d["markdown"] = ""
                    out.append(d)
        out.sort(key=lambda d: (str(d.get("date", "")), str(d.get("createdAt", ""))), reverse=True)
        lib = self.library()
        runs = (lib or {}).get("runs") or []
        settings = self.ws.config.get("settings") or {}
        shared = bool(settings.get("sharedCopy"))
        try:
            sched = "off" if settings.get("demo") or shared else schedule.status(self.ws.path, "library")
        except Exception:      # the scheduler may be unavailable on this system
            sched = "unknown"
        awards = list((lib or {}).get("awards", {}).values())
        # Windows installs Python as python (python3 there can be the Microsoft Store stub); a bare first word also
        # works in PowerShell, which would read a quoted interpreter path as text.
        python = "python" if os.name == "nt" else "python3"
        cmd = lambda *a: " ".join([python, _quote(schedule.LAUNCHER)] + [_quote(x) for x in a])
        return {"digests": out[:52], "lastRefresh": settings.get("lastRefresh"),
                "library": {"lastRun": runs[-1] if runs else None, "schedule": sched,
                            "papers": len((lib or {}).get("works", {})),
                            "nihAwards": sum(1 for a in awards if a.get("source") == "nih"),
                            "nsfAwards": sum(1 for a in awards if a.get("source") == "nsf"),
                            "demo": bool(settings.get("demo")), "liveCheck": settings.get("liveCheck") is not False,
                            "sharedCopy": shared,       # a shared copy has no commands to run and no schedule of its own
                            "commands": {} if shared else {"build": cmd("library", self.ws.path),
                                         "update": cmd("library", self.ws.path, "--update"),
                                         "weekly": cmd("schedule", self.ws.path, "--every", "week", "--day", "mon",
                                                       "--at", "08:00"),
                                         "weeklyOff": cmd("schedule", self.ws.path, "--every", "off")}}}

    def network(self, params: Dict[str, str]) -> Dict[str, Any]:
        with self.lock:
            tf = TieFilter.from_params(params)
            pf = self.people_filter(params)
            g = self.graph(tf)
            dist, preds = self.lab_dist(tf)
            flags = {pid: self.person_flags(pid, dist, pf) for pid, _ in self.nb.active("contacts")}
            shown = {pid for pid, f in flags.items() if f["shown"]}

            def strength(a, b):
                return len(g.pair_edges.get((a, b) if a <= b else (b, a), []))

            root_rank = {r: i for i, r in enumerate(self.nb.root_ids)}
            parent: Dict[str, Optional[str]] = {}
            ghosts = set()

            def choose_parent(pid):
                ps = preds.get(pid) or []
                if not ps:
                    return None
                vis = [p for p in ps if p in shown]
                pool = vis or ps
                return sorted(pool, key=lambda p: (-strength(p, pid), root_rank.get(p, 99),
                                                   norm_text(self.nb.records["contacts"][p]["name"])))[0]

            todo = list(shown)
            while todo:
                pid = todo.pop()
                if pid in parent:
                    continue
                par = choose_parent(pid)
                parent[pid] = par
                if par is not None and par not in shown and par not in parent:
                    ghosts.add(par)
                    todo.append(par)
            nodes_ids = shown | ghosts

            def top_root(pid):
                seen = set()
                while parent.get(pid) is not None and pid not in seen:
                    seen.add(pid)
                    pid = parent[pid]
                return pid if pid in root_rank else None

            nodes = []
            for pid in nodes_ids:
                card = self._card(pid, flags[pid])
                card["ghost"] = pid in ghosts
                card["parent"] = parent.get(pid)
                card["parents"] = [p for p in preds.get(pid, []) if p in nodes_ids]
                card["root"] = top_root(pid)
                card["tieCount"] = len(g.nbrs.get(pid, ()))
                nodes.append(card)
            nodes.sort(key=lambda n: (n["degree"] if n["degree"] is not None else 99, norm_text(n["name"])))
            links = []
            for (a, b), eids in g.pair_edges.items():
                if a in nodes_ids and b in nodes_ids:
                    links.append({"a": a, "b": b, **_link_types(g, eids),
                                  "tree": parent.get(a) == b or parent.get(b) == a})
            hidden = {"doctorate": 0, "placement": 0, "degree": 0, "beyond": 0}
            hidden_within = {"doctorate": 0, "placement": 0}
            for pid, f in flags.items():
                for r in f["hiddenBy"]:
                    if r in hidden:
                        hidden[r] += 1
                    if r in hidden_within and "degree" not in f["hiddenBy"] and "beyond" not in f["hiddenBy"]:
                        hidden_within[r] += 1
            by_degree: Dict[str, int] = {}
            for pid in shown:
                d = flags[pid]["degree"]
                key = str(d) if d is not None else "none"
                by_degree[key] = by_degree.get(key, 0) + 1
            within = sum(1 for f in flags.values() if pf.max_degree is None or
                         (f["degree"] is not None and f["degree"] <= pf.max_degree))
            return {
                "roots": [r for r in self.nb.root_ids if r in nodes_ids],
                "nodes": nodes, "links": links,
                "counts": {"shown": len(shown), "ghosts": len(ghosts), "withinDegree": within,
                           "hiddenWithinDegree": within - len(shown), "hiddenBy": hidden,
                           "hiddenWithinBy": hidden_within, "byDegree": by_degree,
                           "total": len(flags), "excludedRecords": g.excluded_count,
                           "unreachable": sum(1 for f in flags.values() if f["degree"] is None),
                           "degreeLimit": self.degree_limit(),
                           "collection": self._collection_counts(flags)},
                "filters": {**pf.describe(), "ties": tf.describe()},
            }

    def person(self, pid: str, params: Dict[str, str]) -> Dict[str, Any]:
        with self.lock:
            pid, c = self._require_contact(pid)
            tf = TieFilter.from_params(params)
            pf = self.people_filter(params)
            g = self.graph(tf)
            dist, _ = self.lab_dist(tf)
            names = self.names()
            flags = self.person_flags(pid, dist, pf)
            res = g.shortest_paths(self.nb.root_ids, pid, "any", limit=12)
            conn = describe.describe_path_result(res, g, names, "any", self.origin_of_edge)
            conn["headline"] = describe.lab_connection_headline(res.get("distance"), conn["paths"],
                                                                res.get("pathCount", 0), names)
            conn["degreeLabel"] = describe.degree_label(res.get("distance"))
            if res["status"] == "no-path":
                conn["coverageNote"] = self._coverage_note(c)
            # Direct ties grouped by the other person, including records the filter leaves out.
            full = self.graph(TieFilter(status=frozenset(["reviewed", "proposed", "rejected"])))
            ties = []
            for other in sorted(full.nbrs.get(pid, set()), key=lambda o: norm_text(names.get(o, o))):
                recs = full.records_between(pid, other)
                summaries = []
                for e in sorted(recs, key=lambda e: TYPE_ORDER.index(e["type"])):
                    s = describe.edge_summary(e, names, self.origin_of_edge(e["id"]))
                    s["fromPerspective"] = describe.perspective(e, pid)
                    s["included"] = tf.allows(e)
                    summaries.append(s)
                of = self.person_flags(other, dist, pf)
                ties.append({"person": self._card(other, of), "records": summaries,
                             "summary": describe.step_summary(summaries),
                             "groups": sorted({TYPE_GROUP_OF.get(s["type"], "other") for s in summaries}),
                             "includedCount": sum(1 for s in summaries if s["included"]),
                             **describe.tie_section(summaries, pid)})
            ties.sort(key=_tie_order)
            meetings = []
            for mid, m in self.nb.records["meetings"].items():
                if self.nb.canonical_id(m.get("contactId", "")) == pid:
                    meetings.append(self._meeting_view(mid, m))
            meetings.sort(key=lambda m: (m["date"] or "0000", m["createdAt"]), reverse=True)
            edited = self.nb.edited_fields("contacts", pid)
            packet_values = {}
            if pid in self.nb.base_ids["contacts"]:
                base = next((x for x in self.packet.notebook["contacts"] if x["id"] == pid), None)
                if base:
                    packet_values = {f: base.get(f) for f in edited}
            conflicts = [cf for cf in self.nb.conflicts.values()
                         if cf.get("collection") == "contacts" and cf.get("recordId") == pid]
            return {
                "id": pid, "record": copy.deepcopy(c), "origin": self.nb.origin("contacts", pid),
                "archived": pid in self.nb.archived["contacts"], "flags": flags,
                "card": self._card(pid, flags), "connection": conn, "ties": ties, "meetings": meetings,
                "history": self._history_view("contacts", pid), "editedFields": edited,
                "packetValues": packet_values, "conflicts": conflicts,
                "aliases": sorted(a for a, t in self.nb.aliases.items() if t == pid),
                "missing": self._missing(c),
                "historicalCoverage": {k: c.get(k) for k in ("seed", "networkDegree", "expansionStatus")},
                "collection": self._collection_view(pid),
                "library": self._library_view(pid),
                "openalexNotTheirs": self._not_theirs_view(pid),
                "conferences": self._person_conferences(pid),
                "filters": {**pf.describe(), "ties": tf.describe()},
            }

    def _person_conferences(self, pid: str) -> Dict[str, Any]:
        """Their appearances on imported programs, newest first, and per conference the years they are on the program
        out of the years imported (`presentingYears`: in a role other than co-author)."""
        from . import conferences as conf
        confs, items = self.conferences()
        mine = sorted((x for x in items if x["personId"] == pid),
                      key=lambda x: (-(x.get("year") or 0), x["conference"], x.get("title", "")))
        series = {x["id"]: x for x in self._conference_data().get("series", [])} if confs else {}
        summary = []
        for sid in dict.fromkeys(x["series"] for x in mine):
            imported = sorted({c["year"] for c in confs if c.get("series") == sid})
            years = sorted({x["year"] for x in mine if x["series"] == sid})
            presenting = sorted({x["year"] for x in mine if x["series"] == sid and x.get("role") != conf.COAUTHOR})
            ser = series.get(sid) or {}
            summary.append({"series": sid, "short": ser.get("short", sid), "name": ser.get("name", ""),
                            "years": years, "presentingYears": presenting, "imported": imported,
                            "appearances": sum(1 for x in mine if x["series"] == sid)})
        summary.sort(key=lambda x: (-len(x["years"]), x["short"]))
        return {"appearances": mine, "summary": summary, "listed": len(series)}

    def _coverage_note(self, c: Dict[str, Any]) -> str:
        if c.get("expansionStatus") == "identity-only":
            return ("This person's record is identity-only (profile not confirmed), so missing ties are "
                    "possible. No recorded path is not evidence that no relationship exists.")
        return "No recorded path is not evidence that no relationship exists; coverage is incomplete."

    def _missing(self, c: Dict[str, Any]) -> List[Dict[str, str]]:
        out = []

        def miss(field, label, note=""):
            out.append({"field": field, "label": label, "note": note})

        if not (c.get("institution") or "").strip():
            miss("institution", "Current institution", "not recorded")
        if not (c.get("role") or "").strip():
            miss("role", "Current position", "not recorded")
        if "doctorateStatus" not in c:
            miss("doctorateStatus", "Doctorate", "not classified in the packet; this does not mean no doctorate")
        elif c["doctorateStatus"] == "unknown":
            miss("doctorateStatus", "Doctorate", "recorded as unknown; this does not mean no doctorate")
        if not c.get("photoUrl"):
            miss("photoUrl", "Photo", "no portrait URL recorded")
        if not (c.get("researchSummary") or "").strip():
            miss("researchSummary", "Research summary", "not recorded")
        return out

    def _meeting_view(self, mid: str, m: Dict[str, Any]) -> Dict[str, Any]:
        names = self.names()
        pid = self.nb.canonical_id(m.get("contactId", ""))
        meta = self.nb.note_meta.get(mid, {})
        return {"id": mid, **copy.deepcopy(m), "contactName": names.get(pid, pid), "contactId": pid,
                "origin": self.nb.origin("meetings", mid), "archived": mid in self.nb.archived["meetings"],
                "followUpStatus": meta.get("followUpStatus", "open" if (m.get("followUp") or "").strip() else ""),
                "history": self._history_view("meetings", mid),
                "editedFields": self.nb.edited_fields("meetings", mid)}

    def _history_view(self, coll: str, rid: str) -> List[Dict[str, Any]]:
        out = []
        for ev in self.nb.record_history(coll, rid):
            data = ev.get("data") or {}
            item = {"seq": ev["seq"], "ts": ev["ts"], "actor": ev.get("actor", ""), "op": ev["op"],
                    "batch": ev.get("batch") or ev.get("importedIn"), "uuid": ev["uuid"]}
            if ev["op"].endswith(".update"):
                item["changes"] = [{"field": f, "from": (data.get("prev") or {}).get(f), "to": v}
                                   for f, v in data.get("set", {}).items()]
            elif ev["op"].endswith(".append"):
                item["changes"] = [{"field": data.get("field"), "added": len(data.get("items") or [])}]
            elif ev["op"].endswith(".meta"):
                item["changes"] = [{"field": f, "to": v} for f, v in (data.get("set") or {}).items()]
            out.append(item)
        return out

    def neighborhood(self, pid: str, params: Dict[str, str]) -> Dict[str, Any]:
        """A person's direct ties plus the shortest route(s) back to the lab."""
        with self.lock:
            pid, _ = self._require_contact(pid)
            tf = TieFilter.from_params(params)
            pf = self.people_filter(params)
            g = self.graph(tf)
            dist, _ = self.lab_dist(tf)
            nbrs = set(g.nbrs.get(pid, set()))
            res = g.shortest_paths(self.nb.root_ids, pid, "any", limit=12)
            route_nodes = set()
            route_links = set()
            for p in res.get("paths", []):
                route_nodes.update(p)
                for a, b in zip(p, p[1:]):
                    route_links.add((a, b) if a <= b else (b, a))
            ids = {pid} | nbrs | route_nodes
            nodes = []
            for x in ids:
                f = self.person_flags(x, dist, pf)
                card = self._card(x, f)
                card["role_in_view"] = ("center" if x == pid else "neighbor" if x in nbrs else "route")
                card["onRoute"] = x in route_nodes
                card["tieCount"] = len(g.nbrs.get(x, ()))
                nodes.append(card)
            links = []
            for (a, b), eids in g.pair_edges.items():
                if a in ids and b in ids and (pid in (a, b) or (a, b) in route_links or (a in nbrs and b in nbrs)):
                    links.append({"a": a, "b": b, **_link_types(g, eids), "route": (a, b) in route_links,
                                  "spoke": pid in (a, b)})
            return {"center": pid, "nodes": nodes, "links": links, "routes": res.get("paths", []),
                    "distance": res.get("distance"), "filters": {**pf.describe(), "ties": tf.describe()}}

    def path(self, params: Dict[str, str]) -> Dict[str, Any]:
        with self.lock:
            src = params.get("from", "")
            dst = params.get("to", "")
            mode = params.get("mode", "any")
            if mode not in MODES:
                raise ServiceError(f"mode must be one of {MODES}")
            tf = TieFilter.from_params(params)
            g = self.graph(tf)
            names = self.names()
            sources = self.nb.root_ids if src == "lab" else [self._require_contact(src)[0]]
            target = self._require_contact(dst)[0]
            res = g.shortest_paths(sources, target, mode, limit=int(params.get("limit", "25") or 25))
            out = describe.describe_path_result(res, g, names, mode, self.origin_of_edge)
            out["from"] = src
            out["to"] = target
            out["rule"] = ("Any direction: each relationship record can be walked either way; steps keep "
                           "their recorded direction and meaning." if mode == "any" else
                           "Recorded direction: mentor, postdoc and committee records are walked only from "
                           "their 'from' person to their 'to' person (mentor to mentee, postdoc to supervisor, "
                           "committee member to student). Mutual types (coauthor, grant, collaborator, labmate, "
                           "met, event, NIH IRTA colleague) are walked both ways. Training records are left out "
                           "because the packet defines no direction for them.")
            out["filters"] = tf.describe()
            if res["status"] == "no-path":
                c = self.nb.records["contacts"][target]
                out["coverageNote"] = self._coverage_note(c)
            return out

    def people(self, params: Dict[str, str]) -> Dict[str, Any]:
        lib = self.library()
        with self.lock:
            tf = TieFilter.from_params(params)
            pf = self.people_filter(params)
            dist, preds = self.lab_dist(tf)
            names = self.names()
            rows = []
            for pid, c in self.nb.active("contacts"):
                f = self.person_flags(pid, dist, pf)
                card = self._card(pid, f)
                card["via"] = [names.get(p, p) for p in preds.get(pid, [])][:4]
                card["viaCount"] = len(preds.get(pid, []))
                card["papers"] = len(c.get("papers") or [])
                card["grants"] = len(c.get("grants") or [])
                lib_rec = (lib or {}).get("people", {}).get(pid) or {}
                summary = (lib_rec.get("openalex") or {}).get("summary") or {}
                card["works"] = summary.get("worksCount")
                card["awards"] = len((lib_rec.get("nih") or {}).get("awards") or []) + \
                    len((lib_rec.get("nsf") or {}).get("awards") or []) if lib_rec else None
                card["origin"] = self.nb.origin("contacts", pid)
                card["edited"] = bool(self.nb.edited_fields("contacts", pid))
                card["notes"] = 0
                rows.append(card)
            note_count: Dict[str, int] = {}
            for mid, m in self.nb.active("meetings"):
                k = self.nb.canonical_id(m.get("contactId", ""))
                note_count[k] = note_count.get(k, 0) + 1
            for r in rows:
                r["notes"] = note_count.get(r["id"], 0)
            rows.sort(key=lambda r: (r["degree"] if r["degree"] is not None else 99, norm_text(r["name"])))
            return {"rows": rows, "filters": {**pf.describe(), "ties": tf.describe()}}

    def affiliations(self, params: Dict[str, str]) -> Dict[str, Any]:
        """People within two degrees grouped by institution, department and lab (see affiliations.py).

        With `inst`, only that institution's entry and its people's IDs (for highlighting them on the map).
        """
        from . import affiliations as aff
        with self.lock:
            tf = TieFilter.from_params(params)
            pf = self.people_filter(params)
            if pf.max_degree is None or pf.max_degree > 2:
                pf = PeopleFilter(2, pf.doctorate, pf.identified)
            key = ("affiliations", tf, pf)
            res = self._cached(key, lambda: self._affiliations(tf, pf, aff))
            if params.get("inst"):
                inst = next((i for i in res["institutions"] if i["key"] == params["inst"]), None)
                if inst is None:
                    raise NotFound(f"No institution '{params['inst']}' among the people shown")
                ids = [p for d in inst["departments"] for p in d["people"] + [x for lab in d["labs"] for x in lab["people"]]]
                return {"institution": {k: v for k, v in inst.items() if k != "departments"}, "ids": ids + inst["unplaced"]}
            return res

    def _affiliations(self, tf: TieFilter, pf: PeopleFilter, aff) -> Dict[str, Any]:
        dist, preds = self.lab_dist(tf)
        roots = [r for r in self.nb.root_ids if r in self.nb.records["contacts"]]
        memo: Dict[str, frozenset] = {}

        def via(pid: str) -> frozenset:
            # The lab members at the start of this person's shortest paths to the lab.
            if pid in memo:
                return memo[pid]
            memo[pid] = frozenset()
            out = frozenset([pid]) if pid in roots else frozenset().union(*[via(p) for p in preds.get(pid, [])] or [frozenset()])
            memo[pid] = out
            return out
        people = {}
        for pid, c in self.nb.active("contacts"):
            f = self.person_flags(pid, dist, pf)
            if not f["shown"]:
                continue
            people[pid] = {"id": pid, "name": c["name"], "role": c.get("role", ""), "degree": f["degree"],
                           "isLab": f["isLab"], "photoUrl": c.get("photoUrl", ""),
                           "via": [r for r in roots if r in via(pid)]}
        edges = [e for _, e in self.nb.active("edges") if tf.allows(e)]
        res = aff.build(self.nb, people, edges, roots)
        res["filters"] = {**pf.describe(), "ties": tf.describe()}
        res["counts"] = {"people": len(people), "institutions": len(res["institutions"])}
        return res

    def notes(self, params: Dict[str, str]) -> Dict[str, Any]:
        with self.lock:
            out = [self._meeting_view(mid, m) for mid, m in self.nb.records["meetings"].items()
                   if params.get("archived") == "1" or mid not in self.nb.archived["meetings"]]
            out.sort(key=lambda m: (m["date"] or "", m["createdAt"]), reverse=True)
            return {"notes": out}

    def search(self, params: Dict[str, str]) -> Dict[str, Any]:
        with self.lock:
            q = (params.get("q") or "").strip()
            if not q:
                raise ServiceError("Enter a search term")
            tf = TieFilter.from_params(params)
            pf = self.people_filter(params)
            res = run_search(self.nb, q, self.info_fn(tf, pf), self.aliases, edge_ok=tf.allows, library=self.library(),
                             events=self.conferences()[1])
            if self.degree_limit() is not None:
                res = _drop_beyond(res, self.degree_limit())
            return res

    def conflicts(self, params: Dict[str, str]) -> Dict[str, Any]:
        with self.lock:
            names = self.names()
            items = []
            for cf in self.nb.conflicts.values():
                if params.get("status", "open") != "all" and cf["status"] != params.get("status", "open"):
                    continue
                item = dict(cf)
                if cf.get("collection") == "contacts":
                    item["recordName"] = names.get(cf.get("recordId"), cf.get("recordId"))
                rec = self.nb.records.get(cf.get("collection"), {}).get(cf.get("recordId"))
                item["currentNow"] = rec.get(cf.get("field")) if rec else None
                items.append(item)
            items.sort(key=lambda x: x.get("openedSeq", 0), reverse=True)
            return {"conflicts": items}

    def history(self, params: Dict[str, str]) -> Dict[str, Any]:
        with self.lock:
            limit = int(params.get("limit", "200") or 200)
            evs = self.nb.events[-limit:]
            names = self.names()
            out = []
            for ev in reversed(evs):
                kind = ev["op"].split(".")[0]
                rid = ev.get("id")
                label = names.get(rid, rid) if kind == "contact" else rid
                out.append({"seq": ev["seq"], "ts": ev["ts"], "actor": ev.get("actor", ""), "op": ev["op"],
                            "id": rid, "label": label, "batch": ev.get("batch") or ev.get("importedIn")})
            return {"events": out, "total": len(self.nb.events)}

    def validation(self) -> Dict[str, Any]:
        with self.lock:
            current = self.nb.to_notebook()
            schema_errors = validate_schema(current, self.packet.schema)
            rule_issues = validate_rules(current)
            return {"packet": self.report,
                    "current": {"schemaErrors": schema_errors[:100],
                                "ruleIssues": [i for i in rule_issues if i["level"] == "error"][:200],
                                "ok": not schema_errors and not any(i["level"] == "error" for i in rule_issues)}}

    # ------------------------------------------------------------------ write APIs

    def _precheck(self, events: List[Dict[str, Any]]) -> None:
        """Refuse to write events that could not be applied, so the log always replays."""
        created = set()
        for ev in events:
            kind, _, action = ev["op"].partition(".")
            if kind not in KIND_TO_COLL:
                continue
            coll = KIND_TO_COLL[kind]
            key = (coll, ev["id"])
            exists = ev["id"] in self.nb.records[coll] or key in created
            if action == "create":
                if exists:
                    raise ServiceError(f"{coll[:-1]} ID '{ev['id']}' is already in use")
                created.add(key)
            elif not exists:
                raise ServiceError(f"No {coll[:-1]} with ID '{ev['id']}'")

    def _append(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        self._precheck(events)
        written = self.ws.append(events)
        for ev in written:
            self.nb.apply(ev)
        return written

    def _validate_record(self, coll: str, rec: Dict[str, Any]) -> None:
        errors = [f"{e['path'].replace('$', coll[:-1])}: {e['message']}"
                  for e in validate_schema(rec, entity_schema(self.packet.schema, coll))]
        issues: List[Dict[str, str]] = []
        contact_ids = {rid for rid, _ in self.nb.active("contacts")}
        if coll == "contacts":
            check_contact(rec, issues)
        elif coll == "edges":
            check_edge(rec, issues, contact_ids)
        elif coll == "meetings":
            check_meeting(rec, issues, contact_ids)
        errors += [f"{i['field']}: {i['message']}" for i in issues if i["level"] == "error"]
        if errors:
            raise ServiceError("The record did not pass validation", errors)

    def _clean(self, value):
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return [self._clean(v) for v in value]
        return value

    def new_contact_id(self, name: str) -> str:
        base = slugify(name)
        rid, n = base, 2
        while self.nb.id_taken("contacts", rid):
            rid = f"{base}-{n}"
            n += 1
        return rid

    def create_contact(self, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        with self.lock:
            fields = {k: self._clean(v) for k, v in (payload.get("fields") or {}).items()}
            name = fields.get("name", "")
            if not name:
                raise ServiceError("A name is required")
            bad = [k for k in fields if k in CONTACT_READONLY and k not in ("sources",)]
            if bad:
                raise ServiceError("These fields cannot be set directly: " + ", ".join(bad))
            rid = self.new_contact_id(name)
            rec = {"id": rid, "name": name, "institution": "", "role": "", "lab": "", "email": "", "website": "",
                   "tags": [], "notes": "", "createdAt": utc_now()}
            for k, v in fields.items():
                if v in ("", None, []) and k not in rec:
                    continue
                rec[k] = v
            rec.setdefault("papers", [])
            rec.setdefault("grants", [])
            rec.setdefault("sources", [])
            self._validate_record("contacts", rec)
            dups = self.possible_duplicates(rec)
            if dups and not payload.get("confirmNotDuplicate"):
                raise EditConflict("This may be someone already in the notebook", {"possibleDuplicates": dups})
            self._append([{"op": "contact.create", "id": rid, "actor": actor, "data": {"record": rec}}])
            return {"id": rid}

    def possible_duplicates(self, rec: Dict[str, Any]) -> List[Dict[str, Any]]:
        key = name_key(rec.get("name", ""))
        email = (rec.get("email") or "").strip().lower()
        urls = {u.strip().rstrip("/").lower() for u in (rec.get("profileSourceUrl"), rec.get("website")) if u}
        out = []
        for rid, c in self.nb.active("contacts"):
            if rid == rec.get("id"):
                continue
            reasons = []
            if key and name_key(c["name"]) == key:
                reasons.append("same name")
            elif key and _initials_match(key, name_key(c["name"])):
                reasons.append("similar name")
            if email and (c.get("email") or "").strip().lower() == email:
                reasons.append("same email")
            curls = {u.strip().rstrip("/").lower() for u in (c.get("profileSourceUrl"), c.get("website")) if u}
            if urls & curls and "same name" in reasons:
                reasons.append("same profile URL")
            if reasons:
                out.append({"id": rid, "name": c["name"], "institution": c.get("institution", ""),
                            "role": c.get("role", ""), "reasons": reasons})
        return out[:10]

    def update_contact(self, pid: str, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        with self.lock:
            pid, cur = self._require_contact(pid)
            sets = {k: self._clean(v) for k, v in (payload.get("set") or {}).items()}
            bad = [k for k in sets if k in CONTACT_READONLY]
            if bad:
                raise ServiceError("These fields cannot be edited here: " + ", ".join(bad))
            return self._update("contacts", "contact", pid, cur, sets, payload.get("expected") or {}, actor)

    def _update(self, coll, kind, rid, cur, sets, expected, actor, batch=None):
        stale = {f: cur.get(f) for f, v in expected.items() if cur.get(f) != v}
        if stale:
            raise EditConflict("Someone changed this record after you opened it. Reload to see the current values.",
                               {"current": stale})
        props = entity_schema(self.packet.schema, coll)["properties"]
        required = set(entity_schema(self.packet.schema, coll).get("required", []))
        new = copy.deepcopy(cur)
        for f, v in sets.items():
            if f not in props:
                raise ServiceError(f"'{f}' is not a field of this record type")
            clearing = v is None or (v == "" and ("enum" in props[f] or f not in new))
            if clearing and f in required:
                raise ServiceError(f"'{f}' is required and cannot be removed")
            if clearing:
                new.pop(f, None)
            else:
                new[f] = v
        changed = {f: new.get(f) for f in sets if cur.get(f) != new.get(f)}
        if not changed:
            return {"id": rid, "changed": []}
        self._validate_record(coll, new)
        ev = {"op": f"{kind}.update", "id": rid, "actor": actor, "batch": batch,
              "data": {"set": changed, "prev": {f: copy.deepcopy(cur.get(f)) for f in changed},
                       "base": {f: self.nb.version(coll, rid, f) for f in changed}}}
        self._append([ev])
        return {"id": rid, "changed": sorted(changed)}

    def add_source(self, pid: str, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        with self.lock:
            pid, cur = self._require_contact(pid)
            url = (payload.get("url") or "").strip()
            note = (payload.get("note") or "").strip()
            if not url and not note:
                raise ServiceError("A source needs a URL or a note")
            if url and not is_http_url(url):
                raise ServiceError("The source URL must start with http:// or https://")
            self._append([{"op": "contact.append", "id": pid, "actor": actor,
                           "data": {"field": "sources", "items": [{"url": url, "note": note}]}}])
            return {"id": pid}

    def create_note(self, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        with self.lock:
            f = {k: self._clean(v) for k, v in (payload.get("fields") or {}).items()}
            pid, _ = self._require_contact(f.get("contactId", ""))
            date = f.get("date", "")
            mid = f"note-{pid}-{date or 'undated'}-{_short()}"
            if len(mid) > 100:
                mid = f"note-{_short()}{_short()}"
            rec = {"id": mid, "contactId": pid, "metBy": f.get("metBy", ""), "date": date,
                   "context": f.get("context", ""), "learning": f.get("learning", ""),
                   "followUp": f.get("followUp", ""), "recordedBy": f.get("recordedBy", "") or actor,
                   "createdAt": utc_now()}
            if not rec["metBy"]:
                raise ServiceError("Say who met this person (metBy)")
            if not rec["context"]:
                raise ServiceError("Describe how you met (context)")
            self._validate_record("meetings", rec)
            events = [{"op": "meeting.create", "id": mid, "actor": actor, "data": {"record": rec}}]
            tie = payload.get("tie")
            if tie:
                events.append(self._tie_event_from_note(rec, tie, actor))
            self._append(events)
            return {"id": mid, "tie": events[1]["id"] if len(events) > 1 else None}

    def _tie_event_from_note(self, note: Dict[str, Any], tie: Dict[str, Any], actor: str) -> Dict[str, Any]:
        src, _ = self._require_contact(tie.get("from", ""))
        if src == note["contactId"]:
            raise ServiceError("A relationship needs two different people")
        same = norm_text(note["recordedBy"]) == norm_text(note["metBy"])
        names = self.names()
        evidence = (f"Meeting note recorded by {note['recordedBy']}: {note['metBy']} met "
                    f"{names.get(note['contactId'])}" + (f" on {note['date']}" if note["date"] else "") +
                    f". Context: {note['context']}")[:3000]
        rec = {"id": self._edge_id("met", src, note["contactId"]), "from": src, "to": note["contactId"],
               "type": "met", "provenance": "firsthand" if same else "reported",
               "confidence": tie.get("confidence", "confirmed"), "status": "reviewed",
               "sourceTitle": f"Lab Network meeting note {note['id']}", "sourceUrl": "",
               "evidence": evidence, "checkedAt": today(), "createdAt": utc_now(),
               "relationshipStart": note["date"], "relationshipEnd": note["date"],
               "relationshipStatus": "historical", "relationshipLabel": (tie.get("label") or "Met")[:180]}
        self._validate_record("edges", rec)
        return {"op": "edge.create", "id": rec["id"], "actor": actor, "data": {"record": rec}}

    def _edge_id(self, typ: str, a: str, b: str) -> str:
        rid = f"{typ}-{a}-{b}-{_short()}"
        return rid if len(rid) <= 100 else f"{typ}-{_short()}{_short()}"

    def update_note(self, mid: str, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        with self.lock:
            cur = self.nb.records["meetings"].get(mid)
            if cur is None:
                raise NotFound(f"No note with ID '{mid}'")
            sets = {k: self._clean(v) for k, v in (payload.get("set") or {}).items()}
            meta = {k: sets.pop(k) for k in list(sets) if k in META_FIELDS}
            bad = [k for k in sets if k not in MEETING_EDITABLE]
            if bad:
                raise ServiceError("These note fields cannot be edited: " + ", ".join(bad))
            if "contactId" in sets:
                sets["contactId"] = self._require_contact(sets["contactId"])[0]
            res = {"id": mid, "changed": []}
            if sets:
                res = self._update("meetings", "meeting", mid, cur, sets, payload.get("expected") or {}, actor)
            if meta:
                if meta.get("followUpStatus") not in ("open", "done", ""):
                    raise ServiceError("followUpStatus must be open or done")
                self._append([{"op": "meeting.meta", "id": mid, "actor": actor, "data": {"set": meta}}])
                res["changed"] = list(res["changed"]) + list(meta)
            return res

    def create_tie(self, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        with self.lock:
            f = {k: self._clean(v) for k, v in (payload.get("fields") or {}).items()}
            a, _ = self._require_contact(f.get("from", ""))
            b, _ = self._require_contact(f.get("to", ""))
            if a == b:
                raise ServiceError("A relationship needs two different people")
            typ = f.get("type", "")
            if typ not in ALL_TYPES:
                raise ServiceError(f"Unknown relationship type '{typ}'")
            rec = {"id": self._edge_id(typ, a, b), "from": a, "to": b, "type": typ,
                   "provenance": f.get("provenance", "firsthand"), "confidence": f.get("confidence", "confirmed"),
                   "status": f.get("status", "reviewed"), "sourceTitle": f.get("sourceTitle", ""),
                   "sourceUrl": f.get("sourceUrl", ""), "evidence": f.get("evidence", ""),
                   "checkedAt": f.get("checkedAt") or today(), "createdAt": utc_now()}
            for opt in ("relationshipStart", "relationshipEnd", "relationshipStatus", "relationshipLabel",
                        "relationshipStatusNote", "attributionScope", "projectId", "projectTitle"):
                if f.get(opt):
                    rec[opt] = f[opt]
            if not rec["evidence"]:
                raise ServiceError("Describe the evidence for this relationship")
            self._validate_record("edges", rec)
            dup = self.duplicate_edge(rec)
            if dup and not payload.get("confirmNotDuplicate"):
                raise EditConflict("A matching relationship record already exists", {"existing": dup})
            self._append([{"op": "edge.create", "id": rec["id"], "actor": actor, "data": {"record": rec}}])
            return {"id": rec["id"]}

    def duplicate_edge(self, rec: Dict[str, Any]) -> Optional[str]:
        """An existing record with the same people, type, direction, source, project and evidence."""
        a, b = rec["from"], rec["to"]
        for eid, e in self.nb.active("edges"):
            if eid == rec.get("id") or e["type"] != rec["type"] or {e["from"], e["to"]} != {a, b}:
                continue
            if rec["type"] in ("mentor", "postdoc", "committee") and (e["from"], e["to"]) != (a, b):
                continue
            if ((e.get("sourceUrl") or "") == (rec.get("sourceUrl") or "")
                    and (e.get("projectId") or "") == (rec.get("projectId") or "")
                    and norm_text(e.get("evidence", "")) == norm_text(rec.get("evidence", ""))):
                return eid
        return None

    def update_tie(self, eid: str, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        with self.lock:
            cur = self.nb.records["edges"].get(eid)
            if cur is None:
                raise NotFound(f"No relationship with ID '{eid}'")
            sets = {k: self._clean(v) for k, v in (payload.get("set") or {}).items()}
            bad = [k for k in sets if k not in EDGE_EDITABLE]
            if bad:
                raise ServiceError("These relationship fields cannot be edited: " + ", ".join(bad) +
                                   ". Record a new relationship instead.")
            return self._update("edges", "edge", eid, cur, sets, payload.get("expected") or {}, actor)

    def archive(self, coll: str, rid: str, actor: str, restore: bool = False) -> Dict[str, Any]:
        with self.lock:
            if coll not in COLLECTIONS or rid not in self.nb.records[coll]:
                raise NotFound(f"No {coll[:-1]} with ID '{rid}'")
            if rid in self.nb.base_ids[coll]:
                raise ServiceError("Records from the original packet cannot be archived. Edit them, or set a "
                                   "relationship's review status to 'rejected' to stop counting it.")
            kind = {v: k for k, v in KIND_TO_COLL.items()}[coll]
            op = "restore" if restore else "archive"
            events = [{"op": f"{kind}.{op}", "id": rid, "actor": actor, "data": {}}]
            if coll == "contacts" and not restore:
                for eid, e in self.nb.active("edges"):
                    if rid in (e["from"], e["to"]):
                        events.append({"op": "edge.archive", "id": eid, "actor": actor,
                                       "data": {"cascadeFrom": rid}})
                for mid, m in self.nb.active("meetings"):
                    if m.get("contactId") == rid:
                        events.append({"op": "meeting.archive", "id": mid, "actor": actor,
                                       "data": {"cascadeFrom": rid}})
            self._append(events)
            return {"id": rid, "archived": not restore, "cascaded": len(events) - 1}

    # ------------------------------------------------------------------ research library (see library.py)

    def library(self) -> Optional[Dict[str, Any]]:
        """workspace/library.json, reloaded whenever the weekly update rewrites it.

        A record marked as not someone's leaves them here at once, as the next library run will take it out of the file
        (library.drop_not_theirs). Their summary and paper list are dropped too, until that run reads them again.
        """
        path = os.path.join(self.ws.path, "library.json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        marks = tuple(sorted((pid, tuple(sorted(m))) for pid, m in self.nb.not_theirs.items()))
        if self._library is None or self._library[0] != (mtime, marks):
            from .library import drop_not_theirs, settle
            from .refresh import openalex_links
            with open(path, "r", encoding="utf-8") as f:
                lib = json.load(f)
            links = lambda pid: openalex_links(self.nb, pid) if pid in self.nb.records["contacts"] else []
            if marks and isinstance(lib.get("people"), dict) and drop_not_theirs(lib, dict(marks), links):
                lib.setdefault("works", {})
                lib.setdefault("awards", {})
                settle(lib)
            # Search-ready titles: plain-ASCII titles fold by lowercasing and keep their character positions.
            for item in list(lib.get("works", {}).values()) + list(lib.get("awards", {}).values()):
                t = item.get("title") or ""
                item["_norm"] = t.lower() if t.isascii() else None
            self._library = ((mtime, marks), lib)
        return self._library[1]

    def _library_view(self, pid: str) -> Optional[Dict[str, Any]]:
        lib = self.library()
        rec = (lib or {}).get("people", {}).get(pid)
        if not rec:
            return None
        from .connectors.openalex import author_url
        from .library import author_ids, award_title
        o = rec.get("openalex") or {}
        ids = author_ids(o)                      # a researcher OpenAlex splits across records has several
        clean = lambda d: {k: v for k, v in d.items() if not k.startswith("_")
                           and k not in ("people", "roles", "parts", "fy", "whole")}
        works = [{"id": w, **clean(lib["works"][w])} for w in o.get("works") or [] if w in lib["works"]]
        keys = list((rec.get("nih") or {}).get("awards") or []) + list((rec.get("nsf") or {}).get("awards") or [])
        awards = []
        for k in keys:
            if k in lib["awards"]:
                title, part = award_title(lib["awards"][k], pid)
                awards.append({"key": k, **clean(lib["awards"][k]), "title": title, "part": part,
                               "role": lib["awards"][k].get("roles", {}).get(pid, "")})
        awards.sort(key=lambda a: a.get("end") or a.get("start") or "", reverse=True)
        urls = [o.get("url") or author_url(ids[0])] + [author_url(i) for i in ids[1:]] if ids else []
        held = [{**h, "url": author_url(h["id"])} for h in o.get("orcidHeld") or [] if h.get("id")]
        return {"openalex": {"status": o.get("status", "pending"), "id": ids[0] if ids else "", "ids": ids,
                             "url": urls[0] if urls else o.get("url", ""), "urls": urls,
                             "basis": o.get("basis", ""),
                             "summary": o.get("summary"), "summaryAt": o.get("summaryAt", ""),
                             "worksAt": o.get("worksAt", ""), "held": held,
                             "orcidCheckedAt": o.get("orcidCheckedAt", "")},
                "works": works, "awards": awards,
                "nih": {k: (rec.get("nih") or {}).get(k) for k in ("status", "basis", "checkedAt")},
                "nsf": {k: (rec.get("nsf") or {}).get(k) for k in ("status", "checkedAt", "reason")},
                "standard": lib.get("standard"), "updatedAt": lib.get("updatedAt", "")}

    def latest(self, pid: str) -> Dict[str, Any]:
        """New papers and NIH awards for one person since their last library update, checked live (cached 6 hours).

        Runs outside the service lock: it waits on the network.
        """
        from . import library as libmod
        pid, _ = self._require_contact(pid)
        settings = self.ws.config.get("settings") or {}
        shared = settings.get("sharedCopy")
        if settings.get("demo") or shared or settings.get("liveCheck") is False:
            note = ("The demo lab's people are fictional, so profiles are not checked against OpenAlex and NIH."
                    if settings.get("demo") else
                    f"This shared copy shows the research library as of {shared.get('madeAt', 'its last update')}; it "
                    "does not check OpenAlex and NIH itself." if shared else
                    "The live check is off for this workspace (turn it on with: config --live-check on).")
            return {"checkedAt": utc_now(), "works": [], "awards": [], "skipped": True, "notes": [note]}
        lib = self.library()
        if not lib or pid not in lib.get("people", {}):
            return {"checkedAt": utc_now(), "works": [], "awards": [],
                    "notes": ["This person is not in the research library yet, so nothing was checked."]}
        hit = self._latest.get(pid)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
        res = libmod.latest(lib, pid, self._live(), not_theirs=self.nb.openalex_not_theirs(pid))
        self._latest[pid] = (time.time(), res)
        return res

    def _live(self):
        """The client for live lookups: no cache, at most $0.05 of the OpenAlex allowance while the app runs."""
        from .connectors.http import Client
        if self._live_client is None:
            self._live_client = Client(cache_dir=None, budget_usd=0.05,
                                       mailto=(self.ws.config.get("settings") or {}).get("contactEmail", ""))
        return self._live_client

    def _live_refusal(self) -> Optional[str]:
        settings = self.ws.config.get("settings") or {}
        if settings.get("demo"):
            return "The demo lab is fictional, so it does not look people up in OpenAlex."
        if settings.get("sharedCopy"):
            return "This shared copy does not look people up in OpenAlex; the lab's own copy of Lab Network does."
        if settings.get("liveCheck") is False:
            return "Live lookups are off for this workspace (turn them on with: config --live-check on)."
        return None

    def outside_find(self, params: Dict[str, str]) -> Dict[str, Any]:
        """OpenAlex author records for a name (`name`, optional `institution`), each marked with the person it
        belongs to when it is already in the network. Runs outside the service lock: it waits on the network."""
        from . import outside
        name = (params.get("name") or "").strip()
        if len(name) < 3:
            raise ServiceError("Enter a name to look up")
        refusal = self._live_refusal()
        if refusal:
            return {"name": name, "candidates": [], "skipped": True, "note": refusal}
        key = ("find", norm_text(name), norm_text(params.get("institution") or ""))
        hit = self._latest.get(key)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
        try:
            cands = outside.find(self._live(), name, params.get("institution") or "")
        except Exception as err:          # the allowance, the network or OpenAlex itself
            raise ServiceError(f"OpenAlex could not be searched just now: {err}")
        index = outside.library_index(self.library() or {})
        with self.lock:
            for c in cands:
                pid = index.get(c["openalexId"])
                c["person"] = pid if pid and self.nb.is_active("contacts", pid) else None
        res = {"name": name, "candidates": cands}
        self._latest[key] = (time.time(), res)
        return res

    def outside_connect(self, aid: str, params: Dict[str, str]) -> Dict[str, Any]:
        """Who in the network has published with an OpenAlex author (papers with 30 or fewer authors), with each
        person's degree. The author's degree through them is one more. Nothing is saved."""
        from . import outside
        from .connectors.openalex import short_id
        aid = short_id(aid).upper()
        if not re.fullmatch(r"A\d+", aid):
            raise ServiceError("Give an OpenAlex author record, such as A5023888391")
        refusal = self._live_refusal()
        if refusal:
            return {"author": {"openalexId": aid}, "bridges": [], "skipped": True, "note": refusal}
        hit = self._latest.get(("connect", aid))
        if hit and time.time() - hit[0] < 6 * 3600:
            res = hit[1]
        else:
            try:
                res = outside.connect(self._live(), self.library() or {}, aid)
            except Exception as err:
                raise ServiceError(f"OpenAlex could not be read just now: {err}")
            self._latest[("connect", aid)] = (time.time(), res)
        with self.lock:
            tf = TieFilter.from_params(params)
            pf = self.people_filter(params)
            dist, _ = self.lab_dist(tf)
            bridges = []
            for b in res["bridges"]:
                pid = self.nb.canonical_id(b["personId"])
                if not self.nb.is_active("contacts", pid):
                    continue
                bridges.append({**b, **self._card(pid, self.person_flags(pid, dist, pf))})
            bridges.sort(key=lambda b: (b["degree"] if b["degree"] is not None else 99, -b["shared"]))
            ds = [b["degree"] for b in bridges if b["degree"] is not None]
            person = res.get("person")
            if person:
                person = self.nb.canonical_id(person)
                person = person if self.nb.is_active("contacts", person) else None
            return {**res, "person": person, "bridges": bridges, "degree": (min(ds) + 1) if ds else None,
                    "rule": f"Papers with {outside.MAX_AUTHORS} or fewer authors, the newest {outside.MAX_WORKS}. "
                            "Larger team papers are left out, as when the network was collected."}

    def _not_theirs_view(self, pid: str) -> List[Dict[str, Any]]:
        from .connectors.openalex import author_url
        marks = self.nb.openalex_not_theirs(pid)
        return [{**m, "url": author_url(aid)} for aid, m in sorted(marks.items(), key=lambda kv: kv[1].get("ts", ""))]

    def mark_not_theirs(self, pid: str, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """Mark an OpenAlex author record as not this person's, or withdraw the mark (`undo`).

        The mark is an identity note in the change log, so it outlasts the research library. The library, refresh and
        expand never link a marked record to the person, whatever links it (a record's sources cannot be removed); one
        they hold now leaves them in the app at once and in library.json at the next library run, which reads their
        papers again. `openalexId` is the record's ID or URL; `name` (the name on the record) and `note` are optional.
        """
        from .connectors.openalex import find_author_ids
        raw = str(payload.get("openalexId") or "").strip()
        found = find_author_ids(raw) or ([raw.upper()] if re.fullmatch(r"[Aa]\d+", raw) else [])
        if len(set(found)) != 1:
            raise ServiceError("Give one OpenAlex author record: its ID, such as A5023888391, or its URL")
        aid = found[0]
        undo = bool(payload.get("undo"))
        note = str(payload.get("note") or "").strip()[:1000]
        with self.lock:
            pid, _ = self._require_contact(pid)
            cur = self.nb.openalex_not_theirs(pid).get(aid)
            if (not cur) if undo else (cur and (not note or cur.get("note") == note)):     # nothing to change
                return {"id": pid, "openalexId": aid, "notTheirs": bool(cur), "changed": False, "held": False}
            from .library import author_ids
            o = (((self.library() or {}).get("people") or {}).get(pid) or {}).get("openalex") or {}
            held = o.get("status") == "linked" and aid in author_ids(o)       # the library holds it for them now
            name = str(payload.get("name") or "").strip()[:200] or (cur or {}).get("name") or self._record_name(pid, aid)
            self._append([{"op": "workspace.note", "id": f"identity-{pid}-{_short()}", "actor": actor,
                           "data": {"kind": "identity", "person": pid, "openalexId": aid, "notTheirs": not undo,
                                    "name": name, "note": note, "date": today()}}])
        self._latest.pop(pid, None)
        return {"id": pid, "openalexId": aid, "notTheirs": not undo, "changed": True, "held": held}

    def _record_name(self, pid: str, aid: str) -> str:
        """The name on an OpenAlex record, when the library has it: a record held back from the person, or theirs."""
        lib = self.library() or {}
        o = ((lib.get("people") or {}).get(pid) or {}).get("openalex") or {}
        held = {h.get("id"): h.get("name", "") for h in o.get("orcidHeld") or []}
        if aid in held:
            return held[aid]
        from .library import author_ids
        return (o.get("summary") or {}).get("name", "") if author_ids(o) == [aid] else ""

    def _collection_counts(self, flags: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """How many first-degree people had their connections collected, by status (None before any run)."""
        if not self.nb.collection_runs:
            return None
        first = [pid for pid, f in flags.items() if f["degree"] == 1 and "archived" not in f["hiddenBy"]]
        by = Counter((self.nb.collection.get(pid) or {}).get("status", "none") for pid in first)
        return {"firstDegree": len(first), "byStatus": dict(by), "lastRun": self.nb.collection_runs[-1]}

    def _collection_view(self, pid: str) -> Optional[Dict[str, Any]]:
        info = self.nb.collection.get(pid)
        if not info:
            return None
        from .expand import STATUS_TEXT
        out = {k: info.get(k) for k in ("status", "degree", "date", "method", "standard", "openalexIds", "basis",
                                         "added", "linked", "works", "coauthors", "reason", "batchId")}
        out["text"] = STATUS_TEXT.get(info.get("status"), info.get("status", ""))
        return out

    # ---------------------------------------------------------------- Slack intake

    def intake(self) -> Dict[str, Any]:
        from . import intake
        return intake.view(self)

    def intake_check(self, client=None) -> Dict[str, Any]:
        """Read the Slack channel now (outside the service lock: it waits on the network)."""
        from . import intake
        try:
            counts = intake.check(self, client=client)
        except intake.IntakeError as exc:
            raise ServiceError(str(exc))
        return {**intake.view(self), "checked": counts}

    def intake_decide(self, ts: str, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        from . import intake
        try:
            intake.decide_item(self, ts, payload.get("action", ""), payload.get("person", ""), actor or "someone",
                               confirm_new=bool(payload.get("confirmNew")))
        except intake.IntakeError as exc:
            raise ServiceError(str(exc))
        return intake.view(self)

    def intake_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        from . import intake
        try:
            if "mode" in payload:
                intake.set_mode(self.ws, payload["mode"])
            if "members" in payload:
                intake.set_members(self, payload["members"] or {})
            if payload.get("forgetForm"):
                intake.forget_form(self.ws)
        except intake.IntakeError as exc:
            raise ServiceError(str(exc))
        return intake.view(self)

    def add_workspace_note(self, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """Record which people's connections a collection run covered (see expand.py)."""
        note = payload.get("note", payload) if isinstance(payload, dict) else None
        if not isinstance(note, dict) or note.get("kind") != "collection" or not isinstance(note.get("people"), dict):
            raise ServiceError("A workspace note needs kind 'collection' and a people object")
        note = copy.deepcopy(note)
        # People the import held back (possible duplicates) are not in the notebook; leave them out.
        unknown = [pid for pid in note["people"] if pid not in self.nb.records["contacts"]]
        for pid in unknown:
            del note["people"][pid]
        with self.lock:
            written = self._append([{"op": "workspace.note", "id": f"collection-{note.get('date', '')}-{_short()}",
                                     "actor": actor, "data": note}])
            return {"seq": written[0]["seq"], "people": len(note["people"]), "leftOut": len(unknown)}

    def undo_import(self, batch_id: str, actor: str) -> Dict[str, Any]:
        """Archive every record an import batch created, and mark its collection note as undone.

        Values the batch filled in or appended on existing records stay; the history shows each one.
        """
        with self.lock:
            if batch_id not in self.nb.batches:
                raise NotFound(f"No import batch '{batch_id}'")
            created = {"contacts": [], "edges": [], "meetings": []}
            appended = 0
            for ev in self.nb.events:
                if ev.get("batch") != batch_id:
                    continue
                kind, _, action = ev["op"].partition(".")
                if kind in KIND_TO_COLL and action == "create":
                    created[KIND_TO_COLL[kind]].append(ev["id"])
                elif action in ("update", "append"):
                    appended += 1
            events = []
            gone = set()
            for coll in ("contacts", "edges", "meetings"):
                kind = {v: k for k, v in KIND_TO_COLL.items()}[coll]
                for rid in created[coll]:
                    if rid in self.nb.archived[coll] or rid in gone:
                        continue
                    events.append({"op": f"{kind}.archive", "id": rid, "actor": actor, "data": {"undoBatch": batch_id}})
                    gone.add(rid)
                    if coll == "contacts":
                        for eid, e in self.nb.active("edges"):
                            if rid in (e["from"], e["to"]) and eid not in gone:
                                events.append({"op": "edge.archive", "id": eid, "actor": actor,
                                               "data": {"cascadeFrom": rid, "undoBatch": batch_id}})
                                gone.add(eid)
            people = {pid: {"status": "undone", "reason": f"import batch {batch_id} was undone"}
                      for pid, info in self.nb.collection.items() if info.get("batchId") == batch_id}
            if people:
                events.append({"op": "workspace.note", "id": f"collection-undo-{_short()}", "actor": actor,
                               "data": {"kind": "collection", "method": "undo", "date": today(), "batchId": batch_id,
                                        "people": people}})
            if events:
                self._append(events)
            return {"batchId": batch_id, "archived": sum(1 for e in events if e["op"].endswith(".archive")),
                    "keptUpdates": appended, "collectionReset": len(people)}

    def resolve_conflict(self, cid: str, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        with self.lock:
            cf = self.nb.conflicts.get(cid)
            if cf is None:
                raise NotFound(f"No conflict with ID '{cid}'")
            if cf["status"] != "open":
                raise ServiceError("This conflict is already resolved")
            resolution = payload.get("resolution")
            if resolution not in ("keep-current", "use-incoming"):
                raise ServiceError("resolution must be keep-current or use-incoming")
            events = []
            if resolution == "use-incoming":
                coll = cf["collection"]
                kind = {v: k for k, v in KIND_TO_COLL.items()}[coll]
                cur = self.nb.records[coll].get(cf["recordId"])
                if cur is None:
                    raise NotFound("The record for this conflict no longer exists")
                new = copy.deepcopy(cur)
                new[cf["field"]] = copy.deepcopy(cf["incoming"])
                self._validate_record(coll, new)
                events.append({"op": f"{kind}.update", "id": cf["recordId"], "actor": actor,
                               "batch": cf.get("batchId"),
                               "data": {"set": {cf["field"]: copy.deepcopy(cf["incoming"])},
                                        "prev": {cf["field"]: copy.deepcopy(cur.get(cf["field"]))},
                                        "base": {cf["field"]: self.nb.version(coll, cf["recordId"], cf["field"])},
                                        "conflictId": cid}})
            events.append({"op": "conflict.resolve", "id": cid, "actor": actor,
                           "data": {"conflictId": cid, "resolution": resolution, "note": payload.get("note", "")}})
            self._append(events)
            return {"id": cid, "status": resolution}


def _link_types(g: Graph, eids: List[str]) -> Dict[str, Any]:
    """Pair-level summary of the records between two people, for drawing."""
    types, prov, directional = [], [], []
    for eid in eids:
        e = g.edges[eid]
        if e["type"] not in types:
            types.append(e["type"])
        if e["provenance"] not in prov:
            prov.append(e["provenance"])
        if e["type"] in ("mentor", "postdoc", "committee"):
            directional.append({"type": e["type"], "from": e["from"], "to": e["to"]})
    types.sort(key=TYPE_ORDER.index)
    return {"types": types, "count": len(eids), "prov": sorted(prov), "directional": directional}


def _initials_match(a: str, b: str) -> bool:
    """'chris lee' vs 'christopher lee': same surname, one first name a prefix of the other."""
    pa, pb = a.split(), b.split()
    if len(pa) < 2 or len(pb) < 2 or pa[-1] != pb[-1]:
        return False
    fa, fb = pa[0], pb[0]
    return fa != fb and (fa.startswith(fb) or fb.startswith(fa)) and min(len(fa), len(fb)) >= 3
