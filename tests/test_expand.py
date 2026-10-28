"""Expansion from OpenAlex coauthorships, against a fake OpenAlex and ORCID (no network)."""
from __future__ import annotations

import json
import os
import shutil
import unittest
import urllib.error
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from labnetwork.connectors import openalex as oa
from labnetwork.connectors.http import BudgetReached, Client
from labnetwork.expand import expand, name_score
from labnetwork.service import Service
from labnetwork.store import Workspace
from tests.fixtures import new_workspace


def author(aid, name, inst="Lakeside University", orcid="", works=10):
    return {"id": f"https://openalex.org/{aid}", "display_name": name, "orcid": orcid and f"https://orcid.org/{orcid}",
            "works_count": works, "cited_by_count": 0, "topics": [{"display_name": "Sleep research"}],
            "last_known_institutions": [{"display_name": inst}],
            "affiliations": [{"institution": {"display_name": inst}, "years": [2024]}]}


def work(wid, year, title, authors, doi=""):
    return {"id": f"https://openalex.org/{wid}", "doi": doi and f"https://doi.org/{doi}", "ids": {},
            "title": title, "publication_year": year, "publication_date": f"{year}-06-01",
            "authorships": [{"author": {"id": f"https://openalex.org/{a}", "display_name": AUTHORS[a]["display_name"],
                                        "orcid": AUTHORS[a]["orcid"]},
                             "institutions": [{"display_name": inst, "id": "https://openalex.org/I" + inst.replace(" ", "")}]}
                            for a, inst in authors]}


AUTHORS = {a["id"].rsplit("/", 1)[1]: a for a in [
    author("A1", "Pat Investigator"),
    author("A3", "Chris Coauthor"),
    author("A4", "Cory Bridge"),
    author("A5", "Morgan Mentor", inst="Northfield Institute"),
    author("A6", "Mira Mentor", inst="Northfield Institute"),
    author("A7", "Mira Mentor", inst="Northfield Institute"),
    author("A9", "Nia Newcoauthor"),
    author("A10", "Ola Otherperson", inst="Elsewhere College", orcid="0000-0000-0000-0010"),
    author("A12", "Frankie Faraway"),
    author("A13", "Isa Isolated", inst="Elsewhere College"),
    author("A14", "Uma Unknown", inst="Elsewhere College"),
]}
LU, EC, NI = "Lakeside University", "Elsewhere College", "Northfield Institute"
WORKS = [
    work("W1", 2024, "Sleep and attention-deficit/hyperactivity disorder in twins", [("A1", LU), ("A4", LU)],
         doi="10.0000/sleep1"),
    work("W2", 2023, "A second paper with a new coauthor", [("A1", LU), ("A3", LU), ("A9", LU)]),
    work("W3", 2022, "Chris and Ola, first", [("A3", LU), ("A10", EC), ("A13", EC), ("A14", EC)]),
    work("W4", 2021, "Chris and Ola, second", [("A3", LU), ("A10", EC), ("A13", EC), ("A14", EC), ("A12", LU)]),
    work("W5", 2020, "Morgan and Ola", [("A5", "Northfield Institute"), ("A10", EC)]),
    work("W6", 2010, "Too old to count", [("A3", LU), ("A14", EC)]),
]


ORCID_WORKS = {}        # ORCID iD -> [(DOI, year)] listed on the record


class FakeOpenAlex:
    def __init__(self):
        self.calls = []
        self.page_size = 0         # when set, works come this many to a page
        self.fails = lambda url: False     # a request it refuses, as a server error would

    def __call__(self, method, url, body, headers):
        self.calls.append(url)
        if self.fails(url):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        u = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        meta = {"cost_usd": 0.0001}
        if u.netloc == "api.openalex.org" and u.path == "/authors" and "search" in q:
            return {"results": [a for a in AUTHORS.values() if a["display_name"] == q["search"]],
                    "meta": {"cost_usd": 0.001}}
        if u.netloc == "api.openalex.org" and u.path == "/authors":
            ids = q["filter"].split(":", 1)[1].split("|")
            return {"results": [AUTHORS[i] for i in ids if i in AUTHORS], "meta": meta}
        if u.netloc == "api.openalex.org" and u.path == "/works":
            parts = dict(p.split(":", 1) for p in q["filter"].split(","))
            since = parts.get("from_publication_date", "1900-01-01")
            limit = int(parts["authors_count"].lstrip("<")) if "authors_count" in parts else 10 ** 6
            if "doi" in parts:
                dois = set(parts["doi"].split("|"))
                match = lambda w: (w["doi"] or "").replace("https://doi.org/", "") in dois
            else:
                ids = set(parts["authorships.author.id"].split("|"))
                match = lambda w: bool(ids & {a["author"]["id"].rsplit("/", 1)[1] for a in w["authorships"]})
            out = sorted((w for w in WORKS if w["publication_date"] >= since and len(w["authorships"]) < limit
                          and match(w)), key=lambda w: w["publication_date"], reverse=True)
            if self.page_size:              # a request without a cursor gets the first page only
                start = 0 if q.get("cursor", "*") == "*" else int(q["cursor"])
                more = start + self.page_size < len(out)
                return {"results": out[start:start + self.page_size],
                        "meta": {**meta, "next_cursor": str(start + self.page_size) if more else None}}
            return {"results": out, "meta": meta}
        if u.netloc == "pub.orcid.org" and u.path.endswith("/works"):
            listed = ORCID_WORKS.get(u.path.split("/")[-2], [])
            return {"group": [{"external-ids": {"external-id": [
                {"external-id-type": "doi", "external-id-value": d.upper(), "external-id-normalized": {"value": d}}]},
                "work-summary": [{"publication-date": {"year": {"value": str(y)}}}]} for d, y in listed]}
        if u.netloc == "pub.orcid.org" and u.path.endswith("/activities"):
            return {"educations": {"affiliation-group": [{"summaries": [{"education-summary": {
                "role-title": "PhD, Psychology", "organization": {"name": EC}, "end-date": {"year": {"value": "2015"}}}}]}]},
                "employments": {"affiliation-group": [{"summaries": [{"employment-summary": {
                    "role-title": "Assistant Professor", "organization": {"name": EC},
                    "start-date": {"year": {"value": "2019"}}, "end-date": None}}]}]}}
        raise AssertionError(f"unexpected request {url}")


class ExpandTests(unittest.TestCase):
    def setUp(self):
        self.tmp, ws = new_workspace()
        self.ws_path = ws.path
        self.fake = FakeOpenAlex()
        # A first-degree person with no doctorate and no research relationship is not looked up.
        svc = Service(ws)
        lee = svc.create_contact({"fields": {"name": "Lee Labmate", "institution": LU, "role": "Research assistant"}},
                                 "test")
        svc.create_tie({"fields": {"from": "pd", "to": lee["id"], "type": "labmate", "evidence": "Same lab roster"}},
                       "test")
        self.lee = lee["id"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_expand(self, **kw):
        client = Client(transport=self.fake, budget_usd=kw.pop("budget_usd", 0.08))
        return expand(self.ws_path, client=client, log=lambda m: None, **kw)

    def test_name_scores(self):
        self.assertEqual(name_score("S. Alexandra Venn", "Alexandra Venn"), 2)
        self.assertEqual(name_score("Jie Wen", "J. Wen"), 1)
        self.assertEqual(name_score("Jie Wen", "Jing Wen"), 0)
        self.assertEqual(name_score("Margaret Holt-Varga", "Margaret Holt‐Varga"), 2)
        self.assertEqual(name_score("Marcus J. Whitfield Jr.", "Marcus Whitfield"), 2)

    def test_plan_identifies_adds_links_and_holds(self):
        res = self.run_expand(dry_run=True, per_person=5)
        plan = res["plan"]
        cov = plan["coverage"]
        # Lab member accepted on a recorded paper; others not found.
        self.assertEqual(cov["pi"]["status"], "collected")
        self.assertEqual(cov["stu"]["status"], "not-found")
        # First degree: identified on a shared paper, by a unique institution match, or not at all.
        self.assertEqual(cov["c1"]["status"], "collected")
        self.assertIn("named with Pat Investigator", cov["c1"]["basis"])
        self.assertEqual(cov["m1"]["status"], "collected")
        self.assertIn("only OpenAlex author", cov["m1"]["basis"])
        self.assertEqual(cov["m2"]["status"], "ambiguous")
        self.assertEqual(cov[self.lee]["status"], "not-looked-up")
        self.assertEqual(cov["t1"]["status"], "not-found")      # a coauthor record with tg makes them a researcher
        self.assertEqual(cov["g1"]["status"], "not-found")
        names = {c["name"] for c in plan["notebook"]["contacts"] if c.get("name")}
        # A lab member's coauthor missing from the notebook joins the first degree; c1's frequent coauthors
        # join the second degree; the 2010 paper is outside the window.
        self.assertIn("Nia Newcoauthor", names)
        self.assertIn("Ola Otherperson", names)
        self.assertIn("Uma Unknown", names)
        pairs = {(e["from"], e["to"]) for e in plan["notebook"]["edges"]}
        self.assertIn(("pi", "c3"), pairs)          # existing person, confirmed by a shared institution
        self.assertIn(("c1", "far"), pairs)         # existing person linked on one shared work
        self.assertNotIn(("pi", "c1"), pairs)       # a coauthor record already exists
        # Same name as an existing contact, nothing to confirm it: held back, not merged or added.
        self.assertEqual([h["name"] for h in plan["report"]["held"]], ["Isa Isolated"])
        self.assertNotIn("Isa Isolated", names)
        ola = next(c for c in plan["notebook"]["contacts"] if c.get("name") == "Ola Otherperson")
        self.assertEqual(ola["doctorateStatus"], "confirmed")
        self.assertEqual(ola["role"], "Assistant Professor")
        self.assertEqual(ola["profileConfidence"], "uncertain")

    def test_cap_on_new_people(self):
        plan = self.run_expand(dry_run=True, per_person=1)["plan"]
        added = [c["name"] for c in plan["notebook"]["contacts"] if c.get("name") in ("Ola Otherperson", "Uma Unknown")]
        self.assertEqual(len(added), 1)

    def test_apply_records_collection_and_undo(self):
        res = self.run_expand()
        batch = res["result"]["batchId"]
        svc = Service(Workspace(self.ws_path))
        ids = {c["name"]: pid for pid, c in svc.nb.active("contacts")}
        self.assertIn("Ola Otherperson", ids)
        self.assertEqual(svc.nb.collection["c1"]["status"], "collected")
        p = svc.person("c1", {"maxDegree": "all", "doctorate": "any", "identified": "0"})
        self.assertEqual(p["collection"]["status"], "collected")
        self.assertEqual(p["collection"]["coauthors"], 6)        # every OpenAlex coauthor since 2016, for the profile
        dist, _ = svc.lab_dist()
        self.assertEqual(dist[ids["Nia Newcoauthor"]], 1)
        self.assertEqual(dist[ids["Ola Otherperson"]], 2)
        self.assertEqual(dist["c3"], 1)              # was second degree before the new coauthor record
        self.assertIn("https://openalex.org/A5", [x["url"] for x in svc.nb.records["contacts"]["m1"]["sources"]])
        out = svc.undo_import(batch, "test")
        self.assertGreater(out["archived"], 0)
        # The OpenAlex link the run wrote to Morgan's record comes off again.
        self.assertGreaterEqual(out["removedItems"], 1)
        self.assertNotIn("https://openalex.org/A5", [x["url"] for x in svc.nb.records["contacts"]["m1"]["sources"]])
        self.assertNotIn("Ola Otherperson", {c["name"] for _, c in svc.nb.active("contacts")})
        self.assertEqual(svc.nb.collection["c1"]["status"], "undone")
        dist, _ = svc.lab_dist()
        self.assertEqual(dist["c3"], 2)

    def test_a_lab_member_with_two_record_links_is_read_from_both(self):
        svc = Service(Workspace(self.ws_path))
        for a in ("A1", "A15"):            # OpenAlex splits Pat across two records
            svc.add_source("pi", {"url": f"https://openalex.org/{a}", "note": "OpenAlex author record"}, "test")
        with mock.patch.dict(AUTHORS, {"A15": author("A15", "Pat Investigator"), "A16": author("A16", "Quinn Newpal")}):
            extra = [work("W7", 2025, "Under Pat's second record", [("A15", LU), ("A16", LU)]),
                     work("W8", 2024, "Names Pat under both records", [("A1", LU), ("A15", LU)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True, per_person=5)["plan"]
        names = [c["name"] for c in plan["notebook"]["contacts"] if c.get("name")]
        self.assertIn("Quinn Newpal", names)                    # a coauthor found only under the second record
        self.assertNotIn("Pat Investigator", names)             # Pat's own second record is not a new person
        self.assertNotIn("Pat Investigator", [h["name"] for h in plan["report"]["held"]])

    def test_records_marked_not_theirs_are_not_read_or_matched_to_them(self):
        svc = Service(Workspace(self.ws_path))
        for a in ("A1", "A15"):
            svc.add_source("pi", {"url": f"https://openalex.org/{a}", "note": "OpenAlex author record"}, "test")
        svc.mark_not_theirs("pi", {"openalexId": "A15"}, "test")           # linked on Pat's record, but a namesake's
        svc.mark_not_theirs("c3", {"openalexId": "A4"}, "test")            # another Cory Bridge
        with mock.patch.dict(AUTHORS, {"A15": author("A15", "Pat Investigator"), "A16": author("A16", "Quinn Newpal")}):
            extra = [work("W7", 2025, "Under the namesake's record", [("A15", LU), ("A16", LU)]),
                     work("W8", 2024, "Pat and the namesake", [("A1", LU), ("A15", LU)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True, per_person=5)["plan"]
        names = [c["name"] for c in plan["notebook"]["contacts"] if c.get("name")]
        self.assertNotIn("Quinn Newpal", names)                 # found only under the namesake's record
        # A coauthor record marked as not someone's is never matched to them by name: it is held back instead.
        held = {(h["name"], h["openalexId"]) for h in plan["report"]["held"]}
        self.assertTrue({("Pat Investigator", "A15"), ("Cory Bridge", "A4")} <= held, held)
        self.assertNotIn(("pi", "c3"), {(e["from"], e["to"]) for e in plan["notebook"]["edges"]})

    def test_identities_skip_records_marked_not_theirs(self):
        svc = Service(Workspace(self.ws_path))
        # A wrong link on Morgan's record, marked: Morgan is identified again and the record gains the right link.
        svc.add_source("m1", {"url": "https://openalex.org/A6", "note": "OpenAlex author record"}, "test")
        svc.mark_not_theirs("m1", {"openalexId": "A6"}, "test")
        svc.mark_not_theirs("c1", {"openalexId": "A3"}, "test")
        plan = self.run_expand(dry_run=True)["plan"]
        cov = plan["coverage"]
        self.assertEqual((cov["m1"]["status"], cov["m1"]["openalexIds"]), ("collected", ["A5"]))
        added = {c["id"]: [x["url"] for x in c["sources"]] for c in plan["notebook"]["contacts"] if "name" not in c}
        self.assertEqual(added.get("m1"), ["https://openalex.org/A5"])
        # Chris is named on Pat's papers and is the only Chris Coauthor at Lakeside, but that record is marked.
        self.assertEqual((cov["pi"]["status"], cov["c1"]["status"]), ("collected", "not-found"))
        svc.mark_not_theirs("pi", {"openalexId": "A1"}, "test")
        cov = self.run_expand(dry_run=True)["plan"]["coverage"]
        self.assertEqual(cov["pi"]["status"], "not-found")      # the lab member's name search skips it too

    def write_library(self, people):
        lib = {"format": "labnetwork-library", "version": 2, "standard": {"sinceYear": 2016, "maxAuthors": 30,
               "perPerson": 50}, "updatedAt": "", "runs": [], "works": {}, "awards": {},
               "people": {pid: {"openalex": o} for pid, o in people.items()}}
        with open(os.path.join(self.ws_path, "library.json"), "w", encoding="utf-8") as f:
            json.dump(lib, f)

    def new_names(self, plan):
        return [c["name"] for c in plan["notebook"]["contacts"] if c.get("name")]

    def test_every_page_of_works_is_read(self):
        self.fake.page_size = 1           # one work a page: Chris's papers with Ola are not on the first page
        plan = self.run_expand(dry_run=True)["plan"]
        self.assertIn("Ola Otherperson", self.new_names(plan))
        self.assertEqual(plan["coverage"]["c1"]["works"], 3)
        self.assertEqual(plan["coverage"]["c1"]["coauthors"], 6)

    def test_no_limit_on_new_people_by_default(self):
        five = {f"A6{i}": author(f"A6{i}", n) for i, n in enumerate(["Avery Ash", "Blair Birch", "Cam Cedar",
                                                                       "Dee Dogwood", "Eli Elm", "Fay Fir"])}
        with mock.patch.dict(AUTHORS, five):
            extra = [work(f"W6{k}", 2024 - k, f"Chris and six coauthors, paper {k}", [("A3", LU)] + [(a, LU) for a in five])
                     for k in range(2)]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                everyone = self.new_names(self.run_expand(dry_run=True)["plan"])
                capped = self.new_names(self.run_expand(dry_run=True, per_person=5)["plan"])
        two_papers = {a["display_name"] for a in five.values()} | {"Ola Otherperson", "Uma Unknown"}
        self.assertEqual(two_papers & set(everyone), two_papers)
        self.assertEqual(len(two_papers & set(capped)), 5)

    def test_the_research_librarys_match_is_used(self):
        # Two Mira Mentors at Northfield: the name search cannot choose, but the library matched Mira to A6. The
        # library also matched Isa, whose name alone would hold her back.
        self.write_library({
            "m2": {"status": "linked", "id": "A6", "ids": ["A6"],
                   "basis": "the only OpenAlex author with this name at an institution on their record"},
            "iso": {"status": "linked", "id": "A13", "ids": ["A13"],
                    "basis": "named with Chris Coauthor on 2 recent OpenAlex papers"}})
        with mock.patch.dict(AUTHORS, {"A65": author("A65", "Rory Northfield", inst=NI)}):
            extra = [work("W65", 2024, "Mira and Rory, first", [("A6", NI), ("A65", NI)]),
                     work("W66", 2023, "Mira and Rory, second", [("A6", NI), ("A65", NI)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        cov = plan["coverage"]
        self.assertEqual(cov["m2"]["status"], "collected")
        self.assertIn("research library", cov["m2"]["basis"])
        self.assertIn("Rory Northfield", self.new_names(plan))
        self.assertNotIn("Isa Isolated", [h["name"] for h in plan["report"]["held"]])
        self.assertIn(("c1", "iso"), {(e["from"], e["to"]) for e in plan["notebook"]["edges"]})
        # The library's matches stay in the library: they are not written to the records.
        self.assertNotIn("m2", [c["id"] for c in plan["notebook"]["contacts"] if "name" not in c])

    def test_works_that_connect_to_nothing_known_are_left_out(self):
        svc = Service(Workspace(self.ws_path))
        # Someone an earlier run added through Chris alone does not vouch for the papers they share with Chris.
        rex = svc.create_contact({"fields": {"name": "Rex Earlier", "institution": "Faraway Polytechnic",
                                             "tags": ["openalex-expand", "expand-second-degree"]}}, "test")
        svc.add_source(rex["id"], {"url": "https://openalex.org/A20", "note": "OpenAlex author record"}, "test")
        svc.create_tie({"fields": {"from": "c1", "to": rex["id"], "type": "coauthor",
                                   "evidence": "Named together on 2 works"}}, "test")
        fp = "Faraway Polytechnic"
        with mock.patch.dict(AUTHORS, {"A20": author("A20", "Rex Earlier", inst=fp),
                                       "A21": author("A21", "Sol Stranger", inst=fp)}):
            # Another Chris Coauthor, filed under Chris's record: no coauthor or institution in common with Chris.
            extra = [work("W20", 2024, "The namesake's first paper", [("A3", fp), ("A20", fp), ("A21", fp)]),
                     work("W21", 2023, "The namesake's second paper", [("A3", fp), ("A20", fp), ("A21", fp)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        names = self.new_names(plan)
        self.assertNotIn("Sol Stranger", names)
        self.assertIn("Ola Otherperson", names)          # Chris's own papers still count
        self.assertEqual((plan["coverage"]["c1"]["works"], plan["coverage"]["c1"]["unanchored"]), (3, 2))

    def test_people_an_earlier_run_added_do_not_vouch_for_papers(self):
        svc = Service(Workspace(self.ws_path))
        # Rex came in through Morgan. A paper under Chris's record that names Rex is still not shown to be Chris's.
        rex = svc.create_contact({"fields": {"name": "Rex Earlier", "institution": "Faraway Polytechnic",
                                             "tags": ["openalex-expand", "expand-second-degree"]}}, "test")
        svc.add_source(rex["id"], {"url": "https://openalex.org/A20", "note": "OpenAlex author record"}, "test")
        svc.create_tie({"fields": {"from": "m1", "to": rex["id"], "type": "coauthor",
                                   "evidence": "Named together on 2 works"}}, "test")
        fp = "Faraway Polytechnic"
        with mock.patch.dict(AUTHORS, {"A20": author("A20", "Rex Earlier", inst=fp),
                                       "A21": author("A21", "Sol Stranger", inst=fp)}):
            extra = [work("W20", 2024, "The namesake's first paper", [("A3", fp), ("A20", fp), ("A21", fp)]),
                     work("W21", 2023, "The namesake's second paper", [("A3", fp), ("A20", fp), ("A21", fp)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        self.assertNotIn("Sol Stranger", self.new_names(plan))
        self.assertEqual(plan["coverage"]["c1"]["unanchored"], 2)

    def test_one_paper_met_by_chance_does_not_bring_in_a_namesakes_other_papers(self):
        # Under Chris's record, a namesake's three papers chained by coauthors; one of them happens to name Frankie.
        Service(Workspace(self.ws_path)).add_source("far", {"url": "https://openalex.org/A12", "note": "OpenAlex"},
                                                    "test")
        fp = "Faraway Polytechnic"
        new = {"A31": author("A31", "Ned Nearby", inst=fp), "A32": author("A32", "Gil Group", inst=fp)}
        with mock.patch.dict(AUTHORS, new):
            extra = [work("W30", 2024, "The namesake with Frankie", [("A3", fp), ("A31", fp), ("A12", LU)]),
                     work("W31", 2023, "The namesake, second paper", [("A3", fp), ("A31", fp), ("A32", fp)]),
                     work("W32", 2022, "The namesake, third paper", [("A3", fp), ("A32", fp)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        names = self.new_names(plan)
        self.assertNotIn("Ned Nearby", names)
        self.assertNotIn("Gil Group", names)
        self.assertEqual(plan["coverage"]["c1"]["unanchored"], 2)

    def test_a_paper_met_by_chance_counts_once_when_it_is_recorded_twice(self):
        # As above, with the namesake's paper that names Frankie also on record as a preprint.
        Service(Workspace(self.ws_path)).add_source("far", {"url": "https://openalex.org/A12", "note": "OpenAlex"},
                                                    "test")
        fp = "Faraway Polytechnic"
        new = {"A31": author("A31", "Ned Nearby", inst=fp), "A32": author("A32", "Gil Group", inst=fp)}
        with mock.patch.dict(AUTHORS, new):
            extra = [work("W30", 2024, "The namesake with Frankie", [("A3", fp), ("A31", fp), ("A12", LU)]),
                     work("W33", 2023, "The Namesake with Frankie", [("A3", fp), ("A31", fp), ("A12", LU)]),
                     work("W31", 2023, "The namesake, second paper", [("A3", fp), ("A31", fp), ("A32", fp)]),
                     work("W32", 2022, "The namesake, third paper", [("A3", fp), ("A32", fp)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        self.assertNotIn("Gil Group", self.new_names(plan))
        self.assertNotIn("Ned Nearby", self.new_names(plan))

    FP = "Faraway Polytechnic"
    LAB_NAMESAKE = {"A101": author("A101", "Vic Visitor", inst=FP), "A102": author("A102", "Bo Bystander", inst=FP),
                    "A103": author("A103", "Gus Gradschool", inst=FP), "A104": author("A104", "Hal Hallmate", inst=FP),
                    "A105": author("A105", "Ivy Ivory", inst=FP), "A106": author("A106", "Jo Jumble", inst=FP),
                    "A107": author("A107", "Wes Westward", inst=FP), "A108": author("A108", "Kai Consortium"),
                    "A109": author("A109", "Lu Large")}

    def test_a_lab_members_papers_that_connect_to_nothing_known_are_left_out(self):
        # Pat's record also holds a namesake's two papers from another place, with people no one in the lab knows.
        with mock.patch.dict(AUTHORS, self.LAB_NAMESAKE):
            fp = self.FP
            extra = [work("W100", 2012, "Namesake paper, first", [("A1", fp), ("A101", fp)]),
                     work("W101", 2013, "Namesake paper, second", [("A1", fp), ("A101", fp), ("A102", fp)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        names = self.new_names(plan)
        self.assertNotIn("Vic Visitor", names)
        self.assertNotIn("Bo Bystander", names)
        self.assertIn("Nia Newcoauthor", names)             # Pat's own paper at Lakeside still counts
        self.assertEqual((plan["coverage"]["pi"]["works"], plan["coverage"]["pi"]["unanchored"]), (2, 2))

    def test_papers_on_a_lab_members_orcid_record_count_and_vouch_for_that_institution(self):
        # Pat's doctoral papers at Faraway: two are on Pat's ORCID record, a third is not but lists the same place.
        Service(Workspace(self.ws_path)).add_source(
            "pi", {"url": "https://orcid.org/0000-0000-0000-0001", "note": "ORCID record"}, "test")
        with mock.patch.dict(AUTHORS, self.LAB_NAMESAKE):
            fp = self.FP
            extra = [work("W102", 2014, "Doctoral paper one", [("A1", fp), ("A103", fp)], doi="10.0000/phd1"),
                     work("W103", 2015, "Doctoral paper two", [("A1", fp), ("A103", fp)], doi="10.0000/phd2"),
                     work("W104", 2015, "Doctoral paper three", [("A1", fp), ("A104", fp)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra), \
                    mock.patch.dict(ORCID_WORKS, {"0000-0000-0000-0001": [("10.0000/phd1", 2014), ("10.0000/phd2", 2015)]}):
                plan = self.run_expand(dry_run=True)["plan"]
        names = self.new_names(plan)
        self.assertIn("Gus Gradschool", names)
        self.assertIn("Hal Hallmate", names)
        self.assertNotIn("unanchored", plan["coverage"]["pi"])

    def test_a_lab_members_paper_counts_when_it_names_someone_the_lab_recorded(self):
        # Morgan is Pat's recorded mentor, and Chris's coauthor record with Pat came from another public source. Rae's
        # only tie to the lab is a coauthor record an expand run wrote from Pat's OpenAlex works.
        svc = Service(Workspace(self.ws_path))
        rae = svc.create_contact({"fields": {"name": "Rae Recorded", "institution": LU}}, self.EXPAND)["id"]
        svc.add_source(rae, {"url": "https://openalex.org/A115", "note": "OpenAlex author record"}, self.EXPAND)
        svc.create_tie({"fields": {"from": "pi", "to": rae, "type": "coauthor", "provenance": "public",
                                   "sourceTitle": "An earlier paper (2019)", "sourceUrl": "https://openalex.org/W199",
                                   "evidence": "Named together on 1 work indexed by OpenAlex"}}, self.EXPAND)
        new = {"A115": author("A115", "Rae Recorded"), "A116": author("A116", "Kit Curated", inst=self.FP)}
        with mock.patch.dict(AUTHORS, {**self.LAB_NAMESAKE, **new}):
            fp = self.FP
            extra = [work("W105", 2014, "Pat and Morgan at Faraway", [("A1", fp), ("A5", NI), ("A105", fp)]),
                     work("W106", 2014, "Pat and Rae elsewhere", [("A1", "Distant University"), ("A115", LU),
                                                                     ("A106", fp)]),
                     work("W116", 2014, "Pat and Chris at Faraway", [("A1", fp), ("A3", LU), ("A116", fp)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        names = self.new_names(plan)
        self.assertIn("Ivy Ivory", names)
        self.assertIn("Kit Curated", names)
        self.assertNotIn("Jo Jumble", names)

    EXPAND = "expand (OpenAlex)"            # who writes the records an expand run adds

    def add_vic(self, svc, member="pi"):
        """Vic, whom an earlier run added as a lab member's coauthor from a namesake's paper on the member's record,
        and the works: that paper, and Vic's two papers with Wes."""
        vic = svc.create_contact({"fields": {"name": "Vic Visitor", "institution": self.FP,
                                             "tags": ["openalex-expand", "expand-first-degree"]}}, self.EXPAND)["id"]
        svc.add_source(vic, {"url": "https://openalex.org/A101", "note": "OpenAlex author record"}, self.EXPAND)
        svc.create_tie({"fields": {"from": member, "to": vic, "type": "coauthor", "provenance": "public",
                                   "sourceTitle": "Namesake paper, first (2012)", "sourceUrl": "https://openalex.org/W100",
                                   "evidence": "Named together on 1 work"}}, self.EXPAND)
        fp = self.FP
        return vic, [work("W100", 2012, "Namesake paper, first", [("A1", fp), ("A101", fp)]),
                     work("W107", 2024, "Vic and Wes, first", [("A101", fp), ("A107", fp)]),
                     work("W108", 2023, "Vic and Wes, second", [("A101", fp), ("A107", fp)])]

    def test_people_tied_to_the_lab_only_by_a_lab_members_left_out_papers_are_not_collected(self):
        svc = Service(Workspace(self.ws_path))
        with mock.patch.dict(AUTHORS, self.LAB_NAMESAKE):
            vic, extra = self.add_vic(svc)
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
                cov = plan["coverage"][vic]
                self.assertEqual((cov["status"], cov["through"]), ("tie-unanchored", ["pi"]))
                self.assertNotIn("Wes Westward", self.new_names(plan))
                self.assertEqual(plan["firstDegree"], sum(1 for v in plan["coverage"].values() if v["degree"] == 1))
                # Once someone in the lab records meeting Vic, the paper is Pat's and Vic's coauthors are collected.
                svc.create_tie({"fields": {"from": "pi", "to": vic, "type": "met", "evidence": "Met at a meeting"}},
                               "Pat Investigator")
                plan = self.run_expand(dry_run=True)["plan"]
        self.assertEqual(plan["coverage"][vic]["status"], "collected")
        self.assertIn("Wes Westward", self.new_names(plan))

    def test_a_namesakes_coauthor_left_out_for_one_member_does_not_vouch_for_another(self):
        # Dana's record and Pat's both hold a namesake's papers with Vic; an earlier run tied Vic to Dana from hers.
        svc = Service(Workspace(self.ws_path))
        svc.add_source("pd", {"url": "https://openalex.org/A110", "note": "OpenAlex author record"}, "test")
        with mock.patch.dict(AUTHORS, {**self.LAB_NAMESAKE, "A110": author("A110", "Dana Postdoc", inst=self.FP)}):
            vic, extra = self.add_vic(svc, member="pd")
            fp = self.FP
            extra += [work("W111", 2013, "Dana's namesake with Vic", [("A110", fp), ("A101", fp)]),
                      work("W101", 2013, "Namesake paper, second", [("A1", fp), ("A101", fp), ("A102", fp)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        cov = plan["coverage"][vic]
        self.assertEqual((cov["status"], cov["through"]), ("tie-unanchored", ["pd", "pi"]))
        names = self.new_names(plan)
        self.assertNotIn("Bo Bystander", names)
        self.assertNotIn("Wes Westward", names)
        self.assertNotIn(("pi", vic), {(e["from"], e["to"]) for e in plan["notebook"]["edges"]})

    def test_an_orcid_id_only_on_a_lab_members_openalex_record_does_not_count(self):
        # The OpenAlex record the library matched to Pat shows a namesake's ORCID iD, which lists the namesake's papers.
        self.write_library({"pi": {"status": "linked", "id": "A1", "ids": ["A1"], "basis": "a match",
                                   "summary": {"orcid": "0000-0000-0000-0028"}}})
        with mock.patch.dict(AUTHORS, self.LAB_NAMESAKE):
            fp = self.FP
            extra = [work("W100", 2012, "Namesake paper, first", [("A1", fp), ("A101", fp)], doi="10.0000/ns1"),
                     work("W101", 2013, "Namesake paper, second", [("A1", fp), ("A101", fp), ("A102", fp)],
                          doi="10.0000/ns2")]
            with mock.patch(__name__ + ".WORKS", WORKS + extra), \
                    mock.patch.dict(ORCID_WORKS, {"0000-0000-0000-0028": [("10.0000/ns1", 2012), ("10.0000/ns2", 2013)]}):
                plan = self.run_expand(dry_run=True)["plan"]
        self.assertNotIn("Bo Bystander", self.new_names(plan))
        self.assertTrue(any("0000-0000-0000-0028" in c and "not on their record" in c for c in plan["report"]["checks"]))

    def test_a_mistyped_orcid_id_on_a_lab_members_record_is_reported_and_not_read(self):
        Service(Workspace(self.ws_path)).add_source(
            "pi", {"url": "https://orcid.org/0000-0000-0000-0002", "note": "ORCID record"}, "test")
        plan = self.run_expand(dry_run=True)["plan"]
        self.assertTrue(any("0000-0000-0000-0002" in e and "not a valid iD" in e for e in plan["report"]["errors"]))
        self.assertFalse(any("0000-0000-0000-0002/works" in u for u in self.fake.calls))

    def test_an_orcid_record_that_cannot_be_read_confirms_nothing(self):
        svc = Service(Workspace(self.ws_path))
        svc.add_source("pi", {"url": "https://orcid.org/0000-0000-0000-0001", "note": "ORCID record"}, "test")
        self.fake.fails = lambda url: "pub.orcid.org" in url and "0000-0000-0000-0001" in url
        with mock.patch.dict(AUTHORS, self.LAB_NAMESAKE):
            vic, extra = self.add_vic(svc)
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        self.assertEqual(plan["coverage"][vic]["status"], "tie-unanchored")
        self.assertNotIn("Wes Westward", self.new_names(plan))
        self.assertTrue(any("Pat Investigator: ORCID record 0000-0000-0000-0001 could not be read" in e
                            for e in plan["report"]["errors"]))

    def test_a_lab_members_orcid_record_is_read_again_each_run(self):
        Service(Workspace(self.ws_path)).add_source(
            "pi", {"url": "https://orcid.org/0000-0000-0000-0001", "note": "ORCID record"}, "test")
        cache = os.path.join(self.tmp, "cache")
        with mock.patch.dict(AUTHORS, self.LAB_NAMESAKE):
            fp = self.FP
            extra = [work("W102", 2014, "Doctoral paper one", [("A1", fp), ("A103", fp)], doi="10.0000/phd1"),
                     work("W103", 2015, "Doctoral paper two", [("A1", fp), ("A103", fp)], doi="10.0000/phd2")]
            plans = []
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                for listed in ([], [("10.0000/phd1", 2014), ("10.0000/phd2", 2015)]):      # Pat adds them to ORCID
                    with mock.patch.dict(ORCID_WORKS, {"0000-0000-0000-0001": listed}):
                        client = Client(cache_dir=cache, transport=self.fake, budget_usd=0.08)
                        plans.append(expand(self.ws_path, client=client, dry_run=True, log=lambda m: None)["plan"])
        self.assertNotIn("Gus Gradschool", self.new_names(plans[0]))
        self.assertIn("Gus Gradschool", self.new_names(plans[1]))

    def test_a_coauthor_record_with_the_lab_that_no_paper_confirms_is_not_collected(self):
        # Kai's coauthor record with Pat was written from a paper with more than 30 authors; Lou's was entered by a
        # person, as a public record.
        svc = Service(Workspace(self.ws_path))
        ids = {}
        for name, aid, actor in (("Kai Consortium", "A108", self.EXPAND), ("Lou Entered", "A112", "Pat Investigator")):
            ids[name] = svc.create_contact({"fields": {"name": name, "institution": LU}}, actor)["id"]
            svc.add_source(ids[name], {"url": f"https://openalex.org/{aid}", "note": "OpenAlex author record"}, actor)
            svc.create_tie({"fields": {"from": "pi", "to": ids[name], "type": "coauthor", "provenance": "public",
                                       "sourceTitle": "A consortium paper (2020)", "sourceUrl": "https://doi.org/10.0000/big",
                                       "evidence": "Named together on 1 work"}}, actor)
        new = {"A112": author("A112", "Lou Entered"), "A113": author("A113", "Max Moreland")}
        with mock.patch.dict(AUTHORS, {**self.LAB_NAMESAKE, **new}):
            extra = [work("W109", 2024, "Kai and Lu, first", [("A108", LU), ("A109", LU)]),
                     work("W110", 2023, "Kai and Lu, second", [("A108", LU), ("A109", LU)]),
                     work("W112", 2024, "Lou and Max, first", [("A112", LU), ("A113", LU)]),
                     work("W113", 2023, "Lou and Max, second", [("A112", LU), ("A113", LU)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        names = self.new_names(plan)
        self.assertEqual(plan["coverage"][ids["Kai Consortium"]]["status"], "tie-unconfirmed")
        self.assertNotIn("Lu Large", names)
        self.assertEqual(plan["coverage"][ids["Lou Entered"]]["status"], "collected")
        self.assertIn("Max Moreland", names)

    def test_people_in_the_network_only_through_someone_no_longer_collected_are_listed(self):
        svc = Service(Workspace(self.ws_path))
        with mock.patch.dict(AUTHORS, self.LAB_NAMESAKE):
            vic, extra = self.add_vic(svc)
            wes = svc.create_contact({"fields": {"name": "Wes Westward", "institution": self.FP,
                                                 "tags": ["openalex-expand", "expand-second-degree"]}}, self.EXPAND)["id"]
            svc.create_tie({"fields": {"from": vic, "to": wes, "type": "coauthor", "provenance": "public",
                                       "sourceTitle": "Vic and Wes, first (2024)", "sourceUrl": "https://openalex.org/W107",
                                       "evidence": "Named together on 2 works"}}, self.EXPAND)
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                res = self.run_expand()
        self.assertEqual(res["plan"]["coverage"][vic]["onlyThrough"], [wes])
        with open(res["digestPath"], encoding="utf-8") as f:
            self.assertIn("in the network only through them: Wes Westward", f.read())
        c = Service(Workspace(self.ws_path)).person(vic, {"maxDegree": "all", "doctorate": "any",
                                                         "identified": "0"})["collection"]
        self.assertEqual((c["status"], c["through"], c["onlyThrough"]), ("tie-unanchored", ["Pat Investigator"], 1))

    def test_a_run_for_some_people_records_no_one_else(self):
        svc = Service(Workspace(self.ws_path))
        with mock.patch.dict(AUTHORS, self.LAB_NAMESAKE):
            vic, extra = self.add_vic(svc)
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True, only=["Chris Coauthor"])["plan"]
        self.assertNotIn(vic, plan["coverage"])
        self.assertEqual(plan["coverage"]["c1"]["status"], "collected")

    def test_a_run_for_some_people_still_keeps_flagged_people_from_vouching(self):
        # Chris's record also holds a namesake's two papers with Vic, whom a full run flags, and Tam.
        svc = Service(Workspace(self.ws_path))
        with mock.patch.dict(AUTHORS, {**self.LAB_NAMESAKE, "A117": author("A117", "Tam Tagalong", inst=self.FP)}):
            vic, extra = self.add_vic(svc)
            fp = self.FP
            extra += [work(wid, year, f"Chris's namesake with Vic and Tam ({year})", [("A3", fp), ("A101", fp), ("A117", fp)])
                      for wid, year in (("W117", 2024), ("W118", 2023))]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plans = [self.run_expand(dry_run=True)["plan"],
                         self.run_expand(dry_run=True, only=["Chris Coauthor"])["plan"]]
        for plan in plans:
            self.assertNotIn("Tam Tagalong", self.new_names(plan))
            self.assertEqual(plan["coverage"]["c1"]["works"], 3)

    def held_vic_plan(self, inst):
        """A plan in which Pat's own paper at Lakeside also names a Vic Visitor, at `inst`, under another OpenAlex
        record (A150)."""
        svc = Service(Workspace(self.ws_path))
        with mock.patch.dict(AUTHORS, {**self.LAB_NAMESAKE, "A150": author("A150", "Vic Visitor", inst=inst)}):
            vic, extra = self.add_vic(svc)
            extra.append(work("W150", 2024, "Pat's own paper with a Vic Visitor", [("A1", LU), ("A150", inst)]))
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                return vic, self.run_expand(dry_run=True)["plan"]

    def test_a_held_namesake_of_a_flagged_person_does_not_confirm_their_tie(self):
        vic, plan = self.held_vic_plan(LU)                 # Vic's own record says Faraway
        self.assertEqual(plan["coverage"][vic]["status"], "tie-unanchored")
        self.assertNotIn("Wes Westward", self.new_names(plan))
        self.assertTrue(any("https://openalex.org/A150" in c for c in plan["report"]["checks"]))

    def test_a_held_record_at_the_persons_own_institution_confirms_their_tie(self):
        vic, plan = self.held_vic_plan(self.FP)
        self.assertEqual(plan["coverage"][vic]["status"], "collected")
        self.assertIn("Wes Westward", self.new_names(plan))

    def test_people_further_out_through_someone_no_longer_collected_are_listed_too(self):
        # Wes and Xan came in through Vic, and an earlier run also recorded them as coauthors of each other.
        svc = Service(Workspace(self.ws_path))
        with mock.patch.dict(AUTHORS, self.LAB_NAMESAKE):
            vic, extra = self.add_vic(svc)
            ids = [svc.create_contact({"fields": {"name": name, "institution": self.FP,
                                                  "tags": ["openalex-expand", "expand-second-degree"]}}, self.EXPAND)["id"]
                   for name in ("Wes Westward", "Xan Xavier")]
            for a, b in ((vic, ids[0]), (vic, ids[1]), (ids[0], ids[1])):
                svc.create_tie({"fields": {"from": a, "to": b, "type": "coauthor", "provenance": "public",
                                           "sourceTitle": "A paper (2024)", "sourceUrl": "https://openalex.org/W107",
                                           "evidence": "Named together on 2 works indexed by OpenAlex"}}, self.EXPAND)
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        self.assertEqual(plan["coverage"][vic]["onlyThrough"], sorted(ids))

    def test_people_tied_to_a_lab_member_whose_works_cannot_be_read_keep_their_earlier_note(self):
        svc = Service(Workspace(self.ws_path))
        self.fake.fails = lambda url: "authorships.author.id:A1,authors_count" in url
        with mock.patch.dict(AUTHORS, self.LAB_NAMESAKE):
            vic, extra = self.add_vic(svc)
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        self.assertNotIn(vic, plan["coverage"])
        self.assertNotIn("pi", plan["coverage"])            # Pat's own earlier note stays too
        self.assertNotIn("Wes Westward", self.new_names(plan))
        self.assertTrue(any("could not be read" in e for e in plan["report"]["errors"]))
        self.assertEqual(plan["firstDegree"], sum(1 for v in plan["coverage"].values() if v["degree"] == 1))

    def test_a_shared_nih_award_joined_by_name_alone_confirms_nothing(self):
        # Onboarding joined one of Pat's NIH co-investigators to Vic by name; only the co-investigator's NIH profile ID
        # on Vic's record shows they are one person.
        svc = Service(Workspace(self.ws_path))
        with mock.patch.dict(AUTHORS, self.LAB_NAMESAKE):
            vic, extra = self.add_vic(svc)
            svc.create_tie({"fields": {"from": "pi", "to": vic, "type": "grant", "provenance": "public",
                                       "sourceTitle": "NIH RePORTER award record",
                                       "sourceUrl": "https://reporter.nih.gov/project-details/1",
                                       "evidence": "Both are named as principal investigators on NIH project R01XX000002"}},
                           "onboarding")
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                before = self.run_expand(dry_run=True)["plan"]
                svc.add_source(vic, {"url": "https://reporter.nih.gov/project-details/1",
                                     "note": "Named as PI on NIH project R01XX000002; NIH RePORTER PI profile ID 1"}, "test")
                after = self.run_expand(dry_run=True)["plan"]
        self.assertEqual(before["coverage"][vic]["status"], "tie-unanchored")
        self.assertEqual(after["coverage"][vic]["status"], "collected")

    def test_institutions_match_by_name_or_an_affiliated_unit(self):
        from labnetwork.expand import _inst_match
        for recorded, named in (("Lakeside University", "Lakeside University Medical Center"),
                                ("Department of Psychology, Lakeside University", "Lakeside University"),
                                ("Elsewhere College; Lakeside University", "Lakeside University"),
                                ("Lakeside University", "Elsewhere College; Lakeside University"),
                                ("The Ohio State University", "Ohio State University")):
            self.assertTrue(_inst_match(recorded, named), (recorded, named))
        for recorded, named in (("University of California", "University of California, Los Angeles"),
                                ("University of Pennsylvania", "Indiana University of Pennsylvania"),
                                ("Lakeside University", "Lakeside University Press"), ("", "Lakeside University"),
                                ("Northfield Institute, Lakeside", "Lakeside Medical Center")):
            self.assertFalse(_inst_match(recorded, named), (recorded, named))

    def test_only_a_full_name_or_orcid_id_marks_the_person_on_a_paper_listed_on_orcid(self):
        from labnetwork.expand import _selves
        with mock.patch.dict(AUTHORS, {"A114": author("A114", "C. Coauthor")}):
            w = work("W114", 2024, "Listed on ORCID", [("A114", LU), ("A3", LU)])
        self.assertEqual(_selves(w, "Chris Coauthor", ""), {"A3"})

    def test_people_onboarding_added_do_not_vouch_for_papers(self):
        svc = Service(Workspace(self.ws_path))
        # Onboarding added Rex through Morgan and Chris, from the same kind of coauthorship expand checks.
        rex = svc.create_contact({"fields": {"name": "Rex Earlier", "institution": "Faraway Polytechnic",
                                             "tags": ["openalex", "openalex-second-degree"]}}, "test")
        svc.add_source(rex["id"], {"url": "https://openalex.org/A20", "note": "OpenAlex author record"}, "test")
        for other in ("m1", "c1"):
            svc.create_tie({"fields": {"from": other, "to": rex["id"], "type": "coauthor",
                                       "evidence": "Named together on 2 works"}}, "test")
        fp = "Faraway Polytechnic"
        with mock.patch.dict(AUTHORS, {"A20": author("A20", "Rex Earlier", inst=fp),
                                       "A21": author("A21", "Sol Stranger", inst=fp)}):
            extra = [work("W20", 2024, "The namesake's first paper", [("A3", fp), ("A20", fp), ("A21", fp)]),
                     work("W21", 2023, "The namesake's second paper", [("A3", fp), ("A20", fp), ("A21", fp)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        self.assertNotIn("Sol Stranger", self.new_names(plan))

    def test_a_match_with_no_works_since_the_start_year_is_not_written_to_the_record(self):
        svc = Service(Workspace(self.ws_path))
        nell = svc.create_contact({"fields": {"name": "Nell Nowhere", "institution": "Quiet College",
                                              "role": "Professor"}}, "test")
        svc.create_tie({"fields": {"from": "pi", "to": nell["id"], "type": "collaborator",
                                   "evidence": "Collaborators on a study"}}, "test")
        with mock.patch.dict(AUTHORS, {"A80": author("A80", "Nell Nowhere", inst="Quiet College")}):
            with mock.patch(__name__ + ".WORKS", WORKS + [work("W80", 2010, "Nell, long ago", [("A80", LU)])]):
                plan = self.run_expand(dry_run=True)["plan"]
        self.assertNotIn(nell["id"], [c["id"] for c in plan["notebook"]["contacts"] if "name" not in c])

    def test_a_match_by_initial_holds_only_for_the_person_whose_tie_confirmed_it(self):
        # R. Quill is Chris's coauthor; OpenAlex's Robin Quill on Chris's papers is taken to be them, through that tie.
        svc = Service(Workspace(self.ws_path))
        rq = svc.create_contact({"fields": {"name": "R. Quill", "institution": "Elsewhere College"}}, "test")["id"]
        svc.create_tie({"fields": {"from": "c1", "to": rq, "type": "coauthor", "evidence": "Reported"}}, "test")
        with mock.patch.dict(AUTHORS, {"A60": author("A60", "Robin Quill", inst=EC)}):
            extra = [work(f"W6{k}", 2024 - k, f"Paper {k} with Robin Quill", [(who, LU), ("A60", EC)])
                     for k, who in ((7, "A3"), (8, "A3"), (9, "A5"))]
            extra.append(work("W70", 2020, "Morgan and Robin, second", [("A5", NI), ("A60", EC)]))
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        pairs = {frozenset((e["from"], e["to"])) for e in plan["notebook"]["edges"]}
        self.assertNotIn(frozenset(("m1", rq)), pairs)        # Morgan has no tie to R. Quill to confirm it
        self.assertNotIn("Robin Quill", self.new_names(plan))

    def test_notices_are_not_papers(self):
        from labnetwork.expand import NOTICE_RX
        for t in ("Correction: Sleep and ADHD", "Author Correction: X", "Erratum to: Y", "Corrigendum", "Retracted: Z",
                  "Retraction Note: A", "Notice of Retraction: C", "Expression of Concern: B"):
            self.assertTrue(NOTICE_RX.match(t), t)
        for t in ("Correction of refractive error in children", "Correctional settings and mental health",
                  "Corrections officers and stress", "Correction for multiple testing in twin studies"):
            self.assertFalse(NOTICE_RX.match(t), t)

    def test_a_record_whose_works_connect_to_nothing_is_not_collected(self):
        svc = Service(Workspace(self.ws_path))
        nell = svc.create_contact({"fields": {"name": "Nell Nowhere", "institution": "Quiet College",
                                              "role": "Professor"}}, "test")
        svc.add_source(nell["id"], {"url": "https://openalex.org/A80", "note": "OpenAlex author record"}, "test")
        svc.create_tie({"fields": {"from": "pi", "to": nell["id"], "type": "collaborator",
                                   "evidence": "Collaborators on a study"}}, "test")
        du = "Distant University"
        with mock.patch.dict(AUTHORS, {"A80": author("A80", "Nell Nowhere", inst=du),
                                       "A81": author("A81", "Pip Distant", inst=du)}):
            extra = [work("W80", 2024, "Another Nell, first", [("A80", du), ("A81", du)]),
                     work("W81", 2023, "Another Nell, second", [("A80", du), ("A81", du)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        cov = plan["coverage"][nell["id"]]
        self.assertEqual((cov["status"], cov["unanchored"]), ("unanchored", 2))
        self.assertNotIn("Pip Distant", self.new_names(plan))

    def test_papers_listed_on_orcid_count(self):
        svc = Service(Workspace(self.ws_path))
        svc.add_source("c1", {"url": "https://orcid.org/0000-0000-0000-0003", "note": "ORCID record"}, "test")
        with mock.patch.dict(AUTHORS, {"A30": author("A30", "Chris Coauthor"), "A31": author("A31", "Wren Splitwork")}):
            extra = [work("W30", 2024, "Chris and Wren under Chris's record", [("A3", LU), ("A31", LU)]),
                     work("W31", 2023, "Chris and Wren under another record", [("A30", LU), ("A31", LU)],
                          doi="10.0000/split")]
            with mock.patch(__name__ + ".WORKS", WORKS + extra), \
                    mock.patch.dict(ORCID_WORKS, {"0000-0000-0000-0003": [("10.0000/split", 2023)]}):
                plan = self.run_expand(dry_run=True)["plan"]
        names = self.new_names(plan)
        self.assertIn("Wren Splitwork", names)          # two shared papers, one of them found through ORCID
        self.assertNotIn("Chris Coauthor", names)       # Chris's own authorship on it is not a new person
        self.assertEqual(plan["coverage"]["c1"]["orcidWorks"], 1)

    def test_new_people_with_an_orcid_id_already_taken_are_held_back(self):
        svc = Service(Workspace(self.ws_path))
        svc.add_source("far", {"url": "https://orcid.org/0000-0000-0000-0014", "note": "ORCID record"}, "test")
        with mock.patch.dict(AUTHORS, {"A14": author("A14", "Uma Unknown", inst=EC, orcid="0000-0000-0000-0014"),
                                       "A42": author("A42", "O. Otherperson", inst=EC, orcid="0000-0000-0000-0010")}):
            extra = [work("W42", 2024, "Chris and O., first", [("A3", LU), ("A42", EC)]),
                     work("W43", 2023, "Chris and O., second", [("A3", LU), ("A42", EC)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        held = {h["name"]: h.get("sameOrcidAs") for h in plan["report"]["held"] if h.get("sameOrcidAs")}
        names = self.new_names(plan)
        self.assertEqual(held.get("Uma Unknown"), "far")                 # Frankie's ORCID iD
        self.assertNotIn("Uma Unknown", names)
        # Ola and O. Otherperson carry one ORCID iD: the first added stays, the other is held back.
        self.assertEqual(len({"Ola Otherperson", "O. Otherperson"} & set(held)), 1)
        self.assertEqual(len({"Ola Otherperson", "O. Otherperson"} & set(names)), 1)
        # No coauthor record is left pointing at someone held back.
        people = {c["id"] for c in plan["notebook"]["contacts"]} | set(svc.nb.records["contacts"])
        self.assertTrue(all(e["from"] in people and e["to"] in people for e in plan["notebook"]["edges"]))

    def test_people_this_run_joins_to_the_lab_have_their_coauthors_collected_in_it(self):
        # Cory is second degree until this run records his paper with Pat (W1); his own coauthors come in the same run.
        with mock.patch.dict(AUTHORS, {"A70": author("A70", "Gale Grove")}):
            extra = [work("W70", 2024, "Cory and Gale, first", [("A4", LU), ("A70", LU)]),
                     work("W71", 2023, "Cory and Gale, second", [("A4", LU), ("A70", LU)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        self.assertEqual(plan["coverage"]["c3"]["status"], "collected")
        self.assertIn("Gale Grove", self.new_names(plan))

    def test_the_plan_holds_back_whoever_the_import_would(self):
        # The importer holds back a similar first name with the same surname, and a second new record with one name;
        # the plan does too, so a dry run lists only the people an import adds.
        Service(Workspace(self.ws_path)).create_contact({"fields": {"name": "Tamsin-Jo Lookalike"}}, "test")
        new = {"A75": author("A75", "Tam-Lee Lookalike"), "A76": author("A76", "Wren Miller"),
               "A77": author("A77", "Wren Miller")}
        with mock.patch.dict(AUTHORS, new):
            extra = [work(f"W7{k}", 2024 - k, f"Chris with Tam-Lee and two Wrens, paper {k}",
                          [("A3", LU)] + [(a, LU) for a in new]) for k in range(5, 7)]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                res = self.run_expand()
        plan = res["plan"]
        held = {h["openalexId"]: h for h in plan["report"]["held"]}
        self.assertEqual(held["A75"]["nameMatch"], "similar name to")
        self.assertEqual(self.new_names(plan).count("Wren Miller"), 1)
        self.assertEqual(len({"A76", "A77"} & set(held)), 1)
        self.assertNotIn("Tam-Lee Lookalike", self.new_names(plan))
        self.assertEqual(res["result"]["applied"]["skipped"], 0)          # the import has nobody left to hold back

    def test_a_match_whose_works_connect_to_nothing_is_not_written_to_the_record(self):
        svc = Service(Workspace(self.ws_path))
        nell = svc.create_contact({"fields": {"name": "Nell Nowhere", "institution": "Quiet College",
                                              "role": "Professor"}}, "test")
        svc.create_tie({"fields": {"from": "pi", "to": nell["id"], "type": "collaborator",
                                   "evidence": "Collaborators on a study"}}, "test")
        du = "Distant University"
        # The only Nell Nowhere at Quiet College, whose papers all list another place and share no one with the network.
        with mock.patch.dict(AUTHORS, {"A80": author("A80", "Nell Nowhere", inst="Quiet College"),
                                       "A81": author("A81", "Pip Distant", inst=du)}):
            extra = [work("W80", 2024, "Another Nell, first", [("A80", du), ("A81", du)]),
                     work("W81", 2023, "Another Nell, second", [("A80", du), ("A81", du)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        self.assertEqual(plan["coverage"][nell["id"]]["status"], "unanchored")
        self.assertNotIn(nell["id"], [c["id"] for c in plan["notebook"]["contacts"] if "name" not in c])

    def test_papers_on_records_an_earlier_run_wrote_do_not_vouch(self):
        svc = Service(Workspace(self.ws_path))
        # An earlier run recorded Chris and Rex on the namesake's paper; that paper is still not shown to be Chris's.
        rex = svc.create_contact({"fields": {"name": "Rex Earlier", "institution": "Faraway Polytechnic",
                                             "tags": ["openalex-expand", "expand-second-degree"]}}, "test")
        svc.add_source(rex["id"], {"url": "https://openalex.org/A20", "note": "OpenAlex author record"}, "test")
        svc.create_tie({"fields": {"from": "c1", "to": rex["id"], "type": "coauthor", "evidence": "Named together",
                                   "sourceTitle": "The namesake's first paper (2024)",
                                   "sourceUrl": "https://doi.org/10.0000/namesake1"}}, "test")
        fp = "Faraway Polytechnic"
        with mock.patch.dict(AUTHORS, {"A20": author("A20", "Rex Earlier", inst=fp),
                                       "A21": author("A21", "Sol Stranger", inst=fp)}):
            extra = [work("W20", 2024, "The namesake's first paper", [("A3", fp), ("A20", fp), ("A21", fp)],
                          doi="10.0000/namesake1"),
                     work("W21", 2023, "The namesake's second paper", [("A3", fp), ("A20", fp), ("A21", fp)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        self.assertNotIn("Sol Stranger", self.new_names(plan))

    def test_a_record_marked_not_theirs_voids_the_library_match_that_joined_it(self):
        # The library joined A6 and A7 by ORCID iD and matched them to Mira; A6 was then marked as not hers.
        self.write_library({"m2": {"status": "linked", "id": "A6", "ids": ["A6", "A7"], "basis": "a match"}})
        Service(Workspace(self.ws_path)).mark_not_theirs("m2", {"openalexId": "A6"}, "test")
        with mock.patch.dict(AUTHORS, {"A65": author("A65", "Rory Northfield", inst=NI)}):
            extra = [work("W65", 2024, "The other Mira and Rory, first", [("A7", NI), ("A65", NI)]),
                     work("W66", 2023, "The other Mira and Rory, second", [("A7", NI), ("A65", NI)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        self.assertNotEqual(plan["coverage"]["m2"]["status"], "collected")
        self.assertNotIn("Rory Northfield", self.new_names(plan))

    def test_people_whose_author_records_cannot_be_read_wait_for_the_next_run(self):
        # The author records of the new second-degree people cannot be read, so their ORCID iDs go unchecked.
        self.fake.fails = lambda url: "/authors?" in url and "A14" in url
        plan = self.run_expand(dry_run=True)["plan"]
        self.assertNotIn("Uma Unknown", self.new_names(plan))
        self.assertIn("Nia Newcoauthor", self.new_names(plan))
        self.assertTrue(any("could not be read" in e for e in plan["report"]["errors"]))
        self.assertTrue(all(e["from"] in {c["id"] for c in plan["notebook"]["contacts"]} | {"pi", "c1", "c3", "m1"}
                            or e["to"] != "uma-unknown" for e in plan["notebook"]["edges"]))

    def test_a_spending_stop_while_checking_new_people_keeps_the_rest_of_the_run(self):
        real = oa.get_authors

        def get_authors(client, ids):
            if "A14" in ids:
                raise BudgetReached("https://api.openalex.org/authors", 0, "stopped at the limit")
            return real(client, ids)
        with mock.patch("labnetwork.expand.oa.get_authors", side_effect=get_authors):
            plan = self.run_expand(dry_run=True)["plan"]
        self.assertTrue(plan["stopped"])
        self.assertEqual(plan["coverage"]["c1"]["status"], "stopped")    # Chris's coauthors were not all checked
        self.assertIn("Nia Newcoauthor", self.new_names(plan))          # first degree: checked before the stop
        self.assertNotIn("Uma Unknown", self.new_names(plan))
        self.assertTrue(any("spending limit" in e for e in plan["report"]["errors"]))

    def test_a_preprint_and_its_article_count_as_one_shared_work(self):
        new = {"A91": author("A91", "Dee Duplicate"), "A92": author("A92", "Cal Correction")}
        with mock.patch.dict(AUTHORS, new):
            extra = [work("W90", 2023, "Sleep timing in a cohort of twins (preprint)", [("A3", LU), ("A91", LU)]),
                     work("W91", 2024, "Sleep Timing in a Cohort of Twins", [("A3", LU), ("A91", LU)]),
                     work("W92", 2022, "Naps and attention in adolescents", [("A3", LU), ("A92", LU)]),
                     work("W93", 2023, "Correction: Naps and attention in adolescents", [("A3", LU), ("A92", LU)])]
            extra[0]["title"] = "Sleep timing in a cohort of twins"
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        names = self.new_names(plan)
        self.assertNotIn("Dee Duplicate", names)
        self.assertNotIn("Cal Correction", names)
        self.assertIn("Ola Otherperson", names)

    def test_an_orcid_record_that_cannot_be_read_still_leaves_its_id_on_the_record(self):
        self.fake.fails = lambda url: "pub.orcid.org" in url and "0000-0000-0000-0010/activities" in url
        plan = self.run_expand(dry_run=True)["plan"]
        ola = next(c for c in plan["notebook"]["contacts"] if c.get("name") == "Ola Otherperson")
        self.assertIn("https://orcid.org/0000-0000-0000-0010", [s["url"] for s in ola["sources"]])

    def test_an_orcid_id_found_on_a_first_degree_record_holds_back_their_other_records(self):
        # Chris's own OpenAlex record carries an ORCID iD; a second record with it is Chris again, not a new person.
        with mock.patch.dict(AUTHORS, {"A3": author("A3", "Chris Coauthor", orcid="0000-0000-0000-0003"),
                                       "A93": author("A93", "Topher Ellison", orcid="0000-0000-0000-0003")}):
            extra = [work("W94", 2024, "Morgan and Topher, first", [("A5", NI), ("A93", NI)]),
                     work("W95", 2023, "Morgan and Topher, second", [("A5", NI), ("A93", NI)])]
            with mock.patch(__name__ + ".WORKS", WORKS + extra):
                plan = self.run_expand(dry_run=True)["plan"]
        held = {h["name"]: h.get("sameOrcidAs") for h in plan["report"]["held"]}
        self.assertEqual(held.get("Topher Ellison"), "c1")
        self.assertNotIn("Topher Ellison", self.new_names(plan))

    def test_a_long_paper_title_still_fits_its_coauthor_record(self):
        long = dict(WORKS[1], title="A second paper with a new coauthor " + "and a very long subtitle " * 14)
        with mock.patch(__name__ + ".WORKS", [WORKS[0], long] + WORKS[2:]):
            res = self.run_expand()
        self.assertEqual(res["result"]["applied"]["rejected"], 0)
        svc = Service(Workspace(self.ws_path))
        nia = next(pid for pid, c in svc.nb.active("contacts") if c["name"] == "Nia Newcoauthor")
        self.assertTrue(any({e["from"], e["to"]} == {"pi", nia} for _, e in svc.nb.active("edges")))

    def test_budget_stops_before_spending(self):
        with self.assertRaises(SystemExit):
            self.run_expand(dry_run=True, budget_usd=0.0)

    def test_a_plan_larger_than_an_upload_is_applied_in_one_batch(self):
        with mock.patch("labnetwork.importer.MAX_BYTES", 1000):
            res = self.run_expand()
        svc = Service(Workspace(self.ws_path))
        self.assertIn("Ola Otherperson", {c["name"] for _, c in svc.nb.active("contacts")})
        self.assertEqual(list(svc.nb.batches), [res["result"]["batchId"]])

    def test_a_plan_larger_than_the_running_app_accepts_stops_before_sending(self):
        with mock.patch("labnetwork.importer.MAX_BYTES", 1000), \
                mock.patch("labnetwork.expand._running_port", return_value=8765), \
                mock.patch("labnetwork.expand._post") as post:
            with self.assertRaises(SystemExit) as stop:
                self.run_expand()
        self.assertIn("Stop the app and run expand again", str(stop.exception))
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
