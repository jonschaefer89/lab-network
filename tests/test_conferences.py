"""Conference programs: import, name matching, search (including AND) and profile pages."""
from __future__ import annotations

import io
import os
import shutil
import unittest
from contextlib import redirect_stdout

from labnetwork import cli, conferences
from labnetwork.service import NotFound, Service
from labnetwork.store import Workspace
from tests import fixtures

PROGRAM = """Name,Affiliation,Role,Title,Session
"Mentor, Morgan",Northfield Institute,Chair,Mentoring across the lifespan,Symposium 12
"Pat Investigator, PhD",,Presenter,Sleep loss and attention in twins,Symposium 12
Chris Coauthor,Lakeside University,Presenter,Cohort retention,Poster session A
Chris Coauthor,Lakeside University,Presenter,Cohort retention,Poster session A
Chris Coauthor,Lakeside University,Discussant,Open science in twin registries,Symposium 30
C. Bridge,Lakeside University,Presenter,Polygenic scores for sleep,Poster session B
Nobody Here,Elsewhere College,Presenter,An unrelated talk,Poster session B
"""

EVERYONE = {"maxDegree": "all", "doctorate": "any", "identified": "0"}


class ConferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.ws = fixtures.new_workspace()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.csv = os.path.join(self.tmp, "program.csv")
        with open(self.csv, "w", encoding="utf-8") as f:
            f.write(PROGRAM)
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["conference", self.ws.path, self.csv, "--name", "AACAP 73rd Annual Meeting",
                             "--short", "AACAP", "--year", "2026", "--location", "Atlanta, GA",
                             "--url", "https://example.org/aacap-program"])
        self.assertEqual(code, 0)
        self.assertIn("Added aacap-2026: 6 appearances", out.getvalue())     # the repeated row counts once
        self.svc = Service(Workspace(self.ws.path))

    def test_names_match_the_notebook(self):
        conf = self.svc.conference_list({"id": "aacap-2026"})["conferences"][0]
        by = {(a["name"], a["role"]): (a["person"], a["basis"]) for a in conf["appearances"]}
        self.assertEqual(by[("Morgan Mentor", "Chair")], ("m1", "name and institution"))   # "Last, First" turned
        self.assertEqual(by[("Pat Investigator", "Presenter")][0], "pi")                     # degree dropped
        self.assertEqual(by[("C. Bridge", "Presenter")], ("c3", "initials and institution"))
        self.assertEqual(by[("Nobody Here", "Presenter")], (None, "none"))
        self.assertEqual(conf["inNetwork"], 4)
        self.assertEqual(conf["url"], "https://example.org/aacap-program")
        with self.assertRaises(NotFound):
            self.svc.conference_list({"id": "srp-1999"})

    def test_search_and_profile(self):
        r = self.svc.search({"q": "AACAP", **EVERYONE})
        self.assertEqual(sorted({e["personId"] for e in r["events"]}), ["c1", "c3", "m1", "pi"])
        self.assertEqual(r["counts"]["events"], 5)
        talk = self.svc.search({"q": "twin registries", **EVERYONE})["events"]
        self.assertEqual([(e["personId"], e["role"]) for e in talk], [("c1", "Discussant")])
        both = self.svc.search({"q": "ADHD AND AACAP", **EVERYONE})
        self.assertEqual(sorted(p["personId"] for p in both["peopleSummary"]), ["c1", "c3", "pi"])   # not Morgan
        mine = self.svc.person("c1", EVERYONE)["conferences"]["appearances"]
        self.assertEqual([(x["conference"], x["role"]) for x in mine], [("AACAP 2026", "Presenter"),     # by title
                                                                        ("AACAP 2026", "Discussant")])
        self.assertEqual(mine[0]["sourceUrl"], "https://example.org/aacap-program")

    def test_replace_and_remove(self):
        with redirect_stdout(io.StringIO()):
            cli.main(["conference", self.ws.path, self.csv, "--name", "AACAP 73rd Annual Meeting", "--short", "AACAP",
                      "--year", "2026"])
        self.assertEqual(len(conferences.load(self.ws)["conferences"]), 1)
        self.assertTrue(conferences.remove(self.ws, "aacap-2026"))
        svc = Service(Workspace(self.ws.path))
        self.assertEqual(svc.search({"q": "AACAP", **EVERYONE})["events"], [])
        self.assertEqual(svc.person("c1", EVERYONE)["conferences"]["appearances"], [])

    def test_the_list_years_and_breakdown(self):
        older = os.path.join(self.tmp, "older.csv")
        with open(older, "w", encoding="utf-8") as f:
            f.write("name,institution,role,title\nChris Coauthor,Lakeside University,Presenter,Twins at school\n"
                    "Nobody Here,Elsewhere College,Chair,Another session\nNobody Here,Elsewhere College,Presenter,A talk\n")
        with redirect_stdout(io.StringIO()):
            cli.main(["conference", self.ws.path, older, "--name", "AACAP 70th Annual Meeting", "--short", "AACAP",
                      "--year", "2023"])
        svc = Service(Workspace(self.ws.path))
        listed = svc.conference_list({})["series"]
        self.assertEqual([(x["id"], [p["year"] for p in x["programs"]], x["inNetwork"]) for x in listed],
                         [("aacap", [2026, 2023], 4)])
        b = svc.conference_list({"series": "aacap", **EVERYONE})
        self.assertEqual(b["years"], [2023, 2026])
        chris = next(p for p in b["people"] if p["personId"] == "c1")
        self.assertEqual((chris["years"], chris["appearances"], chris["degree"]), ([2023, 2026], 3, chris["degree"]))
        self.assertEqual(b["people"][0]["personId"], "c1")                     # most years first
        self.assertEqual([(o["name"], o["years"]) for o in b["outside"]], [("Nobody Here", [2023, 2026])])
        summary = svc.person("c1", EVERYONE)["conferences"]["summary"]
        self.assertEqual([(x["short"], x["years"], x["imported"]) for x in summary], [("AACAP", [2023, 2026], [2023, 2026])])
        # Off the list: hidden everywhere, programs kept; back on the list: restored.
        self.assertEqual(svc.conference_series({}, "Pat", remove="aacap"), {"removed": "aacap", "programsKept": 2})
        self.assertEqual(svc.conference_list({})["series"], [])
        self.assertEqual(svc.conference_list({})["offList"], 2)
        self.assertEqual(svc.search({"q": "AACAP", **EVERYONE})["events"], [])
        svc.conference_series({"short": "AACAP", "name": "AACAP Annual Meeting", "url": "https://example.org/aacap"}, "Pat")
        self.assertEqual(len(svc.search({"q": "AACAP", **EVERYONE})["events"]), 6)
        with self.assertRaises(Exception):
            svc.conference_series({"short": "SRP", "url": "not a url"}, "Pat")


if __name__ == "__main__":
    unittest.main()
