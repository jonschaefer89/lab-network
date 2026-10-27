"""Expansion from OpenAlex coauthorships, against a fake OpenAlex and ORCID (no network)."""
from __future__ import annotations

import os
import shutil
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from labnetwork.connectors.http import Client
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
            "authorships": [{"author": {"id": f"https://openalex.org/{a}", "display_name": AUTHORS[a]["display_name"]},
                             "institutions": [{"display_name": inst}]} for a, inst in authors]}


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
LU, EC = "Lakeside University", "Elsewhere College"
WORKS = [
    work("W1", 2024, "Sleep and attention-deficit/hyperactivity disorder in twins", [("A1", LU), ("A4", LU)],
         doi="10.0000/sleep1"),
    work("W2", 2023, "A second paper with a new coauthor", [("A1", LU), ("A3", LU), ("A9", LU)]),
    work("W3", 2022, "Chris and Ola, first", [("A3", LU), ("A10", EC), ("A13", EC), ("A14", EC)]),
    work("W4", 2021, "Chris and Ola, second", [("A3", LU), ("A10", EC), ("A13", EC), ("A14", EC), ("A12", LU)]),
    work("W5", 2020, "Morgan and Ola", [("A5", "Northfield Institute"), ("A10", EC)]),
    work("W6", 2010, "Too old to count", [("A3", LU), ("A14", EC)]),
]


class FakeOpenAlex:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append(url)
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
            ids = set(parts["authorships.author.id"].split("|"))
            since = parts.get("from_publication_date", "1900-01-01")
            limit = int(parts["authors_count"].lstrip("<")) if "authors_count" in parts else 10 ** 6
            out = [w for w in WORKS if w["publication_date"] >= since and len(w["authorships"]) < limit and
                   ids & {a["author"]["id"].rsplit("/", 1)[1] for a in w["authorships"]}]
            return {"results": sorted(out, key=lambda w: w["publication_date"], reverse=True), "meta": meta}
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
        dist, _ = svc.lab_dist()
        self.assertEqual(dist[ids["Nia Newcoauthor"]], 1)
        self.assertEqual(dist[ids["Ola Otherperson"]], 2)
        self.assertEqual(dist["c3"], 1)              # was second degree before the new coauthor record
        out = svc.undo_import(batch, "test")
        self.assertGreater(out["archived"], 0)
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

    def test_budget_stops_before_spending(self):
        with self.assertRaises(SystemExit):
            self.run_expand(dry_run=True, budget_usd=0.0)


if __name__ == "__main__":
    unittest.main()
