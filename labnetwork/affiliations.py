"""Institutions, departments and labs of the people near the lab, inferred from their records.

Each grouping says where it came from:
  Institution   the institution on the person's record (the first one when several are listed)
  Department    read from the position title ("Professor of Psychology", "Department of Psychiatry")
  Lab           the lab named on the person's record, or a current lab roster that lists them
A lab stays whole, in the department its faculty's titles name. Former lab memberships are listed with the
person but do not place them. Working at the same institution is never counted as a relationship; the
relationship counts here are recorded relationships between people who happen to be there.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .graph import TYPE_GROUP_OF
from .model import norm_text

_UNIT = r"(?:Department|Dept\.?|School|College|Division|Institute|Program|Programme|Center|Centre)"
_DEPT_PATTERNS = [
    re.compile(r"\b(?:Department|Dept\.?)\s+of\s+(?P<d>[^,;()]+)", re.I),
    re.compile(r"(?P<d>\b(?:School|College|Division|Institute|Program|Programme|Center|Centre)\s+(?:of|for|in|on)\s+[^,;()]+)", re.I),
    re.compile(r"\bProfessor(?:\s+Emerit(?:us|a))?\s+of\s+(?P<d>[^,;()]+)", re.I),
    re.compile(r"\b(?:Lecturer|Instructor|Scientist|Fellow|Researcher|Student|Candidate|Scholar|Chair)\s+(?:in|of)\s+"
               r"(?P<d>[^,;()]+)", re.I),
    re.compile(r",\s*(?P<d>[^,;()]+?\s+" + _UNIT + r")\b", re.I),
    re.compile(r",\s*(?P<d>[^,;()]*\b(?:Psychology|Psychiatry|Pediatrics|Neuroscience|Neurology|Medicine|Epidemiology|"
               r"Sociology|Economics|Education|Public Health|Biostatistics|Genetics|Human Development|"
               r"Child Development|Social Work|Nursing|Statistics|Behavioral Science)[^,;()]*)", re.I),
]
_CUT = re.compile(r"\s+(?:and|&)\s+(?=(?:the\s+)?(?:Institute|Center|Centre|School|Department|Program)\b)|"
                  r"\s+[–—-]\s+|\s+at\s+|\s*/\s*", re.I)
_INST_SUFFIX = re.compile(r",?\s*(?:twin cities|main campus|system)$", re.I)
# "Rivera Lab public roster", "The BEAR Lab team": a named lab, group or center followed by roster or team.
_ROSTER = re.compile(r"^(?:the\s+)?(?P<lab>.*\b(?:lab|laboratory|group|center|centre|institute|program)\b.*?)"
                     r"\s+(?:public\s+)?(?:roster|team)$", re.I)
_FACULTY = re.compile(r"\bprofessor\b|\bfaculty\b|\bprincipal investigator\b|\blecturer\b", re.I)


def department_of(role: str) -> Optional[str]:
    """The department or unit a position title names, or None. 'Professor of Psychology; Chair' -> 'Psychology'."""
    for part in (role or "").split(";"):
        # The unit named first in the title wins ('Professor, Pediatrics and Institute of ...' -> Pediatrics).
        found = []
        for i, rx in enumerate(_DEPT_PATTERNS):
            m = rx.search(part)
            if m:
                d = _CUT.split(m.group("d"))[0].strip(" .,-")
                d = re.sub(r"^(?:the\s+)?(?:(?:Department|Dept\.?)\s+of\s+)?", "", d, flags=re.I)
                if 2 < len(d) <= 90 and not re.match(r"^(?:year|month)\b", d, re.I):
                    found.append((m.start("d"), i, d))
        if found:
            return min(found)[2]
    return None


def institution_key(name: str) -> str:
    return norm_text(_INST_SUFFIX.sub("", (name or "").split(";")[0].strip()))


def dept_key(name: str) -> str:
    k = norm_text((name or "").replace("&", " and "))
    return re.sub(r"^(?:department of|dept of) ", "", k)


def _most_common(names: Iterable[str]) -> str:
    c = Counter(n for n in names if n)
    return c.most_common(1)[0][0] if c else ""


def build(nb, people: Dict[str, Dict[str, Any]], edges: List[Dict[str, Any]], roots: List[str]) -> Dict[str, Any]:
    """Group `people` (id -> card with degree and via) by institution, department and lab.

    `edges` are the relationship records that count under the current filter.
    """
    contacts = nb.records["contacts"]
    # Lab memberships from lab rosters (labmate records whose source is a roster).
    roster_labs: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for e in edges:
        m = _ROSTER.match((e.get("sourceTitle") or "").strip()) if e["type"] == "labmate" else None
        if m:
            for pid in (e["from"], e["to"]):
                roster_labs[pid].append((m.group("lab").strip(), e.get("relationshipStatus") or "unknown"))
    for pid, card in people.items():
        c = contacts[pid]
        inst_raw = (c.get("institution") or "").split(";")[0].strip()
        card["institutionName"] = inst_raw
        card["institutionKey"] = institution_key(inst_raw)
        dep = department_of(c.get("role", ""))
        card["department"] = {"name": dep, "basis": "position title"} if dep else None
        lab_name = (c.get("labName") or c.get("lab") or "").strip()
        status = (c.get("labStatus") or "").strip()
        former = []
        if lab_name and status != "former":
            card["lab"] = {"name": lab_name, "basis": "lab named on their record" +
                           ("" if status in ("current", "") else ", membership status not recorded" if status == "unknown"
                            else f", status {status}")}
        else:
            card["lab"] = None
            if lab_name:
                former.append(lab_name)
        for lab, st in roster_labs.get(pid, []):
            if st == "historical":
                former.append(lab)
            elif card["lab"] is None:
                card["lab"] = {"name": lab, "basis": f"listed on the {lab} roster"}
        card["formerLabs"] = sorted({f for f in former if not card["lab"] or f != card["lab"]["name"]})

    insts: Dict[str, Dict[str, Any]] = {}
    for pid, card in people.items():
        k = card["institutionKey"]
        if not k:
            continue
        insts.setdefault(k, {"key": k, "names": [], "people": []})
        insts[k]["names"].append(card["institutionName"])
        insts[k]["people"].append(pid)
    out = []
    for k, inst in insts.items():
        members = inst["people"]
        # A lab stays whole, in the department its faculty's titles name (or, without faculty, most members').
        lab_dept: Dict[str, Optional[str]] = {}
        by_lab: Dict[str, List[str]] = defaultdict(list)
        for pid in members:
            if people[pid]["lab"]:
                by_lab[norm_text(people[pid]["lab"]["name"])].append(pid)
        for lk, pids in by_lab.items():
            titled = [p for p in pids if people[p]["department"]]
            faculty = [p for p in titled if _FACULTY.search(contacts[p].get("role", ""))]
            lab_dept[lk] = _most_common(people[p]["department"]["name"] for p in (faculty or titled)) or None
        depts: Dict[str, Dict[str, Any]] = {}
        unplaced = []
        for pid in members:
            card = people[pid]
            lk = norm_text(card["lab"]["name"]) if card["lab"] else None
            dname = (lab_dept.get(lk) if lk else None) or (card["department"] or {}).get("name")
            if not dname and not lk:
                unplaced.append(pid)
                continue
            dk = dept_key(dname) if dname else ""
            d = depts.setdefault(dk, {"key": dk, "names": [], "labs": {}, "people": [],
                                      "basis": "position titles" if dname else ""})
            if dname:
                d["names"].append(dname)
            if lk:
                lab = d["labs"].setdefault(lk, {"key": lk, "names": [], "people": [], "bases": Counter()})
                lab["names"].append(card["lab"]["name"])
                lab["people"].append(pid)
                lab["bases"][card["lab"]["basis"]] += 1
            else:
                d["people"].append(pid)
        dept_list = []
        for dk, d in depts.items():
            labs = [{"key": lab["key"], "name": _most_common(lab["names"]), "count": len(lab["people"]),
                     "people": _order(lab["people"], people),
                     "basis": (next(iter(lab["bases"])) if len(lab["bases"]) == 1 else
                               "; ".join(f"{b} ({n})" for b, n in lab["bases"].most_common()))}
                    for lab in d["labs"].values()]
            labs.sort(key=lambda x: (-x["count"], x["name"]))
            count = len(d["people"]) + sum(x["count"] for x in labs)
            dept_list.append({"key": dk, "name": _most_common(d["names"]) or "Department not recorded",
                              "basis": d["basis"] or "lab only", "count": count, "labs": labs,
                              "people": _order(d["people"], people)})
        dept_list.sort(key=lambda x: (x["key"] == "", -x["count"], x["name"]))
        by_degree = Counter(str(people[p]["degree"]) for p in members)
        via = Counter(r for p in members for r in people[p]["via"])
        out.append({"key": k, "name": _most_common(inst["names"]), "count": len(members), "byDegree": dict(by_degree),
                    "via": [{"id": r, "count": via[r]} for r in roots if via.get(r)],
                    "departments": dept_list, "unplaced": _order(unplaced, people)})
    out.sort(key=lambda x: (-x["count"], x["name"]))

    # Recorded relationships within and between institutions.
    within: Dict[str, Counter] = defaultdict(Counter)
    within_people: Dict[str, set] = defaultdict(set)
    between: Dict[Tuple[str, str], Counter] = defaultdict(Counter)
    for e in edges:
        a, b = e["from"], e["to"]
        if a not in people or b not in people:
            continue
        ka, kb = people[a]["institutionKey"], people[b]["institutionKey"]
        if not ka or not kb:
            continue
        g = TYPE_GROUP_OF.get(e["type"], "association")
        if ka == kb:
            within[ka][g] += 1
            within_people[ka].update((a, b))
        else:
            between[tuple(sorted((ka, kb)))][g] += 1
    names = {i["key"]: i["name"] for i in out}
    for inst in out:
        k = inst["key"]
        inst["within"] = {"records": sum(within[k].values()), "byGroup": dict(within[k]), "people": len(within_people[k])}
        conn = [(o, cnt) for pair, cnt in between.items() if k in pair for o in pair if o != k]
        conn.sort(key=lambda x: (-sum(x[1].values()), names.get(x[0], x[0])))
        inst["connected"] = [{"key": o, "name": names.get(o, o), "records": sum(cnt.values()), "byGroup": dict(cnt)}
                             for o, cnt in conn[:15]]
        inst["connectedCount"] = len(conn)
    no_inst = [pid for pid, card in people.items() if not card["institutionKey"]]
    return {"institutions": out, "noInstitution": {"count": len(no_inst), "people": _order(no_inst, people)},
            "people": people}


def _order(pids: List[str], people: Dict[str, Dict[str, Any]]) -> List[str]:
    return sorted(pids, key=lambda p: (people[p]["degree"] if people[p]["degree"] is not None else 9,
                                       norm_text(people[p]["name"])))
