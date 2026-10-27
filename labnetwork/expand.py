"""Fill in the first and second degree from public coauthorship records.

`expand` reads OpenAlex (and ORCID, for new people's degrees and positions) and sends what it finds through the
importer as one batch, like any other import. Nothing in the packet changes, and `undo-import` reverses the batch.

1. Lab members are looked up by name and accepted only when their OpenAlex works include papers already recorded
   for them (two or more; one when only one is recorded).
2. First degree: every coauthor of a lab member on papers with 30 or fewer authors is connected to that member.
   Coauthors already in the notebook gain a coauthor record if they lack one; the rest are added.
3. A first-degree person already in the notebook is identified on the lab members' OpenAlex papers: the author
   whose name matches theirs, preferring a paper already recorded between them. People without a shared paper who
   have a confirmed doctorate or a research relationship are looked up by name and accepted only when exactly one
   candidate's institutions match theirs. Everyone else is not looked up.
4. Second degree: for each identified first-degree person, coauthors are counted over works since the start year
   (papers with 30 or fewer authors). Coauthors already in the notebook gain a coauthor record if they lack one.
   Others with at least `min_shared` shared works are added until `per_person` of the person's coauthors are
   expansion additions (from this run or an earlier one), so a second run adds only the missing records.

A coauthor whose name matches someone already in the notebook, with no shared institution or relationship to
confirm it is the same person, is held back and listed instead of added. Everyone checked is recorded in a
collection note (a workspace.note event), so the app can say whose connections were collected.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .connectors import openalex as oa
from .connectors import orcid as oc
from .connectors.http import BudgetReached, Client, HttpError
from .graph import TieFilter
from .importer import Importer
from .model import norm_text
from .refresh import _held, _running_port, openalex_links
from .search import _paper_ids, _strip_year
from .service import Service, slugify
from .store import Workspace, today, utc_now

MAX_AUTHORS = 30
WORK_SELECT = "id,doi,ids,title,publication_year,authorships"
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md"}
RESEARCH_TYPES = {"coauthor", "grant", "collaborator"}

STATUS_TEXT = {
    "collected": "coauthors collected from OpenAlex",
    "not-found": "not found in OpenAlex",
    "ambiguous": "several OpenAlex authors match; not collected until someone confirms which is theirs",
    "not-looked-up": "not looked up: no confirmed doctorate or research relationship recorded",
    "stopped": "not reached: the run stopped at its spending limit",
    "undone": "an earlier collection was undone",
}


# --------------------------------------------------------------------------- names

def name_parts(name: str) -> Tuple[List[str], str]:
    """Given-name tokens and family name, folded. A hyphenated family name stays one token."""
    words = [w for w in (name or "").replace("‐", "-").split() if not w.startswith("(")]
    toks = [t for t in (norm_text(w).replace(" ", "-") for w in words) if t]
    while len(toks) > 1 and toks[-1] in SUFFIXES:
        toks.pop()
    return (toks[:-1], toks[-1]) if toks else ([], "")


def family_key(name: str) -> str:
    return name_parts(name)[1].split("-")[-1]


def surname_tokens(name: str) -> Set[str]:
    """Every token after the first full given name, with hyphenated parts: 'Rafael Ortega Medina' ->
    {ortega, medina}; 'Ilse Brandt-Kessler' -> {brandt-kessler, brandt, kessler}."""
    g, f = name_parts(name)
    toks = g + [f]
    first = next((i for i, t in enumerate(toks) if len(t) > 1), None)
    out: Set[str] = set()
    for t in toks[(first + 1) if first is not None else 1:]:
        if len(t) > 1:
            out.add(t)
            out.update(x for x in t.split("-") if len(x) > 1)
    return out


def _family_ok(a: str, b: str) -> bool:
    return a == b or a.split("-")[-1] == b or b.split("-")[-1] == a or a.replace("-", "") == b.replace("-", "")


def name_score(a: str, b: str) -> int:
    """2: same family name and first given name; 1: compatible (an initial, or a short form such as Matt and
    Matthew); 0: no match. Leading initials are skipped ('T. R. Calvin Okoro' and 'Calvin Okoro')."""
    ga, fa = name_parts(a)
    gb, fb = name_parts(b)
    if not fa or not fb or not ga or not gb:
        return 0
    if not _family_ok(fa, fb):
        # Two-part surnames: 'Rafael Ortega' and 'Rafael Ortega Medina', 'Ilse Brandt' and 'Ilse Brandt-Kessler'.
        fa1 = next((t for t in ga if len(t) > 1), "")
        fb1 = next((t for t in gb if len(t) > 1), "")
        if fa1 and fa1 == fb1 and (fa.split("-")[0] in surname_tokens(b) or fb.split("-")[0] in surname_tokens(a)):
            return 2
        return 0
    if "".join(ga).replace("-", "") == "".join(gb).replace("-", ""):
        return 2                     # 'Ji-Woo' and 'Ji Woo'
    fa1 = next((t for t in ga if len(t) > 1), "")
    fb1 = next((t for t in gb if len(t) > 1), "")
    if fa1 and fb1:
        x, y = fa1.replace("-", ""), fb1.replace("-", "")
        if x == y:
            return 2
        short, long_ = sorted((x, y), key=len)
        return 1 if len(short) >= 3 and long_.startswith(short) else 0
    # At least one side has initials only: compare first letters.
    return 1 if ga[0][0] == gb[0][0] else 0


def _inst_match(a: str, b: str) -> bool:
    x, y = norm_text(a), norm_text(b)
    return bool(x) and bool(y) and (x in y or y in x)


# --------------------------------------------------------------------------- OpenAlex reads

def _work_ids(w: Dict[str, Any]) -> List[str]:
    ids = w.get("ids") or {}
    url = " ".join(str(ids.get(k) or "") for k in ("pmid", "pmcid"))
    return _paper_ids(oa.work_title(w), (w.get("doi") or "") + " " + url)


def _works(client: Client, aids: List[str], since: str = "", max_authors: Optional[int] = MAX_AUTHORS,
           per_page: int = 200, select: str = WORK_SELECT, use_cache: bool = True) -> List[Dict[str, Any]]:
    flt = "authorships.author.id:" + "|".join(oa.short_id(a) for a in aids)
    if max_authors:
        flt += f",authors_count:<{max_authors + 1}"
    if since:
        flt += f",from_publication_date:{since}"
    data = client.get(f"{oa.API}/works", oa._params(client, {"filter": flt, "sort": "publication_date:desc",
                                                             "per-page": per_page, "select": select}),
                      use_cache=use_cache)
    return data.get("results", [])


def _authorships(w: Dict[str, Any]):
    for a in w.get("authorships") or []:
        au = a.get("author") or {}
        if au.get("id"):
            yield (oa.short_id(au["id"]), au.get("display_name") or a.get("raw_author_name") or "",
                   [i.get("display_name", "") for i in a.get("institutions") or [] if i.get("display_name")])


def _coauthor_counts(works: List[Dict[str, Any]], mine: Set[str], max_authors: int = MAX_AUTHORS):
    """Shared-work counts, latest shared work, names and institutions for each coauthor (newest works first)."""
    counts: Counter = Counter()
    latest: Dict[str, Dict[str, Any]] = {}
    names: Dict[str, str] = {}
    insts: Dict[str, Set[str]] = defaultdict(set)
    for w in works:
        if len(w.get("authorships") or []) > max_authors:
            continue
        seen = set()
        for aid, nm, ins in _authorships(w):
            if aid in mine or aid in seen:
                continue
            seen.add(aid)
            counts[aid] += 1
            latest.setdefault(aid, w)
            names[aid] = nm
            insts[aid].update(ins)
    ranked = sorted(counts, key=lambda a: (-counts[a], -(latest[a].get("publication_year") or 0)))
    return ranked, counts, latest, names, insts


# --------------------------------------------------------------------------- plan

class _Plan:
    """Collects new people, new coauthor records and held-back names while the steps run."""

    def __init__(self, svc: Service, since_year: int):
        self.nb = svc.nb
        self.contacts = self.nb.records["contacts"]
        self.active = {pid for pid, _ in self.nb.active("contacts")}
        self.g = svc.graph(TieFilter())
        self.since_year = since_year
        self.by_family: Dict[str, List[str]] = defaultdict(list)
        for pid in self.active:
            for k in {family_key(self.contacts[pid]["name"])} | surname_tokens(self.contacts[pid]["name"]):
                self.by_family[k].append(pid)
        self.oa_of: Dict[str, List[str]] = {}
        self.by_oa: Dict[str, str] = {}
        for pid in self.active:
            ids = openalex_links(self.nb, pid)           # every record linked on their record, unless marked not theirs
            if ids:
                self.oa_of[pid] = ids
                for a in ids:
                    self.by_oa[a] = pid
        self.pairs = {tuple(sorted((e["from"], e["to"]))) for _, e in self.nb.active("edges") if e["type"] == "coauthor"}
        self.new_people: Dict[str, Dict[str, Any]] = {}     # OpenAlex id -> plan
        self.edges: List[Dict[str, Any]] = []
        self.edge_ids: Set[str] = set()
        self.held: List[Dict[str, Any]] = []
        self.held_ids: Set[str] = set()

    def added_by_expansion(self, pid: str) -> bool:
        """Whether a person was added to the second degree by an earlier expansion (counts toward the cap)."""
        c = self.contacts.get(pid)
        if not c:
            return False
        return "expand-second-degree" in (c.get("tags") or []) or any(
            "as a frequent coauthor of" in (s.get("note") or "") for s in c.get("sources") or [])

    def name(self, pid: str) -> str:
        if pid in self.contacts:
            return self.contacts[pid]["name"]
        return next((p["name"] for p in self.new_people.values() if p["id"] == pid), pid)

    def known_papers(self, pid: str) -> List[Set[str]]:
        """Papers already recorded for a person (their profile and their coauthor records), each as its set of
        identifiers. Papers that share an identifier count once."""
        papers: List[Set[str]] = []

        def add(keys: List[str]) -> None:
            if not keys:
                return
            for p in papers:
                if p & set(keys):
                    p.update(keys)
                    return
            papers.append(set(keys))
        for p in self.contacts[pid].get("papers") or []:
            add(_paper_ids(p.get("title", ""), (p.get("url") or "") + " " + (p.get("sourceUrl") or ""),
                           p.get("doi", "")))
        for other in self.g.nbrs.get(pid, ()):
            for e in self.g.records_between(pid, other):
                if e["type"] == "coauthor":
                    title, _ = _strip_year(e.get("sourceTitle", ""))
                    add(_paper_ids(title, e.get("sourceUrl", "")))
        return papers

    def known_ids(self, pid: str) -> Set[str]:
        return set().union(*self.known_papers(pid)) if self.known_papers(pid) else set()

    def not_theirs(self, pid: str) -> Dict[str, Any]:
        """The OpenAlex records marked as not this person's (none for someone this run adds)."""
        return self.nb.openalex_not_theirs(pid) if pid in self.contacts else {}

    def existing_match(self, nm: str, insts: Set[str], pid: str,
                       aid: str = "") -> Tuple[Optional[str], Optional[str]]:
        """(contact, None) when exactly one same-name contact is confirmed by a tie to `pid` or a shared
        institution; (None, contact) when a same-name contact exists without confirmation; (None, None) otherwise.
        A contact who marked the coauthor's record `aid` as not theirs is never confirmed as that coauthor."""
        keys = {family_key(nm)} | surname_tokens(nm)
        pool = {x for k in keys for x in self.by_family.get(k, [])}
        scores = {x: name_score(self.contacts[x]["name"], nm) for x in pool}
        cands = [x for x, sc in scores.items() if sc]
        if not cands:
            return None, None
        # A tie to `pid` confirms a match by initial; a shared institution alone needs the full first name.
        ok = [x for x in cands if (x in self.g.nbrs.get(pid, ()) or
              (scores[x] == 2 and any(_inst_match(self.contacts[x].get("institution", ""), i) for i in insts)))
              and aid not in self.not_theirs(x)]
        if len(ok) == 1 and not self.oa_of.get(ok[0]):
            return ok[0], None
        return None, (ok or cands)[0]

    def resolve(self, aid: str, nm: str, insts: Set[str], via: str, shared: int,
                min_shared: int) -> Tuple[Optional[str], bool]:
        """The notebook person an OpenAlex coauthor is, or None. The flag says the coauthor was held back."""
        target = self.by_oa.get(aid)
        if target is not None:
            return target, False
        target, held = self.existing_match(nm, insts, via, aid)
        if target:
            self.by_oa[aid] = target
            return target, False
        if held:
            if shared >= min_shared and aid not in self.held_ids:
                self.held_ids.add(aid)
                self.held.append({"name": nm, "openalexId": aid, "via": self.name(via), "sameNameAs": held,
                                  "shared": shared})
            return None, True
        return None, False

    def add_person(self, aid: str, nm: str, via: str, shared: int, insts: Set[str], degree: int) -> str:
        taken = {p["id"] for p in self.new_people.values()}
        base = slugify(nm)
        rid, n = base, 2
        while self.nb.id_taken("contacts", rid) or rid in taken:
            rid = f"{base}-{n}"
            n += 1
        self.new_people[aid] = {"id": rid, "name": nm, "via": via, "shared": shared, "insts": sorted(insts),
                                "degree": degree}
        self.by_oa[aid] = rid
        return rid

    def add_edge(self, a: str, b: str, n: int, w: Dict[str, Any], since: Optional[int]) -> bool:
        pair = tuple(sorted((a, b)))
        if pair in self.pairs or a == b:
            return False
        self.pairs.add(pair)
        title = oa.work_title(w)[:400]
        year = w.get("publication_year") or ""
        base = f"coauthor-{a}-{b}-oa"[:96]
        eid, k = base, 2
        while eid in self.edge_ids or eid in self.nb.records["edges"]:
            eid = f"{base}-{k}"
            k += 1
        self.edge_ids.add(eid)
        span = f" since {since}" if since else ""
        self.edges.append({
            "id": eid, "from": a, "to": b, "type": "coauthor", "provenance": "public", "confidence": "likely",
            "status": "reviewed", "sourceTitle": f"{title} ({year})" if year else title,
            "sourceUrl": oa.work_link(w), "checkedAt": today(), "createdAt": utc_now(),
            "evidence": (f"Named together on {n} {'work' if n == 1 else 'works'}{span} indexed by OpenAlex (papers with "
                         f"{MAX_AUTHORS} or fewer authors), most recently “{title}” ({year}). Coauthorship alone does "
                         "not show mentorship or a current collaboration."),
            "relationshipStatus": "unknown", "relationshipLabel": "Coauthors"})
        return True


def plan_expand(svc: Service, client: Client, since_year: int = 2016, per_person: int = 5, min_shared: int = 2,
                only: Optional[List[str]] = None, log: Callable[[str], None] = print) -> Dict[str, Any]:
    P = _Plan(svc, since_year)
    nb, contacts = P.nb, P.contacts
    dist, _ = svc.lab_dist(TieFilter())
    lab = [r for r in nb.root_ids if r in P.active]
    first = sorted(p for p, d in dist.items() if d == 1 and p in P.active)
    coverage: Dict[str, Dict[str, Any]] = {}
    identified: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []

    # 1. Lab members, accepted only on works already recorded for them.
    lab_works: Dict[str, List[Dict[str, Any]]] = {}
    for pid in lab:
        name = contacts[pid]["name"]
        if pid not in P.oa_of:
            papers = P.known_papers(pid)
            need = 2 if len(papers) >= 2 else 1
            accepted = []
            try:
                cands = [c for c in oa.search_authors(client, name, contacts[pid].get("institution", ""))
                         if name_score(name, c["name"]) and c["openalexId"] not in P.not_theirs(pid)]
                for c in cands[:4]:
                    works = _works(client, [c["openalexId"]], max_authors=None)
                    hits = sum(1 for p in papers if any(p & set(_work_ids(w)) for w in works))
                    if papers and hits >= need:
                        accepted.append((c["openalexId"], hits))
            except BudgetReached:
                raise
            except HttpError as exc:
                errors.append(f"{name}: {exc}")
            if accepted:
                P.oa_of[pid] = [a for a, _ in accepted]
                for a, _ in accepted:
                    P.by_oa[a] = pid
                hits = sum(h for _, h in accepted)
                identified[pid] = {"ids": P.oa_of[pid], "basis": f"their OpenAlex works include {hits} "
                                                                 f"paper{'s' if hits != 1 else ''} already recorded for them"}
        if pid in P.oa_of:
            lab_works[pid] = _works(client, P.oa_of[pid], max_authors=None)
            log(f"  {name}: {len(lab_works[pid])} OpenAlex works")
        else:
            coverage[pid] = {"status": "not-found", "degree": 0}
            log(f"  {name}: not found in OpenAlex")
    lab_ids = {a for pid in lab for a in P.oa_of.get(pid, [])}

    # 2. Complete the first degree: every coauthor of a lab member (papers with 30 or fewer authors).
    new_first: List[str] = []
    for pid, works in lab_works.items():
        ranked, counts, latest, names, insts = _coauthor_counts(works, set(P.oa_of[pid]))
        added = linked = 0
        for aid in ranked:
            if aid in lab_ids:
                continue
            target, held = P.resolve(aid, names[aid], insts[aid], pid, counts[aid], 1)
            if held:
                continue
            if target is None:
                target = P.add_person(aid, names[aid], pid, counts[aid], insts[aid], degree=1)
                new_first.append(aid)
                added += 1
            if P.add_edge(pid, target, counts[aid], latest[aid], None) and P.new_people.get(aid, {}).get("via") != pid:
                linked += 1
        coverage[pid] = {"status": "collected", "degree": 0, "works": len(works), "coauthors": len(counts),
                         "added": added, "linked": linked}
    if only:
        wanted = {norm_text(x) for x in only}
        first = [p for p in first if p in only or norm_text(contacts[p]["name"]) in wanted]
        new_first = [a for a in new_first if norm_text(P.new_people[a]["name"]) in wanted]

    # 3. Identify first-degree people already in the notebook.
    lab_coauthors: Dict[str, List[Tuple[str, str, str, Set[str]]]] = defaultdict(list)
    for member, works in lab_works.items():
        for w in works:
            wids = set(_work_ids(w))
            for aid, nm, _ in _authorships(w):
                if aid not in lab_ids:
                    for k in {family_key(nm)} | surname_tokens(nm):
                        lab_coauthors[k].append((aid, nm, member, wids))
    for pid in first:
        c = contacts[pid]
        name = c["name"]
        if pid in P.oa_of:
            identified[pid] = {"ids": P.oa_of[pid], "basis": "OpenAlex link on their record"}
            continue
        known = P.known_ids(pid)
        score: Counter = Counter()
        shared: Counter = Counter()
        on_record: Counter = Counter()
        via: Dict[str, str] = {}
        # The same authorship can sit under several surname keys; count each once.
        rows, seen_rows = [], set()
        for k in {family_key(name)} | surname_tokens(name):
            for r in lab_coauthors.get(k, []):
                if (r[0], id(r[3])) not in seen_rows:
                    seen_rows.add((r[0], id(r[3])))
                    rows.append(r)
        for aid, nm, member, wids in rows:
            s = name_score(name, nm)
            if not s or aid in P.not_theirs(pid):
                continue
            score[aid] = max(score[aid], s)
            shared[aid] += 1
            via.setdefault(aid, contacts[member]["name"])
            if wids & known:
                on_record[aid] += 1
        pick = None
        if on_record:
            top = sorted(on_record, key=lambda a: (-score[a], -on_record[a]))
            if len(top) == 1 or (score[top[0]], on_record[top[0]]) != (score[top[1]], on_record[top[1]]):
                pick = (top[0], f"named with {via[top[0]]} on {on_record[top[0]]} OpenAlex "
                                f"paper{'s' if on_record[top[0]] > 1 else ''} already recorded between them")
        elif shared:
            top = sorted(shared, key=lambda a: (-score[a], -shared[a]))
            if len(top) == 1 or score[top[0]] > score[top[1]] or shared[top[0]] >= 2 * shared[top[1]]:
                pick = (top[0], f"named with {via[top[0]]} on {shared[top[0]]} OpenAlex "
                                f"paper{'s' if shared[top[0]] > 1 else ''}")
            else:
                coverage[pid] = {"status": "ambiguous", "degree": 1, "candidates": top[:4]}
                continue
        if pick is None:
            types = {e["type"] for o in P.g.nbrs.get(pid, ()) for e in P.g.records_between(pid, o)}
            if nb.doctorate_state(c) != "confirmed" and not (types & RESEARCH_TYPES):
                coverage[pid] = {"status": "not-looked-up", "degree": 1}
                continue
            inst = c.get("institution", "")
            try:
                cands = [x for x in oa.search_authors(client, name, inst) if name_score(name, x["name"])]
            except BudgetReached:
                raise
            except HttpError as exc:
                errors.append(f"{name}: {exc}")
                coverage[pid] = {"status": "not-found", "degree": 1, "reason": str(exc)}
                continue
            matching = [x for x in cands if x["institutionMatches"] and x["openalexId"] not in P.by_oa
                        and x["openalexId"] not in P.not_theirs(pid)]
            if len(matching) == 1:
                pick = (matching[0]["openalexId"], f"the only OpenAlex author with this name at {inst}")
            else:
                coverage[pid] = {"status": "ambiguous" if matching else "not-found", "degree": 1,
                                 "candidates": [x["openalexId"] for x in matching[:4]]}
                continue
        P.oa_of[pid] = [pick[0]]
        P.by_oa.setdefault(pick[0], pid)
        identified[pid] = {"ids": [pick[0]], "basis": pick[1]}
    log(f"  first degree: {len(new_first)} coauthors of lab members added; "
        f"{sum(1 for p in first if p in identified)} of {len(first)} existing first-degree people identified")

    # 4. Second degree: the coauthors of every identified first-degree person. The first sweep decides who is
    # added; the second links every first-degree person to each coauthor now in the notebook or the plan, so
    # the order people are processed in does not matter.
    since = f"{since_year}-01-01"
    targets = [(p, P.oa_of[p]) for p in first if p in identified] + \
              [(P.new_people[a]["id"], [a]) for a in new_first]
    stopped = False
    collected: Dict[str, Tuple] = {}
    for pid, mine in targets:
        if stopped:
            coverage[pid] = {"status": "stopped", "degree": 1}
            continue
        try:
            works = _works(client, mine, since=since)
        except BudgetReached:
            stopped = True
            coverage[pid] = {"status": "stopped", "degree": 1}
            continue
        except HttpError as exc:
            errors.append(f"{P.name(pid)}: {exc}")
            coverage[pid] = {"status": "not-found", "degree": 1, "reason": str(exc)}
            continue
        ranked, counts, latest, names, insts = _coauthor_counts(works, set(mine))
        collected[pid] = (works, ranked, counts, latest, names, insts)
        added = cap = 0     # cap: this person's frequent coauthors added by any expansion, now or before
        for aid in ranked:
            if counts[aid] < min_shared:
                break
            target, held = P.resolve(aid, names[aid], insts[aid], pid, counts[aid], min_shared)
            if held:
                continue
            if target is not None:
                if P.added_by_expansion(target) or P.new_people.get(aid, {}).get("degree") == 2:
                    cap += 1
            elif cap < per_person:
                P.add_person(aid, names[aid], pid, counts[aid], insts[aid], degree=2)
                added += 1
                cap += 1
        coverage[pid] = {"status": "collected", "degree": 1, "works": len(works), "coauthors": len(counts),
                         "added": added, "linked": 0}
    for pid, (works, ranked, counts, latest, names, insts) in collected.items():
        linked = 0
        for aid in ranked:
            target, held = P.resolve(aid, names[aid], insts[aid], pid, counts[aid], min_shared)
            if target is None or held:
                continue
            if P.add_edge(pid, target, counts[aid], latest[aid], since_year) and \
                    P.new_people.get(aid, {}).get("via") != pid:
                linked += 1
        coverage[pid]["linked"] = linked
    for pid, info in identified.items():
        if pid in coverage:
            coverage[pid].update({"openalexIds": info["ids"], "basis": info["basis"]})
    for aid in new_first:
        rid = P.new_people[aid]["id"]
        if rid in coverage:
            coverage[rid].update({"openalexIds": [aid],
                                  "basis": f"coauthor of {P.name(P.new_people[aid]['via'])} in OpenAlex"})
    log(f"  second degree: {sum(1 for p in P.new_people.values() if p['degree'] == 2)} people added, "
        f"{len(P.edges)} coauthor records in all")

    # 5. Details for new people: OpenAlex author records, then ORCID for degrees and current positions.
    details: Dict[str, Dict[str, Any]] = {}
    if P.new_people:
        try:
            details = oa.get_authors(client, list(P.new_people))
        except HttpError as exc:
            errors.append(f"author details: {exc}")
    log("  reading ORCID records for new people with an ORCID iD")
    new_contacts = [_contact_record(client, aid, plan, details.get(aid) or {}, P.name(plan["via"]), since_year, errors)
                    for aid, plan in P.new_people.items()]
    updates = []
    for pid, info in identified.items():
        no = nb.openalex_not_theirs(pid)      # a link marked as not theirs does not count
        if any(a not in no for s in contacts[pid].get("sources") or [] for a in oa.find_author_ids(s.get("url", ""))):
            continue
        updates.append({"id": pid, "sources": [{"url": oa.author_url(a), "note": (
            f"OpenAlex author record, matched by expand on {today()}: {info['basis']}.")[:1000]} for a in info["ids"]]})
    return {
        "notebook": {"schemaVersion": 1, "contacts": updates + new_contacts, "edges": P.edges, "meetings": []},
        "coverage": coverage,
        "report": {"held": P.held, "errors": errors, "requests": client.requests, "costUsd": round(client.cost_usd, 4),
                   "newFirstDegree": len(new_first),
                   "newSecondDegree": sum(1 for p in P.new_people.values() if p["degree"] == 2),
                   "linksAdded": len(updates)},
        "standard": {"sinceYear": since_year, "perPerson": per_person, "minShared": min_shared,
                     "maxAuthors": MAX_AUTHORS},
        "firstDegree": len(first) + len(new_first), "stopped": stopped,
    }


def _contact_record(client: Client, aid: str, plan: Dict[str, Any], a: Dict[str, Any], via_name: str,
                    since_year: int, errors: List[str]) -> Dict[str, Any]:
    url = oa.author_url(aid)
    last = [i.get("display_name", "") for i in a.get("last_known_institutions") or [] if i.get("display_name")]
    institution = "; ".join(last[:2]) or "; ".join(plan["insts"][:2])
    orcid = (a.get("orcid") or "").replace("https://orcid.org/", "")
    why = ("a coauthor of " + via_name if plan["degree"] == 1 else
           f"a frequent coauthor of {via_name} ({plan['shared']} shared works since {since_year})")
    rec: Dict[str, Any] = {
        "id": plan["id"], "name": plan["name"][:160], "institution": institution[:200], "role": "", "lab": "",
        "email": "", "website": "", "notes": "", "createdAt": utc_now(),
        "tags": ["openalex-expand", "expand-first-degree" if plan["degree"] == 1 else "expand-second-degree"],
        "profileSourceUrl": url, "profileCheckedAt": today(), "profileConfidence": "uncertain",
        "expansionStatus": "identity-only", "photoUrl": "", "photoConfidence": "unverified",
        "sources": [{"url": url, "note": (f"OpenAlex author record, added by expand on {today()} as {why}. "
                                          "Institution is OpenAlex's latest for them.")[:1000]}],
        "researchSummary": "", "researchSourceUrl": "", "researchCheckedAt": "", "papers": [], "grants": []}
    topics = [t.get("display_name", "") for t in (a.get("topics") or [])][:4]
    if topics:
        rec.update({"researchSummary": ("Topics OpenAlex assigns to their works: " + "; ".join(topics) + ".")[:1500],
                    "researchSourceUrl": url, "researchCheckedAt": today()})
    if orcid:
        try:
            edus, emps = oc.activities(client, orcid)
        except HttpError as exc:
            errors.append(f"ORCID {orcid}: {exc}")
            return rec
        rec["sources"].append({"url": oc.record_url(orcid), "note": f"ORCID record, read by expand on {today()}."})
        d = oc.doctorate(edus, orcid, today())
        if d:
            rec.update(d)
        pos = oc.current_position(emps)
        if pos:
            rec["role"] = pos["title"][:200]
            if pos["organization"]:
                rec["institution"] = pos["organization"][:200]
    return rec


# --------------------------------------------------------------------------- run

def cache_root() -> str:
    """This computer's cache folder for Lab Network, outside any lab folder (which may sit in a synced drive)."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Caches/lab-network")
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "labnetwork", "cache")
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "labnetwork")


def default_cache(ws: Workspace) -> str:
    """Response cache for one workspace."""
    return os.path.join(cache_root(), ws.config.get("workspaceId", "workspace"))


def coverage_note(plan: Dict[str, Any], batch_id: str) -> Dict[str, Any]:
    return {"kind": "collection", "method": "openalex-coauthors", "date": today(), "degree": 1,
            "standard": plan["standard"], "batchId": batch_id, "people": plan["coverage"]}


def expand(ws_path: str, since_year: int = 2016, per_person: int = 5, min_shared: int = 2,
           only: Optional[List[str]] = None, dry_run: bool = False, budget_usd: float = 0.08,
           client: Optional[Client] = None, log: Callable[[str], None] = print) -> Dict[str, Any]:
    ws = Workspace(ws_path)
    svc = Service(ws)
    client = client or Client(cache_dir=default_cache(ws), budget_usd=budget_usd,
                              mailto=(ws.config.get("settings") or {}).get("contactEmail", ""))
    log("Reading OpenAlex" + (" (dry run: nothing is saved)" if dry_run else ""))
    try:
        plan = plan_expand(svc, client, since_year=since_year, per_person=per_person, min_shared=min_shared,
                           only=only, log=log)
    except BudgetReached as exc:
        raise SystemExit(f"Stopped before finishing: {exc}. Responses so far are cached; run again tomorrow.")
    nbk, rep = plan["notebook"], plan["report"]
    statuses = Counter(v["status"] for v in plan["coverage"].values() if v.get("degree") == 1)
    log(f"First-degree people: {plan['firstDegree']}. " +
        "; ".join(f"{n} {STATUS_TEXT[s]}" for s, n in statuses.items()))
    log(f"Plan: {rep['newFirstDegree']} new first-degree and {rep['newSecondDegree']} new second-degree people, "
        f"{len(nbk['edges'])} new coauthor records, {rep['linksAdded']} OpenAlex links on existing records, "
        f"{len(rep['held'])} held back as possible duplicates. {client.requests} requests, "
        f"${client.cost_usd:.4f} of OpenAlex's free daily allowance.")
    if dry_run:
        return {"dryRun": True, "plan": plan}
    data = json.dumps(nbk, ensure_ascii=False).encode("utf-8")
    filename = f"expand-{today()}.json"
    actor = "expand (OpenAlex)"
    port = _running_port(ws)
    if port:
        result = _http(port, filename, data, actor)
        note = _post(port, "/api/workspace-notes", {"note": coverage_note(plan, result["batchId"])}, actor)
    else:
        ws.acquire_lock(0)
        try:
            imp = Importer(svc)
            prev = imp.preview(filename, data)
            result = imp.apply(prev["batchId"], {k: "skip" for k in prev["needsDecision"]}, actor)
            result["heldForReview"] = _held(prev)
            note = svc.add_workspace_note(coverage_note(plan, result["batchId"]), actor)
        finally:
            ws.release_lock()
    path = _write_digest(ws, svc, plan, result)
    log(f"Applied as import batch {result['batchId']}: {result['applied']}. Digest: {path}")
    log(f"To undo: python3 labnetwork.py undo-import <workspace> {result['batchId']}")
    return {"dryRun": False, "plan": plan, "result": result, "note": note, "digestPath": path}


def _post(port: int, path: str, body: Dict[str, Any], actor: str) -> Dict[str, Any]:
    import urllib.parse
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "X-LabNetwork-Actor": urllib.parse.quote(actor)})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read().decode("utf-8"))


def _http(port: int, filename: str, data: bytes, actor: str) -> Dict[str, Any]:
    prev = _post(port, "/api/import/preview", {"filename": filename, "content": data.decode("utf-8")}, actor)
    result = _post(port, "/api/import/apply", {"batchId": prev["batchId"],
                                               "decisions": {k: "skip" for k in prev["needsDecision"]}}, actor)
    result["heldForReview"] = _held(prev)
    return result


def _write_digest(ws: Workspace, svc: Service, plan: Dict[str, Any], result: Dict[str, Any]) -> str:
    ddir = os.path.join(ws.path, "digests")
    os.makedirs(ddir, exist_ok=True)
    names = {pid: c["name"] for pid, c in svc.nb.records["contacts"].items()}
    names.update({c["id"]: c["name"] for c in plan["notebook"]["contacts"] if c.get("name")})
    st, rep = plan["standard"], plan["report"]
    lines = [f"# Network expansion, {today()}", "",
             f"First degree: every OpenAlex coauthor of a lab member (papers with {st['maxAuthors']} or fewer authors). "
             f"Second degree: for each first-degree person found in OpenAlex, coauthors on works since "
             f"{st['sinceYear']}. Coauthors already in the notebook gained a coauthor record where they lacked one; up "
             f"to {st['perPerson']} others with {st['minShared']} or more shared works were added per person.", "",
             f"Import batch {result['batchId']}: {result['applied']}. {rep['newFirstDegree']} new first-degree and "
             f"{rep['newSecondDegree']} new second-degree people; {len(plan['notebook']['edges'])} coauthor records. "
             f"{rep['requests']} requests; ${rep['costUsd']:.4f} of OpenAlex's free daily allowance.", ""]
    by_status: Dict[str, List[str]] = defaultdict(list)
    for pid, v in sorted(plan["coverage"].items(), key=lambda kv: names.get(kv[0], kv[0])):
        extra = (f": {v['added']} added, {v['linked']} linked, {v['coauthors']} coauthors in {v['works']} works"
                 if v["status"] == "collected" else "")
        by_status[v["status"]].append(f"- {names.get(pid, pid)}{' (lab member)' if v.get('degree') == 0 else ''}{extra}")
    for s in ("collected", "ambiguous", "not-found", "not-looked-up", "stopped"):
        if by_status.get(s):
            lines += [f"## {STATUS_TEXT[s][0].upper() + STATUS_TEXT[s][1:]} ({len(by_status[s])})"] + by_status[s] + [""]
    held = rep["held"] + [{"name": x} for x in result.get("heldForReview", [])]
    if held:
        lines += [f"## Held back as possible duplicates ({len(held)})",
                  "Each has the same name as someone already in the notebook, with nothing to confirm it is the same "
                  "person, so it was not added.", ""]
        lines += [f"- {x['name']}" + (f" (via {x['via']}; same name as {names.get(x['sameNameAs'], x['sameNameAs'])})"
                                      if x.get("via") else "") for x in held] + [""]
    if rep["errors"]:
        lines += ["## Problems"] + [f"- {e}" for e in rep["errors"]] + [""]
    path = os.path.join(ddir, f"expand-{today()}-{result['batchId'].split('-')[1][9:15]}.md")   # one per batch
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(path[:-3] + ".json", "w", encoding="utf-8") as f:
        json.dump({"date": today(), "kind": "expand", "standard": st, "coverage": plan["coverage"], "report": rep,
                   "result": result}, f, indent=1, ensure_ascii=False)
    return path
