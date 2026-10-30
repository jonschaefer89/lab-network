"""Fill in the first and second degree from public coauthorship records.

`expand` reads OpenAlex (and ORCID, for the papers people list there and for new people's degrees and positions) and
sends what it finds through the importer as one batch, like any other import. Nothing in the packet changes, and
`undo-import` reverses the batch.

A person's OpenAlex records are the ones linked on their record, else the research library's match (with the records
the library joined to them by ORCID iD); a record marked as not theirs never counts.

1. Lab members without OpenAlex records are looked up by name and accepted only when their OpenAlex works include
   papers already recorded for them (two or more; one when only one is recorded).
2. First degree: every coauthor of a lab member on papers with 30 or fewer authors is connected to that member.
   Coauthors already in the notebook gain a coauthor record if they lack one; the rest are added. A person confirmed
   the member's OpenAlex record, not each work on it, so a work counts only when it names another lab member, someone
   whose relationship with the lab is more than a coauthor record written from OpenAlex, or someone named on another
   member's works that count; is on the ORCID record whose iD is on the member's own record; or lists them at their
   recorded institution or at an institution where two such works list them. A first-degree person whose only
   relationship with the lab is a coauthor record written from OpenAlex is collected in step 4 only when this step
   finds them on a lab member's works that count.
3. A first-degree person already in the notebook without OpenAlex records is identified on the lab members' OpenAlex
   papers: the author whose name matches theirs, preferring a paper already recorded between them. People without a
   shared paper who have a confirmed doctorate or a research relationship are looked up by name and accepted only
   when exactly one candidate's institutions match theirs. Everyone else is not looked up.
4. Second degree: for each identified first-degree person, every work since the start year with 30 or fewer authors
   is read from all of their records, with the papers they list on their ORCID record that OpenAlex files under
   other records. OpenAlex sometimes files two researchers with the same name under one record, so a work counts only
   when it connects to what is known about the person: it names someone in the network (other than people whose only
   tie is to this person, and people an expand run or onboarding added to the second degree), was recorded with a lab
   member, or lists them at their recorded institution, or at an institution two such works list them at. Coauthors
   already in the notebook gain a coauthor record if they lack one. Everyone else with at least `min_shared` shared
   works is added; with `per_person`, only until that many of the person's coauthors are expansion additions (from
   this run or an earlier one), so a second run adds only the missing records.

A coauthor whose name matches someone already in the notebook, with no shared institution or relationship to
confirm it is the same person, is held back and listed instead of added. So is a new person whose OpenAlex record
carries the ORCID iD of someone in the notebook or of another new person: they may be one researcher with two
records. Everyone checked is recorded in a collection note (a workspace.note event), so the app can say whose
connections were collected.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .connectors import openalex as oa
from .connectors import orcid as oc
from .connectors.http import BudgetReached, Client, HttpError
from .graph import TieFilter
from .importer import Importer
from .model import name_key, norm_text
from .refresh import _held, _running_port, openalex_links
from .search import _paper_ids, _strip_year
from .service import Service, _initials_match, slugify
from .store import Workspace, today, utc_now

MAX_AUTHORS = 30
MAX_PAGES = 50                  # 10,000 works on a person's records, far more than anyone publishes since the start year
DOIS_PER_REQUEST = 50
WORK_SELECT = "id,doi,ids,title,publication_year,authorships"
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md"}
RESEARCH_TYPES = {"coauthor", "grant", "collaborator"}
ORCID_RX = re.compile(r"orcid\.org/(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", re.I)
NOTICE_RX = re.compile(r"\s*((author |publisher )?(correction|erratum|corrigendum)(\s*:|\s+(to|notice)\b|\s*$)"
                       r"|retract(ion|ed)\b|notice of retraction\b|expression of concern\b)", re.I)  # not papers
SECOND_DEGREE_TAGS = {"expand-second-degree", "openalex-second-degree"}       # added by expand, or by onboarding
AUTOMATED_ACTORS = ("expand", "scheduled refresh")      # the runs that write coauthor records from OpenAlex
NIH_AWARD_TITLE, NIH_PROFILE_NOTE = "NIH RePORTER award record", "NIH RePORTER PI profile ID"     # onboarding's wording
# The notes onboarding and expand write beside an ORCID iD they took from an OpenAlex author record.
OPENALEX_ORCID_NOTES = ("ORCID iD listed on the OpenAlex record", "ORCID iD on their OpenAlex record",
                        "ORCID record, read by expand", "ORCID employment record")

STATUS_TEXT = {
    "collected": "coauthors collected from OpenAlex",
    "not-found": "not found in OpenAlex",
    "ambiguous": "several OpenAlex authors match; not collected until someone confirms which is theirs",
    "not-looked-up": "not looked up: no confirmed doctorate or research relationship recorded",
    "unanchored": "none of their OpenAlex works connects to anything else known about them",
    "tie-unanchored": ("the only papers that tie them to the lab are on a lab member's OpenAlex record but connect to "
                       "nothing else known about the lab member, so they may be a namesake's"),
    "tie-unconfirmed": ("no lab member's OpenAlex works that count name them, so nothing confirms their coauthor record "
                        "with the lab"),
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


AFFILIATED_UNITS = {"medical center", "medical centre", "school of medicine", "medical school", "medical college",
                    "health", "health system", "health sciences", "health science center", "hospital", "hospitals"}


def _inst_words(name: str) -> str:
    x = norm_text((name or "").replace("&", " and "))
    return x[4:] if x.startswith("the ") else x


def _inst_match(recorded: str, named: str) -> bool:
    """Whether an institution named on a paper is one recorded for a person: the same name, or the same name followed
    by an affiliated unit ('Lakeside University' and 'Lakeside University Medical Center'). Either value may list
    several institutions (separated by ';'), and a recorded one may put a department before one (separated by ','),
    which then has to match by its whole name. A name inside a
    longer one is not enough: 'University of California' is not every campus, and 'University of Pennsylvania'
    is not 'Indiana University of Pennsylvania'."""
    whole = {_inst_words(a) for a in {recorded or "", *(recorded or "").split(";")}} - {""}
    pieces = {_inst_words(a) for a in re.split(r"[;,]", recorded or "")} - {""}
    for y in {_inst_words(b) for b in {named or "", *(named or "").split(";")}} - {""}:
        if y in pieces:                 # "Department of Psychology, Lakeside University", but not "..., Boston"
            return True                 # for "Boston Medical Center"
        for x in whole:
            short, long_ = sorted((x, y), key=len)
            if x == y or (long_.startswith(short + " ") and long_[len(short) + 1:] in AFFILIATED_UNITS):
                return True
    return False


# --------------------------------------------------------------------------- OpenAlex reads

def _work_ids(w: Dict[str, Any]) -> List[str]:
    ids = w.get("ids") or {}
    url = " ".join(str(ids.get(k) or "") for k in ("pmid", "pmcid"))
    return _paper_ids(oa.work_title(w), (w.get("doi") or "") + " " + url)


def _works(client: Client, aids: List[str], since: str = "", max_authors: Optional[int] = MAX_AUTHORS,
           per_page: int = 200, select: str = WORK_SELECT, use_cache: bool = True) -> List[Dict[str, Any]]:
    """The newest works on these author records: one page of `per_page`."""
    flt = "authorships.author.id:" + "|".join(oa.short_id(a) for a in aids)
    if max_authors:
        flt += f",authors_count:<{max_authors + 1}"
    if since:
        flt += f",from_publication_date:{since}"
    data = client.get(f"{oa.API}/works", oa._params(client, {"filter": flt, "sort": "publication_date:desc",
                                                             "per-page": per_page, "select": select}),
                      use_cache=use_cache)
    return data.get("results", [])


def _all_works(client: Client, aids: List[str], since: str = "", max_authors: Optional[int] = MAX_AUTHORS,
               select: str = WORK_SELECT, use_cache: bool = True) -> Tuple[List[Dict[str, Any]], bool]:
    """Every work on these author records, newest first, read page by page. The flag says the reading stopped after
    MAX_PAGES pages."""
    flt = "authorships.author.id:" + "|".join(oa.short_id(a) for a in aids)
    if max_authors:
        flt += f",authors_count:<{max_authors + 1}"
    if since:
        flt += f",from_publication_date:{since}"
    out: List[Dict[str, Any]] = []
    cursor = "*"
    for _ in range(MAX_PAGES):
        data = client.get(f"{oa.API}/works", oa._params(client, {"filter": flt, "sort": "publication_date:desc",
                                                                 "per-page": 200, "select": select, "cursor": cursor}),
                          use_cache=use_cache)
        batch = data.get("results", [])
        out += batch
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not batch or not cursor:
            return out, False
    return out, True


def _orcid_listed(client: Client, orcid: str, have: List[Dict[str, Any]], since_year: int) -> List[Dict[str, Any]]:
    """Works a person lists on their ORCID record that OpenAlex has (since the start year, 30 or fewer authors) and
    that are not among `have`, their works under their own OpenAlex records."""
    have_ids = {w.get("id") for w in have}
    have_dois = {(w.get("doi") or "").lower().replace("https://doi.org/", "") for w in have}
    dois = sorted({d for d, year in oc.work_dois(client, orcid)
                   if d not in have_dois and (year is None or year >= since_year - 1) and not set(d) & set(",|")})
    out: List[Dict[str, Any]] = []
    for start in range(0, len(dois), DOIS_PER_REQUEST):
        flt = (f"doi:{'|'.join(dois[start:start + DOIS_PER_REQUEST])},authors_count:<{MAX_AUTHORS + 1},"
               f"from_publication_date:{since_year}-01-01")
        data = client.get(f"{oa.API}/works", oa._params(client, {"filter": flt, "per-page": 200, "select": WORK_SELECT}))
        for w in data.get("results", []):
            if w.get("id") not in have_ids:
                have_ids.add(w.get("id"))
                out.append(w)
    return out


def _selves(w: Dict[str, Any], name: str, orcid: str) -> Set[str]:
    """The authorships on a work that name the person: their ORCID iD, or their name with the full first name (an
    initial could be a coauthor's)."""
    out = set()
    for a in w.get("authorships") or []:
        au = a.get("author") or {}
        if not au.get("id"):
            continue
        if (orcid and oc.clean(au.get("orcid") or "").upper() == orcid) or \
                name_score(name, au.get("display_name") or a.get("raw_author_name") or "") == 2:
            out.add(oa.short_id(au["id"]))
    return out


def _author_details(client: Client, aids: List[str], details: Dict[str, Dict[str, Any]],
                    errors: List[str]) -> Tuple[List[str], Optional[BudgetReached]]:
    """Read new people's OpenAlex author records into `details`, 50 to a request. Returns the people whose records were
    not read, so that their ORCID iD could not be checked, and the spending stop if the run reached one."""
    unread: List[str] = []
    for start in range(0, len(aids), 50):
        chunk = aids[start:start + 50]
        try:
            details.update(oa.get_authors(client, chunk))
        except BudgetReached as exc:
            errors.append(f"The run reached its spending limit before reading the OpenAlex author records of "
                          f"{len(aids) - start} new people; they were not added, and the next run adds them.")
            return unread + aids[start:], exc
        except HttpError as exc:
            errors.append(f"The OpenAlex author records of {len(chunk)} new people could not be read ({exc}); they "
                          "were not added, and the next run adds them.")
            unread += chunk
    return unread, None


def _authorships(w: Dict[str, Any]):
    for a in w.get("authorships") or []:
        au = a.get("author") or {}
        if au.get("id"):
            yield (oa.short_id(au["id"]), au.get("display_name") or a.get("raw_author_name") or "",
                   [i.get("display_name", "") for i in a.get("institutions") or [] if i.get("display_name")])


def _coauthor_counts(works: List[Dict[str, Any]], mine: Set[str], max_authors: int = MAX_AUTHORS):
    """Shared-work counts, latest shared work, names and institutions for each coauthor (newest works first). Works
    with the same DOI or title (a preprint and its article) count once, and corrections and retraction notices not at
    all."""
    counts: Counter = Counter()
    latest: Dict[str, Dict[str, Any]] = {}
    names: Dict[str, str] = {}
    insts: Dict[str, Set[str]] = defaultdict(set)
    paper: Dict[str, int] = {}          # paper identifier -> the first work that had it
    seen: Set[Tuple[str, int]] = set()
    for n, w in enumerate(works):
        if len(w.get("authorships") or []) > max_authors or NOTICE_RX.match(oa.work_title(w) or ""):
            continue
        ids = _work_ids(w)
        p = next((paper[i] for i in ids if i in paper), n)
        for i in ids:
            paper.setdefault(i, p)
        for aid, nm, ins in _authorships(w):
            if aid in mine or (aid, p) in seen:
                continue
            seen.add((aid, p))
            counts[aid] += 1
            if aid not in latest or (w.get("publication_year") or 0) > (latest[aid].get("publication_year") or 0):
                latest[aid] = w             # papers listed on ORCID come after the newest-first OpenAlex list
            names[aid] = nm
            insts[aid].update(ins)
    ranked = sorted(counts, key=lambda a: (-counts[a], -(latest[a].get("publication_year") or 0)))
    return ranked, counts, latest, names, insts


def _anchored(works: List[Dict[str, Any]], mine: Set[str],
              anchors: Callable[[Dict[str, Any]], bool]) -> Tuple[List[Dict[str, Any]], int]:
    """The works that count for a person, and how many were left out. A work counts when it anchors the person by
    itself (`anchors`), or when their own authorship (`mine`) on it lists an institution that at least two such works
    list them at. Neither a shared coauthor nor an institution on one work joins works: a namesake filed under the same
    record shares both across their own papers, and one anchor met by chance would bring in all of them."""
    def places(w: Dict[str, Any]) -> Set[str]:
        return {x["id"] for a in w.get("authorships") or []
                if oa.short_id((a.get("author") or {}).get("id") or "") in mine
                for x in a.get("institutions") or [] if x.get("id")}
    direct = [anchors(w) for w in works]
    paper: Dict[str, int] = {}              # as in _coauthor_counts: a preprint and its article are one paper
    seen: Dict[str, Set[int]] = defaultdict(set)        # institution ID -> papers that count by themselves there
    for n, (w, d) in enumerate(zip(works, direct)):
        if not d or NOTICE_RX.match(oa.work_title(w) or ""):
            continue
        ids = _work_ids(w)
        p = next((paper[i] for i in ids if i in paper), n)
        for i in ids:
            paper.setdefault(i, p)
        for i in places(w):
            seen[i].add(p)
    theirs = {i for i, ps in seen.items() if len(ps) >= 2}
    kept = [w for w, d in zip(works, direct) if d or places(w) & theirs]
    return kept, len(works) - len(kept)


# --------------------------------------------------------------------------- plan

class _Plan:
    """Collects new people, new coauthor records and held-back names while the steps run."""

    def __init__(self, svc: Service, since_year: int, lib: Optional[Dict[str, Any]] = None):
        from .library import author_ids
        self.nb = svc.nb
        self.contacts = self.nb.records["contacts"]
        self.active = {pid for pid, _ in self.nb.active("contacts")}
        self.lab = set(self.nb.root_ids)
        self.g = svc.graph(TieFilter())
        self.since_year = since_year
        self.lib_people = (lib or {}).get("people") or {}
        self.by_family: Dict[str, List[str]] = defaultdict(list)
        for pid in self.active:
            for k in {family_key(self.contacts[pid]["name"])} | surname_tokens(self.contacts[pid]["name"]):
                self.by_family[k].append(pid)
        # The names the importer compares (Service.possible_duplicates). Whoever it would hold back is held back here,
        # so that a dry run plans what the import adds.
        self.by_key: Dict[str, str] = {}
        self.by_surname: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        for pid in sorted(self.active):
            k = name_key(self.contacts[pid]["name"])
            if k:
                self.by_key.setdefault(k, pid)
                self.by_surname[k.split()[-1]].append((k, pid))
        self.new_keys: Dict[str, str] = {}                  # name key -> planned person ID
        # A person's OpenAlex records: the links on their record (with the records the library joined to one of them
        # by ORCID iD), else the research library's match; never a record marked as not theirs.
        self.oa_of: Dict[str, List[str]] = {}
        self.voided: Dict[str, Set[str]] = {}
        self.oa_basis: Dict[str, str] = {}
        self.oa_source: Dict[str, str] = {}
        for pid in sorted(self.active):
            ids = openalex_links(self.nb, pid)
            if ids:
                self.oa_of[pid], self.oa_source[pid] = ids, "record"
                self.oa_basis[pid] = ("OpenAlex link on their record" if len(ids) == 1 else
                                      f"{len(ids)} OpenAlex links on their record")
        for pid in sorted(self.active):
            o = self.library_openalex(pid)
            if not o:
                continue
            # A record marked as not theirs voids the library's match that holds it, with the records joined to it by
            # ORCID iD (one researcher); none of them counts as theirs until the library makes a new match.
            no = self.nb.openalex_not_theirs(pid)
            if pid in self.oa_of:
                group = (o.get("orcidGroup") or {}).get("ids") or []
                extra = [a for a in group if a not in self.oa_of[pid]] if set(group) & set(self.oa_of[pid]) else []
                if set(group) & set(no):
                    self.voided[pid], extra = set(extra), []
                if extra:
                    self.oa_of[pid] = self.oa_of[pid] + extra
                    self.oa_basis[pid] += (f", and {len(extra)} more record{'s' if len(extra) > 1 else ''} with "
                                           "the same ORCID iD, joined by the research library")
            else:
                ids = author_ids(o)
                if set(ids) & set(no):
                    self.voided[pid] = set(ids)
                elif ids:
                    self.oa_of[pid], self.oa_source[pid] = ids, "library"
                    self.oa_basis[pid] = f"the research library's match ({o.get('basis') or 'no basis recorded'})"
        self.by_oa: Dict[str, str] = {}
        for pid, ids in self.oa_of.items():                # links on records first: the first person listed keeps an ID
            for a in ids:
                self.by_oa.setdefault(a, pid)
        self.pairs = {tuple(sorted((e["from"], e["to"]))) for _, e in self.nb.active("edges") if e["type"] == "coauthor"}
        # Who wrote each relationship record added after the packet (the packet's come from onboarding).
        self.made_by = {ev["id"]: ev.get("actor") or "" for ev in self.nb.events if ev["op"] == "edge.create"}
        self.new_people: Dict[str, Dict[str, Any]] = {}     # OpenAlex id -> plan
        self.new_names: Dict[str, str] = {}                 # planned person ID -> name
        self.edges: List[Dict[str, Any]] = []
        self.edge_ids: Set[str] = set()
        self.held: List[Dict[str, Any]] = []
        self.held_ids: Set[str] = set()
        self.orcid_held: Set[str] = set()                   # OpenAlex ids held back: a shared or unchecked ORCID iD

    def library_openalex(self, pid: str) -> Dict[str, Any]:
        """The research library's OpenAlex entry for a person, when it links them to records."""
        o = (self.lib_people.get(pid) or {}).get("openalex") or {}
        return o if o.get("status") == "linked" else {}

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
        return self.new_names.get(pid, pid)

    def orcids(self, pid: str) -> List[str]:
        """ORCID iDs for someone in the notebook: on their record first, then on their OpenAlex records."""
        c = self.contacts[pid]
        found = [m for s in c.get("sources") or [] for m in ORCID_RX.findall(s.get("url", ""))]
        found += ORCID_RX.findall(c.get("profileSourceUrl", ""))
        o = self.library_openalex(pid)
        found += [(o.get("summary") or {}).get("orcid", ""), (o.get("orcidGroup") or {}).get("orcid", "")]
        return list(dict.fromkeys(x for x in (oc.clean(f).upper() for f in found) if x))

    def given_orcids(self, pid: str) -> List[str]:
        """The ORCID iDs on someone's own record, which a person gave or confirmed. Not an iD that OpenAlex shows on
        their author record: a record that holds a namesake's papers can carry the namesake's iD."""
        c = self.contacts[pid]
        found = [m for s in c.get("sources") or [] if not (s.get("note") or "").startswith(OPENALEX_ORCID_NOTES)
                 for m in ORCID_RX.findall(s.get("url", ""))]
        found += ORCID_RX.findall(c.get("profileSourceUrl", ""))
        return list(dict.fromkeys(x for x in (oc.clean(f).upper() for f in found) if x))

    def from_openalex(self, e: Dict[str, Any]) -> bool:
        """Whether a relationship record is a coauthor record written from OpenAlex: by expand or the scheduled refresh,
        or with evidence that says so (onboarding's, and every record those runs write, cite works "indexed by
        OpenAlex"). A coauthor record taken from another public source, such as a paper's author list on PubMed, is
        not."""
        actor = self.made_by.get(e["id"]) or ""
        return e["type"] == "coauthor" and e.get("provenance") == "public" and (
            actor.startswith(AUTOMATED_ACTORS) or "openalex" in f"{e.get('evidence', '')} {e.get('sourceTitle', '')}".lower())

    def entered(self, a: str, b: str) -> bool:
        """Whether a relationship record between two people shows more than an OpenAlex coauthorship. Onboarding joins
        a lab member's NIH co-investigator to someone already in the notebook by name alone, so its shared-award record
        counts only when the person carries the NIH RePORTER profile ID onboarding found for the co-investigator."""
        for e in self.g.records_between(a, b):
            if self.from_openalex(e):
                continue
            if e["type"] == "grant" and e.get("sourceTitle") == NIH_AWARD_TITLE and not all(
                    any(NIH_PROFILE_NOTE in (s.get("note") or "") for s in self.contacts[x].get("sources") or [])
                    for x in (a, b) if x not in self.lab):
                continue
            return True
        return False

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
        return set().union(*self.known_papers(pid))

    def lab_paper_ids(self, pid: str) -> Set[str]:
        """The papers on someone's coauthor records with lab members. Their profile papers and other coauthor records
        may have come from their own OpenAlex record, so only these show which of its works are theirs."""
        out: Set[str] = set()
        for other in self.g.nbrs.get(pid, ()):
            if other in self.lab:
                for e in self.g.records_between(pid, other):
                    if e["type"] == "coauthor":
                        title, _ = _strip_year(e.get("sourceTitle", ""))
                        out.update(_paper_ids(title, e.get("sourceUrl", "")))
        return out

    def not_theirs(self, pid: str) -> Dict[str, Any]:
        """The OpenAlex records marked as not this person's, with the records the library joined to one of them (none
        for someone this run adds)."""
        if pid not in self.contacts:
            return {}
        return {**dict.fromkeys(self.voided.get(pid, ()), {}), **self.nb.openalex_not_theirs(pid)}

    def existing_match(self, nm: str, insts: Set[str], pid: str,
                       aid: str = "") -> Tuple[Optional[str], Optional[str], bool]:
        """(contact, None, firm) when exactly one same-name contact is confirmed by a tie to `pid` or a shared
        institution; (None, contact, False) when a same-name contact exists without confirmation; (None, None, False)
        otherwise. `firm` says the first names agree in full, so the match holds wherever the run meets the coauthor;
        a match by initial rests on the tie to `pid` alone. A contact who marked the coauthor's record `aid` as not
        theirs is never confirmed as that coauthor."""
        keys = {family_key(nm)} | surname_tokens(nm)
        pool = {x for k in keys for x in self.by_family.get(k, [])}
        scores = {x: name_score(self.contacts[x]["name"], nm) for x in pool}
        cands = [x for x, sc in scores.items() if sc]
        if not cands:
            return None, None, False
        # A tie to `pid` confirms a match by initial; a shared institution alone needs the full first name.
        ok = [x for x in cands if (x in self.g.nbrs.get(pid, ()) or
              (scores[x] == 2 and any(_inst_match(self.contacts[x].get("institution", ""), i) for i in insts)))
              and aid not in self.not_theirs(x)]
        if len(ok) == 1 and not self.oa_of.get(ok[0]):
            return ok[0], None, scores[ok[0]] == 2
        return None, (ok or cands)[0], False

    def resolve(self, aid: str, nm: str, insts: Set[str], via: str, shared: int,
                min_shared: int) -> Tuple[Optional[str], bool]:
        """The notebook person an OpenAlex coauthor is, or None. The flag says the coauthor was held back."""
        target = self.by_oa.get(aid)
        if target is not None:
            return target, False
        if aid in self.orcid_held:
            return None, True
        target, held, firm = self.existing_match(nm, insts, via, aid)
        if target:
            if firm:                    # a match by initial holds only for `via`, whose tie confirmed it
                self.by_oa[aid] = target
            return target, False
        match = ("same name as", held) if held else self.import_match(nm)
        if match:
            if shared >= min_shared and aid not in self.held_ids:
                self.held_ids.add(aid)
                self.held.append({"name": nm, "openalexId": aid, "via": self.name(via), "sameNameAs": match[1],
                                  "nameMatch": match[0], "shared": shared})
            return None, True
        return None, False

    def import_match(self, nm: str) -> Optional[Tuple[str, str]]:
        """(how, person) when the importer would hold back a new person with this name: the same name as someone in the
        notebook or an earlier new person, or the same surname and a first name that begins the other's."""
        key = name_key(nm)
        if not key:
            return None
        same = self.by_key.get(key) or self.new_keys.get(key)
        if same:
            return "same name as", same
        for other, pid in self.by_surname.get(key.split()[-1], []):
            if _initials_match(key, other):
                return "similar name to", pid
        return None

    def add_person(self, aid: str, nm: str, via: str, shared: int, insts: Set[str], degree: int) -> str:
        base = slugify(nm)
        rid, n = base, 2
        while self.nb.id_taken("contacts", rid) or rid in self.new_names:
            rid = f"{base}-{n}"
            n += 1
        self.new_people[aid] = {"id": rid, "name": nm, "via": via, "shared": shared, "insts": sorted(insts),
                                "degree": degree}
        self.new_names[rid] = nm
        self.by_oa[aid] = rid
        if name_key(nm):
            self.new_keys.setdefault(name_key(nm), rid)
        return rid

    def hold_same_orcid(self, details: Dict[str, Dict[str, Any]], aids: List[str], owners: Dict[str, str]) -> Set[str]:
        """Take out of the plan, with their coauthor records, the new people whose OpenAlex record carries an ORCID iD
        in `owners` (iD -> person ID; each new person kept adds theirs), and list them. Returns the IDs taken out."""
        gone = set()
        for aid in aids:
            plan = self.new_people.get(aid)
            orcid = oc.clean((details.get(aid) or {}).get("orcid") or "").upper()
            if plan is None or not orcid:
                continue
            if orcid not in owners:
                owners[orcid] = plan["id"]
                continue
            gone.add(self._take_out(aid))
            self.held.append({"name": plan["name"], "openalexId": aid, "via": self.name(plan["via"]),
                              "sameOrcidAs": owners[orcid], "orcid": orcid, "shared": plan["shared"]})
        self._drop_edges(gone)
        return gone

    def drop_unchecked(self, aids: List[str]) -> Set[str]:
        """Take out of the plan, with their coauthor records, the new people whose OpenAlex author records could not be
        read, so that their ORCID iD went unchecked; the next run adds them. Returns the IDs taken out."""
        gone = {self._take_out(a) for a in aids if a in self.new_people}
        self._drop_edges(gone)
        return gone

    def _take_out(self, aid: str) -> str:
        """Remove a planned new person, who is then not added again in this run."""
        plan = self.new_people.pop(aid)
        self.by_oa.pop(aid, None)
        if self.new_keys.get(name_key(plan["name"])) == plan["id"]:
            del self.new_keys[name_key(plan["name"])]
        self.orcid_held.add(aid)
        return plan["id"]

    def _drop_edges(self, gone: Set[str]) -> None:
        if gone:
            self.edges = [e for e in self.edges if e["from"] not in gone and e["to"] not in gone]
            self.pairs = {p for p in self.pairs if p[0] not in gone and p[1] not in gone}

    def add_edge(self, a: str, b: str, n: int, w: Dict[str, Any], since: Optional[int]) -> bool:
        pair = tuple(sorted((a, b)))
        if pair in self.pairs or a == b:
            return False
        self.pairs.add(pair)
        title = oa.work_title(w)[:290]          # with " (year)", within the 300 characters a source title may have
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


def _orcid_owners(P: _Plan) -> Dict[str, str]:
    """ORCID iD -> the person in the notebook it belongs to (the first one listed, when several carry it)."""
    owners: Dict[str, str] = {}
    for pid in sorted(P.active):
        for o in P.orcids(pid):
            owners.setdefault(o, pid)
    return owners


def _orcid_on(w: Dict[str, Any], aid: str) -> str:
    """The ORCID iD on an author's record as a work lists it, or ''."""
    for a in w.get("authorships") or []:
        au = a.get("author") or {}
        if oa.short_id(au.get("id") or "") == aid:
            return oc.clean(au.get("orcid") or "").upper()
    return ""


def _only_through(P: _Plan, flagged: Set[str]) -> Dict[str, List[str]]:
    """For each flagged person, the people within two degrees of the lab who would be further out without the flagged
    people, found through chains of such people that start beside them."""
    def within_two(skip: Set[str]) -> Set[str]:
        seen = {r for r in P.lab if r in P.active}
        frontier = set(seen)
        for _ in range(2):
            frontier = {y for x in frontier for y in P.g.nbrs.get(x, ()) if y not in seen and y not in skip}
            seen |= frontier
        return seen
    dropped = within_two(set()) - within_two(flagged) - flagged
    out: Dict[str, List[str]] = {}
    for p in flagged:
        found, stack = set(), [o for o in P.g.nbrs.get(p, ()) if o in dropped]
        while stack:
            o = stack.pop()
            if o not in found:
                found.add(o)
                stack += [x for x in P.g.nbrs.get(o, ()) if x in dropped and x not in found]
        if found:
            out[p] = sorted(found)
    return out


def _given_orcid_works(P: _Plan, client: Client, pid: str, errors: List[str], checks: List[str],
                       unread: Optional[Set[str]] = None) -> Set[str]:
    """The DOIs a lab member lists on the ORCID records whose iDs are on their own record, read fresh each run. An iD
    that OpenAlex shows on their author record but that is not on their record does not count; it is listed for a
    person to check. A member whose ORCID record cannot be read is added to `unread`."""
    name, given, dois = P.contacts[pid]["name"], P.given_orcids(pid), set()
    for o in given:
        if not oc.valid(o):
            errors.append(f"{name}: the ORCID iD {o} on their record is not a valid iD (its check digit does not "
                          "match). Correct it on their record so the papers they list there can count.")
            continue
        try:
            dois |= {d for d, _ in oc.work_dois(client, o, fresh=True)}
        except BudgetReached:
            raise
        except HttpError as exc:
            errors.append(f"{name}: ORCID record {o} could not be read ({exc}), so no work of theirs counted for being "
                          "listed there.")
            if unread is not None:
                unread.add(pid)
    for o in P.orcids(pid):
        if o not in given:
            checks.append(f"{name}: their OpenAlex record lists the ORCID iD {o}, which is not on their record. If it "
                          f"is theirs, add {oc.record_url(o)} to their record, and the papers they list there will "
                          "count. If it is not, OpenAlex may have filed another researcher's papers under their record.")
    return dois


def _lab_members_works(P: _Plan, client: Client, lab_works: Dict[str, List[Dict[str, Any]]], beyond_oa: Set[str],
                       errors: List[str], checks: List[str], unread: Optional[Set[str]] = None
                       ) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]], Set[str]]:
    """The works that count for each lab member, the works left out, and the people in the notebook that the works
    that count name. A person confirmed each member's OpenAlex
    record, not each work on it, and OpenAlex sometimes files two researchers with the same name under one record. A
    work counts when it names another lab member, someone whose relationship with the lab shows more than an OpenAlex
    coauthorship (`beyond_oa`), or someone named on another member's works that count; when it is on an ORCID
    record whose iD is on the member's own record; or when it lists the member at their recorded institution or at an
    institution where two such works list them. Members' works vouch for one another's coauthors, so the works that
    count are found in passes until a pass adds none."""
    mine = {pid: set(P.oa_of[pid]) for pid in lab_works}
    listed = {pid: _given_orcid_works(P, client, pid, errors, checks, unread) for pid in lab_works}
    found: Dict[Tuple[str, str], Optional[str]] = {}

    def who(aid: str, nm: str, ins: List[str], pid: str) -> Optional[str]:
        """The person in the notebook an authorship names: by OpenAlex ID, else by full name and a tie to the member."""
        if (aid, pid) not in found:
            q = P.by_oa.get(aid)
            if q is None:
                q, _, firm = P.existing_match(nm, set(ins), pid, aid)
                q = q if firm else None
            found[(aid, pid)] = q
        return found[(aid, pid)]

    named: Dict[str, Set[str]] = {pid: set() for pid in lab_works}    # OpenAlex IDs and people on works that count
    kept: Dict[str, List[Dict[str, Any]]] = {pid: [] for pid in lab_works}
    changed = True
    while changed:
        changed = False
        for pid, works in lab_works.items():
            others = set().union(*(named[o] for o in lab_works if o != pid))
            places = [x.strip() for x in (P.contacts[pid].get("institution") or "").split(";") if x.strip()]

            def anchored(w: Dict[str, Any]) -> bool:
                for aid, nm, ins in _authorships(w):
                    if aid in mine[pid]:
                        if any(_inst_match(p, i) for p in places for i in ins):
                            return True
                        continue
                    q = who(aid, nm, ins, pid)
                    if aid in others or (q is not None and (q in others or q in beyond_oa or
                                                            (q in P.lab and q != pid))):
                        return True
                return (w.get("doi") or "").lower().replace("https://doi.org/", "") in listed[pid]
            kept[pid], _ = _anchored(works, mine[pid], anchored)
            now = {x for w in kept[pid] for aid, nm, ins in _authorships(w) if aid not in mine[pid]
                   for x in (aid, who(aid, nm, ins, pid)) if x}
            if now != named[pid]:
                named[pid], changed = now, True
    left = {}
    for pid, works in lab_works.items():
        ids = {id(w) for w in kept[pid]}
        left[pid] = [w for w in works if id(w) not in ids]
    return kept, left, {x for xs in named.values() for x in xs if x in P.contacts}


def _beyond_oa(P: _Plan) -> Set[str]:
    """People whose relationship with the lab shows more than an OpenAlex coauthorship."""
    return {q for o in P.lab for q in P.g.nbrs.get(o, ()) if q not in P.lab and P.entered(q, o)}


def _anchor_people(P: _Plan, first: Set[str], unshown: Set[str]) -> Dict[str, str]:
    """OpenAlex ID -> the person in the network it names, for everyone who may vouch for a first-degree person's work
    (step 4). Not a first-degree person whose only relationship with the lab nothing confirms (`unshown`), and not
    someone expand or onboarding added to the second degree unless they are first degree now (`first`): they are known
    only from the kind of OpenAlex coauthorship this step checks, and counting them would let each run vouch for the
    papers the run before it left out."""
    return {aid: q for aid, q in P.by_oa.items() if q not in unshown and
            (q in first or not SECOND_DEGREE_TAGS & set((P.contacts.get(q) or {}).get("tags") or []))}


def _voucher(P: _Plan, anchors: Dict[str, str], roots: Set[str], lab_counted: Dict[str, Set[str]],
             pid: str) -> Callable[[str, Dict[str, Any]], bool]:
    """Whether the person an authorship names (by OpenAlex ID) vouches for a work of the first-degree person `pid`:
    someone in `anchors` other than them whose place in the network rests on more than a tie to them (a member of
    `roots`, or someone with other relationships). A lab member vouches only for a work that counts for them
    (`lab_counted`, OpenAlex IDs), since their record may hold a namesake's papers."""
    def vouches(aid: str, w: Dict[str, Any]) -> bool:
        q = anchors.get(aid)
        if q in (None, pid) or not (q in roots or P.g.nbrs.get(q, set()) - {pid}):
            return False
        return q not in P.lab or w.get("id") in lab_counted.get(q, ())
    return vouches


def _first_degree_anchor(mine: Set[str], places: List[str], known: Set[str],
                         vouches: Callable[[str, Dict[str, Any]], bool]) -> Callable[[Dict[str, Any]], bool]:
    """Step 4's test of whether a first-degree person's work connects by itself to what is known about them: their own
    authorship (`mine`) lists an institution recorded for them (`places`), it names someone who vouches for it, or it is
    among their papers with lab members (`known`, paper identifiers). `_anchored` adds the works that list them at an
    institution where two such works do."""
    def anchored(w: Dict[str, Any]) -> bool:
        for a in w.get("authorships") or []:
            aid = oa.short_id((a.get("author") or {}).get("id") or "")
            if aid in mine:
                if any(_inst_match(p, i.get("display_name", "")) for p in places for i in a.get("institutions") or []):
                    return True
            elif aid and vouches(aid, w):
                return True
        return bool(known) and bool(known & set(_work_ids(w)))
    return anchored


def connected_works(works: List[Dict[str, Any]], mine: Set[str], places: List[str], known_works: List[Dict[str, Any]],
                    vouches: Callable[[str, Dict[str, Any]], bool], known_ids: Set[str] = frozenset()
                    ) -> List[Dict[str, Any]]:
    """The works that count for a person other than a lab member by step 4's rule (`_first_degree_anchor`, then
    `_anchored`): `mine` are their OpenAlex records, `places` their recorded institutions, `known_works` (or the paper
    identifiers `known_ids`) their papers with lab members, and `vouches` says who vouches for a work."""
    known = set(known_ids) | {i for w in known_works for i in _work_ids(w)}
    return _anchored(works, mine, _first_degree_anchor(mine, places, known, vouches))[0]


class LabCheck:
    """Expand's identity rules for the scheduled refresh and onboarding, which add people and coauthor records from
    OpenAlex between expand runs. `svc` is the Service, or a draft with the same `nb` and `graph` (onboarding's, before
    the packet exists).

    Lab members (step 2): each member's works with 30 or fewer authors that count are in `kept`, by OpenAlex ID in
    `counted`, and the rest in `left`. `unread` holds the members whose OpenAlex works or ORCID records could not be
    read; with them, fewer works count than would otherwise, for every member and for the people their works vouch
    for. A lab member without OpenAlex records is not looked up.

    Everyone else (step 4): `collects` leaves out a first-degree person whose only relationship with the lab is a
    coauthor record written from OpenAlex and whom no lab member's work that counts names, and `anchored` keeps the
    works that connect to what is known about a person.

    `fresh` reads the lab members' works again instead of from the response cache, for a caller judging works newer
    than its cache."""

    def __init__(self, svc: Service, client: Client, errors: List[str], checks: List[str],
                 lib: Optional[Dict[str, Any]] = None, fresh: bool = False):
        self.P = P = _Plan(svc, 0, lib)
        lab_works: Dict[str, List[Dict[str, Any]]] = {}
        self.unread: Set[str] = set()
        for pid in P.nb.root_ids:
            if pid not in P.active or pid not in P.oa_of:
                continue
            try:
                lab_works[pid], _ = _all_works(client, P.oa_of[pid], use_cache=not fresh)
            except BudgetReached:
                raise
            except HttpError as exc:
                errors.append(f"{P.contacts[pid]['name']}: OpenAlex could not be read ({exc}), so none of their works "
                              "counted.")
                self.unread.add(pid)
        self.beyond_oa = _beyond_oa(P)
        self.kept, self.left, self.named = _lab_members_works(P, client, lab_works, self.beyond_oa, errors, checks,
                                                              self.unread)
        self.counted = {pid: {w.get("id") for w in works} for pid, works in self.kept.items()}
        self._step4: Optional[Tuple[Set[str], Dict[str, str], Set[str]]] = None

    def _people(self) -> Tuple[Set[str], Dict[str, str], Set[str]]:
        """(unshown first-degree people, anchors, roots), worked out once."""
        if self._step4 is None:
            P = self.P
            dist, _ = P.g.lab_distances()
            first = {p for p, d in dist.items() if d == 1 and p in P.active}
            unshown = {p for p in first if p not in self.named and p not in self.beyond_oa}
            self._step4 = (unshown, _anchor_people(P, first - unshown, unshown), P.lab & P.active)
        return self._step4

    def collects(self, pid: str) -> bool:
        """Whether step 4 reads this person's works: everyone but a first-degree person whose relationship with the
        lab nothing confirms."""
        return pid not in self._people()[0]

    def anchored(self, pid: str, works: List[Dict[str, Any]], mine: Set[str]) -> List[Dict[str, Any]]:
        """The works of this person (their OpenAlex records: `mine`) that connect to what is known about them."""
        P = self.P
        _, anchors, roots = self._people()
        places = [x.strip() for x in (P.contacts[pid].get("institution") or "").split(";") if x.strip()]
        return connected_works(works, mine, places, [], _voucher(P, anchors, roots, self.counted, pid),
                               known_ids=P.lab_paper_ids(pid))


def plan_expand(svc: Service, client: Client, since_year: int = 2016, per_person: Optional[int] = None,
                min_shared: int = 2, only: Optional[List[str]] = None, log: Callable[[str], None] = print,
                lib: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if lib is None:
        from .library import load
        lib = load(svc.ws)
    P = _Plan(svc, since_year, lib)
    nb, contacts = P.nb, P.contacts
    dist, _ = svc.lab_dist(TieFilter())
    lab = [r for r in nb.root_ids if r in P.active]
    first = sorted(p for p, d in dist.items() if d == 1 and p in P.active)
    coverage: Dict[str, Dict[str, Any]] = {}
    identified: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    swept: Dict[str, List[Tuple[str, str]]] = defaultdict(list)     # person -> (coauthor, person) records added

    # 1. Lab members, accepted only on works already recorded for them.
    lab_works: Dict[str, List[Dict[str, Any]]] = {}
    read_failed: Set[str] = set()       # lab members whose OpenAlex records could not be searched or read
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
                read_failed.add(pid)
            if accepted:
                P.oa_of[pid] = [a for a, _ in accepted]
                for a, _ in accepted:
                    P.by_oa[a] = pid
                hits = sum(h for _, h in accepted)
                identified[pid] = {"ids": P.oa_of[pid], "source": "expand",
                                   "basis": f"their OpenAlex works include {hits} "
                                            f"paper{'s' if hits != 1 else ''} already recorded for them"}
        if pid in P.oa_of:
            try:
                lab_works[pid], _ = _all_works(client, P.oa_of[pid])
            except BudgetReached:
                raise
            except HttpError as exc:
                errors.append(f"{name}: {exc}")
                log(f"  {name}: OpenAlex could not be read ({exc}); their earlier note stays")
                read_failed.add(pid)
                continue
            log(f"  {name}: {len(lab_works[pid])} OpenAlex works with {MAX_AUTHORS} or fewer authors")
        elif pid in read_failed:
            log(f"  {name}: OpenAlex could not be searched; their earlier note stays")
        else:
            coverage[pid] = {"status": "not-found", "degree": 0}
            log(f"  {name}: not found in OpenAlex")
    lab_ids = {a for pid in lab for a in P.oa_of.get(pid, [])}
    beyond_oa = _beyond_oa(P)
    checks: List[str] = []
    lab_works, lab_left, _ = _lab_members_works(P, client, lab_works, beyond_oa, errors, checks)

    # 2. Complete the first degree: every coauthor of a lab member on the works that count.
    new_first: List[str] = []
    reached: Set[str] = set()          # people named with a lab member on works that count
    held_named: Dict[str, str] = {}    # person -> a note for a person to check, if they turn out to be unconfirmed
    for pid, works in lab_works.items():
        left_out = len(lab_left[pid])
        if left_out and not works:
            coverage[pid] = {"status": "unanchored", "degree": 0, "works": 0, "unanchored": left_out}
            continue
        ranked, counts, latest, names, insts = _coauthor_counts(works, set(P.oa_of[pid]))
        for aid in ranked:
            if aid in lab_ids:
                continue
            target, held = P.resolve(aid, names[aid], insts[aid], pid, counts[aid], 1)
            if held:
                # Held back for having the full name of someone tied to the member, under another OpenAlex record: it
                # names them when the institution or ORCID iD agrees. Otherwise a person decides.
                _, same, _ = P.existing_match(names[aid], insts[aid], pid, aid)
                if same in P.g.nbrs.get(pid, ()) and name_score(contacts[same]["name"], names[aid]) == 2:
                    orcid = _orcid_on(latest[aid], aid)
                    if any(_inst_match(contacts[same].get("institution", ""), i) for i in insts[aid]) or \
                            (orcid and orcid in P.orcids(same)):
                        reached.add(same)
                    else:
                        held_named.setdefault(same, (
                            f"{P.name(pid)}'s OpenAlex paper “{oa.work_title(latest[aid])[:150]}” names "
                            f"{names[aid]} under the record {oa.author_url(aid)}, which is not linked to "
                            f"{contacts[same]['name']}'s record. If it is theirs, add it to their record."))
                continue
            if target is None:
                target = P.add_person(aid, names[aid], pid, counts[aid], insts[aid], degree=1)
                new_first.append(aid)
            reached.add(target)
            if P.add_edge(pid, target, counts[aid], latest[aid], None):
                swept[pid].append((aid, target))
        coverage[pid] = {"status": "collected", "degree": 0, "works": len(works), "coauthors": len(counts)}
        if left_out:
            coverage[pid]["unanchored"] = left_out
    # A new first-degree person whose OpenAlex record carries the ORCID iD of someone in the notebook, or of another
    # new person, is held back before anyone is added through them.
    owners = _orcid_owners(P)
    details: Dict[str, Dict[str, Any]] = {}
    unchecked, stop = _author_details(client, new_first, details, errors)
    if stop:
        raise stop
    gone = P.drop_unchecked(unchecked) | P.hold_same_orcid(details, new_first, owners)
    new_first = [a for a in new_first if a in P.new_people]
    # People already in the notebook whom this run gives their first coauthor record with a lab member are first
    # degree once it is applied, so their coauthors are collected in the same run.
    first = sorted(set(first) | {t for recs in swept.values() for _, t in recs if t in P.active and dist.get(t, 2) > 1})
    # A first-degree person whose only relationship with the lab is a coauthor record written from OpenAlex is
    # collected when this run finds them on a lab member's works that count. Otherwise that record may come from a
    # namesake's paper on the member's OpenAlex record, from a paper with more than 30 authors, or from a record no
    # longer linked to the member, so they are not collected; nor do they vouch for anyone's papers. This is decided for
    # the whole first degree, also in a run for named people only.
    left_named: Dict[str, Set[str]] = defaultdict(set)      # person -> members whose left-out works name them
    for pid, works in lab_left.items():
        for w in works:
            for aid, nm, ins in _authorships(w):
                if aid not in P.oa_of[pid]:
                    q = P.by_oa.get(aid) or P.existing_match(nm, set(ins), pid, aid)[0]
                    if q:
                        left_named[q].add(pid)
    unshown = {p for p in first if p not in reached and p not in beyond_oa}
    # Someone tied to a lab member whose OpenAlex records could not be read is not judged: their earlier note stays.
    unjudged = {p for p in unshown if read_failed & P.g.nbrs.get(p, set())}
    if unjudged:
        errors.append(f"{len(unjudged)} first-degree {'person was' if len(unjudged) == 1 else 'people were'} not "
                      "collected because a lab member's OpenAlex records could not be read; the next run checks them "
                      "again.")
    if only:
        wanted = {norm_text(x) for x in only}
        first = [p for p in first if p in only or norm_text(contacts[p]["name"]) in wanted]
        new_first = [a for a in new_first if norm_text(P.new_people[a]["name"]) in wanted]
    first = [p for p in first if p not in unjudged]
    n_first = len(first) + len(new_first)
    only_through = _only_through(P, unshown)
    for p in first:
        if p in unshown:
            coverage[p] = ({"status": "tie-unanchored", "degree": 1, "through": sorted(left_named[p])}
                           if p in left_named else {"status": "tie-unconfirmed", "degree": 1})
            if only_through.get(p):         # people earlier runs added who are in the network only through them
                coverage[p]["onlyThrough"] = only_through[p]
            if p in held_named:
                checks.append(held_named[p])
    first = [p for p in first if p not in unshown]

    # 3. Identify first-degree people already in the notebook, on the same works that make someone first degree.
    lab_coauthors: Dict[str, List[Tuple[str, str, str, Set[str]]]] = defaultdict(list)
    for member, works in lab_works.items():
        for w in works:
            if len(w.get("authorships") or []) > MAX_AUTHORS:
                continue
            wids = set(_work_ids(w))
            for aid, nm, _ in _authorships(w):
                if aid not in lab_ids:
                    for k in {family_key(nm)} | surname_tokens(nm):
                        lab_coauthors[k].append((aid, nm, member, wids))
    for pid in first:
        c = contacts[pid]
        name = c["name"]
        if pid in P.oa_of:
            identified[pid] = {"ids": P.oa_of[pid], "basis": P.oa_basis[pid], "source": P.oa_source[pid]}
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
        identified[pid] = {"ids": [pick[0]], "basis": pick[1], "source": "expand"}
    log(f"  first degree: {len(new_first)} coauthors of lab members added; "
        f"{sum(1 for p in first if p in identified)} of {len(first)} existing first-degree people identified")

    # 4. Second degree: the coauthors of every identified first-degree person, on works that connect to what is known
    # about them. The first sweep decides who is added; the second links every first-degree person to each coauthor
    # now in the notebook or the plan, so the order people are processed in does not matter.
    since = f"{since_year}-01-01"
    targets = [(p, P.oa_of[p]) for p in first if p in identified] + \
              [(P.new_people[a]["id"], [a]) for a in new_first]
    # Who is in the network before anyone is added to the second degree, and may vouch for a first-degree person's work.
    anchors = _anchor_people(P, set(first), unshown)
    roots = set(lab) | {P.new_people[a]["id"] for a in new_first}
    lab_counted = {pid: {w.get("id") for w in works} for pid, works in lab_works.items()}
    orcid_of: Dict[str, str] = {pid: (P.orcids(pid) or [""])[0] for pid, _ in targets if pid in contacts}
    orcid_of.update({P.new_people[a]["id"]: oc.clean((details.get(a) or {}).get("orcid") or "").upper()
                     for a in new_first})
    stopped = False
    # People the library has not summarized may still carry an ORCID iD on their OpenAlex record.
    missing = [a for pid, mine in targets if pid in contacts and not orcid_of.get(pid)
               and not P.library_openalex(pid).get("summary") for a in mine]
    try:
        records = oa.get_authors(client, missing) if missing else {}
        for pid, mine in targets:
            if not orcid_of.get(pid):
                orcid_of[pid] = next((oc.clean(records[a].get("orcid") or "").upper() for a in mine
                                      if (records.get(a) or {}).get("orcid")), "")
    except BudgetReached:
        stopped = True
    except HttpError as exc:
        errors.append(f"OpenAlex author records: {exc}")
    for pid, o in orcid_of.items():           # a new person with one of these iDs is another record of theirs
        if o:
            owners.setdefault(o, pid)
    collected: Dict[str, Tuple] = {}
    for pid, mine in targets:
        if stopped:
            coverage[pid] = {"status": "stopped", "degree": 1}
            continue
        name = P.name(pid)
        try:
            works, truncated = _all_works(client, mine, since=since)
            listed: List[Dict[str, Any]] = []
            if orcid_of.get(pid):
                try:
                    listed = _orcid_listed(client, orcid_of[pid], works, since_year)
                except BudgetReached:
                    raise
                except HttpError as exc:
                    errors.append(f"{name}: papers listed on ORCID: {exc}")
        except BudgetReached:
            stopped = True
            coverage[pid] = {"status": "stopped", "degree": 1}
            continue
        except HttpError as exc:
            errors.append(f"{name}: {exc}")
            coverage[pid] = {"status": "not-found", "degree": 1, "reason": str(exc)}
            continue
        # A paper listed on ORCID counts when OpenAlex names the person on it; their authorship there is theirs.
        selves = [_selves(w, name, orcid_of[pid]) for w in listed]
        listed = [w for w, s in zip(listed, selves) if s]
        mine_all = set(mine).union(*selves)
        known = P.lab_paper_ids(pid) if pid in contacts else set()
        places = [x.strip() for x in (contacts[pid].get("institution") or "").split(";") if x.strip()] \
            if pid in contacts else []
        anchored = _first_degree_anchor(mine_all, places, known, _voucher(P, anchors, roots, lab_counted, pid))
        kept, left_out = _anchored(works + listed, mine_all, anchored)
        if left_out and not kept:
            coverage[pid] = {"status": "unanchored", "degree": 1, "works": 0, "unanchored": left_out}
            continue
        ranked, counts, latest, names, insts = _coauthor_counts(kept, mine_all)
        collected[pid] = (ranked, counts, latest, names, insts)
        cap = 0     # this person's coauthors added by any expansion, now or before (with a cap only)
        for aid in ranked:
            if counts[aid] < min_shared:
                break
            target, held = P.resolve(aid, names[aid], insts[aid], pid, counts[aid], min_shared)
            if held:
                continue
            if target is not None:
                if per_person is not None and (P.added_by_expansion(target) or
                                               P.new_people.get(aid, {}).get("degree") == 2):
                    cap += 1
            elif per_person is None or cap < per_person:
                P.add_person(aid, names[aid], pid, counts[aid], insts[aid], degree=2)
                cap += 1
        coverage[pid] = {"status": "collected", "degree": 1, "works": len(kept), "coauthors": len(counts)}
        listed_ids = {w.get("id") for w in listed}
        listed_kept = sum(1 for w in kept if w.get("id") in listed_ids)
        coverage[pid].update({k: v for k, v in (("unanchored", left_out), ("orcidWorks", listed_kept),
                                                 ("truncated", truncated)) if v})
    for pid, (ranked, counts, latest, names, insts) in collected.items():
        for aid in ranked:
            target, held = P.resolve(aid, names[aid], insts[aid], pid, counts[aid], min_shared)
            # A coauthor record with a lab member comes only from step 2, from a work that counts for the member.
            if target is None or held or target in P.lab:
                continue
            if P.add_edge(pid, target, counts[aid], latest[aid], since_year):
                swept[pid].append((aid, target))
    log(f"  second degree: {sum(1 for p in P.new_people.values() if p['degree'] == 2)} people added, "
        f"{len(P.edges)} coauthor records in all")

    # 5. Details for new people: OpenAlex author records, then ORCID for degrees and current positions. A new person
    # whose record carries an ORCID iD already taken is held back, as in step 2.
    second = [a for a, p in P.new_people.items() if p["degree"] == 2]
    unchecked, stop = _author_details(client, second, details, errors)
    if stop:                        # whose coauthors it could not finish: the next run does
        stopped = True
        for via in {P.new_people[a]["via"] for a in unchecked if a in P.new_people}:
            if (coverage.get(via) or {}).get("status") == "collected":
                coverage[via]["status"] = "stopped"
    gone |= P.drop_unchecked(unchecked)
    gone |= P.hold_same_orcid(details, [a for a in second if a in P.new_people], owners)
    added_by = Counter(p["via"] for p in P.new_people.values())
    for pid, info in coverage.items():
        if info["status"] == "collected":
            info["added"] = added_by[pid]
            info["linked"] = sum(1 for aid, t in swept.get(pid, []) if t not in gone and
                                 P.new_people.get(aid, {}).get("via") != pid)
    for pid, info in identified.items():
        if pid in coverage:
            coverage[pid].update({"openalexIds": info["ids"], "basis": info["basis"]})
    for aid in new_first:
        rid = P.new_people[aid]["id"]
        if rid in coverage:
            coverage[rid].update({"openalexIds": [aid],
                                  "basis": f"coauthor of {P.name(P.new_people[aid]['via'])} in OpenAlex"})
    log(f"  reading ORCID records for {sum(1 for a in P.new_people if (details.get(a) or {}).get('orcid'))} new "
        "people with an ORCID iD")
    new_contacts = [_contact_record(client, aid, plan, details.get(aid) or {}, P.name(plan["via"]), since_year, errors)
                    for aid, plan in P.new_people.items()]
    updates = []
    for pid, info in identified.items():
        # Links already on their record, or the library's match, stay where they are. A match whose works were not
        # read, or did not connect to what is known about them, is not written to their record.
        if info["source"] != "expand" or (coverage.get(pid) or {}).get("status") != "collected" \
                or not coverage[pid].get("works"):
            continue
        no = nb.openalex_not_theirs(pid)      # a link marked as not theirs does not count
        if any(a not in no for s in contacts[pid].get("sources") or [] for a in oa.find_author_ids(s.get("url", ""))):
            continue
        updates.append({"id": pid, "sources": [{"url": oa.author_url(a), "note": (
            f"OpenAlex author record, matched by expand on {today()}: {info['basis']}.")[:1000]} for a in info["ids"]]})
    return {
        "notebook": {"schemaVersion": 1, "contacts": updates + new_contacts, "edges": P.edges, "meetings": []},
        "coverage": coverage,
        "report": {"held": P.held, "errors": errors, "checks": checks, "requests": client.requests,
                   "costUsd": round(client.cost_usd, 4),
                   "newFirstDegree": sum(1 for p in P.new_people.values() if p["degree"] == 1),
                   "newSecondDegree": sum(1 for p in P.new_people.values() if p["degree"] == 2),
                   "linksAdded": len(updates)},
        "standard": {"sinceYear": since_year, "perPerson": per_person, "minShared": min_shared,
                     "maxAuthors": MAX_AUTHORS},
        "firstDegree": n_first, "stopped": stopped,
    }


def _contact_record(client: Client, aid: str, plan: Dict[str, Any], a: Dict[str, Any], via_name: str,
                    since_year: int, errors: List[str]) -> Dict[str, Any]:
    url = oa.author_url(aid)
    last = [i.get("display_name", "") for i in a.get("last_known_institutions") or [] if i.get("display_name")]
    institution = "; ".join(last[:2]) or "; ".join(plan["insts"][:2])
    orcid = (a.get("orcid") or "").replace("https://orcid.org/", "")
    why = ("a coauthor of " + via_name if plan["degree"] == 1 else
           f"a coauthor of {via_name} on {plan['shared']} works since {since_year}")
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
            errors.append(f"{plan['name']}: ORCID record {orcid} could not be read ({exc}), so they were added "
                          "without the doctorate and position it lists")
            rec["sources"].append({"url": oc.record_url(orcid), "note": (
                f"ORCID iD on their OpenAlex record. ORCID did not return the record when expand asked on {today()}.")})
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


def expand(ws_path: str, since_year: int = 2016, per_person: Optional[int] = None, min_shared: int = 2,
           only: Optional[List[str]] = None, dry_run: bool = False, budget_usd: Optional[float] = None,
           client: Optional[Client] = None, log: Callable[[str], None] = print) -> Dict[str, Any]:
    """`budget_usd` defaults to the library's: $0.90 of the $1 a day an OpenAlex key allows, else $0.08 of the free
    $0.10."""
    from .library import default_budget, load
    ws = Workspace(ws_path)
    svc = Service(ws)
    client = client or Client(cache_dir=default_cache(ws),
                              budget_usd=default_budget() if budget_usd is None else budget_usd,
                              mailto=(ws.config.get("settings") or {}).get("contactEmail", ""))
    log("Reading OpenAlex" + (" (dry run: nothing is saved)" if dry_run else ""))
    try:
        plan = plan_expand(svc, client, since_year=since_year, per_person=per_person, min_shared=min_shared,
                           only=only, log=log, lib=load(ws))
    except BudgetReached as exc:
        raise SystemExit(f"Stopped before finishing: {exc}. Responses so far are cached; run again tomorrow.")
    nbk, rep = plan["notebook"], plan["report"]
    statuses = Counter(v["status"] for v in plan["coverage"].values() if v.get("degree") == 1)
    log(f"First-degree people: {plan['firstDegree']}. " +
        "; ".join(f"{n} {STATUS_TEXT[s]}" for s, n in statuses.items()))
    log(f"Plan: {rep['newFirstDegree']} new first-degree and {rep['newSecondDegree']} new second-degree people, "
        f"{len(nbk['edges'])} new coauthor records, {rep['linksAdded']} OpenAlex links on existing records, "
        f"{len(rep['held'])} held back as possible duplicates. {client.requests} requests, "
        f"${client.cost_usd:.4f} of OpenAlex's daily allowance.")
    for c in rep.get("checks") or []:
        log(f"To check: {c}")
    if plan["stopped"]:
        log("The run reached its spending limit before it finished; run expand again tomorrow to add the rest.")
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
            svc = Service(ws)               # the workspace as it is now: the plan may have taken hours
            imp = Importer(svc)
            prev = imp.preview(filename, data, size_limit=False)
            result = imp.apply(prev["batchId"], {k: "skip" for k in prev["needsDecision"]}, actor)
            result["heldForReview"], result["heldMatches"] = _held(prev), _held_matches(prev)
            note = svc.add_workspace_note(coverage_note(plan, result["batchId"]), actor)
        finally:
            ws.release_lock()
    path = _write_digest(ws, svc, plan, result)
    counts = {k: v for k, v in result["applied"].items() if k != "rejectedAtApply"}   # listed in the digest's .json
    log(f"Applied as import batch {result['batchId']}: {counts}. Digest: {path}")
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
    from .importer import MAX_BYTES
    from .server import MAX_BODY
    upload = {"filename": filename, "content": data.decode("utf-8")}
    if len(data) > MAX_BYTES or len(json.dumps(upload).encode("utf-8")) > MAX_BODY:
        raise SystemExit(f"The plan is {len(data) / 1e6:.0f} MB, more than the running app accepts in one import. "
                         "Stop the app and run expand again: without the app, expand applies the plan itself. "
                         "Responses are cached, so the second run is quicker.")
    prev = _post(port, "/api/import/preview", upload, actor)
    result = _post(port, "/api/import/apply", {"batchId": prev["batchId"],
                                               "decisions": {k: "skip" for k in prev["needsDecision"]}}, actor)
    result["heldForReview"], result["heldMatches"] = _held(prev), _held_matches(prev)
    return result


def _held_matches(prev: Dict[str, Any]) -> List[str]:
    """For each person the import held back, in `_held`'s order: who they may duplicate."""
    out = []
    for it in prev["items"]:
        if it["key"] not in prev["needsDecision"]:
            continue
        m = (it.get("matches") or [{}])[0]
        reasons = m.get("reasons") or []
        out.append("same name as another person in this run" if any(r.startswith("same name as row") for r in reasons)
                   else f"similar name to {m['name']}" if reasons == ["similar name"]
                   else f"same name as {m['name']}" if m.get("name") else "")
    return out


def _write_digest(ws: Workspace, svc: Service, plan: Dict[str, Any], result: Dict[str, Any]) -> str:
    ddir = os.path.join(ws.path, "digests")
    os.makedirs(ddir, exist_ok=True)
    names = {pid: c["name"] for pid, c in svc.nb.records["contacts"].items()}
    names.update({c["id"]: c["name"] for c in plan["notebook"]["contacts"] if c.get("name")})
    st, rep, ap = plan["standard"], plan["report"], result["applied"]
    rule = (f"everyone else with {st['minShared']} or more shared works was added" if st.get("perPerson") is None else
            f"up to {st['perPerson']} others with {st['minShared']} or more shared works were added per person")
    works = lambda n: f"{n} {'work' if n == 1 else 'works'}"
    lines = [f"# Network expansion, {today()}", "",
             f"First degree: every OpenAlex coauthor of a lab member on papers with {st['maxAuthors']} or fewer authors. "
             f"A lab member's paper counted when it names another lab member, someone whose relationship with the lab "
             f"is more than a coauthor record written from OpenAlex, or someone named on another member's papers that "
             f"count; is on the ORCID record whose iD is on their record; or lists them at their recorded institution "
             f"or at an institution where two such papers list them. A first-degree person whose only relationship "
             f"with the lab is a coauthor record written from OpenAlex was collected only when such a paper names them. "
             f"Second degree: for each first-degree person found in OpenAlex, coauthors on works since "
             f"{st['sinceYear']}, from every OpenAlex record linked to them and the papers they list on ORCID. A work "
             f"counted when it names someone already in the network, was recorded with a lab member, or lists the "
             f"person at their recorded institution or at an institution where two such works list them. Coauthors "
             f"already in the notebook gained a coauthor record where they lacked one; {rule}.", "",
             f"Plan: {rep['newFirstDegree']} new first-degree and {rep['newSecondDegree']} new second-degree people, "
             f"{len(plan['notebook']['edges'])} coauthor records; {len(rep['held'])} people held back as possible "
             f"duplicates (listed below). Import batch {result['batchId']}: {ap.get('created', 0)} records created and "
             f"{ap.get('updated', 0)} updated." + (
                 f" The import held back {ap.get('skipped', 0)} more {'person' if ap.get('skipped') == 1 else 'people'} "
                 f"and refused {ap.get('rejected', 0)} {'record' if ap.get('rejected') == 1 else 'records'}; the .json "
                 f"file beside this one gives each reason." if ap.get("skipped") or ap.get("rejected")
                 else "") +
             f" {rep['requests']} requests; ${rep['costUsd']:.4f} of OpenAlex's daily allowance.", ""]
    made = {ev["id"]: ev.get("batch") or "" for ev in svc.nb.events if ev["op"] == "contact.create"}
    by_status: Dict[str, List[str]] = defaultdict(list)
    for pid, v in sorted(plan["coverage"].items(), key=lambda kv: names.get(kv[0], kv[0])):
        extra = ""
        if v["status"] == "collected":
            extra = f": {v['added']} added, {v['linked']} linked, {v['coauthors']} coauthors in {works(v['works'])}"
            if v.get("unanchored"):
                extra += f"; {works(v['unanchored'])} left out as unconnected"
        elif v["status"] == "unanchored":
            extra = f": {works(v['unanchored'])}"
        elif v["status"] in ("tie-unanchored", "tie-unconfirmed"):
            if v.get("through"):
                extra = f" (through {', '.join(names.get(o, o) for o in v['through'])})"
            if v.get("onlyThrough"):
                batches = sorted({made.get(o, "") for o in v["onlyThrough"]} - {""})
                extra += (f"; in the network only through them: "
                          f"{', '.join(names.get(o, o) for o in v['onlyThrough'])}"
                          + (f" (added by import batch {', '.join(batches)})" if batches else ""))
        by_status[v["status"]].append(f"- {names.get(pid, pid)}{' (lab member)' if v.get('degree') == 0 else ''}{extra}")
    for s in ("collected", "unanchored", "tie-unanchored", "tie-unconfirmed", "ambiguous", "not-found", "not-looked-up",
              "stopped"):
        if by_status.get(s):
            lines += [f"## {STATUS_TEXT[s][0].upper() + STATUS_TEXT[s][1:]} ({len(by_status[s])})"] + by_status[s] + [""]
    by_import = result.get("heldForReview", [])
    held = rep["held"] + [{"name": x, "match": m} for x, m in
                          zip(by_import, result.get("heldMatches") or [""] * len(by_import))]
    if held:
        lines += [f"## Held back as possible duplicates ({len(held)})",
                  "Each has the same or a similar name as someone already in the notebook or another new person, or "
                  "an OpenAlex record with the same ORCID iD as one of them. Nothing confirms it is a different "
                  "person, so it was not added.", ""]
        for x in held:
            why = ""
            if x.get("sameOrcidAs"):
                why = f" (via {x['via']}; same ORCID iD as {names.get(x['sameOrcidAs'], x['sameOrcidAs'])})"
            elif x.get("via"):
                why = (f" (via {x['via']}; {x.get('nameMatch') or 'same name as'} "
                       f"{names.get(x['sameNameAs'], x['sameNameAs'])})")
            elif x.get("match"):
                why = f" ({x['match']})"
            lines.append(f"- {x['name']}{why}")
        lines.append("")
    if rep.get("checks"):
        lines += ["## For a person to check"] + [f"- {c}" for c in rep["checks"]] + [""]
    if rep["errors"]:
        lines += ["## Problems"] + [f"- {e}" for e in rep["errors"]] + [""]
    path = os.path.join(ddir, f"expand-{today()}-{result['batchId'].split('-')[1][9:15]}.md")   # one per batch
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(path[:-3] + ".json", "w", encoding="utf-8") as f:
        json.dump({"date": today(), "kind": "expand", "standard": st, "coverage": plan["coverage"], "report": rep,
                   "result": result}, f, indent=1, ensure_ascii=False)
    return path
