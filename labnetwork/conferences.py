"""Conference programs: who in the network is on a meeting's program, and what they present.

A program is imported from a CSV or JSON file (one row per appearance: a speaker, chair, discussant or poster
author) and kept in `workspace/conferences.json`, beside the research library. Like the library it is derived from
public pages and can be rebuilt by importing the files again; it is not part of the change log, and it changes no
person's record. Names are matched to the notebook each time the file is read, so people added later are found.

The lab keeps a list of the conferences it follows (`series`: short name, full name, website). Programs belong to a
series by its `id`; only the series on the list show in the app. Taking a conference off the list keeps its
programs in the file, so putting it back restores them. A new lab is asked for its list when it is set up.

A program lists who presents, chairs or discusses, not everyone who attends; the app says "on the program".

Matching, from strongest to weakest:
  name and institution  the same full name (first given name and family name) and a matching institution
  name                  the only person in the notebook with that full name; the program gives no institution, or
                        the notebook has none for them, or it differs (people move)
An initial-only name ("J. Rivera") matches only with a matching institution and a single candidate. A name that
fits several people and no institution decides between them is left unmatched and listed as ambiguous.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .model import Notebook, norm_text
from .store import Workspace, today

FILE = "conferences.json"
# Co-authors are listed on abstracts whether or not they came; every other role puts the person at the meeting.
COAUTHOR = "Author"
ROLES = ("Chair", "Co-chair", "Discussant", "Presenter", "Speaker", "Poster", "Panelist", "Moderator", "Author",
         "Attendee")
COLUMNS = {"name": ("name", "person", "presenter", "speaker", "author"),
           "institution": ("institution", "affiliation", "organization", "organisation"),
           "role": ("role", "type"),
           "title": ("title", "talk", "presentation", "abstract title", "paper"),
           "session": ("session", "symposium", "session title", "track"),
           "when": ("when", "date", "time", "day"),
           "sourceUrl": ("sourceurl", "source", "url", "link")}


def path(ws: Workspace) -> str:
    return os.path.join(ws.path, FILE)


def load(ws: Workspace) -> Dict[str, Any]:
    p = path(ws)
    if not os.path.isfile(p):
        return {"version": 2, "series": [], "conferences": []}
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("conferences", [])
    if "series" not in data:              # version 1 had programs only: each program's short name becomes a series
        data["series"] = []
        for c in data["conferences"]:
            c.setdefault("series", slug(c.get("short") or c.get("name") or ""))
            if not any(x["id"] == c["series"] for x in data["series"]):
                data["series"].append({"id": c["series"], "short": c.get("short") or c.get("name", ""),
                                       "name": c.get("name", ""), "url": "", "addedAt": c.get("addedAt", ""),
                                       "addedBy": c.get("addedBy", "")})
        data["version"] = 2
    return data


def series_add(ws: Workspace, short: str, name: str = "", url: str = "", frequency: str = "",
               actor: str = "") -> Dict[str, Any]:
    """Put a conference on the lab's list (or update its name and website). Returns the series."""
    short = re.sub(r"\s+", " ", short or "").strip()
    if not short:
        raise ValueError("Give the conference's short name, such as AACAP")
    if url and not re.match(r"https?://", url):
        raise ValueError("The website must start with http:// or https://")
    data = load(ws)
    sid = slug(short)
    cur = next((x for x in data["series"] if x["id"] == sid), None)
    if cur is None:
        cur = {"id": sid, "short": short, "addedAt": today(), "addedBy": actor}
        data["series"].append(cur)
    cur.update({"short": short, "name": (name or cur.get("name") or short).strip(), "url": url or cur.get("url", ""),
                "frequency": frequency or cur.get("frequency", "")})
    data["series"].sort(key=lambda x: x["short"].lower())
    save(ws, data)
    return cur


def series_remove(ws: Workspace, sid: str) -> int:
    """Take a conference off the list. Its programs stay in the file; returns how many."""
    data = load(ws)
    if not any(x["id"] == sid for x in data["series"]):
        raise KeyError(sid)
    data["series"] = [x for x in data["series"] if x["id"] != sid]
    save(ws, data)
    return sum(1 for c in data["conferences"] if c.get("series") == sid)


def save(ws: Workspace, data: Dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=ws.path, prefix=".conferences-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path(ws))


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm_text(text)).strip("-")[:60] or "conference"


_DEGREES = {"phd", "md", "mph", "ms", "msc", "ma", "ba", "bs", "bsc", "do", "rn", "lcsw", "psyd", "edd", "jd", "mba",
            "mpa", "msw", "lmft", "lpc", "abpp", "facp", "dphil", "frcpc", "mbbs", "mrcpsych", "jr", "sr", "ii", "iii",
            "fapa", "abpn", "mc", "fs", "usa", "usaf", "usn", "usmc", "uspha", "phs", "ret", "np", "pa", "dnp", "aprn",
            "pmhnp", "faacap", "dfapa", "dfaacap", "facs", "lp", "lcpc", "bcba", "otr", "pt", "mshs", "mscr", "scm"}
_RANKS = {"ltc", "lt", "col", "maj", "capt", "cpt", "cdr", "lcdr", "gen", "dr", "prof", "professor", "mr", "ms", "mrs",
          "sgt", "msgt", "cmdr", "brig", "adm", "radm", "ens"}


def clean_name(name: str) -> str:
    """'Rivera, Maria L.' -> 'Maria L. Rivera'; 'Maria Rivera, PhD' -> 'Maria Rivera'."""
    degree = lambda part: all(norm_text(w.replace(".", "")) in _DEGREES for w in part.split())
    parts = [p.strip() for p in re.sub(r"\s+", " ", name or "").split(",")]
    parts = parts[:1] + [p for p in parts[1:] if p and not degree(p) and norm_text(p.replace(".", "")) not in _RANKS]
    # Titles and military ranks before the name: 'LTC Dana Ortiz', 'Dr. Jane Doe'.
    words = parts[0].split()
    while len(words) > 2 and norm_text(words[0].replace(".", "")) in _RANKS:
        words = words[1:]
    parts[0] = " ".join(words)
    if len(parts) == 2 and parts[0] and parts[1]:
        return f"{parts[1]} {parts[0]}"
    return " ".join(p for p in parts if p)


def read_rows(file_path: str) -> List[Dict[str, str]]:
    """Rows of a program file: CSV with a header row (column names as in COLUMNS, any case), or JSON (a list of
    objects with those keys, or {"appearances": [...]})."""
    with open(file_path, "r", encoding="utf-8-sig") as f:
        text = f.read()
    if file_path.lower().endswith(".json"):
        data = json.loads(text)
        raw = data.get("appearances", []) if isinstance(data, dict) else data
    else:
        raw = list(csv.DictReader(io.StringIO(text)))
    rows = []
    for r in raw:
        norm = {re.sub(r"[\s_-]+", " ", str(k or "")).strip().lower(): str(v or "").strip() for k, v in r.items()}
        row = {}
        for field, names in COLUMNS.items():
            row[field] = next((norm[n] for n in names if norm.get(n)), "")
        row["name"] = clean_name(row["name"])
        if row["name"]:
            rows.append(row)
    return rows


def add(ws: Workspace, *, name: str, year: int, rows: List[Dict[str, str]], short: str = "", dates: str = "",
        location: str = "", url: str = "", note: str = "", actor: str = "", conf_id: str = "",
        series: str = "") -> Dict[str, Any]:
    """Add a conference's program, or replace it when one with the same ID is already there. The program joins the
    series `series` (default: from its short name), which is put on the lab's list if it is not there."""
    if not rows:
        raise ValueError("The program file has no rows with a name")
    if url and not re.match(r"https?://", url):
        raise ValueError("The program URL must start with http:// or https://")
    sid = slug(series or short or name)
    cid = conf_id or slug(f"{short or name} {year}")
    seen, appearances = set(), []
    for r in rows:
        key = (norm_text(r["name"]), norm_text(r.get("title", "")), norm_text(r.get("role", "")))
        if key in seen:
            continue
        seen.add(key)
        appearances.append({k: r.get(k, "") for k in COLUMNS})
    rec = {"id": cid, "series": sid, "name": name, "short": short or name, "year": int(year), "dates": dates, "location": location,
           "url": url, "note": note, "addedAt": today(), "addedBy": actor, "appearances": appearances}
    data = load(ws)
    if not any(x["id"] == sid for x in data["series"]):
        data["series"].append({"id": sid, "short": short or name, "name": name, "url": "", "addedAt": today(),
                               "addedBy": actor})
        data["series"].sort(key=lambda x: x["short"].lower())
    replaced = any(c["id"] == cid for c in data["conferences"])
    data["conferences"] = [c for c in data["conferences"] if c["id"] != cid] + [rec]
    data["conferences"].sort(key=lambda c: (-int(c.get("year") or 0), c["name"]))
    save(ws, data)
    return {"id": cid, "appearances": len(appearances), "replaced": replaced}


def remove(ws: Workspace, cid: str) -> bool:
    data = load(ws)
    keep = [c for c in data["conferences"] if c["id"] != cid]
    if len(keep) == len(data["conferences"]):
        return False
    data["conferences"] = keep
    save(ws, data)
    return True


# --------------------------------------------------------------------------- matching

class Matcher:
    """Matches program names to the notebook's active people."""

    def __init__(self, nb: Notebook):
        from .expand import family_key
        self.nb = nb
        self.by_family: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
        for pid, c in nb.active("contacts"):
            self.by_family.setdefault(family_key(c["name"]), []).append((pid, c))

    def match(self, name: str, institution: str = "") -> Dict[str, Any]:
        from .expand import _inst_match, family_key, name_score
        cands = self.by_family.get(family_key(name), [])
        scored = [(name_score(name, c["name"]), pid, c) for pid, c in cands]
        full = [(pid, c) for s, pid, c in scored if s == 2]
        partial = [(pid, c) for s, pid, c in scored if s == 1]
        # Either value may be the longer one ("Department of Psychiatry, Lakeside University" and "Lakeside University").
        inst = lambda c: bool(institution) and (_inst_match(institution, c.get("institution", "")) or
                                                _inst_match(c.get("institution", ""), institution))
        if len(full) == 1:
            pid, c = full[0]
            return {"person": pid, "basis": "name and institution" if inst(c) else "name"}
        if len(full) > 1:
            at = [pid for pid, c in full if inst(c)]
            if len(at) == 1:
                return {"person": at[0], "basis": "name and institution"}
            return {"person": None, "basis": "ambiguous", "candidates": [pid for pid, _ in full]}
        at = [pid for pid, c in partial if inst(c)]
        if len(at) == 1:
            return {"person": at[0], "basis": "initials and institution"}
        return {"person": None, "basis": "none"}


def label(conf: Dict[str, Any]) -> str:
    short = conf.get("short") or conf.get("name") or ""
    year = str(conf.get("year") or "")
    return short if year in short else f"{short} {year}".strip()


def matched(nb: Notebook, data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Each program of a conference on the list, with its appearances matched to people (`person`, `basis`) and
    counts."""
    m = Matcher(nb)
    listed = {x["id"] for x in data.get("series", [])}
    out = []
    for conf in data.get("conferences", []):
        if conf.get("series") not in listed:
            continue
        apps = []
        for a in conf.get("appearances", []):
            apps.append({**a, **m.match(a["name"], a.get("institution", ""))})
        people = {a["person"] for a in apps if a["person"]}
        out.append({**{k: v for k, v in conf.items() if k != "appearances"}, "label": label(conf),
                    "appearances": apps, "count": len(apps), "names": len({norm_text(a["name"]) for a in apps}),
                    "inNetwork": len(people),
                    "ambiguous": sum(1 for a in apps if a["basis"] == "ambiguous")})
    return out


def items(confs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One searchable item per matched appearance; `text` is what search reads."""
    out = []
    for conf in confs:
        for a in conf["appearances"]:
            if not a["person"]:
                continue
            text = " · ".join(x for x in (f"{label(conf)} ({conf.get('name')})" if conf.get("name") != label(conf)
                                          else label(conf), a.get("role"), a.get("title"), a.get("session")) if x)
            out.append({"personId": a["person"], "conferenceId": conf["id"], "series": conf.get("series", ""),
                        "conference": label(conf),
                        "conferenceName": conf.get("name", ""), "year": conf.get("year"), "dates": conf.get("dates", ""),
                        "location": conf.get("location", ""), "role": a.get("role", ""), "title": a.get("title", ""),
                        "session": a.get("session", ""), "when": a.get("when", ""),
                        "sourceUrl": a.get("sourceUrl") or conf.get("url", ""), "programName": a["name"],
                        "programInstitution": a.get("institution", ""), "basis": a["basis"], "text": text})
    return out


def breakdown(confs: List[Dict[str, Any]], sid: str) -> Dict[str, Any]:
    """One conference across its imported years: each person in the network on its programs (years, roles, number
    of appearances, latest talk), and the names on several years' programs who are not in the network."""
    progs = sorted((c for c in confs if c.get("series") == sid), key=lambda c: c["year"])
    years = [c["year"] for c in progs]
    people: Dict[str, Dict[str, Any]] = {}
    outside: Dict[str, Dict[str, Any]] = {}
    for c in progs:
        for a in c["appearances"]:
            if a["person"]:
                p = people.setdefault(a["person"], {"personId": a["person"], "years": [], "presentingYears": [],
                                                    "roles": {}, "appearances": 0, "latest": None})
                key = None
            else:
                key = name_key_loose(a["name"])
                p = outside.setdefault(key, {"name": a["name"], "institutions": [], "years": [], "presentingYears": [],
                                             "roles": {}, "appearances": 0, "latest": None,
                                             "ambiguous": a["basis"] == "ambiguous"})
                if a.get("institution") and a["institution"] not in p["institutions"]:
                    p["institutions"].append(a["institution"])
            if c["year"] not in p["years"]:
                p["years"].append(c["year"])
            role = a.get("role") or "On the program"
            if role != COAUTHOR and c["year"] not in p["presentingYears"]:
                p["presentingYears"].append(c["year"])
            p["roles"][role] = p["roles"].get(role, 0) + 1
            p["appearances"] += 1
            if a.get("title") and (role != COAUTHOR or not p["latest"] or p["latest"]["role"] == COAUTHOR):
                p["latest"] = {"title": a["title"], "year": c["year"], "role": role, "session": a.get("session", ""),
                               "sourceUrl": a.get("sourceUrl") or c.get("url", "")}
    rank = lambda p: (-len(p["presentingYears"]), -len(p["years"]), -p["appearances"])
    return {"series": sid, "years": years,
            "programs": [{k: v for k, v in c.items() if k != "appearances"} for c in progs],
            "people": sorted(people.values(), key=rank),
            "outside": sorted((p for p in outside.values() if len(p["presentingYears"]) >= 2 or
                               (p["presentingYears"] and p["appearances"] >= 3)),
                              key=lambda p: rank(p) + (p["name"],))[:200],
            "outsideCount": len(outside)}


def name_key_loose(name: str) -> str:
    """'Jane Q. Doe' and 'Jane Doe' -> 'jane doe': first given name and family name, for counting the same name
    across years."""
    from .expand import name_parts
    given, family = name_parts(name)
    first = next((g for g in given if len(g) > 1), given[0] if given else "")
    return f"{first} {family}".strip()
