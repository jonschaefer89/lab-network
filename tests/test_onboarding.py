"""Onboarding build, scheduled refresh and schedule plans, against a fake (offline) API."""
import contextlib
import copy
import datetime as dt
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from labnetwork import library, onboard, schedule  # noqa: E402
from labnetwork.builder import BuildError, build, load_lab, resolve_members  # noqa: E402
from labnetwork.connectors.http import Client  # noqa: E402
from labnetwork.packet import load_packet, validation_report  # noqa: E402
from labnetwork.refresh import refresh  # noqa: E402
from labnetwork.service import Service  # noqa: E402
from labnetwork.store import Workspace  # noqa: E402

YEAR = dt.date.today().year


def author(aid, name, inst, orcid="", topics=("Behavioral genetics",)):
    return {"id": f"https://openalex.org/{aid}", "display_name": name, "orcid": f"https://orcid.org/{orcid}" if orcid else None,
            "works_count": 40, "cited_by_count": 900,
            "last_known_institutions": [{"id": "https://openalex.org/I1", "display_name": inst}],
            "affiliations": [{"institution": {"display_name": inst}, "years": [YEAR - 1, YEAR]}],
            "topics": [{"display_name": t} for t in topics]}


def work(wid, title, year, authors, date=None, insts=None):
    """`insts` gives an author's institution on this work when it is not their latest one."""
    def place(a):
        name = (insts or {}).get(a) or AUTHORS[a]["last_known_institutions"][0]["display_name"]
        return {"display_name": name, "id": "https://openalex.org/I" + name.replace(" ", "")}
    return {"id": f"https://openalex.org/{wid}", "doi": f"https://doi.org/10.0000/{wid.lower()}", "title": title,
            "publication_year": year, "publication_date": date or f"{year}-06-01", "type": "article",
            "primary_location": {"source": {"display_name": "Journal of Examples"}},
            "authorships": [{"author": {"id": f"https://openalex.org/{a}", "display_name": AUTHORS[a]["display_name"],
                                        "orcid": AUTHORS[a]["orcid"]},
                             "institutions": [place(a)]}
                            for a in authors]}


AUTHORS = {
    "A1": author("A1", "Pat Rivera", "Lakeside University", "0000-0000-0000-0001"),
    "A2": author("A2", "Sam Okafor", "Lakeside University"),
    "A9": author("A9", "Sam Okafor", "Distant Polytechnic", topics=("Marine biology",)),
    "A3": author("A3", "Chris Lindqvist", "Northfield Institute", "0000-0000-0000-0003"),
    "A4": author("A4", "Dana Moreau", "Hill College"),
    "A5": author("A5", "Eli Watanabe", "Hill College"),
    "A6": author("A6", "Farah Nadeem", "Coast University"),
    "A7": author("A7", "Gus Tamm", "Coast University"),
    "A8": author("A8", "Hana Brooks", "Lakeside University"),
}

WORKS = {
    "A1": [work("W1", "Twin study of sleep", YEAR - 1, ["A1", "A3", "A4"]),
           work("W2", "Adolescent cohort methods", YEAR - 2, ["A1", "A2", "A3"]),
           work("W3", "Cannabis and cognition", YEAR - 3, ["A1", "A5"])],
    "A2": [work("W2", "Adolescent cohort methods", YEAR - 2, ["A1", "A2", "A3"])],
    "A3": [work("W1", "Twin study of sleep", YEAR - 1, ["A1", "A3", "A4"]),
           work("W4", "Genetic epidemiology review", YEAR - 1, ["A3", "A6", "A7"]),
           work("W5", "Sleep genetics", YEAR - 2, ["A3", "A6", "A4"])],
    "A4": [work("W1", "Twin study of sleep", YEAR - 1, ["A1", "A3", "A4"])],
    "A5": [work("W3", "Cannabis and cognition", YEAR - 3, ["A1", "A5"])],
}

FP, RA = "Faraway Polytechnic", "Remote Academy"
# Made-up people for the identity rules: a namesake's coauthors on papers filed under Pat's record (from Remote
# Academy), and Pat's own coauthors from Faraway Polytechnic, among them Pat's reported mentor and collaborator.
OTHERS = {a["id"].rsplit("/", 1)[1]: a for a in [
    author("A20", "Ottilie Quarrington", RA), author("A21", "Thaddeus Quorn", RA),
    author("A22", "Isolde Brackenridge", FP), author("A23", "Wendeline Ashcombe", FP),
    author("A24", "Morgan Elder", FP), author("A25", "Zephyrine Mallowby", FP),
    author("A26", "Peregrine Oakhurst", RA), author("A27", "Seraphina Wolstencroft", RA),
    author("A28", "L. Thistlewood", RA)]}
PANEL = {f"A{300 + i}": author(f"A{300 + i}", f"Panel Author {i}", "Lakeside University") for i in range(1, 31)}

ORCID_EDU = {
    "0000-0000-0000-0001": [("Ph.D. in Psychology", "Big State University", YEAR - 12)],
    "0000-0000-0000-0003": [("BA", "Small College", YEAR - 20)],
}
ORCID_EMP = {"0000-0000-0000-0003": [("Associate Professor", "Northfield Institute", YEAR - 3, None)]}

PROJECTS = [{"appl_id": 900, "project_num": "1R01XX000001-01", "core_project_num": "R01XX000001",
             "project_title": "Sleep in adolescent twins", "fiscal_year": YEAR - 1, "is_active": True,
             "project_start_date": f"{YEAR - 1}-07-01T00:00:00", "project_end_date": f"{YEAR + 3}-06-30T00:00:00",
             "agency_ic_admin": {"abbreviation": "NIDA"},
             "organization": {"org_name": "LAKESIDE UNIVERSITY"},
             "principal_investigators": [{"profile_id": 111, "full_name": "Pat Rivera", "is_contact_pi": True},
                                         {"profile_id": 222, "full_name": "RILEY COPI", "is_contact_pi": False}]}]


NSF_AWARDS = [{"id": "2400001", "title": "Sleep and attention in adolescence", "piFirstName": "Pat", "piLastName": "Rivera",
               "awardeeName": "Lakeside University", "startDate": "09/01/2023", "expDate": "08/31/2026",
               "fundsObligatedAmt": "400000"},
              {"id": "2400002", "title": "Coastal sediments", "piFirstName": "Pat", "piLastName": "Rivera",
               "awardeeName": "Elsewhere College", "startDate": "09/01/2022", "expDate": "08/31/2025"}]


class FakeAPI:
    def __init__(self):
        self.works = copy.deepcopy(WORKS)
        self.authors = copy.deepcopy(AUTHORS)
        self.orcid_works = {}       # ORCID iD -> [(DOI, year)] listed on the record
        self.fails = lambda url: False      # a request it refuses, as a server error would
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append(url)
        if self.fails(url):
            raise _http_404(url)
        u = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.netloc == "api.openalex.org":
            if u.path.startswith("/authors/orcid:"):
                o = u.path.split("orcid:")[1]
                for a in self.authors.values():
                    if a["orcid"] and a["orcid"].endswith(o):
                        return a
                raise _http_404(url)
            if u.path == "/authors" and "search" in q:
                return {"results": [a for a in self.authors.values() if a["display_name"] == q["search"]]}
            if u.path == "/authors" and q.get("filter", "").startswith("openalex:"):
                ids = q["filter"].split(":", 1)[1].split("|")
                return {"results": [self.authors[i] for i in ids if i in self.authors]}
            if u.path == "/authors" and q.get("filter", "").startswith("orcid:"):    # every record with these iDs
                orcids = set(q["filter"].split(":", 1)[1].split("|"))
                return {"results": [a for a in self.authors.values()
                                    if a["orcid"] and a["orcid"].rsplit("/", 1)[1] in orcids]}
            if u.path == "/works":
                # The build filters on author.id; the research library on authorships.author.id (several with |).
                parts = dict(p.split(":", 1) for p in q["filter"].split(","))
                since = parts.get("from_publication_date", "1900-01-01")
                limit = int(parts["authors_count"].lstrip("<")) if "authors_count" in parts else 10 ** 6
                ws, seen = [], set()
                for aid in (parts.get("author.id") or parts.get("authorships.author.id")).split("|"):
                    for w in self.works.get(aid, []):
                        if w["publication_date"] >= since and w["id"] not in seen and len(w["authorships"]) < limit:
                            seen.add(w["id"])
                            ws.append(w)
                return {"results": ws, "meta": {"next_cursor": None}}
        if u.netloc == "pub.orcid.org":
            o, kind = u.path.split("/")[2:4]
            if kind == "works":
                return {"group": [{"external-ids": {"external-id": [
                    {"external-id-type": "doi", "external-id-value": d, "external-id-normalized": {"value": d}}]},
                    "work-summary": [{"publication-date": {"year": {"value": str(y)}}}]}
                    for d, y in self.orcid_works.get(o, [])]}
            if kind == "educations":
                return {"affiliation-group": [{"summaries": [{"education-summary": {
                    "role-title": t, "organization": {"name": org}, "end-date": {"year": {"value": str(y)}}}}]}
                    for t, org, y in ORCID_EDU.get(o, [])]}
            return {"affiliation-group": [{"summaries": [{"employment-summary": {
                "role-title": t, "organization": {"name": org}, "start-date": {"year": {"value": str(s)}},
                "end-date": None if e is None else {"year": {"value": str(e)}}}}]}
                for t, org, s, e in ORCID_EMP.get(o, [])]}
        if u.netloc == "api.reporter.nih.gov":
            crit = json.loads(body.decode())["criteria"]
            if {111, 222} & set(crit.get("pi_profile_ids", [])):
                return {"results": PROJECTS, "meta": {"total": len(PROJECTS)}}
            return {"results": [], "meta": {"total": 0}}
        if u.netloc == "api.nsf.gov":
            first, last = q["pdPIName"].split()[0], q["pdPIName"].split()[-1]
            return {"response": {"award": [a for a in NSF_AWARDS if a["piLastName"] == last and a["piFirstName"] == first]}}
        raise AssertionError(f"unexpected request {method} {url}")


def _http_404(url):
    import urllib.error
    return urllib.error.HTTPError(url, 404, "Not Found", {}, None)


LAB = {
    "lab": {"name": "Rivera Lab", "institution": "Lakeside University", "website": "https://example.org/rivera-lab"},
    "members": [
        {"name": "Pat Rivera", "role": "Associate Professor", "orcid": "0000-0000-0000-0001", "nihProfileId": "111",
         "facultyPage": "https://example.org/rivera"},
        {"name": "Sam Okafor", "role": "Graduate Student"},
    ],
    "relationships": [
        {"member": "pat-rivera", "name": "Morgan Elder", "type": "mentor", "direction": "they-mentored-member",
         "evidence": "PhD advisor at Big State University", "start": "2008", "end": "2013", "status": "historical"},
        {"member": "pat-rivera", "name": "Chris Lindqvist", "type": "irta_colleague",
         "evidence": "NIH IRTA fellows together", "status": "historical"},
    ],
    "settings": {"sinceYear": YEAR - 10},
}


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.api = FakeAPI()
        self.client = Client(cache_dir=None, transport=self.api)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lab(self):
        p = os.path.join(self.tmp, "lab.json")
        with open(p, "w") as f:
            json.dump(LAB, f)
        return load_lab(p)

    def test_resolution_needs_a_person_for_name_matches(self):
        lab = self.lab()
        rep = resolve_members(lab, self.client)
        self.assertEqual(lab["members"][0]["openalexId"], "A1")      # by ORCID, accepted
        self.assertEqual(rep["needsConfirmation"][0]["member"], "Sam Okafor")
        cands = rep["needsConfirmation"][0]["candidates"]
        self.assertEqual(cands[0]["openalexId"], "A2")                # same institution ranks first
        self.assertTrue(cands[0]["institutionMatches"])
        self.assertNotIn("openalexId", lab["members"][1])            # never chosen automatically

    def build(self):
        lab = self.lab()
        resolve_members(lab, self.client, choose=lambda m, c: "A2")
        out = os.path.join(self.tmp, "packet")
        summary = build(lab, self.client, out, log=lambda m: None)
        return lab, out, summary

    def test_build_produces_a_valid_cited_packet(self):
        lab, out, summary = self.build()
        rep = validation_report(load_packet(out))
        self.assertTrue(rep["ok"], [c for c in rep["checks"] if not c["ok"]])
        with open(os.path.join(out, "notebook.json"), encoding="utf-8") as f:
            nb = json.load(f)
        people = {c["name"]: c for c in nb["contacts"]}
        self.assertEqual(set(people), {"Pat Rivera", "Sam Okafor", "Chris Lindqvist", "Dana Moreau", "Eli Watanabe",
                                       "Farah Nadeem", "Gus Tamm", "Riley Copi", "Morgan Elder"})
        self.assertEqual(people["Pat Rivera"]["doctorateStatus"], "confirmed")       # from ORCID
        self.assertIn("ORCID education record", people["Pat Rivera"]["doctorateEvidence"])
        self.assertNotIn("doctorateStatus", people["Chris Lindqvist"])               # BA only: not classified
        self.assertEqual(people["Chris Lindqvist"]["role"], "Associate Professor")   # ORCID current employment
        self.assertEqual(people["Farah Nadeem"]["profileConfidence"], "uncertain")
        edges = nb["edges"]
        types = sorted((e["type"], people_name(nb, e["from"]), people_name(nb, e["to"])) for e in edges)
        self.assertIn(("mentor", "Morgan Elder", "Pat Rivera"), types)                # direction kept
        self.assertIn(("grant", "Pat Rivera", "Riley Copi"), types)
        self.assertIn(("irta_colleague", "Pat Rivera", "Chris Lindqvist"), types)
        self.assertTrue(all(e["sourceUrl"] for e in edges if e["provenance"] == "public"))
        coauth = [e for e in edges if e["type"] == "coauthor" and {people_name(nb, e["from"]), people_name(nb, e["to"])} ==
                  {"Pat Rivera", "Chris Lindqvist"}]
        self.assertEqual(len(coauth), 1)
        self.assertIn("2 works", coauth[0]["evidence"])
        second = [e for e in edges if e["confidence"] == "likely"]
        self.assertTrue(second)                                                       # second-degree ties
        shared = [e for e in edges if {people_name(nb, e["from"]), people_name(nb, e["to"])} == {"Chris Lindqvist", "Dana Moreau"}]
        self.assertTrue(shared, "two first-degree people who coauthored get their own tie")

    def test_workspace_and_map_from_build(self):
        lab, out, _ = self.build()
        ws = Workspace.create(os.path.join(self.tmp, "ws"), out, lab_name="Rivera Lab")
        ws.config["settings"]["defaultFilters"] = {"maxDegree": "2", "doctorate": "any", "identified": "0"}
        ws.save_config()
        svc = Service(Workspace(ws.path))
        net = svc.network({"doctorate": "any", "identified": "0"})
        by = {n["name"]: n["degree"] for n in net["nodes"]}
        self.assertEqual(by["Pat Rivera"], 0)
        self.assertEqual(by["Chris Lindqvist"], 1)
        self.assertEqual(by["Farah Nadeem"], 2)
        self.assertEqual(svc.state()["defaults"], {"deg": "2", "doct": "any", "ident": "0"})

    def test_refresh_adds_without_overwriting_and_is_idempotent(self):
        lab, out, _ = self.build()
        ws = Workspace.create(os.path.join(self.tmp, "ws"), out, lab_name="Rivera Lab")
        # New public records appear: a new paper with a new coauthor, and Chris moves.
        self.api.works["A1"].insert(0, work("W9", "New sleep paper", YEAR, ["A1", "A8", "A3"], date=dt.date.today().isoformat()))
        self.api.authors["A3"]["last_known_institutions"] = [{"id": "x", "display_name": "Far Away University"}]
        res = refresh(ws.path, scope=1, client=self.client, log=lambda m: None)
        d = res["digest"]
        self.assertEqual([p["title"] for p in d["papers"] if p["name"] == "Pat Rivera"], ["New sleep paper"])
        self.assertEqual([p["name"] for p in d["newPeople"]], ["Hana Brooks"])
        self.assertEqual([a["name"] for a in d["affiliation"]], ["Chris Lindqvist"])
        svc = Service(Workspace(ws.path))
        chris = [c for c in svc.nb.records["contacts"].values() if c["name"] == "Chris Lindqvist"][0]
        self.assertEqual(chris["institution"], "Northfield Institute")              # not overwritten
        self.assertEqual(len(svc.conflicts({})["conflicts"]), 1)
        self.assertTrue(os.path.exists(res["digestPath"]))
        events = len(svc.nb.events)
        again = refresh(ws.path, scope=1, client=self.client, log=lambda m: None)
        self.assertEqual(again["digest"]["papers"], [])
        svc2 = Service(Workspace(ws.path))
        self.assertEqual(len(svc2.conflicts({})["conflicts"]), 1)                   # no duplicate conflict
        self.assertLessEqual(len(svc2.nb.events) - events, 1)                       # only the batch record

    def add_people(self, people):
        """Made-up OpenAlex authors for one test: works name them, and the fake API returns their records."""
        patch = mock.patch.dict(AUTHORS, people)
        patch.start()
        self.addCleanup(patch.stop)
        self.api.authors.update(copy.deepcopy(people))

    def packet(self, out):
        with open(os.path.join(out, "notebook.json"), encoding="utf-8") as f:
            nb = json.load(f)
        return nb, {c["name"] for c in nb["contacts"]}

    def test_build_adds_coauthors_only_from_a_members_works_that_count(self):
        # Pat's OpenAlex record also holds a namesake's two papers from Remote Academy, with people no one in the lab
        # knows. Pat's papers from Faraway Polytechnic count: one names Pat's reported mentor, one is on the ORCID
        # record whose iD is in lab.json, and a third lists Pat where those two do. A paper from Remote Academy counts
        # as well, because it names the reported collaborator by the OpenAlex record lab.json gives for them.
        self.add_people(OTHERS)
        self.api.works["A1"] += [
            work("W20", "A namesake's first paper", YEAR - 4, ["A1", "A20", "A21"], insts={"A1": RA}),
            work("W21", "A namesake's second paper", YEAR - 5, ["A1", "A20"], insts={"A1": RA}),
            work("W22", "Pat and their mentor", YEAR - 6, ["A1", "A24", "A22"], insts={"A1": FP}),
            work("W23", "On Pat's ORCID record", YEAR - 6, ["A1", "A23"], insts={"A1": FP}),
            work("W25", "Another paper from Faraway", YEAR - 7, ["A1", "A25"], insts={"A1": FP}),
            work("W26", "Pat and their collaborator", YEAR - 3, ["A1", "A28"], insts={"A1": RA})]
        self.api.orcid_works["0000-0000-0000-0001"] = [("10.0000/w23", YEAR - 6)]
        lab = self.lab()
        lab["relationships"].append({"member": "pat-rivera", "name": "Ludovica Thistlewood", "type": "collaborator",
                                     "openalexId": "A28", "evidence": "Ran a study together"})
        resolve_members(lab, self.client, choose=lambda m, c: "A2")
        out = os.path.join(self.tmp, "packet")
        build(lab, self.client, out, log=lambda m: None)
        nb, people = self.packet(out)
        self.assertFalse({"Ottilie Quarrington", "Thaddeus Quorn"} & people)
        self.assertTrue({"Isolde Brackenridge", "Wendeline Ashcombe", "Zephyrine Mallowby", "L. Thistlewood",
                         "Dana Moreau"} <= people, people)
        self.assertNotIn("Ludovica Thistlewood", people)        # the collaborator record joined their OpenAlex record
        with open(os.path.join(out, "source-manifest.json"), encoding="utf-8") as f:
            limits = json.load(f)["limitations"]
        note = next((x for x in limits if x.startswith("Lab members' OpenAlex works since")), "")
        self.assertIn("Pat Rivera, 2 of 9.", note)

    def test_build_reads_coauthors_only_from_papers_with_30_or_fewer_authors(self):
        # Pat and Chris each have a paper with 31 authors; Eli is Pat's coauthor and on Chris's paper too.
        self.add_people(PANEL)
        panel = list(PANEL)
        self.api.works["A1"].append(work("W30", "Pat's consortium paper", YEAR - 1, ["A1"] + panel))
        self.api.works["A3"].append(work("W31", "Chris's consortium paper", YEAR - 1, ["A3", "A5"] + panel[:29]))
        _, out, _ = self.build()
        nb, people = self.packet(out)
        self.assertFalse({a["display_name"] for a in PANEL.values()} & people)
        names = {c["id"]: c["name"] for c in nb["contacts"]}
        pairs = {frozenset((names[e["from"]], names[e["to"]])) for e in nb["edges"] if e["type"] == "coauthor"}
        self.assertNotIn(frozenset(("Chris Lindqvist", "Eli Watanabe")), pairs)

    def test_build_reads_coauthors_of_coauthors_only_from_works_that_connect_to_them(self):
        # Chris's record also holds a namesake's two papers from Remote Academy, both with Thaddeus. Chris's own papers
        # from Northfield still bring in Farah and Gus.
        self.add_people(OTHERS)
        self.api.works["A3"] += [
            work("W73", "A namesake's paper with Thaddeus", YEAR - 2, ["A3", "A21"], insts={"A3": RA}),
            work("W74", "A namesake's second paper with Thaddeus", YEAR - 3, ["A3", "A21"], insts={"A3": RA})]
        _, out, _ = self.build()
        _, people = self.packet(out)
        self.assertNotIn("Thaddeus Quorn", people)
        self.assertTrue({"Farah Nadeem", "Gus Tamm"} <= people, people)

    def built_workspace(self):
        _, out, _ = self.build()
        return Workspace.create(os.path.join(self.tmp, "ws"), out, lab_name="Rivera Lab")

    def test_refresh_adds_coauthor_records_only_from_works_that_connect_to_a_person(self):
        # An earlier run tied Ottilie to Pat from a namesake's paper on Pat's record, so nothing confirms her place in
        # the network: her new paper with Chris adds no coauthor record. Dana's new paper at her own institution with
        # Gus adds one; her paper from somewhere else that names only Farah, whom onboarding added, does not.
        ws = self.built_workspace()
        self.add_people(OTHERS)
        svc, actor = Service(Workspace(ws.path)), "expand (OpenAlex)"
        ott = svc.create_contact({"fields": {"name": "Ottilie Quarrington", "institution": RA}}, actor)["id"]
        svc.add_source(ott, {"url": "https://openalex.org/A20", "note": "OpenAlex author record"}, actor)
        svc.create_tie({"fields": {"from": "pat-rivera", "to": ott, "type": "coauthor", "provenance": "public",
                                   "sourceTitle": f"A namesake's first paper ({YEAR - 4})",
                                   "sourceUrl": "https://openalex.org/W20",
                                   "evidence": "Named together on 1 work indexed by OpenAlex"}}, actor)
        today = dt.date.today().isoformat()
        self.api.works["A1"].append(work("W20", "A namesake's first paper", YEAR - 4, ["A1", "A20"], insts={"A1": RA}))
        self.api.works["A20"] = [work("W70", "Ottilie and Chris", YEAR, ["A20", "A3"], date=today)]
        self.api.works["A4"][:0] = [work("W71", "Dana and Gus at Hill College", YEAR, ["A4", "A7"], date=today),
                                    work("W72", "Dana and Farah elsewhere", YEAR, ["A4", "A6"], date=today,
                                         insts={"A4": RA})]
        d = refresh(ws.path, scope=1, client=self.client, log=lambda m: None)["digest"]
        self.assertEqual({frozenset((t["from"], t["to"])) for t in d["newTies"]},
                         {frozenset(("dana-moreau", "gus-tamm"))})
        self.assertEqual(sorted((x["name"], x["title"], x["unconfirmed"]) for x in d["unconnected"]),
                         [("Dana Moreau", "Dana and Farah elsewhere", False),
                          ("Ottilie Quarrington", "Ottilie and Chris", True)])
        self.assertEqual(sorted(p["title"] for p in d["papers"]),          # every new paper is still on its profile
                         ["Dana and Farah elsewhere", "Dana and Gus at Hill College", "Ottilie and Chris"])

    def test_a_paper_that_does_not_connect_waits_while_a_record_cannot_be_read(self):
        # Pat's ORCID record cannot be read, so fewer works count and Dana's paper from elsewhere waits; her paper at
        # her own institution is added.
        ws = self.built_workspace()
        today = dt.date.today().isoformat()
        self.api.works["A4"][:0] = [work("W71", "Dana and Gus at Hill College", YEAR, ["A4", "A7"], date=today),
                                    work("W72", "Dana and Farah elsewhere", YEAR, ["A4", "A6"], date=today,
                                         insts={"A4": RA})]
        self.api.fails = lambda url: "pub.orcid.org" in url and url.endswith("/works")
        d = refresh(ws.path, scope=1, client=self.client, log=lambda m: None)["digest"]
        self.assertEqual([p["title"] for p in d["papers"]], ["Dana and Gus at Hill College"])
        self.assertEqual(([(t["from"], t["to"]) for t in d["newTies"]], d["unconnected"]),
                         ([("dana-moreau", "gus-tamm")], []))
        self.assertTrue(any(e.startswith("1 new paper was not recorded") for e in d["errors"]), d["errors"])
        self.api.fails = lambda url: False
        d = refresh(ws.path, scope=1, client=self.client, log=lambda m: None)["digest"]
        self.assertEqual([x["title"] for x in d["unconnected"]], ["Dana and Farah elsewhere"])

    def test_refresh_adds_coauthors_only_from_a_members_new_works_that_count(self):
        ws = self.built_workspace()
        self.add_people(OTHERS)
        today = dt.date.today().isoformat()
        # Three new papers on Pat's record. From Remote Academy: a namesake's, which also names Farah (in the network
        # through Chris), and one that names Sam, another lab member. From Faraway Polytechnic: one on Pat's ORCID
        # record.
        w41 = work("W41", "Pat and Sam at Remote Academy", YEAR, ["A1", "A2", "A27"], date=today,
                   insts={"A1": RA, "A2": RA})
        self.api.works["A1"][:0] = [
            work("W40", "A namesake's new paper", YEAR, ["A1", "A26", "A6"], date=today, insts={"A1": RA}), w41,
            work("W46", "Pat's new paper on their ORCID record", YEAR, ["A1", "A25"], date=today, insts={"A1": FP})]
        self.api.works["A2"].insert(0, copy.deepcopy(w41))
        self.api.orcid_works["0000-0000-0000-0001"] = [("10.0000/w46", YEAR)]
        res = refresh(ws.path, scope=1, client=self.client, log=lambda m: None)
        d = res["digest"]
        self.assertEqual(sorted(p["title"] for p in d["papers"] if p["name"] == "Pat Rivera"),
                         ["A namesake's new paper", "Pat and Sam at Remote Academy",
                          "Pat's new paper on their ORCID record"])                         # all on the profile
        self.assertEqual(sorted(p["name"] for p in d["newPeople"]), ["Seraphina Wolstencroft", "Zephyrine Mallowby"])
        self.assertEqual([x["title"] for x in d["leftOut"]], ["A namesake's new paper"])
        svc = Service(Workspace(ws.path))
        ids = {c["name"]: pid for pid, c in svc.nb.active("contacts")}
        self.assertNotIn("Peregrine Oakhurst", ids)
        pairs = {frozenset((e["from"], e["to"])) for _, e in svc.nb.active("edges") if e["type"] == "coauthor"}
        self.assertNotIn(frozenset((ids["Pat Rivera"], ids["Farah Nadeem"])), pairs)
        with open(res["digestPath"], encoding="utf-8") as f:
            self.assertIn("## Lab members' new papers that added no coauthors (1)", f.read())

    def test_refresh_adds_no_tie_with_a_lab_member_from_a_work_that_does_not_count_for_them(self):
        # At scope 2 a refresh also reads Farah's new papers, and a namesake's paper filed under Pat's record names her.
        ws = self.built_workspace()
        self.add_people(OTHERS)
        w40 = work("W40", "A namesake's new paper", YEAR, ["A1", "A26", "A6"], date=dt.date.today().isoformat(),
                   insts={"A1": RA})
        for aid in ("A1", "A6"):                # OpenAlex lists a work under each of its authors
            self.api.works.setdefault(aid, []).insert(0, copy.deepcopy(w40))
        d = refresh(ws.path, scope=2, client=self.client, log=lambda m: None)["digest"]
        self.assertEqual(sorted(p["name"] for p in d["papers"]), ["Farah Nadeem", "Pat Rivera"])
        self.assertEqual(d["newTies"], [])
        self.assertEqual(d["newPeople"], [])

    def test_refresh_adds_no_coauthors_from_papers_with_more_than_30_authors(self):
        ws = self.built_workspace()
        self.add_people(PANEL)
        panel, today = list(PANEL), dt.date.today().isoformat()
        self.api.works["A1"].insert(0, work("W42", "Pat's new consortium paper", YEAR, ["A1"] + panel, date=today))
        self.api.works["A3"].insert(0, work("W43", "Chris's new consortium paper", YEAR, ["A3", "A5"] + panel[:29],
                                            date=today))
        d = refresh(ws.path, scope=1, client=self.client, log=lambda m: None)["digest"]
        self.assertEqual(sorted(p["title"] for p in d["papers"]),
                         ["Chris's new consortium paper", "Pat's new consortium paper"])
        self.assertEqual(d["newPeople"], [])
        self.assertEqual(d["newTies"], [])          # not even between Chris and Eli, who are both in the network
        self.assertEqual(d["leftOut"], [])          # left out for their size, not as a namesake's

    def last_refresh(self, ws):
        return (Workspace(ws.path).config.get("settings") or {}).get("lastRefresh")

    def test_a_refresh_that_cannot_check_a_members_works_reads_their_papers_next_time(self):
        # The last refresh was 30 days ago, and OpenAlex listed Pat's new paper late: it is dated 60 days ago, so only
        # a refresh that reads from 45 days before the last one finds it.
        ws = self.built_workspace()
        today = dt.date.today()
        ws.config["settings"]["lastRefresh"] = (today - dt.timedelta(days=30)).isoformat()
        ws.save_config()
        late = today - dt.timedelta(days=60)
        self.api.works["A1"].insert(0, work("W9", "New sleep paper", late.year, ["A1", "A8", "A3"],
                                            date=late.isoformat()))
        self.api.fails = lambda url: "authorships.author.id:A1," in url
        d = refresh(ws.path, scope=1, client=self.client, log=lambda m: None)["digest"]
        self.assertEqual([p for p in d["papers"] if p["name"] == "Pat Rivera"], [])
        self.assertEqual((d["newPeople"], d["leftOut"]), ([], []))
        self.assertTrue(any(e.startswith("The new papers of Pat Rivera were not read") for e in d["errors"]),
                        d["errors"])
        self.assertEqual(self.last_refresh(ws), (today - dt.timedelta(days=30)).isoformat())     # kept
        self.api.fails = lambda url: False
        d = refresh(ws.path, scope=1, client=self.client, log=lambda m: None)["digest"]
        self.assertEqual([p["title"] for p in d["papers"] if p["name"] == "Pat Rivera"], ["New sleep paper"])
        self.assertEqual([p["name"] for p in d["newPeople"]], ["Hana Brooks"])
        self.assertEqual(self.last_refresh(ws), today.isoformat())

    def test_a_paper_checked_without_a_record_it_needs_is_held_back(self):
        # Pat's ORCID record cannot be read, so a new paper that counts only for being listed there waits; Pat's new
        # paper with Sam counts without it.
        ws = self.built_workspace()
        self.add_people(OTHERS)
        today = dt.date.today().isoformat()
        w41 = work("W41", "Pat and Sam at Remote Academy", YEAR, ["A1", "A2", "A27"], date=today,
                   insts={"A1": RA, "A2": RA})
        self.api.works["A1"][:0] = [
            w41, work("W46", "Pat's new paper on their ORCID record", YEAR, ["A1", "A25"], date=today,
                      insts={"A1": FP})]
        self.api.works["A2"].insert(0, copy.deepcopy(w41))
        self.api.orcid_works["0000-0000-0000-0001"] = [("10.0000/w46", YEAR)]
        self.api.fails = lambda url: "pub.orcid.org" in url and url.endswith("/works")
        d = refresh(ws.path, scope=1, client=self.client, log=lambda m: None)["digest"]
        self.assertEqual([p["title"] for p in d["papers"] if p["name"] == "Pat Rivera"],
                         ["Pat and Sam at Remote Academy"])
        self.assertEqual(([p["name"] for p in d["newPeople"]], d["leftOut"]), (["Seraphina Wolstencroft"], []))
        self.assertTrue(any(e.startswith("1 new paper was not recorded") for e in d["errors"]), d["errors"])
        self.assertIsNone(self.last_refresh(ws))
        self.api.fails = lambda url: False
        d = refresh(ws.path, scope=1, client=self.client, log=lambda m: None)["digest"]
        self.assertEqual([p["title"] for p in d["papers"] if p["name"] == "Pat Rivera"],
                         ["Pat's new paper on their ORCID record"])
        self.assertEqual([p["name"] for p in d["newPeople"]], ["Zephyrine Mallowby"])

    def test_a_damaged_research_library_does_not_stop_a_refresh(self):
        # Without the library a name could match the wrong record, so lab members' papers wait for a readable one.
        ws = self.built_workspace()
        path = os.path.join(ws.path, "library.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{")
        today = dt.date.today().isoformat()
        self.api.works["A1"].insert(0, work("W9", "New sleep paper", YEAR, ["A1", "A8", "A3"], date=today))
        d = refresh(ws.path, scope=1, client=self.client, log=lambda m: None)["digest"]
        self.assertEqual(([p for p in d["papers"] if p["name"] == "Pat Rivera"], d["newPeople"]), ([], []))
        self.assertTrue(any(e.startswith("The research library could not be read") for e in d["errors"]), d["errors"])
        self.assertIsNone(self.last_refresh(ws))
        os.remove(path)
        d = refresh(ws.path, scope=1, client=self.client, log=lambda m: None)["digest"]
        self.assertEqual([p["name"] for p in d["newPeople"]], ["Hana Brooks"])

    def test_schedule_plan_is_printed_not_installed(self):
        p = schedule.plan(os.path.join(self.tmp, "ws"), "week", 2)
        self.assertIn("refresh", json.dumps(p))
        self.assertIn("--scope", json.dumps(p))
        if p["platform"] == "macOS":
            self.assertFalse(os.path.exists(p["file"]) and "labnetwork" in open(p["file"]).read() and False)
            self.assertIn("<key>Weekday</key>", p["content"])


class OnboardLibraryTests(unittest.TestCase):
    """Onboarding ends with the research library and an offer of the weekly update."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.api = FakeAPI()
        self.client = Client(cache_dir=None, transport=self.api)
        self.labdir = os.path.join(self.tmp, "rivera-lab")
        os.makedirs(self.labdir)
        lab = copy.deepcopy(LAB)
        lab["members"][1]["openalexId"] = "A2"                 # chosen by a person beforehand
        lab["settings"]["weeklyUpdate"] = {"day": "sat", "at": "19:30"}
        with open(os.path.join(self.labdir, "lab.json"), "w") as f:
            json.dump(lab, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def onboard(self):
        lines = []
        with mock.patch.object(schedule, "install", side_effect=AssertionError("installed without a yes")), \
                mock.patch.object(schedule, "status", return_value="Not scheduled"):
            rc = onboard.run(self.labdir, interactive=False, client=self.client, log=lines.append)
        return rc, "\n".join(lines)

    def test_onboard_builds_the_library_and_prints_the_weekly_command(self):
        rc, out = self.onboard()
        self.assertEqual(rc, 0, out)
        lib = library.load(Workspace(os.path.join(self.labdir, "workspace")))
        self.assertEqual(lib["standard"]["sinceYear"], YEAR - 10)          # the start year chosen for the build
        pat, riley = lib["people"]["pat-rivera"], lib["people"]["riley-copi"]
        self.assertEqual(pat["openalex"]["status"], "linked")
        self.assertEqual(set(pat["openalex"]["works"]), {"W1", "W2", "W3"})
        # NIH: the profile IDs come from lab.json (Pat) and from the award record (Riley), so no name search is needed.
        self.assertEqual(pat["nih"]["profileIds"], [111])
        self.assertIn("on their record", pat["nih"]["basis"])
        self.assertEqual(riley["nih"]["awards"], ["nih:R01XX000001"])
        self.assertEqual(set(lib["awards"]["nih:R01XX000001"]["roles"]), {"pat-rivera", "riley-copi"})
        # NSF: the award at Pat's institution is kept, the same name elsewhere is not.
        self.assertEqual(pat["nsf"]["awards"], ["nsf:2400001"])
        self.assertIn("Weekly update: not installed", out)
        self.assertIn("--every week --day sat --at 19:30", out)
        self.assertIn("--yes", out)
        # A second run finds the library complete and does not run it again.
        runs = len(lib["runs"])
        rc, out = self.onboard()
        self.assertEqual(rc, 0)
        self.assertIn("Research library: already built.", out)
        self.assertEqual(len(library.load(Workspace(os.path.join(self.labdir, "workspace")))["runs"]), runs)

    def test_library_can_be_left_out(self):
        with open(os.path.join(self.labdir, "lab.json")) as f:
            lab = json.load(f)
        lab["settings"]["library"] = False
        with open(os.path.join(self.labdir, "lab.json"), "w") as f:
            json.dump(lab, f)
        rc, out = self.onboard()
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.labdir, "workspace", "library.json")))
        self.assertIn("Research library: skipped", out)
        self.assertFalse(any("api.nsf.gov" in c for c in self.api.calls))

    def test_a_mistyped_weekly_time_stops_before_any_lookup(self):
        with open(os.path.join(self.labdir, "lab.json")) as f:
            lab = json.load(f)
        lab["settings"]["weeklyUpdate"] = {"day": "saturday", "at": "7pm"}
        with open(os.path.join(self.labdir, "lab.json"), "w") as f:
            json.dump(lab, f)
        with self.assertRaises(BuildError):
            onboard.run(self.labdir, interactive=False, client=self.client, log=lambda m: None)
        self.assertEqual(self.api.calls, [])

    def test_weekly_update_setting_forms(self):
        self.assertIsNone(onboard._weekly_time({}))
        self.assertIs(onboard._weekly_time({"weeklyUpdate": False}), False)
        self.assertEqual(onboard._weekly_time({"weeklyUpdate": True}), {"day": "mon", "at": "08:00"})
        self.assertEqual(onboard._weekly_time({"weeklyUpdate": "Sat 7:05"}), {"day": "sat", "at": "07:05"})
        with self.assertRaises(BuildError):
            onboard._weekly_time({"weeklyUpdate": {"day": "someday"}})

    def test_interactive_weekly_update_installs_only_after_a_yes(self):
        ws_dir = os.path.join(self.tmp, "ws")
        installed = []
        settings = {"weeklyUpdate": {"day": "sat", "at": "19:30"}}
        with mock.patch.object(schedule, "install", side_effect=installed.append), \
                mock.patch.object(schedule, "status", return_value="Not scheduled"):
            with mock.patch("builtins.input", return_value="n"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(onboard.weekly_update(ws_dir, settings, True, log=lambda m: None), "declined")
            self.assertEqual(installed, [])
            with mock.patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(onboard.weekly_update(ws_dir, settings, True, log=lambda m: None), "installed")
        self.assertEqual(installed[0]["job"], "library")
        self.assertEqual(installed[0]["when"], "Saturdays at 19:30")
        with mock.patch.object(schedule, "status", return_value="On: Saturdays at 19:30"):
            self.assertEqual(onboard.weekly_update(ws_dir, settings, True, log=lambda m: None), "on")

    def test_closed_input_never_installs_the_weekly_update(self):
        installed = []
        with mock.patch.object(schedule, "install", side_effect=installed.append), \
                mock.patch.object(schedule, "status", return_value="Not scheduled"), \
                mock.patch("builtins.input", side_effect=EOFError), contextlib.redirect_stdout(io.StringIO()):
            result = onboard.weekly_update(os.path.join(self.tmp, "ws"), {"weeklyUpdate": True}, True,
                                           log=lambda m: None)
        self.assertEqual((result, installed), ("declined", []))

    def test_questionnaire_asks_about_the_library_and_weekly_update(self):
        answers = {"Lab name": ["Rivera Lab"], "Institution": ["Lakeside University"],
                   "Member 1 full name": ["Pat Rivera"], "Their full name": [""],
                   "Build the research library": ["y"], "once a week": ["y"],
                   "Day": ["someday", "sat"], "Time": ["7pm", "19:30"],
                   "Short name": ["SRP", ""], "Full name": ["Society for Research in Psychopathology"]}
        asked = []

        def fake_input(prompt):
            asked.append(prompt)
            for key, queue in answers.items():
                if key in prompt and queue:
                    return queue.pop(0)
            return ""                                           # Enter: take the default
        with mock.patch("builtins.input", side_effect=fake_input), contextlib.redirect_stdout(io.StringIO()):
            lab = onboard.questionnaire()
        self.assertTrue(lab["settings"]["library"])
        self.assertEqual(lab["settings"]["weeklyUpdate"], {"day": "sat", "at": "19:30"})
        self.assertEqual(sum("Day" in a for a in asked), 2)     # asked again after a day it could not read
        self.assertEqual(sum("Time" in a for a in asked), 2)
        self.assertEqual(lab["conferences"], [{"short": "SRP", "name": "Society for Research in Psychopathology"}])

    def test_onboard_puts_the_labs_conferences_on_its_list(self):
        from labnetwork import conferences
        with open(os.path.join(self.labdir, "lab.json")) as f:
            lab = json.load(f)
        lab["conferences"] = [{"short": "SRP", "name": "Society for Research in Psychopathology",
                               "url": "https://psychopathology.org/"}, {"short": "AACAP"}]
        with open(os.path.join(self.labdir, "lab.json"), "w") as f:
            json.dump(lab, f)
        rc, out = self.onboard()
        self.assertEqual(rc, 0, out)
        listed = conferences.load(Workspace(os.path.join(self.labdir, "workspace")))["series"]
        self.assertEqual([(x["id"], x["name"]) for x in listed], [("aacap", "AACAP"),
                                                                  ("srp", "Society for Research in Psychopathology")])
        self.assertIn("Conferences on the lab's list: SRP, AACAP", out)


def people_name(nb, pid):
    return next(c["name"] for c in nb["contacts"] if c["id"] == pid)


if __name__ == "__main__":
    unittest.main()
