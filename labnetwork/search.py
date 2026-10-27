"""Topic and name search across profiles, papers, grants and notes.

Scope, in the words shown to users:
  People      name, stable ID, institution, position, lab
  Profiles    source-backed research summaries; notebook tags (labelled as tags)
  Papers      title, venue, author list and selection note of each person-paper record, plus coauthor
              relationship records (their title and evidence), which link the paper to both coauthors
  Grants      title, funder, award/project number of each person-grant record, plus shared-grant
              relationship records, which link the grant to both people
  Notes       contact notes and meeting notes (context, discussion, follow-up)
  Conferences appearances on imported conference programs (conference, role, talk title, session); see conferences.py

Each paper or grant lists how every person is linked to it: the item is on their profile, or a relationship
record names them. A paper on Dana's profile that Pat coauthored therefore also lists Pat when a
coauthor record between them names that paper.

Matching ignores case and accents and requires the term to start at a word
boundary ("twin" finds "twins"; "ADHD" does not match inside another word).
Terms joined by AND (in capitals) must all match: a person is listed when each term matches something linked to
them, and a paper, grant or note that matches every term is marked as such (see search_all).
A query that names a known abbreviation also searches its spelled-out forms;
the response lists every extra form it used so no match is hidden.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Tuple

from .model import Notebook, grant_key, norm_text, paper_key

DEFAULT_ALIASES = [
    ["ADHD", "attention-deficit/hyperactivity disorder", "attention deficit hyperactivity disorder",
     "attention-deficit hyperactivity disorder", "attention deficit/hyperactivity disorder"],
    ["ASD", "autism spectrum disorder"],
    ["PTSD", "posttraumatic stress disorder", "post-traumatic stress disorder"],
    ["MDD", "major depressive disorder"],
    ["OCD", "obsessive-compulsive disorder", "obsessive compulsive disorder"],
    ["SUD", "substance use disorder", "substance use disorders"],
    ["AUD", "alcohol use disorder"],
    ["CUD", "cannabis use disorder"],
    ["GWAS", "genome-wide association", "genomewide association"],
    ["fMRI", "functional magnetic resonance imaging", "functional MRI"],
    ["ABCD", "Adolescent Brain Cognitive Development"],
    ["HiTOP", "Hierarchical Taxonomy of Psychopathology"],
    ["EMA", "ecological momentary assessment"],
]

SCOPE_TEXT = ("People: name, ID, institution, position, lab. Profiles: research summaries and notebook tags. "
              "Papers: title, venue, authors and selection note of papers on profiles, and the title and evidence of "
              "coauthor records, which link a paper to both coauthors. Grants: title, funder, award or project number "
              "of grants on profiles, and shared-grant records, which link a grant to both people. "
              "Notes: contact notes and meeting notes. Conferences: imported programs (conference, role, talk, session).")

_NIH_CORE = re.compile(r"[A-Z]\d{2}[A-Z]{2}\d{6}")


def _strip_year(title: str) -> Tuple[str, str]:
    """'Some paper (2026)' -> ('Some paper', '2026')."""
    m = re.search(r"\s*\(((?:19|20)\d{2})\)\s*$", title or "")
    return ((title[:m.start()].strip(), m.group(1)) if m else ((title or "").strip(), ""))


def _paper_ids(title: str, url: str, doi: str = "") -> List[str]:
    """Identifiers that name one paper: DOI, PubMed or PMC ID, or a title of four or more words.

    Other URLs are left out because one page (a CV, a roster) often lists many papers.
    """
    keys = []
    d = (doi or "").strip().lower()
    if not d:
        m = re.search(r"doi\.org/(10\.[^\s?#]+)", (url or "").lower())
        d = m.group(1) if m else ""
    if d:
        keys.append("doi:" + d.rstrip("/."))
    m = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", (url or "").lower())
    if m:
        keys.append("pmid:" + m.group(1))
    m = re.search(r"\b(pmc\d+)\b", (url or "").lower())
    if m:
        keys.append("pmc:" + m.group(1))
    t = norm_text(title)
    if len(t.split()) >= 4:
        keys.append("title:" + t)
    return keys


def _grant_ids(number: str, title: str) -> List[str]:
    keys = []
    num = re.sub(r"[^A-Z0-9]", "", (number or "").upper())
    core = _NIH_CORE.search(num)
    if core:
        keys.append("nih:" + core.group(0))
    if num:
        keys.append("award:" + num)
    t = norm_text(title)
    if len(t.split()) >= 4:
        keys.append("title:" + t)
    return keys


def _norm_with_map(text: str) -> Tuple[str, List[int]]:
    out_chars: List[str] = []
    index: List[int] = []
    for i, ch in enumerate(text):
        for d in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(d):
                continue
            for c in d.casefold():
                out_chars.append(c)
                index.append(i)
    return "".join(out_chars), index


def _term_regex(term: str) -> Optional[re.Pattern]:
    words = norm_text(term).split()
    if not words:
        return None
    body = r"[^a-z0-9]+".join(re.escape(w) for w in words)
    return re.compile(r"(?<![a-z0-9])" + body)


class Matcher:
    def __init__(self, query: str, alias_groups: List[List[str]]):
        self.query = query.strip()
        q_norm = norm_text(self.query.strip('"'))
        terms = [self.query.strip('"')]
        self.aliases_used: List[str] = []
        if not self.query.startswith('"'):
            for group in alias_groups:
                if q_norm in {norm_text(g) for g in group}:
                    for g in group:
                        if norm_text(g) != q_norm and norm_text(g) not in {norm_text(t) for t in terms}:
                            terms.append(g)
                            self.aliases_used.append(g)
        self.terms = terms
        self.patterns = [p for p in (_term_regex(t) for t in terms) if p is not None]

    @property
    def empty(self) -> bool:
        return not self.patterns

    def find(self, text: str) -> List[Tuple[int, int]]:
        if not text:
            return []
        norm, index = _norm_with_map(text)
        spans = []
        for p in self.patterns:
            for m in p.finditer(norm):
                s, e = index[m.start()], index[m.end() - 1] + 1
                spans.append((s, e))
        spans.sort()
        merged: List[Tuple[int, int]] = []
        for s, e in spans:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
            else:
                merged.append((s, e))
        return merged


def snippet(text: str, spans: List[Tuple[int, int]], width: int = 220) -> Dict[str, Any]:
    """A window of text around the first match, with match offsets relative to it."""
    if not spans:
        return {"text": text[:width], "highlights": []}
    s0 = spans[0][0]
    start = max(0, s0 - width // 3)
    if start > 0:
        sp = text.rfind(" ", 0, start)
        start = sp + 1 if sp >= 0 and start - sp < 25 else start
    end = min(len(text), start + width)
    if end < len(text):
        sp = text.find(" ", end)
        end = sp if 0 <= sp - end < 25 else end
    window = text[start:end]
    hl = [[max(s, start) - start, min(e, end) - start] for s, e in spans if e > start and s < end]
    return {"text": ("…" if start > 0 else "") + window + ("…" if end < len(text) else ""),
            "highlights": [[a + (1 if start > 0 else 0), b + (1 if start > 0 else 0)] for a, b in hl]}


def load_aliases(workspace_path: Optional[str]) -> List[List[str]]:
    if workspace_path:
        p = os.path.join(workspace_path, "topic-aliases.json")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and all(isinstance(g, list) for g in data):
                return data
    return DEFAULT_ALIASES


PROFILE_FIELDS = [("researchSummary", "Research summary"), ("tags", "Notebook tag")]
PEOPLE_FIELDS = [("name", "Name"), ("id", "Stable ID"), ("institution", "Institution"), ("role", "Position"),
                 ("labName", "Lab"), ("lab", "Lab")]
PAPER_FIELDS = [("title", "Title"), ("venue", "Venue"), ("authors", "Authors"),
                ("selectionNote", "Selection note"), ("abstract", "Abstract")]
GRANT_FIELDS = [("title", "Title"), ("funder", "Funder"), ("grantNumber", "Award number"),
                ("projectId", "Project ID"), ("abstract", "Abstract")]
MEETING_FIELDS = [("context", "How we met"), ("learning", "What we discussed"), ("followUp", "Follow-up")]


def search(nb: Notebook, query: str, person_info: Callable[[str], Dict[str, Any]],
           alias_groups: Optional[List[List[str]]] = None, limit: int = 200,
           edge_ok: Optional[Callable[[Dict[str, Any]], bool]] = None,
           library: Optional[Dict[str, Any]] = None,
           events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """edge_ok decides which relationship records count (default: reviewed records).

    `library` (see library.py) adds each person's recent OpenAlex papers, NIH and NSF awards and OpenAlex topics.
    """
    edge_ok = edge_ok or (lambda e: e.get("status") == "reviewed")
    m = Matcher(query, alias_groups if alias_groups is not None else DEFAULT_ALIASES)
    result: Dict[str, Any] = {"query": query, "terms": m.terms, "aliasesUsed": m.aliases_used,
                              "scope": SCOPE_TEXT, "people": [], "profiles": [], "papers": [],
                              "grants": [], "notes": [], "events": []}
    if m.empty:
        result["counts"] = {k: 0 for k in ("people", "profiles", "papers", "grants", "notes", "events")}
        result["peopleSummary"] = []
        return result

    papers: Dict[str, Dict[str, Any]] = {}
    grants: Dict[str, Dict[str, Any]] = {}
    per_person: Dict[str, Dict[str, int]] = {}

    def bump(pid, kind):
        per_person.setdefault(pid, {"people": 0, "profiles": 0, "papers": 0, "grants": 0, "notes": 0,
                                    "events": 0})[kind] += 1

    # Every paper and grant on a profile, by each identifier, so relationship records can be matched to them.
    paper_index: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    grant_index: Dict[str, Tuple[str, Dict[str, Any]]] = {}

    for pid, c in nb.active("contacts"):
        info = person_info(pid)
        seen_people_field = set()
        for field, label in PEOPLE_FIELDS:
            val = c.get(field) or ""
            spans = m.find(val)
            if spans and label not in seen_people_field:
                seen_people_field.add(label)
                result["people"].append({"personId": pid, "name": c["name"], "field": field, "fieldLabel": label,
                                         **snippet(val, spans), **info})
                bump(pid, "people")
                break
        for field, label in PROFILE_FIELDS:
            if field == "tags":
                for tag in c.get("tags") or []:
                    spans = m.find(tag)
                    if spans:
                        result["profiles"].append({"personId": pid, "name": c["name"], "field": "tags",
                                                   "fieldLabel": label, **snippet(tag, spans),
                                                   "sourceUrl": "", "checkedAt": "",
                                                   "evidenceNote": "Tags are notebook metadata, not sourced research text.",
                                                   **info})
                        bump(pid, "profiles")
                continue
            val = c.get(field) or ""
            spans = m.find(val)
            if spans:
                result["profiles"].append({"personId": pid, "name": c["name"], "field": field, "fieldLabel": label,
                                           **snippet(val, spans), "sourceUrl": c.get("researchSourceUrl", ""),
                                           "checkedAt": c.get("researchCheckedAt", ""), **info})
                bump(pid, "profiles")
        for p in c.get("papers") or []:
            for k in _paper_ids(p.get("title", ""), p.get("url", "") or p.get("sourceUrl", ""), p.get("doi", "")):
                paper_index.setdefault(k, (paper_key(p), p))
            hits = [(f, lab, m.find(p.get(f) or "")) for f, lab in PAPER_FIELDS]
            hits = [h for h in hits if h[2]]
            if not hits:
                continue
            key = paper_key(p)
            entry = papers.get(key)
            if entry is None:
                f, lab, spans = hits[0]
                entry = papers[key] = {"key": key, "title": p.get("title", ""), "year": p.get("year", ""),
                                       "venue": p.get("venue", ""), "url": p.get("url", ""), "doi": p.get("doi", ""),
                                       "matchedFields": [], "associations": [],
                                       "snippetField": lab, **snippet(p.get(f) or "", spans)}
                if f != "title":
                    entry["titleHighlights"] = []
                else:
                    entry["titleHighlights"] = [list(s) for s in spans]
            for f, lab, _ in hits:
                if lab not in entry["matchedFields"]:
                    entry["matchedFields"].append(lab)
            entry["associations"].append({
                "personId": pid, "name": c["name"], **info, "basis": "profile",
                "authors": p.get("authors", ""), "authorshipEvidence": p.get("authorshipEvidence", ""),
                "selectionNote": p.get("selectionNote", ""), "verificationStatus": p.get("verificationStatus", ""),
                "sourceUrl": p.get("sourceUrl", "") or p.get("url", ""),
                "evidenceSourceUrl": p.get("evidenceSourceUrl", ""), "checkedAt": p.get("checkedAt", ""),
                "type": p.get("type", "") or p.get("publicationType", ""),
                "matchedFields": [lab for _, lab, _ in hits],
            })
            bump(pid, "papers")
        for g in c.get("grants") or []:
            for k in _grant_ids(g.get("grantNumber") or g.get("projectId") or "", g.get("title", "")):
                grant_index.setdefault(k, (grant_key(g), g))
            hits = [(f, lab, m.find(g.get(f) or "")) for f, lab in GRANT_FIELDS]
            hits = [h for h in hits if h[2]]
            if not hits:
                continue
            key = grant_key(g)
            entry = grants.get(key)
            if entry is None:
                f, lab, spans = hits[0]
                entry = grants[key] = {"key": key, "title": g.get("title", ""), "funder": g.get("funder", ""),
                                       "grantNumber": g.get("grantNumber", ""), "url": g.get("url", ""),
                                       "matchedFields": [], "associations": [], "snippetField": lab,
                                       **snippet(g.get(f) or "", spans),
                                       "titleHighlights": [list(s) for s in spans] if f == "title" else []}
            for f, lab, _ in hits:
                if lab not in entry["matchedFields"]:
                    entry["matchedFields"].append(lab)
            entry["associations"].append({
                "personId": pid, "name": c["name"], **info, "basis": "profile",
                "role": g.get("role", ""), "period": g.get("period", ""), "status": g.get("status", ""),
                "projectId": g.get("projectId", ""), "url": g.get("url", ""), "checkedAt": g.get("checkedAt", ""),
                "verificationMethod": g.get("verificationMethod", ""),
                "projectStartDate": g.get("projectStartDate", ""), "projectEndDate": g.get("projectEndDate", ""),
            })
            bump(pid, "grants")
        val = c.get("notes") or ""
        spans = m.find(val)
        if spans:
            result["notes"].append({"kind": "contact-note", "personId": pid, "name": c["name"],
                                    "field": "notes", "fieldLabel": "Contact note", **snippet(val, spans),
                                    "author": "", **info})
            bump(pid, "notes")

    _record_links(nb, m, person_info, edge_ok, papers, grants, paper_index, grant_index, bump)
    if library:
        _library_links(nb, m, person_info, library, papers, grants, paper_index, grant_index, bump, result)

    for mid, mt in nb.active("meetings"):
        pid = nb.canonical_id(mt.get("contactId", ""))
        c = nb.contact(pid)
        for field, label in MEETING_FIELDS:
            val = mt.get(field) or ""
            spans = m.find(val)
            if spans:
                result["notes"].append({"kind": "meeting", "meetingId": mid, "personId": pid,
                                        "name": c["name"] if c else pid, "field": field, "fieldLabel": label,
                                        **snippet(val, spans), "author": mt.get("recordedBy", ""),
                                        "metBy": mt.get("metBy", ""), "date": mt.get("date", ""),
                                        **person_info(pid)})
                bump(pid, "notes")
                break

    for ev in events or []:
        spans = m.find(ev["text"])
        if spans:
            result["events"].append({**ev, **snippet(ev["text"], spans, width=400), **person_info(ev["personId"]),
                                     "name": (nb.contact(ev["personId"]) or {}).get("name", ev["personId"])})
            bump(ev["personId"], "events")

    def closeness(item):
        d = item.get("degree")
        return (0 if item.get("shown") else 1, d if d is not None else 99, norm_text(item.get("name", "")))

    def year_of(entry, fld="year"):
        ys = re.findall(r"\d{4}", str(entry.get(fld, "")))
        return -int(ys[0]) if ys else 0

    for k in ("people", "profiles", "notes", "events"):
        result[k].sort(key=closeness)
    for coll in (papers, grants):
        for entry in coll.values():
            entry["associations"].sort(key=closeness)
            entry["shownAssociations"] = sum(1 for a in entry["associations"] if a.get("shown"))
            ds = [a["degree"] for a in entry["associations"] if a.get("degree") is not None]
            entry["closestDegree"] = min(ds) if ds else None
    result["papers"] = sorted(papers.values(), key=lambda e: (0 if e["shownAssociations"] else 1,
                                                               e["closestDegree"] if e["closestDegree"] is not None else 99,
                                                               year_of(e), norm_text(e["title"])))
    result["grants"] = sorted(grants.values(), key=lambda e: (0 if e["shownAssociations"] else 1,
                                                               e["closestDegree"] if e["closestDegree"] is not None else 99,
                                                               norm_text(e["title"])))
    result["counts"] = {
        "people": len(result["people"]),
        "profiles": len(result["profiles"]),
        "papers": len(result["papers"]),
        "paperAssociations": sum(len(e["associations"]) for e in result["papers"]),
        "grants": len(result["grants"]),
        "grantAssociations": sum(len(e["associations"]) for e in result["grants"]),
        "notes": len(result["notes"]),
        "events": len(result["events"]),
    }
    summary = []
    for pid, kinds in per_person.items():
        c = nb.contact(pid) or {}
        summary.append({"personId": pid, "name": c.get("name", pid), "role": c.get("role", ""),
                        "institution": c.get("institution", ""), "photoUrl": c.get("photoUrl", ""),
                        **person_info(pid), **kinds})
    summary.sort(key=closeness)
    result["peopleSummary"] = summary
    for k in ("people", "profiles", "notes"):
        result[k] = result[k][:limit]
    result["events"] = result["events"][:limit * 5]
    # Papers and grants are kept longer: the results page groups them by person, and the library adds many.
    result["papers"] = result["papers"][:limit * 5]
    result["grants"] = result["grants"][:limit * 5]
    return result


def _record_links(nb: Notebook, m: Matcher, person_info: Callable[[str], Dict[str, Any]],
                  edge_ok: Callable[[Dict[str, Any]], bool], papers: Dict[str, Dict[str, Any]],
                  grants: Dict[str, Dict[str, Any]], paper_index: Dict[str, Tuple[str, Dict[str, Any]]],
                  grant_index: Dict[str, Tuple[str, Dict[str, Any]]], bump: Callable[[str, str], None]) -> None:
    """Add people named in coauthor and shared-grant records to the paper or grant the record names.

    A record is matched to a profile item by DOI, PubMed or PMC ID, award number or full title. When no profile
    lists the item, the record's own title stands for it. The search terms are checked against the item's fields
    and the record's evidence sentence.
    """
    for eid, e in nb.active("edges"):
        if e.get("type") not in ("coauthor", "grant") or not edge_ok(e):
            continue
        ev_spans = m.find(e.get("evidence") or "")
        if e["type"] == "coauthor":
            title, year = _strip_year(e.get("sourceTitle", ""))
            keys = _paper_ids(title, e.get("sourceUrl", ""))
            found = next((paper_index[k] for k in keys if k in paper_index), None)
            if found:
                key, item = found
                hits = [(f, lab, m.find(item.get(f) or "")) for f, lab in PAPER_FIELDS]
            else:
                key = "record:" + (keys[0] if keys else eid)
                item = {"title": title, "year": year, "url": e.get("sourceUrl", "")}
                hits = [("title", "Title", m.find(title))]
            target, kind = papers, "papers"
        else:
            title = e.get("projectTitle") or ""
            keys = _grant_ids(e.get("projectId") or "", title)
            found = next((grant_index[k] for k in keys if k in grant_index), None)
            if found:
                key, item = found
                hits = [(f, lab, m.find(item.get(f) or "")) for f, lab in GRANT_FIELDS]
            else:
                key = "record:" + (keys[0] if keys else eid)
                item = {"title": title, "grantNumber": e.get("projectId", ""), "url": e.get("sourceUrl", "")}
                hits = [("title", "Title", m.find(title)), ("grantNumber", "Award number", m.find(item["grantNumber"]))]
        hits = [x for x in hits if x[2]]
        if ev_spans:
            hits.append(("evidence", "Relationship record", ev_spans))
        if not hits:
            continue
        entry = target.get(key)
        if entry is None:
            f, lab, spans = hits[0]
            text = e.get("evidence", "") if f == "evidence" else item.get(f) or ""
            entry = target[key] = {"key": key, "title": item.get("title", ""), "matchedFields": [],
                                   "associations": [], "snippetField": lab, **snippet(text, spans),
                                   "titleHighlights": [list(x) for x in spans] if f == "title" else []}
            if kind == "papers":
                entry.update({"year": item.get("year", ""), "venue": item.get("venue", ""),
                              "url": item.get("url", ""), "doi": item.get("doi", "")})
            else:
                entry.update({"funder": item.get("funder", ""), "grantNumber": item.get("grantNumber", ""),
                              "url": item.get("url", "")})
        for f, lab, _ in hits:
            if lab not in entry["matchedFields"]:
                entry["matchedFields"].append(lab)
        for pid in (nb.canonical_id(e["from"]), nb.canonical_id(e["to"])):
            c = nb.contact(pid)
            if not c or not nb.is_active("contacts", pid) or any(a["personId"] == pid for a in entry["associations"]):
                continue
            entry["associations"].append({
                "personId": pid, "name": c["name"], **person_info(pid), "basis": "record",
                "recordId": eid, "recordType": e["type"], "recordEvidence": e.get("evidence", ""),
                "sourceUrl": e.get("sourceUrl", ""), "provenance": e.get("provenance", ""),
                "checkedAt": e.get("checkedAt", ""), "role": "", "matchedFields": [lab for _, lab, _ in hits],
            })
            bump(pid, kind)


def _library_links(nb: Notebook, m: Matcher, person_info: Callable[[str], Dict[str, Any]], library: Dict[str, Any],
                   papers: Dict[str, Dict[str, Any]], grants: Dict[str, Dict[str, Any]],
                   paper_index: Dict[str, Tuple[str, Dict[str, Any]]], grant_index: Dict[str, Tuple[str, Dict[str, Any]]],
                   bump: Callable[[str, str], None], result: Dict[str, Any]) -> None:
    """Match the research library's paper and award titles and its OpenAlex topics.

    A library paper that is also on a profile or in a coauthor record joins that entry; each person it belongs to is
    linked with the basis "openalex". Awards join profile grants with the same award number.
    """
    # A plain substring test on each term's first word skips most titles before any regular expression runs.
    firsts = [w.split()[0] for w in (norm_text(t) for t in m.terms) if w]

    def hit(item: Dict[str, Any]) -> bool:
        norm = item.get("_norm")
        if norm is None:
            norm = _norm_with_map(item.get("title") or "")[0]
        return any(f in norm for f in firsts) and any(p.search(norm) for p in m.patterns)

    def link(entry: Dict[str, Any], pids: List[str], basis: str, roles: Dict[str, str]) -> None:
        for pid in pids:
            c = nb.contact(pid)
            if not c or not nb.is_active("contacts", pid) or any(a["personId"] == pid for a in entry["associations"]):
                continue
            entry["associations"].append({"personId": pid, "name": c["name"], **person_info(pid), "basis": basis,
                                          "role": roles.get(pid, ""), "matchedFields": ["Title"]})
            bump(pid, "papers" if basis == "openalex" else "grants")

    for wid, w in (library.get("works") or {}).items():
        if not hit(w):
            continue
        title = w.get("title") or ""
        keys = _paper_ids(title, (w.get("url") or "") + " " + ("https://doi.org/" + w["doi"] if w.get("doi") else ""),
                          w.get("doi", ""))
        found = next((paper_index[k] for k in keys if k in paper_index), None)
        key = found[0] if found else "openalex:" + wid
        entry = papers.get(key)
        if entry is None:
            spans = m.find(title)
            entry = papers[key] = {"key": key, "title": title, "year": str(w.get("year") or ""), "venue": "",
                                   "url": w.get("url", ""), "doi": w.get("doi", ""), "byline": w.get("byline", ""),
                                   "matchedFields": ["Title"], "associations": [], "snippetField": "Title",
                                   **snippet(title, spans), "titleHighlights": [list(x) for x in spans]}
        link(entry, w.get("people") or [], "openalex", {})
    for akey, a in (library.get("awards") or {}).items():
        # A center grant's cores and projects are searched too; a match there links only that core's investigator.
        part_hits = {pid: t for pid, t in (a.get("parts") or {}).items() if t and hit({"title": t})}
        title_hit = hit(a)
        if not title_hit and not part_hits:
            continue
        title = (a.get("title") or "") if title_hit else next(iter(part_hits.values()))
        found = next((grant_index[k] for k in _grant_ids(a.get("number", ""), title) if k in grant_index), None)
        key = found[0] if found else akey
        entry = grants.get(key)
        if entry is None:
            spans = m.find(title)
            entry = grants[key] = {"key": key, "title": title, "funder": a.get("funder", ""),
                                   "grantNumber": a.get("number", ""), "url": a.get("url", ""),
                                   "period": f"{a.get('start', '')} to {a.get('end', '')}",
                                   "matchedFields": ["Title"], "associations": [], "snippetField": "Title",
                                   **snippet(title, spans), "titleHighlights": [list(x) for x in spans]}
        link(entry, list((a.get("roles") or {}).keys()) if title_hit else list(part_hits), a.get("source", "nih"),
             a.get("roles") or {})
    for pid, rec in (library.get("people") or {}).items():
        o = rec.get("openalex") or {}
        topics = (o.get("summary") or {}).get("topics") or []
        c = nb.contact(pid)
        if not topics or not c or not nb.is_active("contacts", pid):
            continue
        if (c.get("researchSummary") or "").startswith("Topics OpenAlex assigns"):
            continue                       # the same topics are already on their record
        text = "Topics OpenAlex assigns to their works: " + "; ".join(f"{t} ({n} works)" for t, n in topics) + "."
        spans = m.find(text)
        if spans:
            from .connectors.openalex import author_url
            result["profiles"].append({"personId": pid, "name": c["name"], "field": "openalexTopics",
                                       "fieldLabel": "OpenAlex topics", **snippet(text, spans),
                                       "sourceUrl": author_url(o["id"]) if o.get("id") else "",
                                       "checkedAt": o.get("summaryAt", ""),
                                       "evidenceNote": "Assigned by OpenAlex from their works.", **person_info(pid)})
            bump(pid, "profiles")


_AND = re.compile(r'\s+AND\s+')


def split_and(query: str) -> List[str]:
    """'ADHD AND "ABCD study"' -> ['ADHD', '"ABCD study"']. AND inside quotes, or in lower case, is a plain word."""
    parts, buf, quoted = [], "", False
    i = 0
    while i < len(query):
        ch = query[i]
        if ch == '"':
            quoted = not quoted
        if not quoted:
            m = _AND.match(query, i)
            if m and buf.strip():
                parts.append(buf.strip())
                buf, i = "", m.end()
                continue
        buf += ch
        i += 1
    if buf.strip():
        parts.append(buf.strip())
    return parts


def search_all(nb: Notebook, query: str, person_info: Callable[[str], Dict[str, Any]],
               alias_groups: Optional[List[List[str]]] = None, limit: int = 200,
               edge_ok: Optional[Callable[[Dict[str, Any]], bool]] = None,
               library: Optional[Dict[str, Any]] = None,
               events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Search with AND: each term is searched on its own, then only people every term matched are kept.

    A person matches a term through anything linked to them (name, profile, paper, grant or note), so "ADHD AND
    ABCD" lists people with work on both, whether or not one paper names both. Each kept paper, grant or note says
    which terms it matched in `matchedTerms`; `allTerms` is true when it matched all of them.
    """
    terms = split_and(query)
    if len(terms) < 2:
        return search(nb, query, person_info, alias_groups, limit, edge_ok, library, events)
    parts = [search(nb, t, person_info, alias_groups, limit * 5, edge_ok, library, events) for t in terms]
    keep = set.intersection(*({p["personId"] for p in r["peopleSummary"]} for r in parts))
    labels = [t.strip('"') for t in terms]
    result: Dict[str, Any] = {"query": query, "andTerms": labels, "terms": [], "aliasesUsed": [],
                              "scope": parts[0]["scope"], "people": [], "profiles": [], "papers": [], "grants": [],
                              "notes": [], "events": []}
    for r in parts:
        result["terms"] += [t for t in r["terms"] if t not in result["terms"]]
        result["aliasesUsed"] += [t for t in r["aliasesUsed"] if t not in result["aliasesUsed"]]

    def ident(k, x):
        if k == "notes":
            return (x["personId"], x.get("kind"), x.get("meetingId", ""), x.get("field"))
        if k == "events":
            return (x["personId"], x["conferenceId"], x.get("role"), x.get("title"))
        return (x["personId"], x.get("field"), x.get("text"))

    flat: Dict[str, Dict[Any, Dict[str, Any]]] = {k: {} for k in ("people", "profiles", "notes", "events")}
    items: Dict[str, Dict[str, Dict[str, Any]]] = {"papers": {}, "grants": {}}
    for label, r in zip(labels, parts):
        for k in flat:
            for x in r[k]:
                if x["personId"] not in keep:
                    continue
                e = flat[k].setdefault(ident(k, x), dict(x, matchedTerms=[]))
                if label not in e["matchedTerms"]:
                    e["matchedTerms"].append(label)
                    if e is not x and x.get("highlights") and e.get("text") == x.get("text"):
                        e["highlights"] = sorted(e.get("highlights", []) + x["highlights"])
        for k in items:
            for x in r[k]:
                assoc = [a for a in x["associations"] if a["personId"] in keep]
                if not assoc:
                    continue
                e = items[k].get(x["key"])
                if e is None:
                    e = items[k][x["key"]] = dict(x, associations=[], matchedTerms=[])
                elif x.get("titleHighlights"):
                    e["titleHighlights"] = sorted(e.get("titleHighlights", []) + x["titleHighlights"])
                e["matchedTerms"].append(label)
                for a in assoc:
                    if not any(o["personId"] == a["personId"] for o in e["associations"]):
                        e["associations"].append(a)
    allset = len(labels)
    for k in flat:
        result[k] = list(flat[k].values())
    for k in items:
        result[k] = list(items[k].values())
    for k in ("profiles", "notes", "events", "papers", "grants"):
        for e in result[k]:
            e["allTerms"] = len(set(e["matchedTerms"])) == allset
            for f in ("highlights", "titleHighlights"):
                if e.get(f):
                    e[f] = _merge_spans(e[f])

    def closeness(item):
        d = item.get("degree")
        return (0 if item.get("shown") else 1, d if d is not None else 99, norm_text(item.get("name", "")))

    for k in ("people", "profiles", "notes", "events"):
        result[k].sort(key=lambda x: (0 if x.get("allTerms") else 1,) + closeness(x))
    for k in ("papers", "grants"):
        for e in result[k]:
            e["associations"].sort(key=closeness)
            e["shownAssociations"] = sum(1 for a in e["associations"] if a.get("shown"))
            ds = [a["degree"] for a in e["associations"] if a.get("degree") is not None]
            e["closestDegree"] = min(ds) if ds else None
        result[k].sort(key=lambda e: (0 if e["allTerms"] else 1, 0 if e["shownAssociations"] else 1,
                                      e["closestDegree"] if e["closestDegree"] is not None else 99,
                                      norm_text(e.get("title", ""))))
    per: Dict[str, Dict[str, int]] = {pid: {"people": 0, "profiles": 0, "papers": 0, "grants": 0, "notes": 0,
                                            "events": 0} for pid in keep}
    for k in ("people", "profiles", "notes", "events"):
        for x in result[k]:
            per[x["personId"]][k] += 1
    for k in ("papers", "grants"):
        for e in result[k]:
            for a in e["associations"]:
                per[a["personId"]][k] += 1
    summary = []
    for p in parts[0]["peopleSummary"]:
        if p["personId"] in keep:
            summary.append(dict(p, **per[p["personId"]]))
    summary.sort(key=closeness)
    result["peopleSummary"] = summary
    result["counts"] = {
        "people": len(result["people"]), "profiles": len(result["profiles"]), "papers": len(result["papers"]),
        "paperAssociations": sum(len(e["associations"]) for e in result["papers"]),
        "grants": len(result["grants"]), "grantAssociations": sum(len(e["associations"]) for e in result["grants"]),
        "notes": len(result["notes"]), "events": len(result["events"]),
        "allTerms": sum(1 for k in ("papers", "grants", "notes", "profiles", "events") for e in result[k] if e["allTerms"]),
    }
    for k in ("people", "profiles", "notes"):
        result[k] = result[k][:limit]
    result["events"] = result["events"][:limit * 5]
    result["papers"] = result["papers"][:limit * 5]
    result["grants"] = result["grants"][:limit * 5]
    return result


def _merge_spans(spans: List[List[int]]) -> List[List[int]]:
    out: List[List[int]] = []
    for s, e in sorted([list(x) for x in spans]):
        if out and s <= out[-1][1]:
            out[-1][1] = max(e, out[-1][1])
        else:
            out.append([s, e])
    return out
