"""Relationship sections on profiles, search links through relationship records, and the degree limit."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from labnetwork.service import Service
from labnetwork.store import Workspace
from tests.fixtures import edge, notebook, write_packet

ANY = {"maxDegree": "all", "doctorate": "any", "identified": "0"}


def workspace(nb=None, settings=None):
    tmp = tempfile.mkdtemp(prefix="labnetwork-test-")
    ws = Workspace.create(os.path.join(tmp, "ws"), write_packet(tmp, nb), lab_name="Fixture Lab")
    if settings:
        ws.config["settings"].update(settings)
        ws.save_config()
    return tmp, Service(Workspace(ws.path))


class TieSectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.svc = workspace()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sections_in_order(self):
        ties = self.svc.person("pi", ANY)["ties"]
        sections = []
        for t in ties:
            if not sections or sections[-1] != (t["group"], t["sectionLabel"]):
                sections.append((t["group"], t["sectionLabel"]))
        self.assertEqual(sections[:4], [("supervision", "Mentors"), ("supervision", "Mentees"),
                                        ("research", "Coauthors"), ("research", "Shared grants")])
        # Training programs sit with associations, after research.
        self.assertIn(("association", "Training programs"), sections)
        self.assertLess(sections.index(("research", "Coauthors")), sections.index(("association", "Training programs")))
        mentors = [t["person"]["id"] for t in ties if t["sectionLabel"] == "Mentors"]
        self.assertEqual(mentors, ["m1", "m2"])        # dated (2015) before undated

    def test_one_way_sides(self):
        stu = {t["person"]["id"]: t["sectionLabel"] for t in self.svc.person("stu", ANY)["ties"]}
        self.assertEqual(stu["pi"], "Mentors")
        self.assertEqual(stu["cm"], "Committee members")
        cm = {t["person"]["id"]: t["sectionLabel"] for t in self.svc.person("cm", ANY)["ties"]}
        self.assertEqual(cm["stu"], "Students whose committees they served on")
        pd = {t["person"]["id"]: t["sectionLabel"] for t in self.svc.person("pd", ANY)["ties"]}
        self.assertEqual(pd["pdsup"], "Postdoc supervisors")


class SearchRecordTests(unittest.TestCase):
    def setUp(self):
        nb = notebook()
        # stu and c2 coauthored the paper that sits on pi's and c3's profiles; neither has it on a profile.
        nb["edges"].append(edge("e-stu-c2-paper", "stu", "c2", "coauthor",
                                sourceTitle="Sleep and attention-deficit/hyperactivity disorder in twins (2024)",
                                sourceUrl="https://doi.org/10.0000/sleep1"))
        nb["edges"].append(edge("e-stu-c1-poster", "stu", "c1", "coauthor",
                                sourceTitle="Poster: ADHD symptoms and sleep timing (2025)", sourceUrl=""))
        self.tmp, self.svc = workspace(nb)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_coauthor_records_link_papers_to_both_people(self):
        r = self.svc.search({"q": "ADHD", **ANY})
        paper = next(p for p in r["papers"] if p["title"].startswith("Sleep and attention"))
        basis = {a["personId"]: a["basis"] for a in paper["associations"]}
        self.assertEqual(basis["pi"], "profile")
        self.assertEqual(basis["c3"], "profile")
        self.assertEqual(basis["stu"], "record")
        self.assertEqual(basis["c2"], "record")
        # A record whose paper is on no profile stands for the paper itself.
        poster = next(p for p in r["papers"] if p["title"].startswith("Poster"))
        self.assertEqual({a["personId"] for a in poster["associations"]}, {"stu", "c1"})
        self.assertIn("stu", {p["personId"] for p in r["peopleSummary"]})
        # The results page lists people with their position and institution.
        pi = next(p for p in r["peopleSummary"] if p["personId"] == "pi")
        self.assertEqual((pi["role"], pi["institution"]), ("Assistant Professor", "Lakeside University"))

    def test_rejected_records_do_not_link(self):
        nb = notebook()
        nb["edges"].append(edge("e-iso-paper", "iso", "c2", "coauthor", status="rejected",
                                sourceTitle="Sleep and attention-deficit/hyperactivity disorder in twins (2024)"))
        tmp, svc = workspace(nb)
        try:
            r = svc.search({"q": "ADHD", **ANY})
            people = {a["personId"] for p in r["papers"] for a in p["associations"]}
            self.assertNotIn("iso", people)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DegreeLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.svc = workspace(settings={"degreeLimit": 2})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_limit_caps_every_view(self):
        self.assertEqual(self.svc.state()["degreeLimit"], 2)
        net = self.svc.network(ANY)
        self.assertTrue(all(n["degree"] is not None and n["degree"] <= 2 for n in net["nodes"] if not n["ghost"]))
        self.assertEqual(net["counts"]["hiddenBy"]["beyond"], 3)      # far (3), jose (4), iso (no path)
        rows = {r["id"]: r for r in self.svc.people(ANY)["rows"]}
        self.assertIn("beyond", rows["far"]["hiddenBy"])
        self.assertEqual(self.svc.default_filters()["deg"], "2")

    def test_search_drops_topic_matches_beyond_the_limit(self):
        r = self.svc.search({"q": "zebrafish", **ANY})
        self.assertEqual(r["profiles"], [])
        self.assertEqual(r["beyondLimit"]["people"], 1)
        named = self.svc.search({"q": "Frankie", **ANY})
        self.assertEqual([p["personId"] for p in named["people"]], ["far"])


if __name__ == "__main__":
    unittest.main()


class AffiliationTests(unittest.TestCase):
    def test_department_titles(self):
        from labnetwork.affiliations import department_of
        cases = {
            "Assistant Professor of Psychology": "Psychology",
            "Professor of Psychology; Distinguished McKnight University Professor": "Psychology",
            "Postdoctoral Researcher, Department of Psychiatry": "Psychiatry",
            "Professor, Pediatrics and Institute of Child Development; Co-Director": "Pediatrics",
            "Associate Professor, Institute of Child Development": "Institute of Child Development",
            "Staff Psychologist / Clinician Investigator; Assistant Professor of Psychiatry": "Psychiatry",
            "Professor, Medicine – Veterans Administration Medical Center": "Medicine",
            "Graduate Student, Clinical Science Doctoral Program": "Clinical Science Doctoral Program",
            "Research assistant": None,
        }
        for role, want in cases.items():
            self.assertEqual(department_of(role), want, role)

    def test_grouping_and_rosters(self):
        nb = notebook()
        for c in nb["contacts"]:
            if c["id"] == "pi":
                c["role"] = "Assistant Professor of Psychology"
            if c["id"] in ("stu", "pd"):
                c["role"] = "Graduate Student, Clinical Science Doctoral Program"
        nb["edges"].append(edge("e-pi-stu-roster", "pi", "stu", "labmate", sourceTitle="Investigator Lab public roster",
                                relationshipStatus="current", relationshipLabel="Lab affiliation"))
        nb["edges"].append(edge("e-pi-pd-roster", "pi", "pd", "labmate", sourceTitle="Investigator Lab public roster",
                                relationshipStatus="current", relationshipLabel="Lab affiliation"))
        nb["edges"].append(edge("e-generic", "c1", "c3", "labmate", sourceTitle="Official professional roster or biography"))
        tmp, svc = workspace(nb)
        try:
            res = svc.affiliations(ANY)
            lakeside = next(i for i in res["institutions"] if i["name"] == "Lakeside University")
            labs = {lab["name"]: (d["name"], lab["count"]) for d in lakeside["departments"] for lab in d["labs"]}
            # The lab stays whole, in its faculty member's department, not its students' program.
            self.assertEqual(labs["Investigator Lab"], ("Psychology", 3))
            self.assertNotIn("Official professional", " ".join(labs))
            self.assertTrue(all(p["degree"] is not None and p["degree"] <= 2 for p in res["people"].values()))
            self.assertGreater(lakeside["within"]["records"], 0)
            northfield = svc.affiliations({**ANY, "inst": "northfield institute"})
            self.assertEqual(sorted(northfield["ids"]), ["m1", "m2"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
