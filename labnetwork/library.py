"""A research library for the people near the lab: OpenAlex publication records and recent papers, NIH and NSF awards.

The library is derived data. It lives in workspace/library.json, apart from the change log, so it can be rebuilt or
refreshed without touching any curated record. A paper or award in it is linked to the people the source names; none
of it is a relationship between people.

Who is looked up: everyone within the workspace's degree limit (two by default) who is a lab member, has a confirmed
doctorate, has a research relationship, or already has an OpenAlex link. The people `expand` adds to the second degree
can number in the thousands, so the library looks up one of them only once their doctorate is confirmed and their
institution and position are known; people the library already holds stay.

How people are identified:
  OpenAlex  the links on their record or found by `expand`; else the author on a recent paper of someone already linked
            whose name matches theirs, when the two have a recorded relationship; else, for people with a confirmed
            doctorate, the only OpenAlex author with their name at an institution they are recorded at. OpenAlex often
            splits one researcher across several author records. Candidate records that share an ORCID iD count as one
            author, every OpenAlex author link on a person's record is theirs, and once a month the library adds the
            other records that carry a linked person's ORCID iD, so a person can hold several records. A record a
            person marked as not someone's (an identity note in the change log) is never theirs, whatever links it.
  NIH       the RePORTER profile ID on their record (from the lab or a public award record); else the principal
            investigator with their name at an institution they are recorded at (their record's institution or one
            OpenAlex lists for them). A name that matches PIs at several such institutions is held back. Once a PI is
            matched, every award under that NIH profile is theirs.
  NSF       each award whose PI has their name and was made to an institution they are recorded at.

Someone no method identifies is "unmatched"; each weekly update tries again. "pending" means a run has not reached
them yet (the day's OpenAlex allowance ran out, or they wait for one of the limited name searches).

What is kept for each person: their OpenAlex summary (works, works since the start year, citations, h-index, main
topics; for several records, the counts added together), their most recent works since the start year with at most 30
authors (up to 50, over all their records), and their NIH and NSF awards. A weekly update adds what is new and writes
a digest of it.
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import re
import unicodedata
from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .connectors import nsf
from .connectors import openalex as oa
from .connectors import reporter as rp
from .connectors.http import BudgetReached, Client, HttpError
from .expand import _authorships, _family_ok, _works, default_cache, family_key, name_parts, name_score, surname_tokens
from .graph import TieFilter
from .model import norm_text
from .refresh import _nih_profile, _running_port, openalex_links
from .service import Service
from .store import Workspace, _pid_alive, today, utc_now

LIBRARY = "library.json"
VERSION = 2               # 2: a person can hold several OpenAlex records (`ids`); version 1 kept one (`id`)
STANDARD = {"sinceYear": 2016, "maxAuthors": 30, "perPerson": 50, "lookBackDays": 60}
RESEARCH_TYPES = {"coauthor", "grant", "collaborator"}
SUMMARY_DAYS = 7          # reuse an OpenAlex author record fetched within this many days
ORCID_DAYS = 30           # look for a linked person's other records with their ORCID iD at most this often
SEARCHES_PER_RUN = 25     # name searches (the costly OpenAlex call, $0.001 each) per run without an API key
SEARCHES_PER_RUN_KEY = 250   # with a key, which has its own $1 a day
WORK_FIELDS = "id,doi,ids,title,publication_year,publication_date,authorships"   # works as the library stores them
_GENERIC = {"university", "univ", "college", "institute", "institution", "state", "national", "hospital", "school",
            "center", "centre", "medical", "medicine", "health", "research", "foundation", "the", "of", "at", "and",
            "in", "for", "department", "dept", "inc", "llc", "campus", "system", "systems", "sciences", "science",
            "faculty", "division", "program", "clinic", "clinical", "services", "usa", "board", "regents", "trustees"}


class LibraryError(Exception):
    """A library run that cannot start: the demo lab, or another run in progress."""


# --------------------------------------------------------------------------- storage

def empty() -> Dict[str, Any]:
    return {"format": "labnetwork-library", "version": VERSION, "standard": dict(STANDARD), "updatedAt": "", "runs": [],
            "people": {}, "works": {}, "awards": {}}


def library_path(ws: Workspace) -> str:
    return os.path.join(ws.path, LIBRARY)


def load(ws: Workspace) -> Dict[str, Any]:
    p = library_path(ws)
    if not os.path.exists(p):
        return empty()
    with open(p, "r", encoding="utf-8") as f:
        lib = json.load(f)
    for k, v in empty().items():
        lib.setdefault(k, v)
    migrate(lib)
    return lib


def migrate(lib: Dict[str, Any]) -> Dict[str, Any]:
    """Bring a library written by an earlier version up to this one, in place.

    Version 1 kept one OpenAlex record per person (`id`); version 2 also lists every record in `ids`, and keeps `id`
    as the main one, so a version-1 reader still works. Version 1 also decided name searches without counting records
    that share an ORCID iD as one author, so an ambiguous search result loses its check date and is searched again
    (usually from the response cache).
    """
    if int(lib.get("version") or 1) >= VERSION:
        return lib
    for rec in lib["people"].values():
        o = (rec or {}).get("openalex") or {}
        if o.get("id") and not o.get("ids"):
            o["ids"] = [o["id"]]
        if o.get("status") == "ambiguous":
            o.pop("checkedAt", None)
    lib["version"] = VERSION
    return lib


def author_ids(o: Dict[str, Any]) -> List[str]:
    """Every OpenAlex author record of a person's `openalex` entry, the main one first. An entry written by version 1,
    or whose `id` a version-1 run changed since, has only `id` to go on."""
    ids = [i for i in o.get("ids") or [] if i]
    main = o.get("id") or ""
    if main and main not in ids:
        return [main]
    return [main] + [i for i in ids if i != main] if main else ids


def settle(lib: Dict[str, Any]) -> None:
    """Recompute which people each work and award belongs to, in place, and drop the ones that belong to no one."""
    for pid in [p for p, rec in lib["people"].items() if not rec]:
        del lib["people"][pid]
    people_of_work: Dict[str, List[str]] = defaultdict(list)
    for pid, rec in lib["people"].items():
        for wid in (rec.get("openalex") or {}).get("works") or []:
            people_of_work[wid].append(pid)
    for wid, w in list(lib["works"].items()):
        if wid in people_of_work:
            w["people"] = sorted(people_of_work[wid])
        else:
            del lib["works"][wid]
    for key, a in list(lib["awards"].items()):
        a["roles"] = {pid: role for pid, role in (a.get("roles") or {}).items()
                      if key in ((lib["people"].get(pid) or {}).get("nih", {}).get("awards", []) +
                                 (lib["people"].get(pid) or {}).get("nsf", {}).get("awards", []))}
        if not a["roles"]:
            del lib["awards"][key]


def save(ws: Workspace, lib: Dict[str, Any]) -> str:
    """Write atomically, after recomputing which people each work and award belongs to."""
    settle(lib)
    lib["updatedAt"] = utc_now()
    p = library_path(ws)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)
    return p


# --------------------------------------------------------------------------- helpers

def org_tokens(name: str) -> Set[str]:
    return {t for t in norm_text(name).split() if len(t) > 2 and t not in _GENERIC}


def org_match(a: str, b: str) -> bool:
    """Two institution names that share a distinctive word ('Ashdown University', 'ASHDOWN UNIV MED CTR')."""
    return bool(org_tokens(a) & org_tokens(b))


def _orcid(value: str) -> str:
    """'https://orcid.org/0000-0002-1825-009x' -> '0000-0002-1825-009X'."""
    return (value or "").strip().replace("https://orcid.org/", "").replace("http://orcid.org/", "").upper()


def _orcid_groups(items: Iterable[Any], orcid_of: Callable[[Any], str]) -> List[List[Any]]:
    """Candidates grouped by ORCID iD: records that share one are a single researcher whom OpenAlex split across
    several author records. A candidate without an ORCID iD is a group of its own, as are candidates whose ORCID iDs
    differ. Groups keep the order of their first member."""
    groups: List[List[Any]] = []
    by_orcid: Dict[str, List[Any]] = {}
    for it in items:
        k = _orcid(orcid_of(it))
        if k and k in by_orcid:
            by_orcid[k].append(it)
            continue
        groups.append([it])
        if k:
            by_orcid[k] = groups[-1]
    return groups


def _records_note(n: int, orcid: str) -> str:
    return f"{n} OpenAlex records with ORCID {orcid}"


def _spaced(name: str) -> str:
    """Initials as separate words: 'H.L. Brooks' -> 'H. L. Brooks', 'CM Vance' -> 'C M Vance'."""
    words: List[str] = []
    for w in (name or "").replace(".", ". ").split():
        words += list(w) if 2 <= len(w) <= 3 and w.isupper() and not re.search(r"[AEIOUY]", w) else [w]
    return " ".join(words)


def _one_letter_off(a: str, b: str) -> bool:
    """A typo: two neighbouring letters swapped in a name of four letters or more, or one letter added, dropped or
    changed in a name of five or more."""
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 2 and diff[1] == diff[0] + 1 and (a[diff[0]], a[diff[1]]) == (b[diff[1]], b[diff[0]]):
            return len(a) >= 4
        return len(diff) == 1 and len(a) >= 5
    short, long_ = sorted((a, b), key=len)
    return (len(long_) - len(short) == 1 and len(long_) >= 5
            and any(long_[:i] + long_[i + 1:] == short for i in range(len(long_))))


def name_fits(ours: str, theirs: str) -> bool:
    """Whether an OpenAlex record listed under a person's ORCID iD has a name that can be theirs.

    OpenAlex sometimes lists a coauthor's or a namesake's record under a researcher's ORCID iD, and such a record
    nearly always has another name. Besides the library's usual test, this accepts initials written together
    ('H.L. Brooks', 'CM Vance'), the same names in another order ('Pike Albert R', 'Kenneth Lee Rowan' for 'K. Lee
    Rowan'), an initial for the first given name, and a one-letter typo in the first given name or the family name.
    """
    a, b = _spaced(ours), _spaced(theirs)
    if name_score(a, b):
        return True
    ga, fa = name_parts(a)
    gb, fb = name_parts(b)
    if not (ga and fa and gb and fb):
        return False
    first = next((t for t in ga if len(t) > 1), "")
    if first and {fa, first} <= set(gb) | {fb}:
        return True
    if not (_family_ok(fa, fb) or _one_letter_off(fa, fb)):
        return False
    x, y = ga[0].replace("-", ""), gb[0].replace("-", "")
    if len(x) == 1 or len(y) == 1:
        return x[0] == y[0]
    short, long_ = sorted((x, y), key=len)
    return x == y or (len(short) >= 3 and long_.startswith(short)) or _one_letter_off(x, y)


def _institutions(a: Dict[str, Any]) -> List[str]:
    """The institutions an OpenAlex author record lists: its last known ones and its affiliations."""
    names = [i.get("display_name") for i in a.get("last_known_institutions") or []]
    names += [(x.get("institution") or {}).get("display_name") for x in a.get("affiliations") or []]
    return [n for n in names if n]


def _unlike(a: Dict[str, Any], names: List[str], insts: List[str], summary: Dict[str, Any]) -> str:
    """Why a record listed under a person's ORCID iD may be someone else's, or '' when its name can be theirs, it shares
    an institution with them (or one of the two lists none), and, if it has more works than all their records together,
    it shares one of their main research topics. OpenAlex sometimes merges several namesakes into one large record that
    carries the ORCID iD of one of them."""
    if not any(name_fits(n, a.get("display_name") or "") for n in names if n):
        return "another name"
    theirs = _institutions(a)
    if insts and theirs and not any(org_match(x, y) for x in insts for y in theirs):
        return "no institution in common"
    ours = {t for t, _ in summary.get("topics") or []}
    topics = {t.get("display_name") for t in a.get("topics") or []}
    if (a.get("works_count") or 0) > (summary.get("worksCount") or 0) and ours and topics and not ours & topics:
        return "no research topic in common"
    return ""


def _chunks(people: List[str], ids_of: Callable[[str], List[str]], size: int = 50) -> Iterable[List[str]]:
    """People in groups whose OpenAlex records number at most `size` together, so each group is one request."""
    chunk: List[str] = []
    n = 0
    for pid in people:
        k = len(ids_of(pid))
        if chunk and n + k > size:
            yield chunk
            chunk, n = [], 0
        chunk.append(pid)
        n += k
    if chunk:
        yield chunk


def _first_last(name: str) -> Tuple[str, str]:
    """First full given name and family name as written, without accents, for registries that search by name."""
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", name or "") if not unicodedata.combining(ch))
    words = [w.strip(".,") for w in plain.split() if not w.startswith("(")]
    while len(words) > 1 and norm_text(words[-1]) in {"jr", "sr", "ii", "iii", "iv", "phd", "md"}:
        words.pop()
    if len(words) < 2:
        return "", ""
    first = next((w for w in words[:-1] if len(w.strip(".")) > 1), words[0])
    return first, words[-1].split("-")[-1]


def award_name_ok(ours: str, theirs: str) -> bool:
    """Same family name and the same first given name, or a short form of it (Matt and Matthew); an initial alone
    is not enough to give someone an award."""
    if family_key(ours) != family_key(theirs) or not name_score(ours, theirs):
        return False
    g1 = next((g for g in name_parts(ours)[0] if len(g) > 1), "")
    g2 = next((g for g in name_parts(theirs)[0] if len(g) > 1), "")
    return bool(g1 and g2)


def _ago(date: str, days: int) -> str:
    try:
        d = _dt.date.fromisoformat((date or "")[:10])
    except ValueError:
        d = _dt.date.today()
    return (d - _dt.timedelta(days=days)).isoformat()


def _iso(us_date: str) -> str:
    """'07/19/2007' -> '2007-07-19'."""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", us_date or "")
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else (us_date or "")[:10]


def _person_institutions(contact: Dict[str, Any], rec: Dict[str, Any]) -> List[str]:
    names = [x.strip() for x in (contact.get("institution") or "").split(";") if x.strip()]
    if contact.get("primaryInstitution"):
        names.append(contact["primaryInstitution"])
    summary = (rec.get("openalex") or {}).get("summary") or {}
    names += summary.get("institutions") or []
    return names


def waits_for_doctorate_and_position(nb, pid: str, degree: Optional[int], held: Iterable[str]) -> bool:
    """Whether the library leaves someone out for now: `expand` added them to the second degree, the library does not
    hold them yet, and their doctorate is not confirmed or their institution and position are not known. The first run
    after they gain those looks them up."""
    c = nb.records["contacts"][pid]
    return (degree is not None and degree >= 2 and "expand-second-degree" in (c.get("tags") or [])
            and pid not in held and not (nb.doctorate_state(c) == "confirmed" and nb.placement(c)["identified"]))


def _people_in_scope(svc: Service, lib: Optional[Dict[str, Any]] = None) -> List[str]:
    """Lab members and researchers within the degree limit, nearest first, without the people `expand` added who wait
    for a confirmed doctorate and a known position (see waits_for_doctorate_and_position)."""
    nb = svc.nb
    dist, _ = svc.lab_dist(TieFilter())
    limit = svc.degree_limit() or 2
    g = svc.graph(TieFilter())
    held = (lib or {}).get("people") or {}
    out = []
    for pid, c in nb.active("contacts"):
        d = dist.get(pid)
        if d is None or d > limit:
            continue
        types = {e["type"] for o in g.nbrs.get(pid, ()) for e in g.records_between(pid, o)}
        if (nb.is_lab(pid) or nb.doctorate_state(c) == "confirmed" or types & RESEARCH_TYPES
                or openalex_links(nb, pid)) and not waits_for_doctorate_and_position(nb, pid, d, held):
            out.append(pid)
    out.sort(key=lambda p: (dist.get(p, 9), nb.records["contacts"][p]["name"]))
    return out


def _cached_authors(client: Client) -> Dict[str, Tuple[Dict[str, Any], str]]:
    """OpenAlex author records already in the response cache: id -> (record, fetchedAt)."""
    out: Dict[str, Tuple[Dict[str, Any], str]] = {}
    if not client.cache_dir:
        return out
    for f in glob.glob(os.path.join(client.cache_dir, "*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                if "/authors" not in fh.read(300):     # the request key is written first
                    continue
                fh.seek(0)
                cached = json.load(fh)
        except (OSError, ValueError):
            continue
        data = cached.get("data")
        for a in (data.get("results") if isinstance(data, dict) and "results" in data else [data]) or []:
            if isinstance(a, dict) and str(a.get("id", "")).startswith("https://openalex.org/A") and "works_count" in a:
                aid = oa.short_id(a["id"])
                if aid not in out or cached.get("fetchedAt", "") > out[aid][1]:
                    out[aid] = (a, cached.get("fetchedAt", ""))
    return out


def summarize_author(a: Dict[str, Any], since_year: int) -> Dict[str, Any]:
    stats = a.get("summary_stats") or {}
    since = sum(c.get("works_count", 0) for c in a.get("counts_by_year") or [] if (c.get("year") or 0) >= since_year)
    insts, countries = [], set()
    for i in a.get("last_known_institutions") or []:
        if i.get("display_name"):
            insts.append(i["display_name"])
        if i.get("country_code"):
            countries.add(i["country_code"])
    for aff in a.get("affiliations") or []:
        inst = aff.get("institution") or {}
        if inst.get("display_name") and inst["display_name"] not in insts:
            insts.append(inst["display_name"])
        if inst.get("country_code"):
            countries.add(inst["country_code"])
    return {"name": a.get("display_name", ""), "worksCount": a.get("works_count", 0), "worksSince": since,
            "citedBy": a.get("cited_by_count", 0), "hIndex": stats.get("h_index"), "i10": stats.get("i10_index"),
            "topics": [[t.get("display_name", ""), t.get("count", 0)] for t in (a.get("topics") or [])[:6]],
            "institutions": insts[:12], "countries": sorted(countries),
            "orcid": (a.get("orcid") or "").replace("https://orcid.org/", "")}


def combine_summaries(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One summary for a researcher OpenAlex splits across several author records (the main record's first).

    Each work sits under one record, so works, works since the start year, citations and i10 (works cited ten times or
    more) are added together. An h-index cannot be added: the largest record's is kept, and the combined one is at
    least that. Topic counts are added; institutions and countries are merged, the main record's first.
    """
    if len(parts) == 1:
        return parts[0]
    out = dict(parts[0])
    for k in ("worksCount", "worksSince", "citedBy"):
        out[k] = sum(p.get(k) or 0 for p in parts)
    i10 = [p["i10"] for p in parts if p.get("i10") is not None]
    out["i10"] = sum(i10) if i10 else None
    h = [p["hIndex"] for p in parts if p.get("hIndex") is not None]
    out["hIndex"] = max(h) if h else None
    topics: Dict[str, int] = {}
    for p in parts:
        for t, n in p.get("topics") or []:
            topics[t] = topics.get(t, 0) + (n or 0)
    out["topics"] = [[t, n] for t, n in sorted(topics.items(), key=lambda kv: -kv[1])[:6]]
    insts: List[str] = []
    for p in parts:
        insts += [i for i in p.get("institutions") or [] if i not in insts]
    out["institutions"] = insts[:12]
    out["countries"] = sorted({c for p in parts for c in p.get("countries") or []})
    out["orcid"] = next((p["orcid"] for p in parts if p.get("orcid")), "")
    return out


def _work_record(w: Dict[str, Any]) -> Dict[str, Any]:
    names = [nm for _, nm, _ in _authorships(w)]
    byline = ", ".join(names[:3]) + (" et al." if len(names) > 3 else "")
    return {"title": oa.work_title(w)[:500], "year": w.get("publication_year"), "date": w.get("publication_date") or "",
            "doi": (w.get("doi") or "").replace("https://doi.org/", ""), "url": oa.work_link(w),
            "authors": len(w.get("authorships") or []), "byline": byline[:300], "addedAt": today()}


def merge_award(a: Dict[str, Any], records: List[Dict[str, Any]], pid: str) -> None:
    """Fold one person's RePORTER records for an award into the stored award, never losing what is known.

    A lookup may return only some of an award's records: an update asks only for recent additions, and each
    investigator on a center grant sees the records of their own core or project. So the fiscal years are added
    together, the start date is the earliest seen and the end date the latest. The title, institution and link come
    from the award's own record (the one without a subproject), newest year first; a core's or project's record
    fills them only while no newer one is known. A person whose records are all a core's or project's has that part
    kept under `parts`.
    """
    def fy(x: Dict[str, Any]) -> int:
        try:
            return int(x.get("fiscal_year") or 0)
        except (TypeError, ValueError):
            return 0
    old_years = [int(y) for y in a.get("years") or [] if str(y).isdigit()]
    # The year of the record the stored title came from (an award stored before this was kept: its latest year).
    known_fy = int(a.get("fy") or (max(old_years) if old_years else 0)) if a.get("title") else 0
    a["years"] = sorted(set(old_years) | {fy(x) for x in records if fy(x)})
    starts = [str(x.get("project_start_date") or "")[:10] for x in records if x.get("project_start_date")]
    ends = [str(x.get("project_end_date") or "")[:10] for x in records if x.get("project_end_date")]
    a["start"] = min([d for d in [a.get("start", "")] + starts if d] or [""])
    a["end"] = max([d for d in [a.get("end", "")] + ends if d] or [""])
    whole = [x for x in records if not x.get("subproject_id")]
    newest = max(whole or records, key=fy)
    if not a.get("title"):
        better = True
    elif whole:
        better = not a.get("whole") or fy(newest) >= known_fy
    else:
        better = not a.get("whole") and fy(newest) > known_fy
    if better:
        a.update({"title": (newest.get("project_title") or a.get("number", ""))[:500],
                  "ic": (newest.get("agency_ic_admin") or {}).get("abbreviation") or a.get("ic", ""),
                  "activity": newest.get("activity_code") or a.get("activity", ""),
                  "org": (newest.get("organization") or {}).get("org_name") or a.get("org", ""),
                  "url": rp.project_url(newest.get("appl_id")) if newest.get("appl_id") else a.get("url", ""),
                  "fy": fy(newest), "whole": bool(whole)})
    parts = [x for x in records if x.get("subproject_id")]
    if parts and not whole:
        part = max(parts, key=fy)
        a.setdefault("parts", {})[pid] = (part.get("project_title") or "")[:300]
    elif pid in (a.get("parts") or {}) and whole:
        a["parts"].pop(pid)


def award_title(a: Dict[str, Any], pid: str) -> Tuple[str, str]:
    """(title, part) to show for one person's award. A center grant whose own record is known shows its title and
    the person's core or project; one known only from cores' records shows the person's own core as its title."""
    part = (a.get("parts") or {}).get(pid, "")
    if part and not a.get("whole"):
        return part, ""
    return a.get("title", ""), part


# --------------------------------------------------------------------------- identities

def relink(o: Dict[str, Any], ids: List[str], basis: str, group: Optional[Dict[str, Any]] = None) -> None:
    """Link a person's `openalex` entry to their OpenAlex records, the main one first. `group` records the records joined
    because they share an ORCID iD. When the records change, what was read from them is read again: with records added,
    the summary and the paper list (which keeps what it has); with a record dropped, everything, because the stored
    papers may be another researcher's. Either way, the other records with their ORCID iD are looked up again."""
    old = author_ids(o) if o.get("status") == "linked" else []
    o.update({"id": ids[0], "ids": list(ids), "status": "linked", "basis": basis})
    if group:
        o["orcidGroup"] = group
    else:
        o.pop("orcidGroup", None)
    if set(old) == set(ids):
        return
    for k in ("summaryAt", "orcidCheckedAt", "orcidHeld"):
        o.pop(k, None)
    if old and set(old) <= set(ids):
        o["refill"] = True
    else:
        for k in ("works", "worksAt", "datedAt", "summary", "refill"):
            o.pop(k, None)


def link_from_record(o: Dict[str, Any], links: List[str]) -> None:
    """Link a person's `openalex` entry to the OpenAlex records their record links to. Records the library joined because
    they share an ORCID iD stay with the person while their record links to one of them."""
    group = o.get("orcidGroup") or {}
    joined = group.get("ids") or []
    extra = [i for i in joined if i not in links] if set(joined) & set(links) else []
    ids = links + extra
    if o.get("status") == "linked" and author_ids(o) == ids:
        return
    basis = (f"{len(links)} OpenAlex links on their record" if len(links) > 1 else
             "OpenAlex link on their record")
    if extra:
        basis += (f", and {len(extra)} more record{'s' if len(extra) > 1 else ''} with the same ORCID "
                  f"({group.get('orcid', '')})")
    relink(o, ids, basis, group if extra else None)


def drop_not_theirs(lib: Dict[str, Any], marks: Dict[str, Iterable[str]],
                    links_of: Callable[[str], List[str]]) -> Dict[str, List[str]]:
    """Take the OpenAlex records people marked as not theirs out of the library, in place. Returns, for each person who
    held one, the records taken from them.

    A marked record leaves the records held back from the person and the candidates of an ambiguous match; a name search
    that found it is made again. A person who held it starts over, as when the link on their record is replaced: their
    summary and paper list may come from it, so both are read again, and the other records with their ORCID iD are
    looked up again. The records their own record links to (`links_of`, without marked ones) are linked again at once,
    and their paper list is read from OpenAlex, not the response cache (`reread`), whose list for those records may be
    months old; the records held back from them stay listed until that lookup. Anyone else is identified again without
    the marked record. `notTheirs` keeps the marks the library has applied, so a withdrawn mark also brings a fresh look:
    the lookup by ORCID iD and the name search are made again.
    """
    dropped: Dict[str, List[str]] = {}
    for pid, rec in lib["people"].items():
        o = (rec or {}).get("openalex")
        no = set((marks or {}).get(pid) or ())
        if o is None or not (no or o.get("notTheirs")):
            continue
        if set(o.get("notTheirs") or []) - no:          # a mark was withdrawn: look again
            o.pop("orcidCheckedAt", None)
            if o.get("status") in ("ambiguous", "not-found"):
                o.pop("checkedAt", None)
        if no:
            o["notTheirs"] = sorted(no)
        else:
            o.pop("notTheirs", None)
        held = [h for h in o.get("orcidHeld") or [] if h.get("id") not in no]
        if held:
            o["orcidHeld"] = held
        else:
            o.pop("orcidHeld", None)
        if no & set(o.get("candidates") or []):
            o["candidates"] = [c for c in o["candidates"] if c not in no]
            o.pop("checkedAt", None)
        gone = [i for i in author_ids(o) if i in no] if o.get("status") == "linked" else []
        if not gone:
            continue
        for k in [k for k in o if k not in ("notTheirs", "url")]:
            del o[k]
        o["status"] = "unmatched"
        links = links_of(pid)
        if links:
            link_from_record(o, links)
            o["reread"] = True
            if held:
                o["orcidHeld"] = held
        dropped[pid] = gone
    return dropped


# --------------------------------------------------------------------------- the run

class _Run:
    def __init__(self, svc: Service, lib: Dict[str, Any], client: Client, update: bool, log: Callable[[str], None]):
        self.svc, self.lib, self.client, self.update, self.log = svc, lib, client, update, log
        self.nb = svc.nb
        self.contacts = self.nb.records["contacts"]
        self.std = lib["standard"]
        self.since = f"{self.std['sinceYear']}-01-01"
        self.raw_works: Dict[str, List[Dict[str, Any]]] = {}     # person -> works fetched this run
        self.openalex_stopped = ""
        self.new_works: Dict[str, List[str]] = defaultdict(list)
        self.new_awards: Dict[str, List[str]] = defaultdict(list)
        self.counts = defaultdict(int)
        self.searched: Set[str] = set()        # people given a name search this run
        self.first_nih: Set[str] = set()       # people whose NIH awards are read in full for the first time
        runs = lib.get("runs") or []
        # An award RePORTER added since shortly before the previous run is new; NIH posts awards in batches.
        self.nih_cutoff = _ago(str(runs[-1].get("date") or "")[:10] if runs else today(), 14)
        self._cached: Optional[Dict[str, Tuple[Dict[str, Any], str]]] = None

    def rec(self, pid: str) -> Dict[str, Any]:
        return self.lib["people"].setdefault(pid, {})

    def not_theirs(self, pid: str) -> Set[str]:
        """The OpenAlex records a person marked as not this person's."""
        return set(self.nb.openalex_not_theirs(pid))

    def links(self, pid: str) -> List[str]:
        """The OpenAlex records the person's record links to, without the ones marked as not theirs."""
        return openalex_links(self.nb, pid) if pid in self.contacts else []

    def aids(self, pid: str) -> List[str]:
        """A linked person's OpenAlex records, the main one first; none for anyone else."""
        o = (self.lib["people"].get(pid) or {}).get("openalex") or {}
        return author_ids(o) if o.get("status") == "linked" else []

    def taken(self) -> Tuple[Set[str], Set[str]]:
        """OpenAlex records, and the ORCID iDs on them, already linked to someone in the library."""
        ids: Set[str] = set()
        orcids: Set[str] = set()
        for pid in self.lib["people"]:
            o = self.lib["people"][pid].get("openalex") or {}
            if o.get("status") == "linked":
                ids.update(author_ids(o))
                orcids.update(x for x in (_orcid((o.get("summary") or {}).get("orcid", "")),
                                          _orcid((o.get("orcidGroup") or {}).get("orcid", ""))) if x)
        return ids, orcids

    def link(self, pid: str, ids: List[str], basis: str, group: Optional[Dict[str, Any]] = None) -> None:
        """Link a person to their OpenAlex records, the main one first (see relink)."""
        relink(self.rec(pid).setdefault("openalex", {}), ids, basis, group)

    def from_record(self, pid: str, links: List[str]) -> None:
        """Link a person to the OpenAlex records their record links to (see link_from_record)."""
        link_from_record(self.rec(pid).setdefault("openalex", {}), links)

    def cached_authors(self) -> Dict[str, Tuple[Dict[str, Any], str]]:
        """OpenAlex author records in the response cache (read once a run), with those this run has fetched since."""
        if self._cached is None:
            self._cached = _cached_authors(self.client)
        return self._cached

    # ---------------------------------------------------------------- OpenAlex

    def openalex(self, people: List[str]) -> None:
        # A record marked as not someone's leaves them first, whoever linked it; they are identified again without it.
        dropped = drop_not_theirs(self.lib, self.nb.not_theirs, self.links)
        name = lambda p: (self.contacts.get(p) or {}).get("name", p)
        for pid, gone in dropped.items():
            self.log(f"  Marked as not theirs, taken from {name(pid)}: {', '.join(gone)}; their papers are read again")
        for pid in people:                     # the links on a person's record outrank any match the library made
            self.rec(pid).setdefault("openalex", {})
            links = self.links(pid)
            if links:
                self.from_record(pid, links)

        def step(fn, *args):
            # After the allowance runs out, only steps that can use cached responses continue: matching on shared
            # papers, and paper lists already in the cache.
            if self.openalex_stopped and fn not in (self.identify_by_coauthors, self.works):
                return []
            try:
                return fn(*args) or []
            except BudgetReached as exc:
                self.openalex_stopped = str(exc)
                self.log(f"  OpenAlex: {exc}; the next run continues where this one stopped")
                return []
            except HttpError as exc:
                self.openalex_stopped = f"OpenAlex could not be reached ({exc.status or 'network error'})"
                self.log(f"  {self.openalex_stopped}; the next run tries again")
                return []
        unlinked = lambda: [p for p in people if not self.aids(p)]
        step(self.summaries, [p for p in people if self.aids(p)])
        found = step(self.same_orcid, [p for p in people if self.aids(p)])
        step(self.summaries, found)
        found = step(self.identify_by_coauthors, unlinked())
        step(self.summaries, found)
        step(self.works, [p for p in people if self.aids(p)])
        found = step(self.identify_by_coauthors, unlinked())       # again, with the works fetched just now
        step(self.summaries, found)
        step(self.works, found)
        found = step(self.identify_by_search, unlinked())
        step(self.summaries, found)
        step(self.works, found)
        for pid in unlinked():
            o = self.rec(pid).setdefault("openalex", {})
            if o.get("status") in ("ambiguous", "not-found"):
                continue                           # the reason is recorded
            waiting = (self.nb.doctorate_state(self.contacts[pid]) == "confirmed"
                       and pid not in self.searched)    # a name search is still to come
            o["status"] = "pending" if (self.openalex_stopped or waiting) else "unmatched"

    def summaries(self, people: List[str]) -> None:
        cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=SUMMARY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        due = [p for p in people if self.update or not (self.rec(p)["openalex"].get("summaryAt"))]
        if not due:
            return
        cached = self.cached_authors()
        fetch = []
        for pid in due:
            hits = [cached.get(a) for a in self.aids(pid)]
            if hits and all(hit and hit[1] >= cutoff for hit in hits):
                self.set_summary(pid, [hit[0] for hit in hits])
            else:
                fetch.append(pid)
        for chunk in _chunks(fetch, self.aids):
            records = oa.get_authors(self.client, [a for p in chunk for a in self.aids(p)])
            for pid in chunk:
                # A record OpenAlex has since merged into another one is no longer listed; the others still count.
                found = [records[a] for a in self.aids(pid) if a in records]
                if found:
                    self.set_summary(pid, found)
        self.log(f"  OpenAlex summaries: {len(due)} people ({len(due) - len(fetch)} from cached records)")

    def same_orcid(self, people: List[str]) -> List[str]:
        """Add to each linked person the other OpenAlex records that carry their ORCID iD; returns the people who gained
        records.

        OpenAlex often splits one researcher across several records with the same ORCID iD, and the links on a person's
        record, or the library's match, may name only some of them. The records a person holds stay first. OpenAlex also
        lists some coauthors' and namesakes' records under a researcher's ORCID iD, so a record joins only when its name
        can be the person's, it shares an institution with them, and a record larger than all of theirs shares a main
        research topic (see _unlike); the others are kept in `orcidHeld` for a person to decide. A record marked as not
        theirs neither joins nor is held back. A record linked to someone else stays theirs, and an ORCID iD on records
        of two people adds nothing to either: a person sorts that out. Records with another ORCID iD never join. Each
        person is looked up again after ORCID_DAYS days, 50 ORCID iDs to a request.
        """
        recent = _ago(today(), ORCID_DAYS)
        due: Dict[str, str] = {}
        for pid in people:
            o = self.rec(pid)["openalex"]
            orcid = _orcid((o.get("summary") or {}).get("orcid", ""))
            if orcid and (o.get("orcidCheckedAt") or "") <= recent:
                due[pid] = orcid
        if not due:
            return []
        records = {oa.short_id(a["id"]): a for a in
                   oa.authors_by_orcid(self.client, sorted(set(due.values())), use_cache=False) if a.get("id")}
        same: Dict[str, List[str]] = defaultdict(list)                # ORCID iD -> its records, the largest first
        for aid, a in sorted(records.items(), key=lambda kv: -(kv[1].get("works_count") or 0)):
            same[_orcid(a.get("orcid") or "")].append(aid)
        owners: Dict[str, Set[str]] = defaultdict(set)                # each linked record and the people it is linked to
        for p in self.lib["people"]:
            for aid in self.aids(p):
                owners[aid].add(p)
        found, added, shared, held_back = [], 0, {}, []
        for pid, orcid in due.items():
            o = self.rec(pid)["openalex"]
            mine = self.aids(pid)
            links = self.links(pid)
            no = self.not_theirs(pid)
            recs = [a for a in same.get(orcid, []) if a not in no]
            summary = o.get("summary") or {}
            names = [self.contacts[pid]["name"], summary.get("name", "")]
            insts = _person_institutions(self.contacts[pid], self.rec(pid))
            fit, held = [], []
            for a in recs:
                why = "" if a in mine else _unlike(records[a], names, insts, summary)
                if not why:
                    fit.append(a)
                elif not owners.get(a):            # a record linked to someone else is theirs, not held back
                    held.append({"id": a, "name": records[a].get("display_name") or "", "reason": why})
            others = {p for a in fit for p in owners.get(a, ()) if p != pid}
            new = [a for a in fit if a not in mine]
            if others:
                shared[orcid] = sorted(others | {pid})
            elif new and set(links or mine) & set(recs):      # a record they are linked to carries the ORCID iD
                group = {"orcid": orcid, "ids": [a for a in mine if a in recs] + new}
                if links:                      # the group stays with them while their record links to one of these
                    o["orcidGroup"] = group
                    self.from_record(pid, links)
                else:
                    basis = re.sub(r" \(\d+ OpenAlex records with ORCID [^)]*\)$", "", o.get("basis", ""))
                    self.link(pid, mine + new, f"{basis} ({_records_note(len(group['ids']), orcid)})", group)
                for a in new:
                    owners[a].add(pid)
                found.append(pid)
                added += len(new)
            if held:
                o["orcidHeld"] = held
                held_back += [(pid, h) for h in held]
            else:
                o.pop("orcidHeld", None)
            o["orcidCheckedAt"] = today()
        if found:                              # the summaries that follow read these records instead of asking again
            fetched = utc_now()
            self.cached_authors().update({aid: (a, fetched) for aid, a in records.items()})
        self.log(f"  OpenAlex records with the same ORCID iD: {len(due)} people looked up, {added} record"
                 f"{'s' if added != 1 else ''} added to {len(found)}, {len(held_back)} held back")
        name = lambda p: (self.contacts.get(p) or {}).get("name", p)
        for pid, h in held_back:
            self.log(f"  Held back from {name(pid)}: {h['id']} ({h['name']}), {h['reason']}")
        for orcid, pids in shared.items():
            self.log(f"  ORCID {orcid} is on records linked to {' and '.join(name(p) for p in pids)}; no records were "
                     "added for them")
        return found

    def set_summary(self, pid: str, records: List[Dict[str, Any]]) -> None:
        o = self.rec(pid)["openalex"]
        o["summary"] = combine_summaries([summarize_author(a, self.std["sinceYear"]) for a in records])
        o["summaryAt"] = today()
        self.counts["summaries"] += 1

    def works(self, people: List[str]) -> None:
        def undated(p):                        # a list stored by an earlier version, without publication dates
            o = self.rec(p)["openalex"]
            return not o.get("datedAt") and any(not (self.lib["works"].get(w) or {}).get("date") for w in o.get("works") or [])
        # A list is read in full again when it lacks dates, or when the person gained a record since it was read.
        fresh = [p for p in people if not self.rec(p)["openalex"].get("worksAt")
                 or self.rec(p)["openalex"].get("refill") or undated(p)]
        refills = {p: self.rec(p)["openalex"]["worksAt"] for p in fresh if self.rec(p)["openalex"].get("worksAt")}
        stale = [p for p in people if self.rec(p)["openalex"].get("worksAt") and self.update and p not in refills]
        # A refill asks OpenAlex afresh in an update (a cached list would be the same one every run), and so does a list
        # read again after a record marked as not theirs was dropped (a cached one may be months old).
        from_cache = lambda p: not ((self.update and p in refills) or self.rec(p)["openalex"].get("reread"))
        if self.openalex_stopped:              # no allowance left: only lists already in the cache
            fresh = [p for p in fresh if from_cache(p)
                     and self.client.has_cached(_works_url(self.client, self.aids(p), self.since))]
            stale = []
        for chunk in _chunks(stale, self.aids):  # updates first: 50 records per request, recent works only
            since = _ago(min(self.rec(p)["openalex"]["worksAt"] for p in chunk), self.std["lookBackDays"])
            raw = self._all_pages([a for p in chunk for a in self.aids(p)], since)
            mine = defaultdict(list)
            for w in raw:
                for aid, _, _ in _authorships(w):
                    mine[aid].append(w)
            for pid in chunk:              # a paper naming them under two records is kept once
                self.keep_works(pid, [w for a in self.aids(pid) for w in mine.get(a, [])], first=False)
        for i, pid in enumerate(fresh):        # first fills: each person's works since the start year
            if i and i % 250 == 0:
                self.log(f"  OpenAlex works: {i} of {len(fresh)} first fills")
            # One request covers all of a person's records.
            raw = _works(self.client, self.aids(pid), since=self.since, select=WORK_FIELDS, use_cache=from_cache(pid))
            self.raw_works[pid] = raw
            # A refill reports what an update would have: works published within the look-back window.
            new_since = _ago(refills[pid], self.std["lookBackDays"]) if (self.update and pid in refills) else None
            self.keep_works(pid, raw, first=True, new_since=new_since)
        if fresh or stale:
            self.log(f"  OpenAlex works: {len(fresh)} first fills, {len(stale)} people updated")

    def _all_pages(self, aids: List[str], since: str) -> List[Dict[str, Any]]:
        flt = ("authorships.author.id:" + "|".join(aids) + f",authors_count:<{self.std['maxAuthors'] + 1}"
               f",from_publication_date:{since}")
        out, cursor = [], "*"
        while cursor and len(out) < 2000:
            data = self.client.get(f"{oa.API}/works", oa._params(self.client, {
                "filter": flt, "per-page": 200, "cursor": cursor, "select": WORK_FIELDS}),
                use_cache=False)
            out += data.get("results", [])
            cursor = (data.get("meta") or {}).get("next_cursor") if data.get("results") else None
        return out

    def keep_works(self, pid: str, raw: List[Dict[str, Any]], first: bool, new_since: Optional[str] = None) -> None:
        o = self.rec(pid)["openalex"]
        have = list(o.get("works") or [])
        for w in raw:
            if len(w.get("authorships") or []) > self.std["maxAuthors"]:
                continue
            wid = oa.short_id(w.get("id", ""))
            if not wid:
                continue
            if wid not in self.lib["works"]:
                self.lib["works"][wid] = _work_record(w)
            elif not self.lib["works"][wid].get("date") and w.get("publication_date"):
                self.lib["works"][wid]["date"] = w["publication_date"]
            if wid not in have:
                have.append(wid)
                if not first or (new_since and (w.get("publication_date") or "") >= new_since):
                    self.new_works[pid].append(wid)
        have.sort(key=lambda x: self.lib["works"].get(x, {}).get("date") or "", reverse=True)
        o["works"] = have[:self.std["perPerson"]]
        o["worksAt"] = today()
        if first:
            o["datedAt"] = today()        # read with publication dates; a paper OpenAlex no longer lists keeps none
            o.pop("refill", None)
            o.pop("reread", None)

    def identify_by_coauthors(self, unlinked: List[str]) -> List[str]:
        """Match unlinked people to coauthors on recent papers of people they have recorded relationships with. A record
        marked as not theirs is no candidate."""
        g = self.svc.graph(TieFilter())
        found = []
        taken, taken_orcids = self.taken()
        for pid in unlinked:
            name = self.contacts[pid]["name"]
            no = self.not_theirs(pid)
            cands: Dict[str, Tuple[int, int, str]] = {}
            orcids: Dict[str, str] = {}
            for other in g.nbrs.get(pid, ()):
                ids = self.aids(other)
                raw = self.raw_works.get(other) if ids else None
                if raw is None and ids:
                    try:   # works fetched in an earlier run are in the response cache
                        raw = _works(self.client, ids, since=self.since, select=WORK_FIELDS) \
                            if self.client.has_cached(_works_url(self.client, ids, self.since)) else None
                    except HttpError:
                        raw = None
                for w in raw or []:
                    for a in w.get("authorships") or []:
                        au = a.get("author") or {}
                        if au.get("id") and au.get("orcid"):
                            orcids[oa.short_id(au["id"])] = au["orcid"]
                    for caid, cname, _ in _authorships(w):
                        s = name_score(name, cname)
                        if s and caid not in ids and caid not in no:
                            best = cands.get(caid, (0, 0, ""))
                            cands[caid] = (max(best[0], s), best[1] + 1, self.contacts[other]["name"])
            if not cands:
                continue
            # Candidate records that share an ORCID iD are one researcher: their papers count together.
            groups = []
            for grp in _orcid_groups(cands, lambda c: orcids.get(c, "")):
                grp.sort(key=lambda c: (-cands[c][0], -cands[c][1]))
                groups.append((grp, cands[grp[0]][0], sum(cands[c][1] for c in grp), cands[grp[0]][2]))
            ranked = sorted(groups, key=lambda x: (-x[1], -x[2]))
            top = ranked[0]
            if len(ranked) > 1 and (ranked[1][1], ranked[1][2]) >= (top[1], top[2] / 2):
                self.rec(pid)["openalex"].update({"status": "ambiguous",
                                                  "candidates": [c for x in ranked[:4] for c in x[0]]})
                continue
            ids, orcid = top[0], _orcid(orcids.get(top[0][0], ""))
            if any(i in taken for i in ids) or orcid in taken_orcids:
                continue                           # already someone else's
            basis = f"named with {top[3]} on {top[2]} recent OpenAlex paper{'s' if top[2] > 1 else ''}"
            if len(ids) > 1:
                basis += f" ({_records_note(len(ids), orcid)})"
            self.link(pid, ids, basis, {"orcid": orcid, "ids": ids} if len(ids) > 1 else None)
            taken.update(ids)
            if orcid:
                taken_orcids.add(orcid)
            found.append(pid)
        if found:
            self.log(f"  OpenAlex identities from shared papers: {len(found)}")
        return found

    def identify_by_search(self, unlinked: List[str]) -> List[str]:
        """Name search for unlinked people with a confirmed doctorate (the costly call; limited per run). A record marked
        as not theirs is no candidate."""
        found, done = [], 0
        limit = SEARCHES_PER_RUN_KEY if oa.api_key() else SEARCHES_PER_RUN
        taken, taken_orcids = self.taken()
        for pid in unlinked:
            c = self.contacts[pid]
            o = self.rec(pid)["openalex"]
            if self.nb.doctorate_state(c) != "confirmed":
                continue
            if o.get("status") in ("not-found", "ambiguous") and (o.get("checkedAt") or "") > _ago(today(), 90):
                continue
            if done >= limit:
                break
            done += 1
            self.searched.add(pid)
            insts = _person_institutions(c, self.rec(pid))
            no = self.not_theirs(pid)
            cands = [x for x in oa.search_authors(self.client, c["name"], insts[0] if insts else "")
                     if name_score(c["name"], x["name"]) and x["openalexId"] not in no]
            # Candidate records that share an ORCID iD are one author; one already linked to someone else is no
            # candidate, and neither are the other records with its ORCID iD.
            groups = [grp for grp in _orcid_groups(cands, lambda x: x["orcid"])
                      if not any(x["openalexId"] in taken for x in grp) and _orcid(grp[0]["orcid"]) not in taken_orcids]
            matching = [grp for grp in groups if any(
                org_match(i, j) for x in grp for i in insts
                for j in x["lastKnownInstitutions"] + [h["institution"] for h in x["affiliationHistory"]])]
            if len(matching) == 1:
                grp = sorted(matching[0], key=lambda x: -(x["worksCount"] or 0))     # the main record first
                ids, orcid = [x["openalexId"] for x in grp], _orcid(grp[0]["orcid"])
                basis = "the only OpenAlex author with this name at an institution on their record"
                if len(ids) > 1:
                    basis += f" ({_records_note(len(ids), orcid)})"
                self.link(pid, ids, basis, {"orcid": orcid, "ids": ids} if len(ids) > 1 else None)
                taken.update(ids)
                if orcid:
                    taken_orcids.add(orcid)
                found.append(pid)
            else:
                o.update({"status": "ambiguous" if matching else "not-found", "checkedAt": today()})
                if matching:
                    o["candidates"] = [x["openalexId"] for grp in matching[:4] for x in grp]
        if done:
            self.log(f"  OpenAlex name searches: {done}, {len(found)} identified")
        return found

    # ---------------------------------------------------------------- NIH

    def nih(self, people: List[str]) -> None:
        for pid in people:                      # a profile ID on the record outranks a match by name
            prof = _nih_profile(self.contacts[pid])
            n = self.rec(pid).setdefault("nih", {})
            if prof and n.get("profileIds") != [prof]:
                for k in ("candidates", "checkedAt"):
                    n.pop(k, None)
                n.update({"status": "matched", "profileIds": [prof], "awards": [],
                          "basis": f"NIH RePORTER profile ID {prof} on their record"})
        # A first full read lists awards the person already had; only later runs report awards as new.
        self.first_nih = {p for p in people if not self.rec(p)["nih"].get("checkedAt")}
        matched = [p for p in people if (self.rec(p).get("nih") or {}).get("profileIds")]
        unmatched = [p for p in people if p not in matched]
        for start in range(0, len(matched), 25):          # known PIs: their awards (recent ones when updating)
            chunk = matched[start:start + 25]
            since = self._nih_since(chunk)
            ids = sorted({i for p in chunk for i in self.rec(p)["nih"]["profileIds"]})
            records = rp.by_profiles(self.client, ids, added_since=since, use_cache=not self.update)
            for pid in chunk:
                self.keep_nih(pid, records, self.rec(pid)["nih"]["profileIds"], recent_only=bool(since))
        for start in range(0, len(unmatched), 10):        # everyone else: by name, accepted on an institution match
            chunk = unmatched[start:start + 10]
            names = []
            for pid in chunk:
                first, last = _first_last(self.contacts[pid]["name"])
                if first and last:
                    names.append({"first_name": first, "last_name": last})
            if not names:
                continue
            since = self._nih_since(chunk)
            records = rp.by_names(self.client, names, added_since=since, use_cache=not self.update)
            for pid in chunk:
                self.match_nih(pid, records, recent_only=bool(since))
        self.log(f"  NIH RePORTER: {len(matched)} known PIs, {len(unmatched)} looked up by name")

    def _nih_since(self, chunk: List[str]) -> str:
        """Updates ask only for awards added since two weeks before the chunk's last check; anyone never checked
        needs their whole record, so the chunk is read in full."""
        checked = [self.rec(p)["nih"].get("checkedAt") for p in chunk]
        if not self.update or not all(checked):
            return ""
        return _ago(min(checked), 14)

    def _nih_candidates(self, c: Dict[str, Any], rec: Dict[str, Any], records: List[Dict[str, Any]]):
        insts = _person_institutions(c, rec)
        orgs: Dict[int, Set[str]] = defaultdict(set)
        names: Dict[int, str] = {}
        for r in records:
            org = (r.get("organization") or {}).get("org_name") or ""
            for p in r.get("principal_investigators") or []:
                if p.get("profile_id") and award_name_ok(c["name"], p.get("full_name") or ""):
                    orgs[p["profile_id"]].add(org)
                    names[p["profile_id"]] = (p.get("full_name") or "").strip()
        ok = [i for i, os_ in orgs.items() if any(org_match(o, x) for o in os_ for x in insts)]
        return ok, orgs, names, insts

    def match_nih(self, pid: str, records: List[Dict[str, Any]], recent_only: bool = False) -> None:
        c = self.contacts[pid]
        rec = self.rec(pid)
        nih = rec.setdefault("nih", {})
        ok, orgs, names, insts = self._nih_candidates(c, rec, records)
        if ok and recent_only:
            # An update sees only recently added awards, so it cannot tell whether other investigators share the
            # name: decide on the person's whole record, which also brings in their earlier awards.
            first, last = _first_last(c["name"])
            records = rp.by_names(self.client, [{"first_name": first, "last_name": last}], use_cache=False)
            ok, orgs, names, insts = self._nih_candidates(c, rec, records)
        if len(ok) == 1:
            where = next(o for o in sorted(orgs[ok[0]]) if any(org_match(o, x) for x in insts))
            nih.pop("candidates", None)
            nih.update({"status": "matched", "profileIds": ok,
                        "basis": f"NIH PI {' '.join(names[ok[0]].split())} at {where.title()}"})
            self.keep_nih(pid, records, ok)
        elif len(ok) > 1:
            nih.update({"status": "ambiguous", "candidates": ok})
        else:
            nih.setdefault("status", "none")
        nih["checkedAt"] = today()

    def keep_nih(self, pid: str, records: List[Dict[str, Any]], profile_ids: List[int], recent_only: bool = False) -> None:
        """Store the person's awards, and in an update note the ones that are new (see below)."""
        nih = self.rec(pid).setdefault("nih", {})
        mine = [r for r in records if any(p.get("profile_id") in profile_ids for p in r.get("principal_investigators") or [])]
        have = list(nih.get("awards") or [])
        # New means added since shortly before this person's own last check (a run can stop before reaching them).
        cutoff = _ago(nih["checkedAt"], 14) if nih.get("checkedAt") else self.nih_cutoff
        year_ago = _ago(today(), 365)
        for core, r in rp.latest_by_core(mine).items():
            key = "nih:" + core
            same = [x for x in mine if (x.get("core_project_num") or x.get("project_num")) == core]
            a = self.lib["awards"].get(key) or {"source": "nih", "number": core, "funder": "NIH", "addedAt": today(),
                                                "roles": {}}
            merge_award(a, same, pid)
            a["roles"][pid] = rp.pi_role(r, profile_ids[0]) if len(profile_ids) == 1 else "PI"
            self.lib["awards"][key] = a
            if key not in have:
                have.append(key)
                if recent_only:
                    # A query limited to recent additions shows only an award's latest records. It is new when one
                    # of them opens an award (application type 1) or renews one (type 2), or when the award began
                    # within the year (an R00 activation, a contract); not when it is a later year, a transfer or a
                    # supplement of an award that began earlier.
                    began = [str(x.get("project_start_date") or "")[:10] for x in same if x.get("project_start_date")]
                    recent = (any(str(x.get("project_num") or "")[:1] in ("1", "2") for x in same)
                              or (bool(began) and min(began) >= year_ago))
                else:
                    added = [str(x.get("date_added") or "")[:10] for x in same if x.get("date_added")]
                    recent = bool(added) and min(added) >= cutoff              # its first record is recent
                if self.update and pid not in self.first_nih and recent:
                    self.new_awards[pid].append(key)
        nih["awards"] = have
        nih["checkedAt"] = today()

    # ---------------------------------------------------------------- NSF

    def nsf(self, people: List[str]) -> None:
        done = 0
        for i, pid in enumerate(people):
            if i and i % 250 == 0:
                self.log(f"  NSF: {i} of {len(people)} people")
            rec = self.rec(pid)
            countries = ((rec.get("openalex") or {}).get("summary") or {}).get("countries") or []
            if countries and "US" not in countries and not self.nb.is_lab(pid):   # no country listed: still look
                n = rec.setdefault("nsf", {})
                if not n.get("checkedAt"):          # someone checked before keeps their awards and status
                    n.update({"status": "skipped", "reason": "no US institution in OpenAlex"})
                continue
            c = self.contacts[pid]
            first, last = _first_last(c["name"])
            if not (first and last):
                continue
            n = rec.setdefault("nsf", {})
            first_read = not n.get("checkedAt")     # a first read lists awards they already had
            since = _ago(n.get("checkedAt") or "1900-01-01", 30) if self.update and n.get("checkedAt") else ""
            try:
                awards = nsf.by_pi(self.client, first, last, since=since, use_cache=not self.update)
            except HttpError as exc:
                n["error"] = str(exc)[:200]
                continue
            insts = _person_institutions(c, rec)
            have = list(n.get("awards") or [])
            for a in awards:
                full = f"{a.get('piFirstName', '')} {a.get('piLastName', '')}"
                if not award_name_ok(c["name"], full):
                    continue
                if not any(org_match(a.get("awardeeName") or "", x) for x in insts):
                    continue
                key = "nsf:" + str(a.get("id"))
                rec_a = self.lib["awards"].get(key) or {"source": "nsf", "number": str(a.get("id")), "funder": "NSF",
                                                        "addedAt": today(), "roles": {}}
                rec_a.update({"title": (a.get("title") or "")[:500], "org": a.get("awardeeName") or "",
                              "start": _iso(a.get("startDate", "")), "end": _iso(a.get("expDate", "")),
                              "amount": a.get("fundsObligatedAmt") or "", "url": nsf.award_url(str(a.get("id")))})
                rec_a["roles"][pid] = "PI"
                self.lib["awards"][key] = rec_a
                if key not in have:
                    have.append(key)
                    if self.update and not first_read:
                        self.new_awards[pid].append(key)
            n.update({"status": "checked", "awards": have, "checkedAt": today()})
            for stale in ("error", "reason"):
                n.pop(stale, None)
            done += 1
        self.log(f"  NSF: {done} people checked")


def _works_url(client: Client, aids: List[str], since: str) -> str:
    """The URL `_works` requests for these author records, so a cached response can be found without a network call."""
    import urllib.parse
    from .expand import MAX_AUTHORS
    ids = "|".join(oa.short_id(a) for a in aids)
    params = oa._params(client, {"filter": f"authorships.author.id:{ids},authors_count:<{MAX_AUTHORS + 1},"
                                           f"from_publication_date:{since}", "sort": "publication_date:desc",
                                 "per-page": 200, "select": WORK_FIELDS})
    return f"{oa.API}/works?" + urllib.parse.urlencode(params, safe=":|,/")


# --------------------------------------------------------------------------- entry points

def default_budget() -> float:
    """OpenAlex spending limit for one run. Without a key, the free $0.10 a day is shared by everyone on the network
    address, so a run leaves some of it; a key has its own $1 a day."""
    return 0.90 if oa.api_key() else 0.08


def run(ws_path: str, update: bool = False, budget_usd: Optional[float] = None, use_nih: bool = True, use_nsf: bool = True,
        client: Optional[Client] = None, log: Callable[[str], None] = print,
        since_year: Optional[int] = None) -> Dict[str, Any]:
    """Build the library, or with `update` add what is new and write a digest. `since_year` sets the earliest
    publication year of a new library (later runs keep the year it was built with)."""
    ws = Workspace(ws_path)
    if (ws.config.get("settings") or {}).get("demo"):
        raise LibraryError("This is the fictional demo lab. Its research library comes with the demo and is never "
                           "looked up.")
    lock = _take_lock(ws)
    try:
        svc = Service(ws)
        lib = load(ws)
        if since_year and not lib["people"] and not lib["runs"]:
            lib["standard"]["sinceYear"] = int(since_year)
        client = client or Client(cache_dir=default_cache(ws),
                                  budget_usd=default_budget() if budget_usd is None else budget_usd,
                                  mailto=(ws.config.get("settings") or {}).get("contactEmail", ""))
        people = _people_in_scope(svc, lib)
        log(f"Library {'update' if update else 'build'} for {len(people)} people within "
            f"{svc.degree_limit() or 2} degrees of the lab")
        r = _Run(svc, lib, client, update, log)
        try:
            r.openalex(people)
            save(ws, lib)                     # each source is saved as it finishes
            if use_nih:
                try:
                    r.nih(people)
                except HttpError as exc:
                    log(f"  NIH RePORTER stopped: {exc}")
                save(ws, lib)
            if use_nsf:
                r.nsf(people)
        except KeyboardInterrupt:
            save(ws, lib)
            log("Stopped. The records fetched so far are saved; run the command again to continue.")
            raise
        status = coverage(lib, people)
        lib["runs"] = (lib.get("runs") or [])[-51:] + [{
            "date": utc_now(), "mode": "update" if update else "build", "people": len(people),
            "openalexCostUsd": round(client.cost_usd, 4), "openalexStopped": r.openalex_stopped,
            "newWorks": sum(len(v) for v in r.new_works.values()), "newAwards": sum(len(v) for v in r.new_awards.values()),
            "coverage": status}]
        path = save(ws, lib)
        digest = write_digest(ws, svc, lib, r, status) if update else ""
    finally:
        _release_lock(lock)
    log(f"Saved {path}: {status['openalexLinked']} people matched to OpenAlex authors, paper lists for "
        f"{status['openalexFetched']} ({status['openalexPending']} still to fetch), "
        f"{len(lib['works'])} papers, {sum(1 for a in lib['awards'].values() if a['source'] == 'nih')} NIH and "
        f"{sum(1 for a in lib['awards'].values() if a['source'] == 'nsf')} NSF awards. "
        f"OpenAlex cost ${client.cost_usd:.4f}" + (f"; today's allowance left ${client.remaining_usd:.4f}" if client.remaining_usd is not None else ""))
    return {"path": path, "coverage": status, "digest": digest, "stopped": r.openalex_stopped}


def _take_lock(ws: Workspace) -> Any:
    """One library run at a time per workspace (the weekly job, a manual run and onboarding could overlap).

    The operating system holds the lock on library.lock for as long as the run's process lives, so a run that is
    killed or crashes never leaves a stale lock behind. The file stays in place between runs.
    """
    import errno
    path = os.path.join(ws.path, "library.lock")
    f = open(path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, getattr(errno, "EDEADLK", -1),
                             getattr(errno, "EDEADLOCK", -1)):
            return f                    # this drive cannot lock files (some network drives): run without the lock
        f.close()
        held = ""
        try:
            with open(path, "r", encoding="utf-8") as g:
                info = json.loads(g.read() or "{}")
            if info.get("pid"):
                held = f" (process {info['pid']}, started {info.get('startedAt', 'at an unknown time')})"
        except (OSError, ValueError):
            pass
        raise LibraryError(f"Another library run is in progress{held}. Wait for it to finish.")
    f.seek(0)
    f.truncate()
    f.write(json.dumps({"pid": os.getpid(), "startedAt": utc_now()}))
    f.flush()
    return f


def _release_lock(f: Any) -> None:
    try:
        f.seek(0)
        f.truncate()
        f.flush()
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        f.close()


def coverage(lib: Dict[str, Any], people: Iterable[str]) -> Dict[str, Any]:
    people = list(people)
    st = defaultdict(int)
    for pid in people:
        o = (lib["people"].get(pid) or {}).get("openalex") or {}
        st[o.get("status", "pending")] += 1
    linked = [p for p in people if ((lib["people"].get(p) or {}).get("openalex") or {}).get("status") == "linked"]
    fetched = [p for p in linked if lib["people"][p]["openalex"].get("worksAt")]
    nih_matched = sum(1 for p in people if ((lib["people"].get(p) or {}).get("nih") or {}).get("status") == "matched")
    return {"people": len(people), "openalexLinked": len(linked), "openalexFetched": len(fetched),
            "openalexPending": len(linked) - len(fetched) + st.get("pending", 0),
            "openalexUnmatched": st.get("unmatched", 0) + st.get("not-found", 0) + st.get("ambiguous", 0),
            "openalexStatus": dict(st), "nihMatched": nih_matched}


def write_digest(ws: Workspace, svc: Service, lib: Dict[str, Any], r: Any, status: Dict[str, Any], date: str = "",
                 created: str = "") -> str:
    """Markdown and JSON digests of a run's new papers and awards (`r` carries new_works, new_awards, openalex_stopped)."""
    date = date or today()
    created = created or utc_now()
    names = {pid: c["name"] for pid, c in svc.nb.records["contacts"].items()}
    dist, _ = svc.lab_dist()
    order = lambda pid: (dist.get(pid, 9), names.get(pid, pid))
    limit = svc.degree_limit() or 2
    lines = [f"# What's new, {date}", "",
             f"Papers and awards found since the last weekly update for the {status['people']} people within {limit} "
             f"degree{'s' if limit != 1 else ''} of the lab. OpenAlex paper lists: {status['openalexFetched']} people"
             + (f" ({status['openalexPending']} still to fetch)" if status["openalexPending"] else "") + ".", ""]
    papers = [(pid, wid) for pid in sorted(r.new_works, key=order) for wid in r.new_works[pid]]
    lines.append(f"## New papers ({len(papers)})")
    for pid in sorted(r.new_works, key=order):
        lines.append(f"- **{names.get(pid, pid)}**")
        for wid in r.new_works[pid]:
            w = lib["works"].get(wid) or {}
            lines.append(f"  - {w.get('title', wid)} ({w.get('date') or w.get('year') or ''}) {w.get('url', '')}")
    awards = [(pid, k) for pid in r.new_awards for k in r.new_awards[pid]]
    lines += ["", f"## New awards ({len(awards)})"]
    for pid in sorted(r.new_awards, key=order):
        for k in r.new_awards[pid]:
            a = lib["awards"].get(k) or {}
            lines.append(f"- **{names.get(pid, pid)}**: {a.get('funder', '')} {a.get('number', '')}, {award_title(a, pid)[0]} "
                         f"({a.get('start', '')} to {a.get('end', '')}) {a.get('url', '')}")
    if r.openalex_stopped:
        lines += ["", f"OpenAlex stopped early: {r.openalex_stopped}. The next run continues."]
    ddir = os.path.join(ws.path, "digests")
    os.makedirs(ddir, exist_ok=True)
    path = os.path.join(ddir, f"library-{date}.md")
    if os.path.exists(path):                   # a second update the same day keeps the first one's digest
        path = os.path.join(ddir, f"library-{date}-{_dt.datetime.now().strftime('%H%M%S')}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(path[:-3] + ".json", "w", encoding="utf-8") as f:
        json.dump({"date": date, "createdAt": created, "kind": "library", "newWorks": r.new_works, "newAwards": r.new_awards,
                   "coverage": status, "stopped": r.openalex_stopped}, f, indent=1)
    return path


# --------------------------------------------------------------------------- the live check on a profile page

def latest(lib: Dict[str, Any], pid: str, client: Client, not_theirs: Iterable[str] = ()) -> Dict[str, Any]:
    """Papers and NIH awards newer than the person's last library update, straight from the sources. Records marked as
    not theirs (`not_theirs`) are not read."""
    rec = lib["people"].get(pid) or {}
    o = rec.get("openalex") or {}
    std = lib.get("standard") or STANDARD
    nih = rec.get("nih") or {}
    out: Dict[str, Any] = {"checkedAt": utc_now(), "since": o.get("worksAt") or "", "works": [], "awards": [],
                           "notes": []}
    # Per source: whether it was read, the person's last update from it, and why not when it was not.
    sources = {"openalex": {"checked": False, "since": o.get("worksAt") or "", "note": ""},
               "nih": {"checked": False, "since": nih.get("checkedAt") or "", "note": ""}}
    out["sources"] = sources

    def skip(src: str, note: str) -> None:
        sources[src]["note"] = note
        out["notes"].append(note)

    no = set(not_theirs)
    ids = [i for i in author_ids(o) if i not in no]
    if o.get("status") == "linked" and ids:
        since = _ago(o.get("worksAt") or today(), std["lookBackDays"]) if o.get("worksAt") else f"{std['sinceYear']}-01-01"
        try:                                    # one request covers all of the person's records
            data = client.get(f"{oa.API}/works", oa._params(client, {
                "filter": f"authorships.author.id:{'|'.join(ids)},authors_count:<{std['maxAuthors'] + 1},"
                          f"from_publication_date:{since}",
                "sort": "publication_date:desc", "per-page": 50,
                "select": "id,doi,ids,title,publication_year,publication_date,authorships"}), use_cache=False)
            have = o.get("works") or []
            stored = [lib["works"].get(x, {}).get("date") or "" for x in have]
            # With a full list, a paper older than the oldest one kept would not have been kept anyway.
            floor = min(stored) if len(have) >= std["perPerson"] and stored else ""
            for w in data.get("results", []):
                wid = oa.short_id(w.get("id", ""))
                if wid and wid not in have and (w.get("publication_date") or "") >= floor:
                    out["works"].append({"id": wid, **_work_record(w)})
            sources["openalex"]["checked"] = True
        except BudgetReached:
            skip("openalex", "OpenAlex was not checked: today's free allowance is used up.")
        except HttpError as exc:
            skip("openalex", f"OpenAlex could not be reached ({exc.status or 'network'}).")
    elif o.get("status") == "linked":
        skip("openalex", "Their OpenAlex record is marked as not theirs, so new papers were not checked. The next "
                         "library run looks for theirs.")
    else:
        skip("openalex", "No OpenAlex record is linked to this person, so new papers were not checked.")
    if nih.get("profileIds"):
        try:
            records = rp.by_profiles(client, nih["profileIds"], added_since=_ago(nih.get("checkedAt") or today(), 14))
            have = set(nih.get("awards") or [])
            for core, r in rp.latest_by_core(records).items():
                if "nih:" + core not in have:
                    out["awards"].append({"source": "nih", "number": core, "title": r.get("project_title") or core,
                                          "funder": "NIH", "url": rp.project_url(r.get("appl_id")),
                                          "start": (r.get("project_start_date") or "")[:10],
                                          "end": (r.get("project_end_date") or "")[:10]})
            sources["nih"]["checked"] = True
        except HttpError as exc:
            skip("nih", f"NIH RePORTER could not be reached ({exc.status or 'network'}).")
    return out
