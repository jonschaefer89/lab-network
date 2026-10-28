"""Build a lab's starting packet from public records plus what the lab reports.

Input is lab.json (see docs/onboarding.md). Output is a packet directory in the
same format Lab Network loads everywhere: notebook.json, lab-roots.json,
source-manifest.json, packet-checksums.json and a copy of the schema.

Identity rules:
* Lab members must be matched to an OpenAlex author by ORCID or confirmed by a person.
* Coauthors use OpenAlex's own author clusters; their profiles are marked
  "uncertain" because that clustering is algorithmic.
* Coauthors come from papers with 30 or fewer authors. A person confirmed each lab
  member's OpenAlex record, not each work on it, and OpenAlex sometimes files two
  researchers with the same name under one record, so a lab member's work adds
  coauthors only when it counts by the rule expand applies.
* A doctorate is "confirmed" only from an ORCID education record; otherwise the
  field is left out (not classified), which never means "no doctorate".
* Every relationship cites the works or award it comes from. Shared institutions
  and topics never create relationships.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .connectors import openalex as oa
from .connectors import orcid as orc
from .connectors import reporter as rp
from .connectors.http import Client, HttpError
from .expand import MAX_AUTHORS, lab_works_that_count
from .graph import Graph, TieFilter
from .model import Notebook, name_key, norm_text, paper_key
from .packet import BUNDLED_SCHEMA, Packet, sha256_file, validate_rules, validate_schema
from .service import slugify
from .store import utc_now

DEFAULT_SETTINGS = {
    "sinceYear": None,               # default: ten years ago
    "maxCoauthorsPerMember": 40,
    "secondDegree": True,
    "maxWorksPerBridge": 60,
    "maxNewPerBridge": 5,
    "maxPeople": 1500,               # keeps the map readable; expand adds the rest of the second degree
    "maxPapersPerPerson": 12,
    "orcidLookups": True,
    "maxOrcidLookups": 250,
    "nihAwards": True,
}

REL_TYPES = {"mentor", "postdoc", "committee", "training", "coauthor", "grant", "collaborator", "labmate",
             "irta_colleague", "met", "event"}


class BuildError(Exception):
    pass


def load_lab(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        lab = json.load(f)
    problems = []
    if not (lab.get("lab") or {}).get("name"):
        problems.append("lab.name is missing")
    members = lab.get("members") or []
    if not members:
        problems.append("members is empty: list at least the PI")
    for i, m in enumerate(members):
        if not m.get("name"):
            problems.append(f"members[{i}] has no name")
    if problems:
        raise BuildError("lab.json is incomplete: " + "; ".join(problems))
    return lab


def save_lab(path: str, lab: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lab, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def settings_of(lab: Dict[str, Any]) -> Dict[str, Any]:
    s = dict(DEFAULT_SETTINGS)
    s.update({k: v for k, v in (lab.get("settings") or {}).items() if v is not None})
    if not s["sinceYear"]:
        s["sinceYear"] = _dt.date.today().year - 10
    return s


# ---------------------------------------------------------------------------- identity resolution

def resolve_members(lab: Dict[str, Any], client: Client, choose: Optional[Callable] = None) -> Dict[str, Any]:
    """Match members to OpenAlex authors. ORCID matches are accepted; name matches need a person's choice.

    `choose(member, candidates)` returns an OpenAlex ID, "none", or None (decide later).
    """
    report = {"resolved": [], "needsConfirmation": [], "notFound": []}
    institution = (lab.get("lab") or {}).get("institution", "")
    for m in lab["members"]:
        if m.get("openalexId") or m.get("openalexId") == "none":
            continue
        if m.get("orcid"):
            a = oa.author_by_orcid(client, m["orcid"])
            if a:
                m["openalexId"] = oa.short_id(a["id"])
                m["identityConfirmedBy"] = "orcid"
                report["resolved"].append({"member": m["name"], "openalexId": m["openalexId"], "by": "ORCID"})
                continue
        cands = oa.search_authors(client, m["name"], m.get("institution") or institution)
        if not cands:
            report["notFound"].append({"member": m["name"]})
            continue
        picked = choose(m, cands) if choose else None
        if picked:
            m["openalexId"] = picked
            m["identityConfirmedBy"] = "person" if picked != "none" else "none"
            report["resolved"].append({"member": m["name"], "openalexId": picked, "by": "person"})
        else:
            report["needsConfirmation"].append({"member": m["name"], "candidates": cands})
    return report


# ---------------------------------------------------------------------------- build

class _Draft:
    """The lab before its packet exists, with the two parts of a Service that expand's identity rules read."""

    def __init__(self, nb: Notebook):
        self.nb = nb

    def graph(self, tf: TieFilter = TieFilter()) -> Graph:
        return Graph(self.nb, tf)


class _Builder:
    def __init__(self, lab: Dict[str, Any], client: Client, log: Callable[[str], None]):
        self.lab = lab
        self.client = client
        self.log = log
        self.s = settings_of(lab)
        self.today = _dt.date.today().isoformat()
        self.now = utc_now()
        self.contacts: Dict[str, Dict[str, Any]] = {}
        self.order: List[str] = []
        self.by_oa: Dict[str, str] = {}
        self.edges: Dict[str, Dict[str, Any]] = {}
        self.pair_works: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = defaultdict(dict)
        self.notes: List[str] = []
        self.unresolved = 0
        self.works_by_person: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)

    # -- helpers ---------------------------------------------------------------

    def new_id(self, name: str, wanted: str = "") -> str:
        base = slugify(wanted or name)
        rid, n = base, 2
        while rid in self.contacts:
            rid = f"{base}-{n}"
            n += 1
        return rid

    def add_contact(self, rec: Dict[str, Any]) -> str:
        self.contacts[rec["id"]] = rec
        self.order.append(rec["id"])
        return rec["id"]

    def base(self, cid: str, name: str, **fields) -> Dict[str, Any]:
        rec = {"id": cid, "name": name, "institution": "", "role": "", "lab": "", "email": "", "website": "",
               "tags": [], "notes": "", "createdAt": self.now, "seed": False, "photoUrl": "",
               "photoConfidence": "unverified", "sources": [], "researchSummary": "", "researchSourceUrl": "",
               "researchCheckedAt": "", "papers": [], "grants": []}
        rec.update(fields)
        return rec

    def edge(self, typ: str, a: str, b: str, key: str, **fields) -> None:
        eid = f"{typ}-{a}-{b}-{key}"
        if len(eid) > 100:
            eid = f"{typ}-" + hashlib.sha1(eid.encode()).hexdigest()[:20]
        if eid in self.edges:
            return
        rec = {"id": eid, "from": a, "to": b, "type": typ, "provenance": "public", "confidence": "confirmed",
               "status": "reviewed", "sourceTitle": "", "sourceUrl": "", "evidence": "", "checkedAt": self.today,
               "createdAt": self.now}
        rec.update(fields)
        self.edges[eid] = rec

    def oa_contact(self, author: Dict[str, Any], degree_tag: str) -> str:
        aid = oa.short_id(author.get("id", ""))
        if aid in self.by_oa:
            return self.by_oa[aid]
        summ = oa.summarize(author)
        url = oa.author_url(aid)
        insts = summ["lastKnownInstitutions"]
        fields = {"institution": "; ".join(insts)[:200], "profileSourceUrl": url, "profileCheckedAt": self.today,
                  "profileConfidence": "uncertain", "expansionStatus": "identity-only", "tags": [degree_tag]}
        if insts:
            first = (author.get("last_known_institutions") or [{}])[0]
            fields.update({"primaryInstitution": insts[0][:200], "primaryInstitutionId": oa.short_id(first.get("id", "")),
                           "primaryInstitutionSourceUrl": url, "primaryInstitutionCheckedAt": self.today})
        if summ["topics"]:
            fields.update({"researchSummary": ("OpenAlex assigns these research topics from this person's publications: "
                                               + "; ".join(summ["topics"]) + "."),
                           "researchSourceUrl": url, "researchCheckedAt": self.today})
        sources = [{"url": url, "note": f"OpenAlex author record, checked {self.today}. The institution is OpenAlex's "
                                       "last known affiliation and can lag a move."}]
        if summ["orcid"]:
            sources.append({"url": orc.record_url(summ["orcid"]), "note": "ORCID iD listed on the OpenAlex record"})
        name = (summ["name"] or aid)[:160]
        cid = self.new_id(name)
        self.add_contact(self.base(cid, name, sources=sources, **fields))
        self.by_oa[aid] = cid
        return cid

    def paper_record(self, w: Dict[str, Any], note: str = "") -> Optional[Dict[str, Any]]:
        title = oa.work_title(w)
        link = oa.work_link(w)
        if not title or not link:
            return None
        rec = {"title": title[:600], "url": link, "year": str(w.get("publication_year") or ""), "checkedAt": self.today,
               "sourceUrl": w.get("id", ""), "authors": oa.work_authors(w)[:2000], "venue": oa.work_venue(w)[:300],
               "type": (w.get("type") or "")[:120]}
        if (w.get("doi") or "").startswith("https://doi.org/"):
            rec["doi"] = w["doi"].replace("https://doi.org/", "")[:250]
        if note:
            rec["selectionNote"] = note[:1000]
        return rec

    # -- steps -------------------------------------------------------------------

    def members(self) -> None:
        lab = self.lab["lab"]
        for m in self.lab["members"]:
            cid = self.new_id(m["name"], m.get("id", ""))
            m["id"] = cid
            sources = []
            for url, note in ((m.get("facultyPage"), "Faculty or lab profile page (provided by the lab)"),
                              (m.get("scholarUrl"), "Google Scholar profile (identity reference provided by the lab; not machine-read)"),
                              (lab.get("website"), "Lab website (provided by the lab)")):
                if url:
                    sources.append({"url": url, "note": note})
            if m.get("openalexId") and m["openalexId"] != "none":
                sources.append({"url": oa.author_url(m["openalexId"]),
                                "note": f"OpenAlex author record, matched by {m.get('identityConfirmedBy', 'the lab')}"})
            if m.get("orcid"):
                sources.append({"url": orc.record_url(m["orcid"]), "note": "ORCID record"})
            if m.get("nihProfileId"):
                sources.append({"url": "https://reporter.nih.gov/", "note": f"NIH RePORTER PI profile ID {m['nihProfileId']}"})
            profile = m.get("facultyPage") or lab.get("website") or (oa.author_url(m["openalexId"]) if m.get("openalexId") not in (None, "none") else "")
            rec = self.base(cid, m["name"], institution=m.get("institution") or lab.get("institution", ""),
                            role=m.get("role", ""), lab=lab["name"], labName=lab["name"], labId=slugify(lab["name"]),
                            labStatus="current", website=m.get("website") or m.get("facultyPage") or "",
                            profileSourceUrl=profile, profileCheckedAt=self.today if profile else "",
                            profileConfidence="confirmed", tags=["lab-member"], seed=True, networkDegree=0,
                            expansionStatus="profile-confirmed", sources=sources)
            if lab.get("website"):
                rec.update({"labSourceUrl": lab["website"], "labCheckedAt": self.today})
            if m.get("email"):
                rec["email"] = m["email"]
            if m.get("photoUrl"):
                rec.update({"photoUrl": m["photoUrl"], "photoSourceUrl": m.get("photoSourceUrl", ""),
                            "photoAttribution": m.get("photoAttribution", "Provided by the lab"),
                            "photoCheckedAt": self.today,
                            "photoConfidence": "confirmed" if m.get("photoSourceUrl") else "unverified"})
            if m.get("researchSummary"):
                rec.update({"researchSummary": m["researchSummary"],
                            "researchSourceUrl": m.get("researchSourceUrl") or profile,
                            "researchCheckedAt": self.today})
            if m.get("doctorate"):
                d = m["doctorate"]
                src = d.get("sourceUrl") or profile
                status = d.get("status", "confirmed")
                if status in ("confirmed", "not-yet-earned") and not src:
                    # The data rules need a source link for a confirmed classification.
                    self.notes.append(f"{m['name']}: doctorate reported without a source link, recorded as unknown.")
                    status = "unknown"
                rec.update({"doctorateStatus": status, "doctorateType": d.get("type", "")[:120],
                            "doctorateEvidence": d.get("evidence", "Reported by the lab during onboarding")[:1800],
                            "doctorateSourceUrl": src if status != "unknown" else "",
                            "doctorateCheckedAt": self.today if status != "unknown" else ""})
            self.add_contact(rec)
            if m.get("openalexId") and m["openalexId"] != "none":
                self.by_oa[oa.short_id(m["openalexId"])] = cid
            else:
                self.unresolved += 1

    def counted_works(self) -> Dict[str, Set[str]]:
        """The OpenAlex works, by ID, that count for each lab member by the rule expand applies. The rule reads what
        is known about the lab: here, the members and the relationships the lab reported, drafted as a notebook in
        the way reported() later records them. The NIH awards are read after this, so they do not count."""
        lab_ids = [m["id"] for m in self.lab["members"]]
        contacts = [self.contacts[cid] for cid in lab_ids]
        by_oa = dict(self.by_oa)
        by_name = {name_key(c["name"]): c["id"] for c in contacts}
        edges = []
        for i, r in enumerate(self.lab.get("relationships") or []):
            if r.get("type") not in REL_TYPES or r.get("member") not in lab_ids:
                continue
            aid = oa.short_id(r.get("openalexId") or "")
            other = by_oa.get(aid) if aid else None
            if other is None and r.get("name"):
                other = by_name.get(name_key(r["name"]))
            if other is None:
                if not r.get("name"):
                    continue
                other = f"reported-{i}"
                sources = [{"url": oa.author_url(aid), "note": "OpenAlex author record, reported by the lab"}] \
                    if aid else []
                contacts.append(self.base(other, r["name"], institution=r.get("institution") or "", sources=sources))
                by_name[name_key(r["name"])] = other
                if aid:
                    by_oa[aid] = other
            edges.append({"id": f"reported-{i}", "from": r["member"], "to": other, "type": r["type"],
                          "provenance": "reported", "confidence": "confirmed", "status": "reviewed"})
        nb = Notebook(Packet("", {"contacts": contacts, "edges": edges}, [{"id": x} for x in lab_ids], None, None, {},
                             "", 0), [], lab_ids)
        problems: List[str] = []
        kept, left, _ = lab_works_that_count(_Draft(nb), self.client, problems, problems)   # the problems are noted
        since = lambda ws: [w for w in ws if (w.get("publication_year") or 0) >= self.s["sinceYear"]]
        counts = [f"{self.contacts[cid]['name']}, {len(since(works))} of {len(since(works)) + len(since(kept[cid]))}"
                  for cid, works in left.items() if since(works)]
        if counts:              # one note, so the manifest's limitations keep it however many members there are
            self.notes.append(f"Lab members' OpenAlex works since {self.s['sinceYear']} with {MAX_AUTHORS} or fewer "
                              "authors that connect to nothing else known about them, so their coauthors were not "
                              "added (OpenAlex sometimes files two researchers with the same name under one record): "
                              + "; ".join(counts) + ".")
        self.notes += problems
        return {cid: {w.get("id") for w in works} for cid, works in kept.items()}

    def first_degree(self) -> Dict[str, List[str]]:
        s = self.s
        member_oa = {oa.short_id(m["openalexId"]): m["id"] for m in self.lab["members"]
                     if m.get("openalexId") and m["openalexId"] != "none"}
        counted = self.counted_works()
        counts: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        for aid, cid in member_oa.items():
            works = oa.works_for_author(self.client, aid, s["sinceYear"], max_works=300)
            self.log(f"  {self.contacts[cid]['name']}: {len(works)} works since {s['sinceYear']}")
            for w in works:
                self.works_by_person[cid][w.get("id")] = w
                if w.get("id") not in counted.get(cid, ()) or len(w.get("authorships") or []) > MAX_AUTHORS:
                    continue
                for co in oa.coauthors(w):
                    if co["id"] != aid:
                        counts[cid][co["id"]].append(w)
        bridges: Dict[str, List[str]] = {}
        wanted: Dict[str, Tuple[str, List[Dict[str, Any]]]] = {}
        for cid, per in counts.items():
            ranked = sorted(per.items(), key=lambda kv: (-len(kv[1]), -max((w.get("publication_year") or 0) for w in kv[1])))
            keep = [r for r in ranked if r[0] in member_oa] + [r for r in ranked if r[0] not in member_oa][:s["maxCoauthorsPerMember"]]
            if len(ranked) > len(keep):
                self.notes.append(f"{self.contacts[cid]['name']}: kept the {len(keep)} most frequent of {len(ranked)} "
                                  f"OpenAlex coauthors since {s['sinceYear']}.")
            for aid, works in keep:
                wanted.setdefault(aid, (cid, works))
                self.pair_works[(cid, aid)] = {w["id"]: w for w in works}
        authors = oa.get_authors(self.client, [a for a in wanted if a not in member_oa])
        for aid in wanted:
            if aid in member_oa:
                continue
            a = authors.get(aid) or {"id": aid, "display_name": next((c["name"] for w in wanted[aid][1]
                                                                       for c in oa.coauthors(w) if c["id"] == aid), aid)}
            self.oa_contact(a, "openalex-first-degree")
        for (cid, aid), works in self.pair_works.items():
            other = member_oa.get(aid) or self.by_oa.get(aid)
            if not other or other == cid:
                continue
            a, b = sorted([cid, other])
            if member_oa.get(aid) and a != cid:
                continue  # member-member pairs: record once
            self.coauthor_edge(cid, other, list(works.values()), confidence="confirmed")
            for w in works.values():
                self.works_by_person[other][w["id"]] = w
            bridges.setdefault(other, []).append(cid)
        return bridges

    def coauthor_edge(self, a: str, b: str, works: List[Dict[str, Any]], confidence: str) -> None:
        works = sorted(works, key=lambda w: (w.get("publication_date") or ""), reverse=True)
        latest = works[0]
        others = [oa.work_link(w) for w in works[1:6] if oa.work_link(w)]
        n = len(works)
        evidence = (f"Named together on {n} work{'s' if n > 1 else ''} indexed by OpenAlex since {self.s['sinceYear']}, "
                    f"most recently “{oa.work_title(latest)[:200]}” ({latest.get('publication_year')})."
                    + (f" Other works: {'; '.join(others)}." if others else "")
                    + " Coauthorship alone does not show mentorship, a shared grant or a current collaboration.")
        self.edge("coauthor", a, b, "oa", sourceTitle=f"OpenAlex: {n} coauthored work{'s' if n > 1 else ''}",
                  sourceUrl=oa.work_link(latest), evidence=evidence[:3000], confidence=confidence,
                  relationshipStatus="unknown", relationshipLabel=f"Coauthors ({n} work{'s' if n > 1 else ''} since {self.s['sinceYear']})")

    def second_degree(self, bridges: Dict[str, List[str]]) -> None:
        s = self.s
        if not s["secondDegree"]:
            return
        lab_ids = {m["id"] for m in self.lab["members"]}
        first = [cid for cid in bridges if cid not in lab_ids]
        self.log(f"  checking coauthors of {len(first)} first-degree people")
        new_by_bridge: Dict[str, List[Tuple[str, List[Dict[str, Any]]]]] = {}
        for i, cid in enumerate(first):
            src = next((x["url"] for x in self.contacts[cid]["sources"] if "openalex.org/A" in x["url"]), "")
            aid = oa.short_id(src)
            if not aid:
                continue
            try:
                works = oa.works_for_author(self.client, aid, s["sinceYear"], max_works=s["maxWorksPerBridge"])
            except HttpError as exc:
                self.notes.append(f"Could not read works for {self.contacts[cid]['name']}: {exc}")
                continue
            if i and i % 25 == 0:
                self.log(f"    {i}/{len(first)}")
            per: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for w in works:
                self.works_by_person[cid][w.get("id")] = w
                if len(w.get("authorships") or []) > MAX_AUTHORS:
                    continue
                for co in oa.coauthors(w):
                    if co["id"] != aid:
                        per[co["id"]].append(w)
            known, unknown = [], []
            for co_id, ws in per.items():
                (known if co_id in self.by_oa else unknown).append((co_id, ws))
            for co_id, ws in known:
                other = self.by_oa[co_id]
                if other != cid and other not in lab_ids:
                    a, b = sorted([cid, other])
                    self.coauthor_edge(a, b, ws, confidence="likely")
            unknown.sort(key=lambda kv: (-len(kv[1]), -max((w.get("publication_year") or 0) for w in kv[1])))
            new_by_bridge[cid] = unknown[:s["maxNewPerBridge"]]
            if len(unknown) > s["maxNewPerBridge"]:
                self.notes.append(f"{self.contacts[cid]['name']}: added {s['maxNewPerBridge']} of {len(unknown)} "
                                  "coauthors as second-degree people.")
        budget = max(0, s["maxPeople"] - len(self.contacts))
        planned = len({co for lst in new_by_bridge.values() for co, _ in lst if co not in self.by_oa})
        if planned > budget:
            per = max(0, budget // max(1, len(new_by_bridge)))
            self.notes.append(f"Second-degree additions were limited to {per} per first-degree person to stay within "
                              f"{s['maxPeople']} people.")
            new_by_bridge = {k: v[:per] for k, v in new_by_bridge.items()}
        wanted = {co for lst in new_by_bridge.values() for co, _ in lst if co not in self.by_oa}
        authors = oa.get_authors(self.client, wanted)
        for cid, lst in new_by_bridge.items():
            for co_id, ws in lst:
                a = authors.get(co_id) or {"id": co_id, "display_name": next((c["name"] for w in ws for c in oa.coauthors(w)
                                                                              if c["id"] == co_id), co_id)}
                other = self.oa_contact(a, "openalex-second-degree")
                for w in ws:
                    self.works_by_person[other][w["id"]] = w
                x, y = sorted([cid, other])
                self.coauthor_edge(x, y, ws, confidence="likely")

    def papers(self) -> None:
        cap = self.s["maxPapersPerPerson"]
        for cid, works in self.works_by_person.items():
            rec = self.contacts.get(cid)
            if rec is None:
                continue
            ordered = sorted(works.values(), key=lambda w: (w.get("publication_date") or ""), reverse=True)
            seen = set()
            for w in ordered:
                p = self.paper_record(w, "Recent work indexed by OpenAlex")
                if not p or paper_key(p) in seen:
                    continue
                seen.add(paper_key(p))
                rec["papers"].append(p)
                if len(rec["papers"]) >= cap:
                    break

    def orcid_details(self) -> None:
        if not self.s["orcidLookups"]:
            return
        lab_ids = {m["id"] for m in self.lab["members"]}
        firsts = {e["to"] for e in self.edges.values() if e["from"] in lab_ids} | {e["from"] for e in self.edges.values() if e["to"] in lab_ids}
        todo = [cid for cid in self.order if cid in lab_ids or cid in firsts]
        done = 0
        for cid in todo:
            rec = self.contacts[cid]
            o = next((orc.clean(x["url"]) for x in rec["sources"] if "orcid.org/" in x["url"]), "")
            if not o or done >= self.s["maxOrcidLookups"]:
                continue
            done += 1
            try:
                edus = orc.educations(self.client, o)
                emps = orc.employments(self.client, o)
            except HttpError as exc:
                self.notes.append(f"ORCID lookup failed for {rec['name']}: {exc}")
                continue
            d = orc.doctorate(edus, o, self.today)
            if d and "doctorateStatus" not in rec:
                rec.update(d)
            pos = orc.current_position(emps)
            if pos and not rec["role"]:
                rec["role"] = pos["title"][:300]
                if not rec["institution"]:
                    rec["institution"] = pos["organization"][:200]
                rec["sources"].append({"url": orc.record_url(o),
                                       "note": f"ORCID employment record (current): {pos['title']}, {pos['organization']}"[:1500]})
        self.log(f"  ORCID records read: {done}")

    def nih(self) -> None:
        if not self.s["nihAwards"]:
            return
        names = {cid: name_key(c["name"]) for cid, c in self.contacts.items()}
        for m in self.lab["members"]:
            if not m.get("nihProfileId"):
                continue
            pid = int(m["nihProfileId"])
            try:
                recs = rp.projects(self.client, profile_ids=[pid])
            except HttpError as exc:
                self.notes.append(f"NIH RePORTER lookup failed for {m['name']}: {exc}")
                continue
            for core, r in rp.latest_by_core(recs).items():
                years = r["_fiscalYears"]
                period = f"{(r.get('project_start_date') or '')[:10]} to {(r.get('project_end_date') or '')[:10]}".strip()
                active = bool(r.get("is_active"))
                ic = r.get("agency_ic_admin") if isinstance(r.get("agency_ic_admin"), dict) else {}
                abbr = (ic or {}).get("abbreviation") or ""
                grant = {"title": (r.get("project_title") or core)[:600], "url": rp.project_url(r.get("appl_id")),
                         "funder": (f"NIH / {abbr}" if abbr else "NIH")[:300],
                         "period": period[:100], "checkedAt": self.today, "grantNumber": core[:150],
                         "role": rp.pi_role(r, pid)[:200], "projectId": (r.get("project_num") or "")[:200],
                         "status": ("active in NIH record" if active else "historical NIH record")[:600],
                         "verificationMethod": f"NIH RePORTER record for fiscal years {years[0]}–{years[-1]}; award dates are not a person's participation dates."[:1000]}
                if r.get("appl_id"):
                    grant["applicationId"] = int(r["appl_id"])
                for k_src, k_dst in (("project_start_date", "projectStartDate"), ("project_end_date", "projectEndDate"),
                                     ("budget_start", "budgetStartDate"), ("budget_end", "budgetEndDate")):
                    if r.get(k_src):
                        grant[k_dst] = r[k_src][:10]
                self.contacts[m["id"]]["grants"].append(grant)
                for p in r["_allPis"]:
                    if p.get("profile_id") == pid:
                        continue
                    full = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}"
                    nk = name_key(full)
                    match = [cid for cid, k in names.items() if k and (k == nk or _same_person(k, nk))]
                    if len(match) == 1:
                        other = match[0]
                    else:
                        other = self.new_id(full)
                        org = (r.get("organization") or {}).get("org_name", "") if isinstance(r.get("organization"), dict) else ""
                        self.add_contact(self.base(other, _title_name(full), institution="", tags=["nih-co-pi"],
                                                   profileSourceUrl=rp.project_url(r.get("appl_id")),
                                                   profileCheckedAt=self.today, profileConfidence="uncertain",
                                                   expansionStatus="identity-only",
                                                   sources=[{"url": rp.project_url(r.get("appl_id")),
                                                             "note": f"Named as PI on NIH project {core}; NIH RePORTER PI profile ID {p.get('profile_id')}. "
                                                                     f"Project organization: {org or 'not stated'}."}]))
                        names[other] = nk
                    self.edge("grant", m["id"], other, slugify(core), sourceTitle="NIH RePORTER award record",
                              sourceUrl=rp.project_url(r.get("appl_id")), projectId=core[:200],
                              projectTitle=(r.get("project_title") or "")[:600],
                              evidence=(f"Both are named as principal investigators on NIH project {core} "
                                        f"(“{(r.get('project_title') or '')[:200]}”), fiscal years {years[0]}–{years[-1]}. "
                                        "Award dates describe the project, not either person's participation.")[:3000],
                              relationshipStatus="current" if active else "historical",
                              relationshipLabel="Shared NIH principal-investigator award")

    def reported(self) -> None:
        """Relationships the lab reported during onboarding (mentors, trainees, committees, IRTA peers...)."""
        lab_ids = {m["id"] for m in self.lab["members"]}
        by_name = {name_key(c["name"]): cid for cid, c in self.contacts.items()}
        when = self.today
        for i, r in enumerate(self.lab.get("relationships") or []):
            typ = r.get("type")
            member = r.get("member")
            if typ not in REL_TYPES or member not in lab_ids:
                self.notes.append(f"relationships[{i}] skipped: needs a known member and a valid type")
                continue
            other = None
            if r.get("openalexId"):
                other = self.by_oa.get(oa.short_id(r["openalexId"]))
            if other is None and r.get("name"):
                other = by_name.get(name_key(r["name"]))
            if other is None:
                if not r.get("name"):
                    self.notes.append(f"relationships[{i}] skipped: no name for the other person")
                    continue
                other = self.add_contact(self.base(self.new_id(r["name"]), r["name"], institution=r.get("institution", ""),
                                                   role=r.get("role", ""), tags=["reported"], profileConfidence="unverified",
                                                   expansionStatus="identity-only",
                                                   sources=[{"url": r["url"], "note": "Profile link reported by the lab"}] if r.get("url") else []))
                by_name[name_key(r["name"])] = other
            direction = r.get("direction", "")
            a, b = member, other
            # Stored direction follows the data dictionary: mentor -> mentee, postdoc -> supervisor, committee member -> student.
            if typ == "mentor" and direction == "they-mentored-member":
                a, b = other, member
            if typ == "postdoc" and direction == "member-was-their-postdoc":
                a, b = member, other
            elif typ == "postdoc":
                a, b = other, member
            if typ == "committee" and direction == "they-served-on-member-committee":
                a, b = other, member
            fields = {"provenance": "reported", "sourceTitle": f"Reported by the lab during onboarding, {when}",
                      "sourceUrl": r.get("sourceUrl", ""), "evidence": (r.get("evidence") or "Reported by the lab during onboarding.")[:3000],
                      "relationshipStatus": r.get("status", "unknown"), "relationshipLabel": (r.get("label") or "")[:180]}
            if r.get("start"):
                fields["relationshipStart"] = str(r["start"])[:40]
            if r.get("end"):
                fields["relationshipEnd"] = str(r["end"])[:40]
            if fields["sourceUrl"] and not fields["sourceUrl"].startswith("http"):
                fields["sourceUrl"] = ""
            self.edge(typ, a, b, f"r{i}", **fields)

    # -- output ------------------------------------------------------------------------

    def notebook(self) -> Dict[str, Any]:
        lab_ids = [m["id"] for m in self.lab["members"]]
        contacts = [self.contacts[c] for c in self.order]
        for c in contacts:
            if not c["papers"]:
                c["papers"] = []
        coverage_notes = (" ".join(self.notes) + " Positions come only from ORCID current employment or from the lab; "
                          "most second-degree people have no recorded position. A missing doctorate means it was not "
                          "found on ORCID, not that the person lacks one.").strip()
        return {
            "schemaVersion": 1, "contacts": contacts, "meetings": [], "edges": list(self.edges.values()), "research": [],
            "appliedSeeds": [],
            "coverage": {"updatedAt": self.today,
                         "scope": (f"OpenAlex coauthors of lab members since {self.s['sinceYear']} (up to "
                                   f"{self.s['maxCoauthorsPerMember']} per member) on papers with {MAX_AUTHORS} or "
                                   "fewer authors that connect to what is known about the member"
                                   + (f", their most frequent coauthors on papers with {MAX_AUTHORS} or fewer authors "
                                      f"(up to {self.s['maxNewPerBridge']} each)" if self.s["secondDegree"] else "")
                                   + ", NIH RePORTER principal-investigator awards for members with a profile ID, "
                                   "and relationships the lab reported during onboarding."),
                         "seedIds": lab_ids, "status": "bounded-complete",
                         "sourcesReviewed": len(self.client.sources), "pendingSources": 0,
                         "unresolvedIdentities": self.unresolved, "notes": coverage_notes[:6000]},
        }


def build(lab: Dict[str, Any], client: Client, out_dir: str, log: Callable[[str], None] = print) -> Dict[str, Any]:
    b = _Builder(lab, client, log)
    log("Adding lab members")
    b.members()
    log("Reading lab members' works from OpenAlex")
    bridges = b.first_degree()
    log(f"  {len(bridges)} first-degree coauthors kept")
    if b.s["secondDegree"]:
        log("Adding second-degree coauthors")
        b.second_degree(bridges)
    log("Attaching recent papers")
    b.papers()
    log("Checking ORCID for doctorates and current positions")
    b.orcid_details()
    log("Reading NIH awards")
    b.nih()
    b.reported()
    nb = b.notebook()
    with open(BUNDLED_SCHEMA, encoding="utf-8") as f:
        schema = json.load(f)
    errors = validate_schema(nb, schema)
    issues = [i for i in validate_rules(nb) if i["level"] == "error"]
    if errors or issues:
        raise BuildError("The built notebook failed validation (please report this): "
                         + "; ".join([e["path"] + " " + e["message"] for e in errors[:5]] +
                                     [f"{i['collection']} {i['id']} {i['field']}: {i['message']}" for i in issues[:5]]))
    write_packet(out_dir, nb, lab, b.client.sources, b.notes)
    return {"people": len(nb["contacts"]), "relationships": len(nb["edges"]), "notes": b.notes,
            "requests": b.client.requests, "cached": b.client.cache_hits, "packet": out_dir}


def write_packet(out_dir: str, nb: Dict[str, Any], lab: Dict[str, Any], sources: List[Dict[str, Any]],
                 notes: List[str]) -> None:
    from .packet import count_summary
    os.makedirs(out_dir, exist_ok=True)
    data = json.dumps(nb, ensure_ascii=False, indent=1).encode("utf-8")
    with open(os.path.join(out_dir, "notebook.json"), "wb") as f:
        f.write(data)
    roots = [{"id": m["id"], "match_count": 1, "name": m["name"], "role": m.get("role", ""),
              "institution": m.get("institution") or lab["lab"].get("institution", "")} for m in lab["members"]]
    with open(os.path.join(out_dir, "lab-roots.json"), "w", encoding="utf-8") as f:
        json.dump(roots, f, indent=2, ensure_ascii=False)
    shutil.copyfile(BUNDLED_SCHEMA, os.path.join(out_dir, "notebook.schema.json"))
    manifest = {
        "dataset_status": f"built by labnetwork onboard on {_dt.date.today().isoformat()}",
        "saved_at": utc_now(), "delivered_file": "notebook.json", "delivered_utf8_bytes": len(data),
        "delivered_sha256": hashlib.sha256(data).hexdigest(), "counts": count_summary(nb),
        "sources": {"openalex": sum(1 for s in sources if "openalex" in s["url"]),
                    "nihReporter": sum(1 for s in sources if "reporter.nih.gov" in s["url"]),
                    "orcid": sum(1 for s in sources if "orcid.org" in s["url"])},
        "limitations": [
            "OpenAlex author identities are algorithmic; coauthor profiles are marked uncertain until someone checks them.",
            "Coauthorship is a documented fact about papers, not evidence of mentorship, closeness or current collaboration.",
            "Positions and doctorates come only from ORCID or from the lab; missing values mean not found, not absent.",
            "Coverage is bounded by the settings in lab.json; people outside those limits are not represented.",
        ] + notes[:20],
    }
    with open(os.path.join(out_dir, "source-manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    with open(os.path.join(out_dir, "build-sources.json"), "w", encoding="utf-8") as f:
        json.dump(sources, f, indent=0)
    names = ["notebook.json", "lab-roots.json", "notebook.schema.json", "source-manifest.json", "build-sources.json"]
    sums = {n: sha256_file(os.path.join(out_dir, n)) for n in names}
    with open(os.path.join(out_dir, "packet-checksums.json"), "w", encoding="utf-8") as f:
        json.dump(sums, f, indent=2)


def _same_person(a: str, b: str) -> bool:
    pa, pb = a.split(), b.split()
    if len(pa) < 2 or len(pb) < 2 or pa[-1] != pb[-1]:
        return False
    return pa[0] == pb[0] or (len(pa[0]) >= 3 and len(pb[0]) >= 3 and (pa[0].startswith(pb[0]) or pb[0].startswith(pa[0])))


def _title_name(full: str) -> str:
    return " ".join(w.capitalize() if w.isupper() or w.islower() else w for w in full.split())
