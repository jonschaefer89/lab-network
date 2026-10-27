"""Plain-language descriptions of relationship records and connection paths.

Sentences follow the stored direction and meaning of each record. Tense comes
from relationshipStatus: current -> present, historical -> past, anything else
-> a neutral "is recorded as" form, so no tenure or currentness is implied.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from .graph import ONE_WAY, TYPE_GROUP_OF, TYPE_ORDER, UNSPECIFIED_DIRECTION
from .model import norm_text

TYPE_LABELS = {
    "mentor": "Mentor",
    "postdoc": "Postdoc supervision",
    "committee": "Committee",
    "training": "Training program",
    "coauthor": "Coauthors",
    "grant": "Shared grant",
    "collaborator": "Collaborators",
    "labmate": "Lab or center colleagues",
    "irta_colleague": "NIH IRTA colleagues",
    "met": "Met",
    "event": "Same event",
}

PROVENANCE_LABELS = {
    "public": "Public record",
    "reported": "Reported",
    "firsthand": "Firsthand note",
}

_SENTENCES = {
    # type: (current, historical, neutral); {a} = from, {b} = to
    "mentor": ("{a} mentors {b}", "{a} mentored {b}", "{a} is recorded as a mentor of {b}"),
    "postdoc": ("{a} is a postdoc supervised by {b}", "{a} was a postdoc supervised by {b}",
                "{a} is recorded as a postdoc supervised by {b}"),
    "committee": ("{a} serves on {b}'s committee", "{a} served on {b}'s committee",
                  "{a} is recorded as a committee member for {b}"),
    "training": ("{a} and {b} have a training-program connection",
                 "{a} and {b} had a training-program connection",
                 "{a} and {b} have a recorded training-program connection"),
    "coauthor": ("{a} and {b} coauthored a publication",) * 3,
    "grant": ("{a} and {b} are named on the same grant", "{a} and {b} were named on the same grant",
              "{a} and {b} are named on the same grant record"),
    "collaborator": ("{a} and {b} collaborate", "{a} and {b} collaborated",
                     "{a} and {b} have a documented collaboration"),
    "labmate": ("{a} and {b} are lab or center colleagues", "{a} and {b} were lab or center colleagues",
                "{a} and {b} have a documented lab or center association"),
    "irta_colleague": ("{a} and {b} are NIH IRTAs together", "{a} and {b} were NIH IRTAs together",
                       "{a} and {b} are recorded as NIH IRTA colleagues"),
    "met": ("{a} met {b}",) * 3,
    "event": ("{a} and {b} took part in the same event",) * 3,
}

# The other person's role, seen from one participant's profile:
# (when the profile person is the record's `from`, when they are its `to`).
_PERSPECTIVE = {
    "mentor": ("Mentee", "Mentor"),
    "postdoc": ("Postdoc supervisor", "Postdoc"),
    "committee": ("Student on their committee", "Committee member"),
    "training": ("Training-program connection", "Training-program connection"),
    "coauthor": ("Coauthor", "Coauthor"),
    "grant": ("Shared grant", "Shared grant"),
    "collaborator": ("Collaborator", "Collaborator"),
    "labmate": ("Lab or center colleague", "Lab or center colleague"),
    "irta_colleague": ("NIH IRTA colleague", "NIH IRTA colleague"),
    "met": ("Met", "Met"),
    "event": ("Same event", "Same event"),
}


# Sections of a person's relationship list, in display order: (type, end of the record the listed person is on,
# label). For one-way types the end tells mentors from mentees; mutual types have one section each.
TIE_SECTIONS = [
    ("mentor", "from", "Mentors"),
    ("postdoc", "to", "Postdoc supervisors"),
    ("committee", "from", "Committee members"),
    ("mentor", "to", "Mentees"),
    ("postdoc", "from", "Postdocs"),
    ("committee", "to", "Students whose committees they served on"),
    ("coauthor", None, "Coauthors"),
    ("grant", None, "Shared grants"),
    ("collaborator", None, "Collaborators"),
    ("training", None, "Training programs"),
    ("labmate", None, "Lab or center colleagues"),
    ("irta_colleague", None, "NIH IRTA colleagues"),
    ("met", None, "Met"),
    ("event", None, "Same event"),
]

_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md"}


def tie_section(records: List[Dict[str, Any]], me: str) -> Dict[str, Any]:
    """Place one person's records with `me` in a section, using the first record by display precedence.

    Records the current filter leaves out decide the section only when nothing else is counted.
    """
    pool = [r for r in records if r.get("included", True)] or records
    first = min(pool, key=lambda r: TYPE_ORDER.index(r["type"]) if r["type"] in TYPE_ORDER else 99)
    t = first["type"]
    end = ("to" if first["from"] == me else "from") if t in ONE_WAY else None
    same = [r for r in pool if r["type"] == t and (end is None or (r["to"] if end == "to" else r["from"]) != me)]
    index = next((i for i, (st, se, _) in enumerate(TIE_SECTIONS) if st == t and se == end), len(TIE_SECTIONS))
    label = TIE_SECTIONS[index][2] if index < len(TIE_SECTIONS) else "Other"
    return {"group": TYPE_GROUP_OF.get(t, "association"), "section": index, "sectionLabel": label,
            "sectionRecords": len(same), "latestYear": latest_year(same)}


def latest_year(records: List[Dict[str, Any]]) -> Optional[int]:
    """Most recent year in the records' dates; 9999 for a relationship recorded as current; None if undated."""
    best = None
    for r in records:
        if r.get("relationshipStatus") == "current":
            return 9999
        text = " ".join(str(r.get(k) or "") for k in ("relationshipStart", "relationshipEnd"))
        years = [int(m.group(0)) for m in _YEAR.finditer(text)]
        if not years:
            m = re.search(r"\((19|20)\d{2}\)\s*$", r.get("sourceTitle") or "")
            years = [int(m.group(0)[1:5])] if m else []
        if years:
            best = max(best or 0, max(years))
    return best


def surname_key(name: str) -> Tuple[str, str]:
    """Sort key by family name ('Marcus J. Whitfield Jr.' sorts under W), then the full name."""
    # Split on spaces only, so hyphenated family names stay whole; skip parenthesised nicknames.
    parts = [norm_text(w).replace(" ", "-") for w in (name or "").split() if not w.startswith("(")]
    parts = [w for w in parts if w]
    while len(parts) > 1 and parts[-1] in _SUFFIXES:
        parts.pop()
    return (parts[-1] if parts else "", norm_text(name))


def timing(e: Dict[str, Any]) -> str:
    start = (e.get("relationshipStart") or "").strip()
    end = (e.get("relationshipEnd") or "").strip()
    if start and end:
        return start if start == end else f"{start}–{end}"
    if start:
        return f"from {start}"
    if end:
        return f"until {end}"
    return ""


def sentence(e: Dict[str, Any], names: Dict[str, str]) -> str:
    forms = _SENTENCES.get(e.get("type"), ("{a} and {b} are connected",) * 3)
    status = e.get("relationshipStatus")
    form = forms[0] if status == "current" else forms[1] if status == "historical" else forms[2]
    return form.format(a=names.get(e["from"], e["from"]), b=names.get(e["to"], e["to"]))


def narrative(e: Dict[str, Any], names: Dict[str, str]) -> str:
    text = e.get("relationshipNarrative") or ""
    if not text:
        return ""
    return text.replace("{from}", names.get(e["from"], e["from"])).replace("{to}", names.get(e["to"], e["to"]))


def perspective(e: Dict[str, Any], me: str) -> str:
    """The other person's role in this record, as read from `me`'s profile (e.g. 'Mentor')."""
    side = _PERSPECTIVE.get(e.get("type"), ("Connected", "Connected"))
    return side[0] if e["from"] == me else side[1]


def qualifiers(e: Dict[str, Any]) -> List[str]:
    out = []
    label = (e.get("relationshipLabel") or "").strip()
    if label and label.lower() not in (e.get("type", ""), TYPE_LABELS.get(e.get("type"), "").lower()):
        out.append(label)
    t = timing(e)
    if t:
        out.append(t)
    if e.get("attributionScope") == "joint-lab":
        out.append("joint-lab record; individual supervision not established")
    return out


def edge_summary(e: Dict[str, Any], names: Dict[str, str], origin: str = "packet",
                 walked_from: Optional[str] = None) -> Dict[str, Any]:
    out = {
        "id": e["id"],
        "type": e["type"],
        "typeLabel": TYPE_LABELS.get(e["type"], e["type"]),
        "group": TYPE_GROUP_OF.get(e["type"], "other"),
        "direction": ("one-way" if e["type"] in ONE_WAY else
                      "unspecified" if e["type"] in UNSPECIFIED_DIRECTION else "mutual"),
        "from": e["from"],
        "to": e["to"],
        "fromName": names.get(e["from"], e["from"]),
        "toName": names.get(e["to"], e["to"]),
        "sentence": sentence(e, names),
        "qualifiers": qualifiers(e),
        "label": e.get("relationshipLabel", ""),
        "timing": timing(e),
        "relationshipStart": e.get("relationshipStart", ""),
        "relationshipEnd": e.get("relationshipEnd", ""),
        "relationshipStatus": e.get("relationshipStatus", ""),
        "relationshipStatusNote": e.get("relationshipStatusNote", ""),
        "provenance": e.get("provenance", ""),
        "provenanceLabel": PROVENANCE_LABELS.get(e.get("provenance"), e.get("provenance", "")),
        "confidence": e.get("confidence", ""),
        "reviewStatus": e.get("status", ""),
        "sourceTitle": e.get("sourceTitle", ""),
        "sourceUrl": e.get("sourceUrl", ""),
        "evidence": e.get("evidence", ""),
        "checkedAt": e.get("checkedAt", ""),
        "createdAt": e.get("createdAt", ""),
        "attributionScope": e.get("attributionScope", ""),
        "projectId": e.get("projectId", ""),
        "projectTitle": e.get("projectTitle", ""),
        "narrative": narrative(e, names),
        "narrativeSourceUrl": e.get("relationshipNarrativeSourceUrl", ""),
        "narrativeCheckedAt": e.get("relationshipNarrativeCheckedAt", ""),
        "origin": origin,
    }
    if walked_from is not None:
        out["walkedAlongStoredDirection"] = (e["from"] == walked_from)
    return out


def step_summary(records: List[Dict[str, Any]]) -> str:
    """One phrase for all records between two people, e.g. 'coauthors (3 records); NIH IRTAs together'."""
    by_type: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for r in sorted(records, key=lambda r: TYPE_ORDER.index(r["type"]) if r["type"] in TYPE_ORDER else 99):
        by_type.setdefault(r["type"], []).append(r)
    parts = []
    for t, recs in by_type.items():
        label = TYPE_LABELS.get(t, t).lower()
        if t == "irta_colleague":
            label = "NIH IRTA colleagues"
        if t in ONE_WAY:
            # One-way records keep their direction, e.g. "mentor of X".
            dirs = sorted({f"{r['fromName']} → {r['toName']}" for r in recs})
            label = f"{label} ({'; '.join(dirs)})"
        parts.append(label + (f" ×{len(recs)}" if len(recs) > 1 else ""))
    return "; ".join(parts)


def describe_path_result(result: Dict[str, Any], graph, names: Dict[str, str], mode: str,
                         origin_of) -> Dict[str, Any]:
    """Attach step-by-step evidence to a Graph.shortest_paths() result."""
    out = dict(result)
    paths = []
    for p in result.get("paths", []):
        steps = []
        for st in graph.path_steps(p, mode):
            ordered = sorted(st["records"], key=lambda e: (TYPE_ORDER.index(e["type"]) if e["type"] in TYPE_ORDER else 99,
                                                           e.get("relationshipStart") or "", e["id"]))
            recs = [edge_summary(e, names, origin_of(e["id"]), walked_from=st["from"]) for e in ordered]
            steps.append({"from": st["from"], "to": st["to"], "fromName": names.get(st["from"], st["from"]),
                          "toName": names.get(st["to"], st["to"]), "records": recs,
                          "summary": step_summary(recs)})
        chain = names.get(p[0], p[0])
        for st in steps:
            chain += f" → [{st['summary']}] → {st['toName']}"
        paths.append({"people": p, "steps": steps, "text": chain})
    out["paths"] = paths
    out["mode"] = mode
    return out


def degree_label(d: Optional[int]) -> str:
    if d is None:
        return "No recorded path"
    if d == 0:
        return "Lab member"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(d if d < 20 else d % 10, "th")
    return f"{d}{suffix} degree"


def lab_connection_headline(d: Optional[int], paths: List[Dict[str, Any]], path_count: int,
                            names: Dict[str, str]) -> str:
    if d is None:
        return "No recorded path to the lab under the current relationship filters."
    if d == 0:
        return "Member of the lab."
    if d == 1:
        roots = OrderedDict()
        for p in paths:
            roots[p["people"][0]] = p["steps"][0]["summary"]
        parts = [f"{names.get(r, r)} ({s})" for r, s in roots.items()]
        return "Directly connected to " + _join(parts) + "."
    bridges = OrderedDict()
    for p in paths:
        bridges.setdefault(p["people"][-2], []).append(p)
    names_list = [names.get(b, b) for b in bridges]
    more = path_count - len(paths)
    head = f"{d} steps from the lab, through " + _join(names_list)
    if more > 0:
        head += f" (and {more} more equally short paths)"
    return head + "."


def _join(items: List[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"
