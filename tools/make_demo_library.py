#!/usr/bin/env python3
"""Generate the demo lab's research library and one weekly digest (deterministic; every record is made up).

Run after tools/make_demo.py:  python3 tools/make_demo_library.py
Writes demo/library.json and demo/digest-samples/library-2026-09-26.{md,json}; the demo command copies them into
the demo workspace (the library to its folder, the digest to its digests/ folder). Every link points at example.org, a domain reserved for documentation, and every
funder and identity note is marked fictional.
"""
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import random  # noqa: E402

from labnetwork import library as L  # noqa: E402
from labnetwork.graph import TieFilter  # noqa: E402
from labnetwork.service import Service  # noqa: E402
from labnetwork.store import Workspace  # noqa: E402

R = random.Random(20261017)
BUILT = "2026-09-19"          # the first build
UPDATED = "2026-09-26"        # the weekly update that wrote the sample digest
EX = "https://example.org"
PER_PERSON = 15               # recent papers kept per person (50 in a real library; fewer keeps the demo small)
DEMO = os.path.join(ROOT, "demo")

TOPICS = ["ADHD", "sleep", "adolescent development", "twin studies", "substance use", "anxiety", "psychosis risk",
          "ecological momentary assessment", "behavioral genetics", "school functioning", "family environment",
          "emotion regulation", "longitudinal methods", "cannabis use", "depression"]
# Titles name a measured construct; a method topic picks the kind of study instead.
PHRASE = {"ADHD": "ADHD symptoms", "sleep": "sleep duration", "adolescent development": "pubertal timing",
          "substance use": "substance use", "anxiety": "anxiety", "psychosis risk": "psychotic-like experiences",
          "school functioning": "school engagement", "family environment": "family conflict",
          "emotion regulation": "emotion regulation", "cannabis use": "cannabis use", "depression": "depressive symptoms"}
METHOD_TITLES = {
    "twin studies": ["{T} across adolescence: a twin study", "Genetic and environmental influences on {t}"],
    "behavioral genetics": ["A genetically informed study of {t}", "Polygenic scores and {t} in adolescence"],
    "ecological momentary assessment": ["Daily measurement of {t} and {u}", "Within-day links between {t} and {u}"],
    "longitudinal methods": ["Longitudinal associations between {t} and {u}",
                             "Within-person links between {t} and {u} across adolescence"]}
TITLES = ["Longitudinal associations between {t} and {u}", "Parenting and {t} in adolescence",
          "{T} and {u} in a population cohort", "Sex differences in the course of {t}", "Neighborhood context and {t}",
          "Peer relationships and {t} in early adolescence"]
AWARD_TITLES = ["Mechanisms linking {t} and {u} in adolescence", "A longitudinal study of {t} in twins",
                "Daily processes in {t} and {u}", "Genetically informed research training on {t}",
                "Developmental pathways to {t}"]
OTHER_FIRST = ["Alex", "Bea", "Cal", "Dee", "Eva", "Finn", "Gil", "Hale", "Ivo", "Jae", "Kit", "Lux", "Mio", "Nell"]
OTHER_LAST = ["Arden", "Birch", "Corvin", "Dale", "Ember", "Frost", "Glen", "Heath", "Isles", "Juniper", "Kestrel"]
SCALE = [("Professor", 90, 260), ("Associate Professor", 40, 130), ("Assistant Professor", 15, 60),
         ("Research Scientist", 25, 100), ("Senior Lecturer", 10, 45), ("Postdoctoral Researcher", 8, 30),
         ("Graduate Student", 2, 12), ("Lab Manager", 1, 6)]
FACULTY = ("Professor", "Associate Professor", "Assistant Professor", "Research Scientist")


def title_case(t):
    return t if t.isupper() else t[0].upper() + t[1:]


def make_title(own, templates=None):
    """A made-up title on one of the person's topics."""
    constructs = [t for t in own if t in PHRASE] or [R.choice(sorted(PHRASE))]
    t = PHRASE[R.choice(constructs)]
    u = PHRASE[R.choice([x for x in sorted(PHRASE) if PHRASE[x] != t])]
    methods = [m for m in own if m in METHOD_TITLES]
    if templates is None:
        templates = METHOD_TITLES[R.choice(methods)] if methods and R.random() < 0.5 else TITLES
    return R.choice(templates).format(t=t, u=u, T=t[0].upper() + t[1:])


def topics_of(c):
    s = (c.get("researchSummary") or "").removeprefix("Studies ").rstrip(".")
    own = [x for x in s.split(" and ") if x in TOPICS] if s else []
    return own or R.sample(TOPICS, 2)


def scale_of(role):
    for prefix, lo, hi in SCALE:
        if role.startswith(prefix):
            return lo, hi
    return 5, 60


def work_date(key, year, latest):
    month = int(key[:2], 16) % 12 + 1
    day = int(key[2:4], 16) % 28 + 1
    date = f"{year}-{month:02d}-{day:02d}"
    return min(date, latest)


def byline(names):
    return ", ".join(names[:3]) + (" et al." if len(names) > 3 else "")


def main():
    tmp = tempfile.mkdtemp()
    try:
        ws = Workspace.create(os.path.join(tmp, "ws"), os.path.join(ROOT, "demo", "packet"),
                              lab_name="Demo Lab (fictional data)")
        svc = Service(ws)
        nb = svc.nb
        contacts = nb.records["contacts"]
        people = L._people_in_scope(svc)
        scope = set(people)
        dist, _ = svc.lab_dist(TieFilter())
        g = svc.graph(TieFilter())
        neighbors = {pid: sorted(o for o in g.nbrs.get(pid, ()) if o in scope) for pid in people}
        own_topics = {pid: topics_of(contacts[pid]) for pid in people}

        lib = L.empty()
        lib["standard"]["perPerson"] = PER_PERSON
        works = lib["works"]

        def add_work(pids, title, year, date, url, names, added, salt="", key=""):
            # Profile papers keep the demo packet's links; made-up papers add a salt so that two people never share a
            # paper by chance.
            key = key or hashlib.sha1((title + str(year) + salt).encode()).hexdigest()[:8]
            wid = "Wdemo" + key
            if wid not in works:
                works[wid] = {"title": title, "year": year, "date": date, "doi": "", "url": url or f"{EX}/papers/{key}",
                              "authors": len(names), "byline": byline(names), "addedAt": added}
            for pid in pids:
                lst = lib["people"][pid]["openalex"].setdefault("works", [])
                if wid not in lst:
                    lst.append(wid)
            return wid

        # ------------------------------------------------------------ identities and summaries
        for i, pid in enumerate(people):
            c = contacts[pid]
            rec = lib["people"].setdefault(pid, {})
            coauthors = [o for o in neighbors[pid] if any(e["type"] == "coauthor" for e in g.records_between(pid, o))]
            identified = c.get("profileConfidence") == "confirmed"
            if nb.is_lab(pid):
                status, basis = "linked", "ORCID iD listed by the lab (fictional)"
            elif coauthors and (identified or R.random() < 0.6):
                k = R.randint(1, 4)
                status = "linked"
                basis = f"named with {contacts[coauthors[0]]['name']} on {k} recent paper{'s' if k > 1 else ''} (fictional)"
            elif identified:
                status = R.choices(["linked", "unmatched", "ambiguous"], [75, 15, 10])[0]
                basis = "OpenAlex link on their record (fictional)" if status == "linked" else ""
            else:
                status, basis = "unmatched", ""
            o = rec["openalex"] = {"status": status}
            if status != "linked":
                if status == "ambiguous":
                    o["candidates"] = [f"Ademo{i:04d}a", f"Ademo{i:04d}b"]
                continue
            role = c.get("role") or ""
            lo, hi = scale_of(role)
            count = R.randint(lo, hi)
            since = count if role.startswith(("Graduate", "Postdoctoral", "Lab Manager")) else round(count * R.uniform(0.45, 0.8))
            cited = count * R.randint(12, 45)
            h = min(count, max(1, round(math.sqrt(cited / R.uniform(3.2, 4.8)))))
            own = own_topics[pid]
            extra = [t for t in TOPICS if t not in own]
            topics = own + R.sample(extra, R.randint(2, 3))
            top = max(2, round(since * 0.4))
            insts = [c["institution"]] if c.get("institution") else []
            o.update({"id": f"Ademo{i:04d}", "url": f"{EX}/openalex/Ademo{i:04d}", "basis": basis,
                      "summary": {"name": c["name"], "worksCount": count, "worksSince": since, "citedBy": cited,
                                  "hIndex": h, "i10": min(count, max(h - 2, round(h * R.uniform(1.2, 2.0)))),
                                  "topics": [[title_case(t), max(1, round(top * (0.75 ** j)))] for j, t in enumerate(topics)],
                                  "institutions": insts, "countries": ["US"], "orcid": ""},
                      "summaryAt": UPDATED, "works": [], "worksAt": UPDATED})

        linked = [p for p in people if lib["people"][p]["openalex"]["status"] == "linked"]

        # ------------------------------------------------------------ works: profile papers, then more
        for pid in linked:
            for p in contacts[pid].get("papers") or []:
                year = int(p["year"])
                if year < 2016:
                    continue
                key = p["url"].rsplit("/", 1)[-1]          # the packet's link names the paper
                names = [x.strip() for x in (p.get("authors") or "").split(",") if x.strip()]
                add_work([pid], p["title"], year, work_date(key, year, "2026-09-12"), p.get("url"), names, BUILT, key=key)
        for pid in linked:
            c = contacts[pid]
            o = lib["people"][pid]["openalex"]
            want = min(o["summary"]["worksSince"], PER_PERSON)
            own = own_topics[pid]
            n = 0
            while len(o["works"]) < want and n < 60:
                n += 1
                title = make_title(own)
                year = R.choice([2016, 2017, 2018, 2019, 2020, 2021, 2022, 2022, 2023, 2023, 2024, 2024, 2025, 2025, 2026])
                pids = [pid]
                linked_nbrs = [x for x in neighbors[pid] if x in linked and x != pid]
                if linked_nbrs and R.random() < 0.25:
                    pids.append(R.choice(linked_nbrs))
                names = [contacts[x]["name"] for x in pids] + [f"{R.choice(OTHER_FIRST)} {R.choice(OTHER_LAST)}"
                                                                for _ in range(R.randint(1, 5))]
                R.shuffle(names)
                key = hashlib.sha1((title + str(year) + pid + str(n)).encode()).hexdigest()[:8]
                add_work(pids, title, year, work_date(key, year, "2026-09-12"), "", names, BUILT, salt=pid + str(n))

        # ------------------------------------------------------------ the weekly update's new papers
        new_works = {}
        recent = [p for p in linked if dist.get(p, 9) <= 1]
        for pid in R.sample(recent, min(8, len(recent))):
            c = contacts[pid]
            title = make_title(own_topics[pid])
            names = [c["name"]] + [f"{R.choice(OTHER_FIRST)} {R.choice(OTHER_LAST)}" for _ in range(R.randint(1, 4))]
            date = f"2026-09-{R.randint(20, 25):02d}"
            new_works[pid] = [add_work([pid], title, 2026, date, "", names, UPDATED, salt=pid + "new")]

        # ------------------------------------------------------------ awards
        awards = lib["awards"]
        new_awards = {}
        n_nih = n_nsf = 0
        for pid in people:
            rec = lib["people"][pid]
            rec["nih"] = {"status": "none", "checkedAt": UPDATED}
            rec["nsf"] = {"status": "checked", "awards": [], "checkedAt": UPDATED}
        faculty = [p for p in linked if (contacts[p].get("role") or "").startswith(FACULTY)
                   and nb.doctorate_state(contacts[p]) == "confirmed"]
        for pid in faculty:
            c = contacts[pid]
            own = own_topics[pid]
            if R.random() < 0.35:
                rec = lib["people"][pid]
                rec["nih"] = {"status": "matched", "profileIds": [10000000 + people.index(pid)],
                              "basis": f"NIH PI {c['name']} at {c['institution']} (fictional)", "awards": [],
                              "checkedAt": UPDATED}
                for _ in range(R.randint(1, 3)):
                    n_nih += 1
                    early = (c.get("role") or "").startswith("Assistant")       # career awards go to early faculty
                    activity = R.choice(["R01", "R21", "R03", "K23", "K01"] if early else ["R01", "R01", "R21", "U01", "R34"])
                    length = {"R01": 5, "R21": 2, "R03": 2, "K23": 5, "K01": 5, "U01": 5, "R34": 3}[activity]
                    start = R.randint(2017, 2025)
                    num = f"DEMO-{activity}-{n_nih:04d}"
                    awards["nih:" + num] = {
                        "source": "nih", "number": num, "funder": "NIH (fictional)",
                        "title": make_title(own, AWARD_TITLES),
                        "ic": "", "activity": activity, "org": c["institution"], "start": f"{start}-07-01",
                        "end": f"{start + length}-06-30", "years": list(range(start, min(start + length, 2026) + 1)),
                        "url": f"{EX}/awards/nih/{num}", "roles": {pid: "Contact PI"}, "addedAt": BUILT}
                    rec["nih"]["awards"].append("nih:" + num)
                    partners = [x for x in neighbors[pid] if x in faculty and x != pid]
                    if partners and R.random() < 0.25:
                        q = R.choice(partners)
                        qn = lib["people"][q]["nih"]
                        if qn["status"] != "matched":
                            lib["people"][q]["nih"] = qn = {
                                "status": "matched", "profileIds": [10000000 + people.index(q)],
                                "basis": f"NIH PI {contacts[q]['name']} at {contacts[q]['institution']} (fictional)",
                                "awards": [], "checkedAt": UPDATED}
                        awards["nih:" + num]["roles"][q] = "Multiple PI"
                        qn["awards"].append("nih:" + num)
            if R.random() < 0.12:
                n_nsf += 1
                num = f"DEMO-24{n_nsf:05d}"
                start = R.randint(2019, 2025)
                awards["nsf:" + num] = {
                    "source": "nsf", "number": num, "funder": "NSF (fictional)",
                    "title": make_title(own, AWARD_TITLES),
                    "org": c["institution"], "start": f"{start}-09-01", "end": f"{start + 3}-08-31",
                    "amount": str(R.randrange(150000, 900000, 1000)), "url": f"{EX}/awards/nsf/{num}",
                    "roles": {pid: "PI"}, "addedAt": BUILT}
                lib["people"][pid]["nsf"]["awards"].append("nsf:" + num)
        # one award found by the weekly update
        nih_people = [p for p in faculty if lib["people"][p]["nih"]["status"] == "matched" and dist.get(p, 9) <= 1]
        if nih_people:
            pid = nih_people[0]
            c = contacts[pid]
            num = "DEMO-R21-0999"
            awards["nih:" + num] = {
                "source": "nih", "number": num, "funder": "NIH (fictional)",
                "title": make_title(own_topics[pid], AWARD_TITLES), "ic": "",
                "activity": "R21", "org": c["institution"], "start": "2026-09-15", "end": "2028-08-31",
                "years": [2026], "url": f"{EX}/awards/nih/{num}", "roles": {pid: "Contact PI"}, "addedAt": UPDATED}
            lib["people"][pid]["nih"]["awards"].append("nih:" + num)
            new_awards[pid] = ["nih:" + num]

        # ------------------------------------------------------------ order, runs, digest
        for pid in linked:
            o = lib["people"][pid]["openalex"]
            summary = o["summary"]          # shared and profile papers can outnumber the drawn count
            summary["worksSince"] = max(summary["worksSince"], len(o["works"]))
            summary["worksCount"] = max(summary["worksCount"], summary["worksSince"])
            o["works"] = sorted(o["works"], key=lambda w: works[w]["date"], reverse=True)[:PER_PERSON]
        status = L.coverage(lib, people)
        lib["runs"] = [
            {"date": f"{BUILT}T19:30:00Z", "mode": "build", "people": len(people), "openalexCostUsd": 0,
             "openalexStopped": "", "newWorks": 0, "newAwards": 0, "coverage": status},
            {"date": f"{UPDATED}T19:30:00Z", "mode": "update", "people": len(people), "openalexCostUsd": 0,
             "openalexStopped": "", "newWorks": sum(len(v) for v in new_works.values()),
             "newAwards": sum(len(v) for v in new_awards.values()), "coverage": status}]
        L.save(ws, lib)
        with open(L.library_path(ws), encoding="utf-8") as f:
            lib = json.load(f)
        lib["updatedAt"] = f"{UPDATED}T19:30:00Z"
        r = SimpleNamespace(new_works=new_works, new_awards=new_awards, openalex_stopped="")
        digest = L.write_digest(ws, svc, lib, r, status, date=UPDATED, created=f"{UPDATED}T19:30:00Z")

        samples = os.path.join(DEMO, "digest-samples")
        if os.path.isdir(samples):
            shutil.rmtree(samples)
        os.makedirs(samples)
        with open(os.path.join(DEMO, "library.json"), "w", encoding="utf-8") as f:
            json.dump(lib, f, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for ext in (".md", ".json"):
            shutil.copyfile(digest[:-3] + ext, os.path.join(samples, os.path.basename(digest)[:-3] + ext))
        print(f"demo library: {len(people)} people, {status['openalexLinked']} linked, {len(lib['works'])} papers, "
              f"{sum(1 for a in lib['awards'].values() if a['source'] == 'nih')} NIH and "
              f"{sum(1 for a in lib['awards'].values() if a['source'] == 'nsf')} NSF awards -> {DEMO}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
