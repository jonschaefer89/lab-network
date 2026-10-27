"""A small hand-checkable packet for tests. All people and sources are fictional."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile

ROOTS = ["pi", "stu", "pd"]


def person(pid, name, inst="Lakeside University", role="Associate Professor", doct="confirmed", **extra):
    c = {"id": pid, "name": name, "institution": inst, "role": role, "lab": "", "email": "", "website": "",
         "tags": [], "notes": "", "createdAt": "2026-01-01T00:00:00Z", "seed": pid in ROOTS,
         "networkDegree": 0 if pid in ROOTS else 2, "expansionStatus": "profile-confirmed",
         "photoUrl": "", "photoConfidence": "unverified", "profileSourceUrl": f"https://example.org/{pid}",
         "profileCheckedAt": "2026-01-01", "profileConfidence": "confirmed", "sources": [],
         "researchSummary": "", "researchSourceUrl": "", "researchCheckedAt": "", "papers": [], "grants": []}
    if doct in ("confirmed", "unknown", "not-yet-earned"):
        c["doctorateStatus"] = doct
        c["doctorateEvidence"] = "Listed on profile page" if doct != "unknown" else ""
        c["doctorateSourceUrl"] = f"https://example.org/{pid}" if doct != "unknown" else ""
        c["doctorateCheckedAt"] = "2026-01-01" if doct != "unknown" else ""
    c.update(extra)
    return c


def edge(eid, a, b, typ, prov="public", status="reviewed", **extra):
    e = {"id": eid, "from": a, "to": b, "type": typ, "provenance": prov, "confidence": "confirmed",
         "status": status, "sourceTitle": "Example source",
         "sourceUrl": "https://example.org/source" if prov == "public" else "",
         "evidence": f"{a} {typ} {b}", "checkedAt": "2026-01-01", "createdAt": "2026-01-01T00:00:00Z"}
    e.update(extra)
    return e


def notebook():
    contacts = [
        person("pi", "Pat Investigator", role="Assistant Professor", doct="none",
               researchSummary="Twin studies of adolescent ADHD and sleep.",
               researchSourceUrl="https://example.org/pi", tags=["seed"],
               papers=[{"title": "Sleep and attention-deficit/hyperactivity disorder in twins",
                        "url": "https://doi.org/10.0000/sleep1", "year": "2024", "checkedAt": "2026-01-01"}],
               grants=[{"title": "Longitudinal twin cohort", "url": "https://example.org/grant1",
                        "funder": "Example Foundation", "period": "2024-2027", "checkedAt": "2026-01-01",
                        "grantNumber": "R01XX000001", "role": "Contact PI"}]),
        person("stu", "Sam Student", role="Graduate Student", doct="none"),
        person("pd", "Dana Postdoc", role="Postdoctoral Researcher", doct="none"),
        person("m1", "Morgan Mentor", inst="Northfield Institute", role="Professor"),
        person("m2", "Mira Mentor", inst="Northfield Institute", role="Professor"),
        person("c1", "Chris Coauthor"),
        person("c2", "Casey Coauthor", inst="", doct="unknown"),
        person("c3", "Cory Bridge", researchSummary="Genetic epidemiology of sleep.",
               researchSourceUrl="https://example.org/c3",
               papers=[{"title": "Sleep and attention-deficit/hyperactivity disorder in twins",
                        "url": "https://doi.org/10.0000/sleep1", "year": "2024", "checkedAt": "2026-01-01"}]),
        person("g1", "Gale Grantee",
               grants=[{"title": "Longitudinal twin cohort", "url": "https://example.org/grant1",
                        "funder": "Example Foundation", "period": "2024-2027", "checkedAt": "2026-01-01",
                        "grantNumber": "R01XX000001", "role": "Co-Investigator"}]),
        person("t1", "Taylor Trainer", doct="none"),
        person("far", "Frankie Faraway", researchSummary="Zebrafish models of ADHD-like behaviour.",
               researchSourceUrl="https://example.org/far"),
        person("iso", "Isa Isolated"),
        person("irta", "Ira Fellow", role="Staff Scientist"),
        person("cm", "Cam Committee"),
        person("pdsup", "Pax Supervisor"),
        person("jose", "José Núñez", notes="Met at the 2025 sleep meeting; interested in ADHD cohorts."),
        person("tg", "Toby Grandtrainee"),
    ]
    edges = [
        edge("e-m1-pi", "m1", "pi", "mentor", relationshipStatus="historical", relationshipStart="2010",
             relationshipEnd="2015", relationshipLabel="Doctoral advisor"),
        edge("e-m2-pi", "m2", "pi", "mentor", relationshipStatus="historical"),
        edge("e-pi-stu", "pi", "stu", "mentor", relationshipStatus="current"),
        edge("e-pi-c1", "pi", "c1", "coauthor"),
        edge("e-pi-c1b", "pi", "c1", "coauthor", sourceUrl="https://example.org/source2"),
        edge("e-stu-c2", "stu", "c2", "coauthor"),
        edge("e-c1-c3", "c1", "c3", "coauthor"),
        edge("e-c2-c3", "c2", "c3", "collaborator"),
        edge("e-pi-g1", "pi", "g1", "grant", projectId="R01XX000001"),
        edge("e-pi-t1", "pi", "t1", "training"),
        edge("e-c3-far", "c3", "far", "labmate"),
        edge("e-pi-irta", "pi", "irta", "irta_colleague", prov="reported", relationshipStatus="historical",
             relationshipLabel="NIH IRTA colleagues"),
        edge("e-cm-stu", "cm", "stu", "committee"),
        edge("e-pd-pdsup", "pd", "pdsup", "postdoc", relationshipStatus="historical"),
        edge("e-pi-pd", "pi", "pd", "labmate", relationshipStatus="current"),
        edge("e-rejected", "iso", "pi", "collaborator", status="rejected"),
        edge("e-jose-far", "jose", "far", "coauthor"),
        edge("e-t1-tg", "t1", "tg", "coauthor"),
    ]
    meetings = [{"id": "meet-1", "contactId": "c1", "metBy": "Pat Investigator", "date": "",
                 "context": "Conference poster session", "learning": "Discussed ADHD twin cohorts",
                 "followUp": "Send preprint", "recordedBy": "Pat Investigator",
                 "createdAt": "2026-01-02T00:00:00Z"}]
    return {"schemaVersion": 1, "contacts": contacts, "meetings": meetings, "edges": edges, "research": [],
            "appliedSeeds": ["fixture-v1"],
            "coverage": {"updatedAt": "2026-01-01", "scope": "Fixture", "seedIds": ROOTS, "status": "in-progress",
                         "sourcesReviewed": 1, "pendingSources": 0, "unresolvedIdentities": 0, "notes": ""}}


def write_packet(directory: str, nb=None) -> str:
    nb = copy.deepcopy(nb or notebook())
    pdir = os.path.join(directory, "packet")
    os.makedirs(pdir, exist_ok=True)
    data = json.dumps(nb, ensure_ascii=False, indent=1).encode("utf-8")
    with open(os.path.join(pdir, "notebook.json"), "wb") as f:
        f.write(data)
    roots = [{"id": r, "match_count": 1, "name": next(c["name"] for c in nb["contacts"] if c["id"] == r)}
             for r in ROOTS]
    with open(os.path.join(pdir, "lab-roots.json"), "w", encoding="utf-8") as f:
        json.dump(roots, f)
    manifest = {"dataset_status": "fixture", "delivered_file": "notebook.json",
                "delivered_sha256": hashlib.sha256(data).hexdigest(), "delivered_utf8_bytes": len(data)}
    with open(os.path.join(pdir, "source-manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    return pdir


def new_workspace(tmp=None):
    """(tempdir, workspace) built on a fresh copy of the fixture packet."""
    from labnetwork.store import Workspace
    tmp = tmp or tempfile.mkdtemp(prefix="labnetwork-test-")
    pdir = write_packet(tmp)
    ws = Workspace.create(os.path.join(tmp, "ws"), pdir, lab_name="Fixture Lab")
    return tmp, ws
