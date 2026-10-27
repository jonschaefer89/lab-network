#!/usr/bin/env python3
"""Generate the fictional demo packet in demo/packet (deterministic; every person and source is made up).

Run:  python3 tools/make_demo.py
All URLs point at example.org, a domain reserved for documentation.
"""
import hashlib
import json
import os
import random
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from labnetwork.packet import BUNDLED_SCHEMA, count_summary, sha256_file, validate_rules, validate_schema  # noqa: E402
from labnetwork.service import slugify  # noqa: E402

R = random.Random(20261016)
TODAY = "2026-10-01"
NOW = "2026-10-01T12:00:00Z"
EX = "https://example.org"

FIRST = ["Amara", "Ravi", "Lena", "Tomás", "Grace", "Ines", "Kofi", "Mirela", "Owen", "Priya", "Hugo", "Sana", "Felix",
         "Noor", "Elias", "Yuki", "Marta", "Dario", "Leila", "Jonas", "Ama", "Bram", "Clara", "Desmond", "Esra", "Farid",
         "Greta", "Hamid", "Ilse", "Joaquín", "Kerstin", "Lucan", "Maeve", "Nikhil", "Odile", "Pavel", "Quinn", "Rosa",
         "Soren", "Talia", "Umar", "Vera", "Wren", "Ximena", "Yara", "Zeno", "Anouk", "Bodhi", "Cyrus", "Delphine"]
LAST = ["Okonkwo", "Menon", "Fischer", "Herrera", "Liu", "Varga", "Asante", "Petrova", "Lindqvist", "Raman", "Moreau",
        "Haddad", "Brandt", "Qureshi", "Sorensen", "Tanaka", "Iglesias", "Romano", "Karimi", "Weber", "Mensah", "de Vries",
        "Novak", "Achebe", "Yilmaz", "Rahimi", "Strand", "Farouk", "Kowalski", "Aranda", "Holm", "Ferrante", "Byrne",
        "Joshi", "Laurent", "Dvorak", "Ellison", "Calderón", "Nyberg", "Halevi", "Osei", "Marsh", "Castellanos", "Ngata",
        "Pellegrini", "Vasquez", "Thorne", "Abara", "Kaur", "Delacroix"]
INSTITUTIONS = ["Lakeshore University", "Northfield Institute of Technology", "Harbor State University",
                "Pinecrest Medical College", "Riverside Children's Research Center", "Ashdown University",
                "Mapleton University", "Granite Peak University", "Bayview Health Sciences University"]
TOPICS = ["ADHD", "sleep", "adolescent development", "twin studies", "substance use", "anxiety", "psychosis risk",
          "ecological momentary assessment", "behavioral genetics", "school functioning", "family environment",
          "emotion regulation", "longitudinal methods", "cannabis use", "depression"]
ROLES_FACULTY = ["Professor", "Associate Professor", "Assistant Professor", "Research Scientist", "Senior Lecturer"]

used = {"Amara Okonkwo", "Ravi Menon", "Lena Fischer", "Tomás Herrera", "Grace Liu"}   # the lab itself


def name():
    while True:
        n = f"{R.choice(FIRST)} {R.choice(LAST)}"
        if n not in used:
            used.add(n)
            return n


contacts, edges, meetings = [], [], []
ids = {}


def person(n, inst, role, doct="confirmed", topics=None, lab=False, identified=True):
    pid = slugify(n)
    k = 2
    while pid in ids:
        pid = f"{slugify(n)}-{k}"
        k += 1
    page = f"{EX}/people/{pid}"
    t = topics or R.sample(TOPICS, 2)
    c = {"id": pid, "name": n, "institution": inst if identified else "", "role": role if identified else "",
         "lab": "Okonkwo Lab" if lab else "", "email": "", "website": page, "tags": ["fictional-demo"] + (["lab-member"] if lab else []),
         "notes": "", "createdAt": NOW, "seed": lab, "networkDegree": 0 if lab else None,
         "expansionStatus": "profile-confirmed" if identified else "identity-only",
         "photoUrl": "", "photoConfidence": "unverified", "profileSourceUrl": page, "profileCheckedAt": TODAY,
         "profileConfidence": "confirmed" if identified else "uncertain", "sources": [{"url": page, "note": "Fictional profile page"}],
         "researchSummary": f"Studies {t[0]} and {t[1]}." if identified else "", "researchSourceUrl": page if identified else "",
         "researchCheckedAt": TODAY if identified else "", "papers": [], "grants": []}
    if lab:
        c.update({"labName": "Okonkwo Lab", "labStatus": "current", "labSourceUrl": f"{EX}/okonkwo-lab",
                  "labCheckedAt": TODAY})
    if doct in ("confirmed", "not-yet-earned"):
        c.update({"doctorateStatus": doct, "doctorateType": "PhD" if doct == "confirmed" else "",
                  "doctorateEvidence": "Degree listed on the fictional profile page" if doct == "confirmed" else
                  "Listed as a current doctoral student on the fictional profile page",
                  "doctorateSourceUrl": page, "doctorateCheckedAt": TODAY})
    elif doct == "unknown":
        c.update({"doctorateStatus": "unknown", "doctorateEvidence": "", "doctorateSourceUrl": "", "doctorateCheckedAt": ""})
    ids[pid] = c
    contacts.append(c)
    return pid


n_edges = [0]


def tie(a, b, typ, evidence, prov="public", **extra):
    n_edges[0] += 1
    e = {"id": f"{typ}-{a}-{b}-{n_edges[0]}"[:100], "from": a, "to": b, "type": typ, "provenance": prov,
         "confidence": "confirmed", "status": "reviewed", "sourceTitle": "Fictional source",
         "sourceUrl": f"{EX}/sources/{n_edges[0]}" if prov == "public" else "", "evidence": evidence,
         "checkedAt": TODAY, "createdAt": NOW}
    e.update(extra)
    edges.append(e)


# Titles name a measured construct; a method topic sets the kind of study instead.
PHRASE = {"ADHD": "ADHD symptoms", "sleep": "sleep duration", "adolescent development": "pubertal timing",
          "substance use": "substance use", "anxiety": "anxiety", "psychosis risk": "psychotic-like experiences",
          "school functioning": "school engagement", "family environment": "family conflict",
          "emotion regulation": "emotion regulation", "cannabis use": "cannabis use", "depression": "depressive symptoms"}
METHOD_TITLE = {"twin studies": "{T} and {v} across adolescence: a twin study",
                "behavioral genetics": "A genetically informed study of {t} and {v}",
                "ecological momentary assessment": "Daily measurement of {t} and {v}",
                "longitudinal methods": "The longitudinal course of {t} and {v}"}


def paper(pids, topic, year):
    # Two draws per paper, as in earlier versions, so the people and ties stay the same.
    template = R.choice(["Longitudinal associations between {t} and {u}", "{T} and {u} in a population cohort",
                         "Neighborhood context and {t}", "Within-person links between {t} and {u}",
                         "Sex differences in the course of {t}"])
    u = R.choice([x for x in TOPICS if x != topic])
    if topic in METHOD_TITLE or u in METHOD_TITLE:
        method, construct = (topic, u) if topic in METHOD_TITLE else (u, topic)
        t = PHRASE.get(construct, "ADHD symptoms")
        # The second construct comes from a hash, not a draw, so the people and ties stay the same.
        others = [x for x in sorted(PHRASE.values()) if x != t]
        v = others[int(hashlib.sha1(("|".join(pids) + str(year)).encode()).hexdigest(), 16) % len(others)]
        title = METHOD_TITLE[method].format(t=t, v=v, T=t[0].upper() + t[1:])
    else:
        t, v = PHRASE[topic], PHRASE[u]
        title = template.format(t=t, u=v, T=t[0].upper() + t[1:])
    key = hashlib.sha1((title + str(year) + "|".join(pids)).encode()).hexdigest()[:8]   # one link per paper
    rec = {"title": title, "url": f"{EX}/papers/{key}", "year": str(year), "checkedAt": TODAY,
           "authors": ", ".join(ids[p]["name"] for p in pids), "venue": "Journal of Fictional Findings"}
    for p in pids:
        ids[p]["papers"].append(dict(rec))
    return title


def grant(pids, topic, number):
    rec = {"title": f"Mechanisms linking {topic} and adolescent outcomes", "url": f"{EX}/awards/{number}",
           "funder": "Example Health Foundation (fictional)", "period": "2023-07-01 to 2028-06-30", "checkedAt": TODAY,
           "grantNumber": number, "projectId": number, "status": "active in the fictional award record",
           "projectStartDate": "2023-07-01", "projectEndDate": "2028-06-30"}
    for i, p in enumerate(pids):
        ids[p]["grants"].append(dict(rec, role="Contact PI" if i == 0 else "Multiple PI"))


# ---------------------------------------------------------------- the lab
pi = person("Amara Okonkwo", "Lakeshore University", "Associate Professor of Psychology", lab=True,
            topics=["ADHD", "sleep"])
pd = person("Ravi Menon", "Lakeshore University", "Postdoctoral Researcher", lab=True, topics=["ecological momentary assessment", "anxiety"])
s1 = person("Lena Fischer", "Lakeshore University", "Graduate Student", doct="not-yet-earned", lab=True, topics=["ADHD", "school functioning"])
s2 = person("Tomás Herrera", "Lakeshore University", "Graduate Student", doct="not-yet-earned", lab=True, topics=["cannabis use", "psychosis risk"])
lm = person("Grace Liu", "Lakeshore University", "Lab Manager", doct="none", lab=True, topics=["twin studies", "longitudinal methods"])
lab = [pi, pd, s1, s2, lm]
for a, b in [(pi, pd), (pi, s1), (pi, s2), (pi, lm), (pd, s1), (s1, s2)]:
    tie(a, b, "labmate", "Both listed on the fictional Okonkwo Lab team page.", relationshipStatus="current",
        relationshipLabel="Lab affiliation")
tie(pi, s1, "mentor", "Primary doctoral advisor, per the fictional program page.", relationshipStatus="current",
    relationshipLabel="Doctoral advisor", relationshipStart="2023")
tie(pi, s2, "mentor", "Primary doctoral advisor, per the fictional program page.", relationshipStatus="current",
    relationshipLabel="Doctoral advisor", relationshipStart="2024")
tie(pd, pi, "postdoc", "Postdoctoral researcher in the Okonkwo Lab, per the fictional team page.", relationshipStatus="current",
    relationshipStart="2025")

# ---------------------------------------------------------------- first degree
mentors = [person(name(), "Northfield Institute of Technology", "Professor", topics=["twin studies", "ADHD"]) for _ in range(2)]
for m in mentors:
    tie(m, pi, "mentor", "Listed as doctoral co-advisor on the PI's fictional CV.", relationshipStatus="historical",
        relationshipLabel="Former PhD co-adviser", relationshipStart="2010", relationshipEnd="2015")
pdsup = person(name(), "Harbor State University", "Professor", topics=["sleep", "adolescent development"])
tie(pi, pdsup, "postdoc", "The PI's fictional CV lists a postdoc with this supervisor.", relationshipStatus="historical",
    relationshipStart="2015", relationshipEnd="2018")
irta = person(name(), "Pinecrest Medical College", "Assistant Professor", topics=["psychosis risk", "anxiety"])
tie(pi, irta, "irta_colleague", "The PI reported that they were NIH IRTAs together (fictional).", prov="reported",
    relationshipStatus="historical", relationshipLabel="NIH IRTA colleagues")
first = mentors + [pdsup, irta]
for i in range(34):
    inst = R.choice(INSTITUTIONS)
    doct = R.choices(["confirmed", "none", "unknown", "not-yet-earned"], [70, 18, 5, 7])[0]
    role = "Graduate Student" if doct == "not-yet-earned" else R.choice(ROLES_FACULTY)
    first.append(person(name(), inst, role, doct=doct, identified=R.random() > 0.12))
for p in first[4:]:
    hub = R.choice([pi, pi, pi, pd, s1, s2, lm])
    typ = R.choices(["coauthor", "collaborator", "grant", "committee", "met"], [55, 20, 10, 10, 5])[0]
    t = R.choice(TOPICS)
    if typ == "coauthor":
        title = paper([hub, p], t, R.randint(2017, 2026))
        tie(hub, p, "coauthor", f"Coauthors of the fictional paper “{title}”.")
    elif typ == "collaborator":
        tie(hub, p, "collaborator", f"Named on the same fictional project team studying {t}.", relationshipStatus="current",
            relationshipLabel="Named project team")
    elif typ == "grant":
        num = f"DEMO-{R.randint(1000, 9999)}"
        grant([hub, p], t, num)
        tie(hub, p, "grant", f"Both named as investigators on fictional award {num}.", projectId=num,
            relationshipStatus="current")
    elif typ == "committee":
        tie(p, s1 if hub != s2 else s2, "committee", "Serves on the student's dissertation committee (fictional).",
            relationshipStatus="current", relationshipLabel="Dissertation committee")
    else:
        tie(hub, p, "met", "Met at a fictional conference poster session.", prov="firsthand", relationshipStatus="historical")

# ---------------------------------------------------------------- second degree
for hub in first:
    for _ in range(R.randint(2, 7)):
        doct = R.choices(["confirmed", "none", "not-yet-earned"], [60, 30, 10])[0]
        role = "Graduate Student" if doct == "not-yet-earned" else R.choice(ROLES_FACULTY + ["Postdoctoral Researcher"])
        q = person(name(), R.choice(INSTITUTIONS), role, doct=doct, identified=R.random() > 0.15)
        typ = R.choices(["coauthor", "mentor", "labmate", "collaborator"], [50, 20, 20, 10])[0]
        t = R.choice(TOPICS)
        if typ == "coauthor":
            tie(hub, q, "coauthor", f"Coauthors of the fictional paper “{paper([hub, q], t, R.randint(2015, 2026))}”.")
        elif typ == "mentor":
            tie(hub, q, "mentor", "Doctoral advisor, per the fictional department page.",
                relationshipStatus=R.choice(["current", "historical"]))
        else:
            tie(hub, q, typ, f"Listed together on a fictional {('lab roster' if typ == 'labmate' else 'project page')}.",
                relationshipStatus=R.choice(["current", "historical", "unknown"]))
# a few third-degree people and cross ties to make the graph realistic
second = [c["id"] for c in contacts if c["id"] not in set(lab) | set(first)]
for _ in range(30):
    a, b = R.sample(second, 2)
    tie(a, b, "coauthor", f"Coauthors of the fictional paper “{paper([a, b], R.choice(TOPICS), R.randint(2015, 2026))}”.")
for hub in R.sample(second, 12):
    q = person(name(), R.choice(INSTITUTIONS), R.choice(ROLES_FACULTY))
    tie(hub, q, "collaborator", "Named on the same fictional project page.", relationshipStatus="unknown")
for p in R.sample(first[4:], 10):
    q = R.choice(first[4:])
    if q != p:
        tie(p, q, "coauthor", f"Coauthors of the fictional paper “{paper([p, q], R.choice(TOPICS), R.randint(2016, 2026))}”.")

# ---------------------------------------------------------------- notes
note_people = R.sample(first[4:], 4)
for i, p in enumerate(note_people):
    meetings.append({"id": f"demo-note-{i + 1}", "contactId": p, "metBy": R.choice(["Amara Okonkwo", "Ravi Menon", "Lena Fischer"]),
                     "date": f"2026-0{i + 3}-1{i}", "context": R.choice(["Poster session at a fictional conference",
                                                                          "Panel discussion at a fictional symposium",
                                                                          "Introduced by a colleague over email"]),
                     "learning": f"Talked about {R.choice(TOPICS)} measures and a possible shared dataset.",
                     "followUp": R.choice(["Send our preprint", "Share the measurement protocol", ""]),
                     "recordedBy": "Amara Okonkwo", "createdAt": NOW})

nb = {"schemaVersion": 1, "contacts": contacts, "meetings": meetings, "edges": edges, "research": [], "appliedSeeds": [],
      "coverage": {"updatedAt": TODAY, "scope": "Fictional demo network generated by tools/make_demo.py.",
                   "seedIds": lab, "status": "bounded-complete", "sourcesReviewed": 0, "pendingSources": 0,
                   "unresolvedIdentities": 0, "notes": "Every person, institution, paper and award here is fictional."}}

with open(BUNDLED_SCHEMA, encoding="utf-8") as f:
    schema = json.load(f)
errs = validate_schema(nb, schema)
issues = [i for i in validate_rules(nb) if i["level"] == "error"]
if errs or issues:
    sys.exit(f"demo packet invalid: {errs[:3]} {issues[:3]}")

out = os.path.join(ROOT, "demo", "packet")
os.makedirs(out, exist_ok=True)
data = json.dumps(nb, ensure_ascii=False, indent=1).encode("utf-8")
with open(os.path.join(out, "notebook.json"), "wb") as f:
    f.write(data)
roots = [{"id": p, "match_count": 1, "name": ids[p]["name"], "role": ids[p]["role"], "institution": ids[p]["institution"]} for p in lab]
with open(os.path.join(out, "lab-roots.json"), "w", encoding="utf-8") as f:
    json.dump(roots, f, indent=2, ensure_ascii=False)
shutil.copyfile(BUNDLED_SCHEMA, os.path.join(out, "notebook.schema.json"))
manifest = {"dataset_status": "fictional demo data", "saved_at": NOW, "delivered_file": "notebook.json",
            "delivered_utf8_bytes": len(data), "delivered_sha256": hashlib.sha256(data).hexdigest(),
            "counts": count_summary(nb), "limitations": ["Every record is fictional."]}
with open(os.path.join(out, "source-manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2, ensure_ascii=False)
sums = {n: sha256_file(os.path.join(out, n)) for n in ("notebook.json", "lab-roots.json", "notebook.schema.json", "source-manifest.json")}
with open(os.path.join(out, "packet-checksums.json"), "w", encoding="utf-8") as f:
    json.dump(sums, f, indent=2)
print(f"demo packet: {len(contacts)} people, {len(edges)} relationships, {len(meetings)} notes -> {out}")
