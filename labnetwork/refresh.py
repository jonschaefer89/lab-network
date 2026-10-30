"""Periodic updates: new papers, coauthors, awards and possible affiliation changes for people near the lab.

refresh() builds an update file and sends it through the normal importer, so it
follows the same rules as any import: new records are added, empty fields are
filled, and a value that differs from yours (such as a new affiliation) becomes
an open conflict instead of overwriting anything. Possible duplicate people are
skipped and listed in the digest for a person to review.

People are updated when their record carries an OpenAlex author link (and, for
awards, an NIH RePORTER profile ID in their source notes). Packets built by
`labnetwork onboard` have these; others can gain them through the app.

Coauthors come from papers with 30 or fewer authors. A person confirmed each lab
member's OpenAlex record, not each work on it, and OpenAlex sometimes files two
researchers with the same name under one record, so refresh applies expand's
rules (expand.LabCheck): a new person, or a coauthor record with a lab member,
comes only from a work that counts for that member, and anyone else's work adds
coauthor records only when expand would collect them and the work connects to
what is known about them.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Set

from .connectors import openalex as oa
from .connectors import reporter as rp
from .connectors.http import Client, HttpError
from .importer import Importer
from .model import name_key, norm_text, paper_key
from .service import Service, slugify
from .store import Workspace, today, utc_now

PROFILE_RX = re.compile(r"profile ID (\d+)", re.I)


def _openalex_id(c: Dict[str, Any]) -> List[str]:
    """The OpenAlex author records linked on a person's record, the main one first: every author URL in their sources,
    in order, else the one in their profile URL. OpenAlex can split one researcher across several records, so a
    record can link several."""
    ids: List[str] = []
    for s in c.get("sources") or []:
        ids += [a for a in oa.find_author_ids(s.get("url", "")) if a not in ids]
    return ids or oa.find_author_ids(c.get("profileSourceUrl", ""))[:1]


def openalex_links(nb, pid: str) -> List[str]:
    """The OpenAlex author records a person's record links, without the ones marked as not theirs. Sources cannot be
    taken off a record, so a person marks a wrong link instead."""
    no = nb.openalex_not_theirs(pid)
    return [a for a in _openalex_id(nb.records["contacts"][pid]) if a not in no]


def _nih_profile(c: Dict[str, Any]) -> Optional[int]:
    for s in c.get("sources") or []:
        m = PROFILE_RX.search(s.get("note", ""))
        if m:
            return int(m.group(1))
    return None


def plan_update(svc: Service, client: Client, scope: int = 1, since: Optional[str] = None,
                max_people: int = 600, log: Callable[[str], None] = print,
                lib: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from .expand import MAX_AUTHORS, LabCheck       # expand imports this module
    nb = svc.nb
    dist, _ = svc.lab_dist()
    settings = svc.ws.config.get("settings") or {}
    last = since or settings.get("lastRefresh") or (nb.coverage or {}).get("updatedAt") or \
        (_dt.date.today() - _dt.timedelta(days=60)).isoformat()
    since_date = (_dt.date.fromisoformat(last[:10]) - _dt.timedelta(days=45)).isoformat()
    people = sorted((pid for pid, d in dist.items() if d is not None and d <= scope),
                    key=lambda p: (dist[p], p))[:max_people]
    by_oa = {}
    for pid, c in nb.active("contacts"):
        for a in openalex_links(nb, pid):
            by_oa[a] = pid
    updates: Dict[str, Dict[str, Any]] = {}
    new_contacts: Dict[str, Dict[str, Any]] = {}
    new_edges: List[Dict[str, Any]] = []
    digest = {"since": since_date, "scope": scope, "papers": [], "newPeople": [], "newTies": [], "affiliation": [],
              "grants": [], "leftOut": [], "unconnected": [], "checked": 0, "skippedNoLink": 0, "checks": [],
              "errors": []}
    existing_pairs = {(e["from"], e["to"], e["type"]) for _, e in nb.active("edges")}
    lab = set(nb.root_ids)
    # Expand's rules. When a record they read could not be read, fewer works count than should, so a new paper the
    # rules would leave out is held back instead of recorded (`complete` is False).
    check: Optional[LabCheck] = None
    try:
        if lib is None:
            from .library import load
            lib = load(svc.ws)
    except (OSError, ValueError) as exc:        # without the library a name could match the wrong record: no check
        digest["errors"].append(f"The research library could not be read ({exc}), so no new paper was checked.")
    else:
        try:
            check = LabCheck(svc, client, digest["errors"], digest["checks"], lib)
        except HttpError as exc:
            digest["errors"].append(f"Lab members' OpenAlex works could not be checked ({exc}).")
    counted: Dict[str, Set[str]] = check.counted if check else {}     # lab member -> works that count, by OpenAlex ID
    complete = check is not None and not check.unread
    unchecked: List[str] = []           # lab members whose new papers were not read
    held: Set[str] = set()              # new papers held back, by OpenAlex ID
    checked_ids = []
    for pid in people:
        c = nb.records["contacts"][pid]
        aids = openalex_links(nb, pid)
        if not aids:
            digest["skippedNoLink"] += 1
            continue
        checked_ids.append((pid, aids[0]))      # affiliation changes are read from the main record
        if pid in lab and pid not in counted:
            unchecked.append(c["name"])
            continue
        try:
            works = oa.works_for_author(client, aids, 0, max_works=50, since_date=since_date)
        except HttpError as exc:
            digest["errors"].append(f"{c['name']}: {exc}")
            continue
        digest["checked"] += 1
        have = {paper_key(p) for p in c.get("papers") or []}
        # Anyone but a lab member: the works expand's step 4 would count for them, by OpenAlex ID. None when expand
        # would not collect their coauthors at all, because their relationship with the lab is unconfirmed.
        connects: Optional[Set[str]] = None
        if pid not in lab and check is not None and check.collects(pid):
            small_works = [w for w in works if len(w.get("authorships") or []) <= MAX_AUTHORS]
            connects = {w.get("id") for w in check.anchored(pid, small_works, set(aids))}
        for w in works:
            title, link = oa.work_title(w), oa.work_link(w)
            if not title or not link:
                continue
            p = {"title": title[:600], "url": link, "year": str(w.get("publication_year") or ""), "checkedAt": today(),
                 "sourceUrl": w.get("id", ""), "authors": oa.work_authors(w)[:2000], "venue": oa.work_venue(w)[:300],
                 "selectionNote": f"Added by scheduled refresh on {today()}"}
            if (w.get("doi") or "").startswith("https://doi.org/"):
                p["doi"] = w["doi"].replace("https://doi.org/", "")[:250]
            if paper_key(p) in have:
                continue
            have.add(paper_key(p))
            small = len(w.get("authorships") or []) <= MAX_AUTHORS
            # The lab members named on the work, the person included, for whom it does not count; and whether, for
            # anyone else, it is not among their works that count.
            doubt = {m for m in {pid, *(by_oa.get(co["id"]) for co in oa.coauthors(w))} & lab
                     if w.get("id") not in counted.get(m, ())} if small else set()
            unconnected = small and pid not in lab and w.get("id") not in (connects or ())
            if (doubt or unconnected) and not complete:
                held.add(w.get("id") or link)   # not recorded, so the next refresh checks it again
                continue
            updates.setdefault(pid, {"id": pid, "papers": []})["papers"].append(p)
            digest["papers"].append({"personId": pid, "name": c["name"], "title": title, "year": p["year"], "url": link})
            if not small:
                continue
            if pid in doubt:
                digest["leftOut"].append({"personId": pid, "name": c["name"], "title": title, "year": p["year"],
                                          "url": link})
                continue
            if unconnected:
                digest["unconnected"].append({"personId": pid, "name": c["name"], "title": title, "year": p["year"],
                                              "url": link, "unconfirmed": connects is None})
                continue
            for co in oa.coauthors(w):
                if co["id"] in aids:
                    continue
                other = by_oa.get(co["id"])
                if other in doubt:
                    continue                    # the work is that lab member's too, and does not count for them
                if other is None and pid in lab and co["id"] not in new_contacts:
                    other = _new_person(nb, co, new_contacts)
                    by_oa[co["id"]] = other
                    digest["newPeople"].append({"personId": other, "name": co["name"], "via": c["name"], "title": title})
                elif other is None:
                    continue
                if other == pid:
                    continue
                a, b = (pid, other) if pid in lab or pid < other else (other, pid)
                if (a, b, "coauthor") in existing_pairs or (b, a, "coauthor") in existing_pairs:
                    continue
                existing_pairs.add((a, b, "coauthor"))
                new_edges.append({"id": f"coauthor-{a}-{b}-refresh-{today()}"[:100], "from": a, "to": b, "type": "coauthor",
                                  "provenance": "public", "confidence": "confirmed" if pid in lab else "likely",
                                  "status": "reviewed", "sourceTitle": "OpenAlex work found by scheduled refresh",
                                  "sourceUrl": link, "checkedAt": today(), "createdAt": utc_now(),
                                  "evidence": (f"Named together on “{title[:200]}” ({p['year']}), indexed by OpenAlex. "
                                               "Coauthorship alone does not show mentorship or a current collaboration."),
                                  "relationshipStatus": "unknown", "relationshipLabel": "Coauthors"})
                digest["newTies"].append({"from": a, "to": b, "title": title})
    if unchecked:
        digest["errors"].append(f"The new papers of {', '.join(unchecked)} were not read, because their OpenAlex works "
                                "could not be checked; the next refresh reads them.")
    if held:
        one = len(held) == 1
        digest["errors"].append(f"{len(held)} new {'paper was' if one else 'papers were'} not recorded, because a "
                                f"record needed to check {'it' if one else 'them'} against what is known about the lab "
                                f"could not be read; the next refresh checks {'it' if one else 'them'} again.")
    digest["heldBack"] = bool(unchecked or held)       # refresh() then keeps the date of the last refresh
    # Affiliation changes, read in batches.
    authors = oa.get_authors(client, [a for _, a in checked_ids]) if checked_ids else {}
    for pid, aid in checked_ids:
        a = authors.get(aid)
        if not a:
            continue
        insts = [i.get("display_name", "") for i in a.get("last_known_institutions") or []]
        cur = nb.records["contacts"][pid].get("institution", "")
        if insts and cur and not any(norm_text(i) and norm_text(i) in norm_text(cur) for i in insts):
            updates.setdefault(pid, {"id": pid})["institution"] = "; ".join(insts)[:200]
            digest["affiliation"].append({"personId": pid, "name": nb.records["contacts"][pid]["name"],
                                          "current": cur, "openalex": "; ".join(insts)})
    # NIH awards for lab members with a RePORTER profile ID.
    fy = _dt.date.fromisoformat(since_date).year
    for pid in nb.root_ids:
        c = nb.records["contacts"].get(pid)
        prof = _nih_profile(c) if c else None
        if not prof:
            continue
        try:
            recs = rp.projects(client, profile_ids=[prof], since_fy=fy)
        except HttpError as exc:
            digest["errors"].append(f"NIH RePORTER for {c['name']}: {exc}")
            continue
        have = {(g.get("grantNumber") or "").upper() for g in c.get("grants") or []}
        for core, r in rp.latest_by_core(recs).items():
            if core.upper() in have:
                continue
            g = {"title": (r.get("project_title") or core)[:600], "url": rp.project_url(r.get("appl_id")), "funder": "NIH",
                 "period": f"{(r.get('project_start_date') or '')[:10]} to {(r.get('project_end_date') or '')[:10]}"[:100],
                 "checkedAt": today(), "grantNumber": core[:150], "role": rp.pi_role(r, prof)[:200],
                 "status": "found by scheduled refresh", "projectId": (r.get("project_num") or "")[:200]}
            updates.setdefault(pid, {"id": pid}).setdefault("grants", []).append(g)
            digest["grants"].append({"personId": pid, "name": c["name"], "title": g["title"], "number": core})
    nb_update = {"schemaVersion": 1, "contacts": list(updates.values()) + list(new_contacts.values()),
                 "edges": new_edges, "meetings": []}
    return {"notebook": nb_update, "digest": digest}


def _new_person(nb, co: Dict[str, Any], new_contacts: Dict[str, Dict[str, Any]]) -> str:
    base = slugify(co["name"] or co["id"])
    rid, n = base, 2
    while nb.id_taken("contacts", rid) or rid in {c["id"] for c in new_contacts.values()}:
        rid = f"{base}-{n}"
        n += 1
    url = oa.author_url(co["id"])
    new_contacts[co["id"]] = {
        "id": rid, "name": (co["name"] or co["id"])[:160], "institution": "; ".join(co["institutions"])[:200],
        "role": "", "lab": "", "email": "", "website": "", "tags": ["openalex-refresh"], "notes": "",
        "createdAt": utc_now(), "profileSourceUrl": url, "profileCheckedAt": today(), "profileConfidence": "uncertain",
        "expansionStatus": "identity-only", "photoUrl": "", "photoConfidence": "unverified",
        "sources": [{"url": url, "note": f"OpenAlex author record, found by scheduled refresh on {today()}. "
                                          "Institution is from the new paper's author list."}],
        "researchSummary": "", "researchSourceUrl": "", "researchCheckedAt": "", "papers": [], "grants": []}
    return rid


def write_digest(ws: Workspace, digest: Dict[str, Any], result: Optional[Dict[str, Any]]) -> str:
    ddir = os.path.join(ws.path, "digests")
    os.makedirs(ddir, exist_ok=True)
    stamp = today()
    d = dict(digest, date=stamp, applied=(result or {}).get("applied"), heldForReview=(result or {}).get("heldForReview", []))
    lines = [f"# Lab Network update, {stamp}", "",
             f"Checked {d['checked']} people within {d['scope']} degree(s) of the lab for works since {d['since']}."]
    if d["skippedNoLink"]:
        lines.append(f"{d['skippedNoLink']} people in range have no OpenAlex link and were not checked.")
    lines.append("")
    for title, key, fmt in (("New papers", "papers", lambda x: f"- {x['name']}: {x['title']} ({x['year']}) {x['url']}"),
                            ("New coauthors of lab members", "newPeople", lambda x: f"- {x['name']}, via {x['via']}: {x['title']}"),
                            ("Possible affiliation changes (open conflicts; nothing was overwritten)", "affiliation",
                             lambda x: f"- {x['name']}: recorded “{x['current']}”, OpenAlex now lists “{x['openalex']}”"),
                            ("New NIH awards", "grants", lambda x: f"- {x['name']}: {x['number']} {x['title']}")):
        items = d.get(key) or []
        lines.append(f"## {title} ({len(items)})")
        lines.extend(fmt(x) for x in items[:200])
        lines.append("")
    if d.get("heldForReview"):
        lines.append(f"## Held for review ({len(d['heldForReview'])})")
        lines.append("These look like people already in the notebook, so they were not added. Import the digest's "
                     "update file from the Data screen to decide.")
        lines.extend(f"- {x}" for x in d["heldForReview"])
        lines.append("")
    if d.get("leftOut"):
        lines.append(f"## Lab members' new papers that added no coauthors ({len(d['leftOut'])})")
        lines.append("OpenAlex sometimes files two researchers with the same name under one record. These papers on a "
                     "lab member's record connect to nothing else known about them, so their coauthors were not added, "
                     "and a later refresh does not read them again. A paper counts once the ORCID record whose iD is "
                     "on the member's record lists it, once someone records a relationship with one of its coauthors, "
                     "or once the institution it lists for the member is on their record; `expand` then adds its "
                     "coauthors.")
        lines.extend(f"- {x['name']}: {x['title']} ({x['year']}) {x['url']}" for x in d["leftOut"][:200])
        lines.append("")
    if d.get("unconnected"):
        lines.append(f"## Other people's new papers that added no coauthor records ({len(d['unconnected'])})")
        lines.append("OpenAlex can file a namesake's papers under anyone's record. A refresh adds coauthor records "
                     "from a paper of someone outside the lab only when it counts for them, as in `expand`: its "
                     "authors include someone in the network who vouches for it, it was recorded with a lab member, or "
                     "it lists them at their recorded institution or at an institution where two such papers list "
                     "them. A first-degree person's papers count only when their relationship with the lab is more "
                     "than a coauthor record written from OpenAlex, or they are an author of a lab member's paper that "
                     "counts (\"unconfirmed\" marks the people for whom neither holds). A later refresh does not read "
                     "these papers again.")
        lines.extend(f"- {x['name']}{' (unconfirmed)' if x.get('unconfirmed') else ''}: {x['title']} ({x['year']}) "
                     f"{x['url']}" for x in d["unconnected"][:200])
        lines.append("")
    if d.get("checks"):
        lines.append("## For a person to check")
        lines.extend(f"- {x}" for x in d["checks"])
        lines.append("")
    if d.get("errors"):
        lines.append("## Problems")
        lines.extend(f"- {e}" for e in d["errors"])
    md = "\n".join(lines) + "\n"
    with open(os.path.join(ddir, f"{stamp}.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(ddir, f"{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1, ensure_ascii=False)
    return os.path.join(ddir, f"{stamp}.md")


def _running_port(ws: Workspace) -> Optional[int]:
    from .store import _pid_alive
    try:
        with open(ws.lock_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        return int(info["port"]) if _pid_alive(int(info["pid"])) else None
    except (OSError, ValueError, KeyError):
        return None


def _via_http(port: int, filename: str, data: bytes) -> Dict[str, Any]:
    base = f"http://127.0.0.1:{port}"

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "X-LabNetwork-Actor": "scheduled%20refresh"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))

    prev = post("/api/import/preview", {"filename": filename, "content": data.decode("utf-8")})
    decisions = {k: "skip" for k in prev["needsDecision"]}
    result = post("/api/import/apply", {"batchId": prev["batchId"], "decisions": decisions})
    result["heldForReview"] = _held(prev)
    return result


def _held(prev: Dict[str, Any]) -> List[str]:
    return [it.get("name") or it.get("id") or it["key"] for it in prev["items"] if it["key"] in prev["needsDecision"]]


def refresh(ws_path: str, scope: int = 1, since: Optional[str] = None, dry_run: bool = False,
            client: Optional[Client] = None, log: Callable[[str], None] = print) -> Dict[str, Any]:
    ws = Workspace(ws_path)
    svc = Service(ws)
    client = client or Client(cache_dir=None, mailto=(ws.config.get("settings") or {}).get("contactEmail", ""))
    plan = plan_update(svc, client, scope=scope, since=since, log=log)
    data = json.dumps(plan["notebook"], ensure_ascii=False).encode("utf-8")
    d = plan["digest"]
    log(f"Checked {d['checked']} people: {len(d['papers'])} new papers, {len(d['newPeople'])} new people, "
        f"{len(d['newTies'])} new coauthor ties, {len(d['affiliation'])} possible moves, {len(d['grants'])} new awards.")
    if d["leftOut"]:
        log(f"{len(d['leftOut'])} of the lab members' new papers connect to nothing else known about them and added no "
            "coauthors.")
    if d["unconnected"]:
        log(f"{len(d['unconnected'])} of other people's new papers added no coauthor records (listed in the digest).")
    for x in d["checks"]:
        log(f"To check: {x}")
    if dry_run:
        return {"dryRun": True, "digest": d}
    filename = f"refresh-{today()}.json"
    port = _running_port(ws)
    if port:
        result = _via_http(port, filename, data)
    else:
        ws.acquire_lock(0)
        try:
            imp = Importer(svc)
            prev = imp.preview(filename, data)
            result = imp.apply(prev["batchId"], {k: "skip" for k in prev["needsDecision"]}, "scheduled refresh")
            result["heldForReview"] = _held(prev)
        finally:
            ws.release_lock()
    if d["heldBack"]:           # the next refresh reads the same papers again
        log("Some papers were held back (see the digest), so the date of the last refresh stays as it was.")
    else:
        ws.config.setdefault("settings", {})["lastRefresh"] = today()
        ws.save_config()
    path = write_digest(ws, d, result)
    log(f"Digest written to {path}")
    return {"dryRun": False, "digest": d, "result": result, "digestPath": path}
