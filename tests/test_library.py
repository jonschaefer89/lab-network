"""The research library against fake OpenAlex, NIH RePORTER and NSF services (no network)."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import plistlib
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
import urllib.error
from urllib.parse import parse_qs, urlsplit

from labnetwork import library, schedule
from labnetwork.connectors.http import Client
from labnetwork.service import Service
from labnetwork.store import Workspace
from tests.fixtures import notebook, write_packet

LU, NI = "Lakeside University", "Northfield Institute"
TODAY = dt.date.today().isoformat()          # updates count back from the real date, so new items use it too


def author(aid, name, inst, works=40, country="US", orcid=""):
    return {"id": f"https://openalex.org/{aid}", "display_name": name, "orcid": orcid and f"https://orcid.org/{orcid}",
            "works_count": works,
            "cited_by_count": 900, "summary_stats": {"h_index": 12, "i10_index": 20},
            "counts_by_year": [{"year": y, "works_count": 3} for y in range(2014, 2027)],
            "topics": [{"display_name": "Sleep and attention", "count": 9}, {"display_name": "Twin studies", "count": 6}],
            "last_known_institutions": [{"display_name": inst, "country_code": country}],
            "affiliations": [{"institution": {"display_name": inst, "country_code": country}, "years": [2024]}]}


AUTHORS = {a["id"].rsplit("/", 1)[1]: a for a in [
    author("A1", "Pat Investigator", LU), author("A3", "Chris Coauthor", LU), author("A5", "Morgan Mentor", NI),
    author("A6", "Morgan Mentor", "Far Away University", country="GB")]}


def work(wid, date, title, authors, n_extra=0):
    auths = [{"author": {"id": f"https://openalex.org/{a}", "display_name": AUTHORS[a]["display_name"],
                         "orcid": AUTHORS[a]["orcid"] or None},
              "institutions": [{"display_name": LU}]} for a in authors]
    auths += [{"author": {"id": f"https://openalex.org/X{i}", "display_name": f"Consortium Member {i}"},
               "institutions": []} for i in range(n_extra)]
    return {"id": f"https://openalex.org/{wid}", "doi": f"https://doi.org/10.1/{wid.lower()}", "ids": {},
            "title": title, "publication_year": int(date[:4]), "publication_date": date, "authorships": auths}


class Fake:
    def __init__(self, remaining="0.09"):
        self.works = [
            work("W1", "2024-05-01", "Sleep timing and attention in adolescent twins", ["A1", "A3"]),
            work("W2", "2023-03-01", "A cohort study of sleep", ["A1"]),
            work("W3", "2022-01-01", "Huge consortium paper on sleep", ["A1"], n_extra=40),
            work("W4", "2010-01-01", "An old paper", ["A1"]),
            work("W5", "2021-06-01", "Mentor paper on development", ["A5"]),
        ]
        self.projects = [self.project("R01XX000001", 111, "Pat Investigator", "LAKESIDE UNIVERSITY", "Twin sleep cohort"),
                         self.project("R01XX000002", 222, "Pat Investigator", "OTHER UNIVERSITY", "Not our Pat"),
                         self.project("R21XX000003", 333, "Morgan Mentor", "NORTHFIELD INSTITUTE", "Mentor grant"),
                         self.project("R21XX000004", 444, "Morgan Mentor", "NORTHFIELD INSTITUTE OF ART", "Other Morgan")]
        self.nsf = [{"id": "100", "title": "Sleep in adolescence", "piFirstName": "Pat", "piLastName": "Investigator",
                     "awardeeName": LU, "startDate": "07/01/2020", "expDate": "06/30/2024", "fundsObligatedAmt": "300000"},
                    {"id": "101", "title": "Somewhere else", "piFirstName": "Pat", "piLastName": "Investigator",
                     "awardeeName": "Elsewhere College", "startDate": "07/01/2019", "expDate": "06/30/2021"}]
        self.remaining = remaining
        self.fail_search = False
        self.calls = []

    @staticmethod
    def project(core, pid, name, org, title, added="2026-01-05T00:00:00", kind=None):
        # The first digit of a project number is the application type: 1 opens an award, 5 is a later year.
        kind = kind or ("1" if added[:10] == TODAY else "5")
        return {"appl_id": int(core[-3:]), "project_num": kind + core + ("-01" if kind == "1" else "-02"),
                "core_project_num": core, "project_title": title,
                "fiscal_year": 2026, "organization": {"org_name": org}, "agency_ic_admin": {"abbreviation": "NIMH"},
                "activity_code": core[:3], "project_start_date": "2024-01-01T00:00:00", "project_end_date": "2028-12-31T00:00:00",
                "date_added": added, "principal_investigators": [{"profile_id": pid, "full_name": name, "is_contact_pi": True}]}

    def __call__(self, method, url, body, headers):
        self.calls.append(url)
        u = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        hdrs = {"x-ratelimit-remaining-usd": self.remaining}
        if u.netloc == "api.openalex.org" and u.path == "/authors" and "search" in q and self.fail_search:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b""))
        if u.netloc == "api.openalex.org" and u.path == "/authors" and "search" in q:
            return {"results": [a for a in AUTHORS.values() if a["display_name"] == q["search"]], "meta": {"cost_usd": 0.001}}, hdrs
        if u.netloc == "api.openalex.org" and u.path == "/authors":
            key, values = q["filter"].split(":", 1)
            if key == "orcid":                  # every record that carries one of these ORCID iDs
                orcids = {v.upper() for v in values.split("|")}
                res = [a for a in AUTHORS.values() if a["orcid"] and a["orcid"].rsplit("/", 1)[1].upper() in orcids]
            else:
                res = [AUTHORS[i] for i in values.split("|") if i in AUTHORS]
            return {"results": res, "meta": {"cost_usd": 0.0001, "count": len(res)}}, hdrs
        if u.netloc == "api.openalex.org" and u.path == "/works":
            parts = dict(p.split(":", 1) for p in q["filter"].split(","))
            # The library filters on authorships.author.id, a refresh on author.id; several records join with |.
            ids = set((parts.get("authorships.author.id") or parts.get("author.id")).split("|"))
            since = parts.get("from_publication_date", "1900-01-01")
            limit = int(parts["authors_count"].lstrip("<")) if "authors_count" in parts else 10 ** 6
            out = [w for w in self.works if w["publication_date"] >= since and len(w["authorships"]) < limit and
                   ids & {a["author"]["id"].rsplit("/", 1)[1] for a in w["authorships"]}]
            out.sort(key=lambda w: w["publication_date"], reverse=True)
            if q.get("select"):                 # like OpenAlex, return only the fields asked for
                keep = set(q["select"].split(","))
                out = [{k: v for k, v in w.items() if k in keep} for w in out]
            return {"results": out, "meta": {"cost_usd": 0.0001, "next_cursor": None}}, hdrs
        if u.netloc == "api.reporter.nih.gov":
            crit = json.loads(body.decode())["criteria"]
            res = self.projects
            if "pi_profile_ids" in crit:
                res = [r for r in res if r["principal_investigators"][0]["profile_id"] in crit["pi_profile_ids"]]
            if "pi_names" in crit:
                last = {n["last_name"].lower() for n in crit["pi_names"]}
                res = [r for r in res if r["principal_investigators"][0]["full_name"].split()[-1].lower() in last]
            if "date_added" in crit:
                res = [r for r in res if r["date_added"][:10] >= crit["date_added"]["from_date"]]
            return {"results": res, "meta": {"total": len(res)}}
        if u.netloc == "api.nsf.gov":
            first, last = q["pdPIName"].split()[0], q["pdPIName"].split()[-1]
            return {"response": {"award": [a for a in self.nsf if a["piLastName"] == last and a["piFirstName"] == first]}}
        raise AssertionError(f"unexpected request {url}")


class LibraryTests(unittest.TestCase):
    def setUp(self):
        nb = notebook()
        for c in nb["contacts"]:
            if c["id"] == "pi":
                c["sources"] = [{"url": "https://openalex.org/A1", "note": "OpenAlex author record"}]
        self.tmp = tempfile.mkdtemp(prefix="labnetwork-test-")
        ws = Workspace.create(os.path.join(self.tmp, "ws"), write_packet(self.tmp, nb), lab_name="Fixture Lab")
        self.ws_path = ws.path
        self.fake = Fake()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_lib(self, **kw):
        return library.run(self.ws_path, client=Client(transport=self.fake), log=lambda m: None, **kw)

    def test_build_identifies_fetches_and_matches_awards(self):
        self.run_lib()
        lib = library.load(Workspace(self.ws_path))
        pi, c1, m1 = (lib["people"][p] for p in ("pi", "c1", "m1"))
        self.assertEqual(pi["openalex"]["status"], "linked")
        self.assertEqual(pi["openalex"]["summary"]["worksCount"], 40)
        # Since 2016 and 30 or fewer authors: the consortium and 2010 papers are left out.
        self.assertEqual(pi["openalex"]["works"], ["W1", "W2"])
        # Chris is found on Pat's paper (they have a coauthor record); Morgan by a unique institution match.
        self.assertEqual(c1["openalex"]["id"], "A3")
        self.assertIn("named with Pat Investigator", c1["openalex"]["basis"])
        self.assertEqual(m1["openalex"]["id"], "A5")
        # NIH: only the Pat at an institution on the record; both Morgans sit at "Northfield" places, so held back.
        self.assertEqual(pi["nih"]["profileIds"], [111])
        self.assertEqual(pi["nih"]["awards"], ["nih:R01XX000001"])
        self.assertEqual(m1["nih"]["status"], "ambiguous")
        self.assertEqual(pi["nsf"]["awards"], ["nsf:100"])
        self.assertIn("nih:R01XX000001", lib["awards"])
        self.assertEqual(lib["works"]["W1"]["people"], ["c1", "pi"])

    def test_update_adds_new_items_and_writes_a_digest(self):
        self.run_lib()
        self.fake.works.append(work("W9", TODAY, "New sleep paper", ["A1", "A3"]))
        self.fake.projects.append(Fake.project("R01XX000009", 111, "Pat Investigator", "LAKESIDE UNIVERSITY",
                                               "A new award", added=TODAY + "T00:00:00"))
        res = self.run_lib(update=True)
        lib = library.load(Workspace(self.ws_path))
        self.assertEqual(lib["people"]["pi"]["openalex"]["works"][0], "W9")
        self.assertIn("nih:R01XX000009", lib["people"]["pi"]["nih"]["awards"])
        with open(res["digest"], encoding="utf-8") as f:
            digest = f.read()
        self.assertIn("New sleep paper", digest)
        self.assertIn("R01XX000009", digest)
        again = self.run_lib(update=True)
        with open(again["digest"], encoding="utf-8") as f:
            self.assertNotIn("New sleep paper", f.read())                       # nothing new the second time
        self.assertNotEqual(again["digest"], res["digest"])                     # the first digest is kept
        self.assertTrue(os.path.exists(res["digest"]))

    def test_a_first_read_of_awards_is_not_reported_as_new(self):
        self.run_lib(use_nih=False, use_nsf=False)
        res = self.run_lib(update=True)              # NIH and NSF are read for the first time
        with open(res["digest"][:-3] + ".json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["newAwards"], {})
        self.assertIn("nih:R01XX000001", library.load(Workspace(self.ws_path))["people"]["pi"]["nih"]["awards"])
        self.fake.projects.append(Fake.project("R01XX000009", 111, "Pat Investigator", "LAKESIDE UNIVERSITY",
                                               "A new award", added=TODAY + "T00:00:00"))
        res = self.run_lib(update=True)
        with open(res["digest"][:-3] + ".json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["newAwards"], {"pi": ["nih:R01XX000009"]})

    def test_service_profile_people_search_and_live_check(self):
        self.run_lib()
        svc = Service(Workspace(self.ws_path))
        view = svc.person("pi", {"maxDegree": "all", "doctorate": "any", "identified": "0"})["library"]
        self.assertEqual([w["id"] for w in view["works"]], ["W1", "W2"])
        self.assertEqual(view["awards"][0]["number"], "R01XX000001")
        rows = {r["id"]: r for r in svc.people({"maxDegree": "all", "doctorate": "any", "identified": "0"})["rows"]}
        self.assertEqual(rows["pi"]["works"], 40)
        r = svc.search({"q": "sleep timing", "maxDegree": "all", "doctorate": "any", "identified": "0"})
        bases = {a["personId"]: a["basis"] for p in r["papers"] for a in p["associations"]}
        self.assertEqual(bases.get("c1"), "openalex")
        g = svc.search({"q": "twin sleep cohort", "maxDegree": "all", "doctorate": "any", "identified": "0"})
        self.assertTrue(any(a["basis"] in ("nih", "profile") and a["personId"] == "pi"
                            for x in g["grants"] for a in x["associations"]))
        # Live check: a paper published since the last update is reported, the stored ones are not.
        self.fake.works.append(work("W10", TODAY, "Brand new", ["A1"]))
        svc._live_client = Client(transport=self.fake)
        live = svc.latest("pi")
        self.assertEqual([w["id"] for w in live["works"]], ["W10"])
        # Each source says whether it was read and when this person was last updated from it.
        lib = library.load(Workspace(self.ws_path))["people"]["pi"]
        self.assertEqual(live["sources"]["openalex"], {"checked": True, "since": lib["openalex"]["worksAt"], "note": ""})
        self.assertEqual(live["sources"]["nih"], {"checked": True, "since": lib["nih"]["checkedAt"], "note": ""})
        self.assertEqual(live["notes"], [])

    def test_look_up_someone_outside_the_network(self):
        self.run_lib()
        svc = Service(Workspace(self.ws_path))
        svc._live_client = Client(transport=self.fake)
        everyone = {"maxDegree": "all", "doctorate": "any", "identified": "0"}
        with mock.patch.dict(AUTHORS, {"A9": author("A9", "Olive Outsider", "Elsewhere College")}):
            self.fake.works += [work("W20", "2025-01-01", "Outsider and Pat", ["A9", "A1"]),
                                work("W21", "2025-02-01", "Outsider and Chris", ["A9", "A3"]),
                                work("W22", "2025-03-01", "Outsider in a consortium", ["A9", "A3"], n_extra=40)]
            found = svc.outside_find({"name": "Olive Outsider"})
            self.assertEqual([(c["openalexId"], c["person"]) for c in found["candidates"]], [("A9", None)])
            self.assertEqual(svc.outside_find({"name": "Pat Investigator"})["candidates"][0]["person"], "pi")
            r = svc.outside_connect("A9", everyone)
            self.assertIsNone(r["person"])
            self.assertEqual({b["personId"]: b["shared"] for b in r["bridges"]}, {"pi": 1, "c1": 1})   # not W22
            self.assertEqual(r["bridges"][0]["personId"], "pi")             # the closest to the lab first
            self.assertEqual(r["degree"], 1)
            self.assertEqual(svc.outside_connect("a1", everyone)["person"], "pi")
        with self.assertRaises(Exception):
            svc.outside_connect("W1", everyone)

    def test_stops_at_the_daily_allowance(self):
        self.fake.remaining = "0.001"          # below the reserve after the first OpenAlex response
        res = self.run_lib()
        self.assertIn("allowance", res["stopped"])
        lib = library.load(Workspace(self.ws_path))
        self.assertEqual(lib["people"]["pi"]["nih"]["awards"], ["nih:R01XX000001"])   # NIH still ran
        self.assertGreater(res["coverage"]["openalexPending"], 0)

    def test_weekly_schedule_plan(self):
        from labnetwork import schedule
        p = schedule.plan(self.ws_path, "week", job="library", day="sat", at="19:30")
        self.assertIn("--update", p["content"] or json.dumps(p["install"]))
        self.assertEqual(p["when"], "Saturdays at 19:30")
        self.assertIn("library", p["label"])

    def test_nsf_skips_only_people_known_to_work_outside_the_us(self):
        def morgan(country):
            a5 = json.loads(json.dumps(AUTHORS["A5"]))
            a5["last_known_institutions"][0]["country_code"] = country
            a5["affiliations"][0]["institution"]["country_code"] = country
            return a5
        with mock.patch.dict(AUTHORS, {"A5": morgan("GB")}):
            self.run_lib()
        self.assertEqual(library.load(Workspace(self.ws_path))["people"]["m1"]["nsf"]["status"], "skipped")
        os.remove(os.path.join(self.ws_path, "library.json"))
        with mock.patch.dict(AUTHORS, {"A5": morgan(None)}):        # OpenAlex lists no country
            self.run_lib()
        self.assertEqual(library.load(Workspace(self.ws_path))["people"]["m1"]["nsf"]["status"], "checked")

    def test_a_later_record_never_shortens_an_award(self):
        a = {"source": "nih", "number": "R01XX000001", "title": "Twin sleep cohort", "start": "2020-07-01",
             "end": "2027-05-31", "years": [2020, 2021, 2022, 2023, 2024], "org": "LAKESIDE UNIVERSITY", "roles": {}}
        later = {"appl_id": 5, "core_project_num": "R01XX000001", "project_title": "Twin sleep cohort", "fiscal_year": 2025,
                 "project_start_date": "", "project_end_date": "2025-05-31T00:00:00",
                 "organization": {"org_name": "LAKESIDE UNIVERSITY"}}
        library.merge_award(a, [later], "pi")
        self.assertEqual(a["years"], [2020, 2021, 2022, 2023, 2024, 2025])
        self.assertEqual((a["start"], a["end"]), ("2020-07-01", "2027-05-31"))     # neither date is lost

    def test_a_center_grant_keeps_its_own_title_and_each_persons_core(self):
        def rec(sub, title, pid):
            return {"appl_id": hash(title) % 1000, "core_project_num": "P50XX000009", "subproject_id": sub,
                    "project_title": title, "fiscal_year": 2025, "project_start_date": "2023-07-01T00:00:00",
                    "project_end_date": "2028-06-30T00:00:00", "organization": {"org_name": "LAKESIDE UNIVERSITY"},
                    "principal_investigators": [{"profile_id": pid}]}
        center = rec(None, "Lakeside Sleep Research Center", 1)
        core_a, core_b = rec("5001", "Pilot Project Core", 2), rec("5002", "Data Core", 3)
        for order in ([("p1", [center]), ("p2", [core_a]), ("p3", [core_b])],
                      [("p3", [core_b]), ("p2", [core_a]), ("p1", [center])]):
            a = {"source": "nih", "number": "P50XX000009", "roles": {}}
            for pid, records in order:
                library.merge_award(a, records, pid)
            self.assertEqual(a["title"], "Lakeside Sleep Research Center")          # whatever the order
            self.assertEqual(a["parts"], {"p2": "Pilot Project Core", "p3": "Data Core"})
        self.assertEqual(library.award_title(a, "p2"), ("Lakeside Sleep Research Center", "Pilot Project Core"))
        a = {"source": "nih", "number": "P50XX000009", "roles": {}}
        library.merge_award(a, [core_a], "p2")
        library.merge_award(a, [core_b], "p3")
        self.assertEqual(a["title"], "Pilot Project Core")       # with no center record, the first core's title stays
        self.assertEqual(library.award_title(a, "p3"), ("Data Core", ""))   # but each person sees their own core

    def test_award_names_need_a_first_name(self):
        self.assertTrue(library.award_name_ok("Matt Okafor", "MATTHEW K OKAFOR"))
        self.assertTrue(library.award_name_ok("Soo Hyun Baek", "SOO H BAEK"))
        self.assertFalse(library.award_name_ok("John Smith", "J SMITH"))

    def test_unmatched_people_are_not_counted_as_still_to_fetch(self):
        with mock.patch.object(library, "SEARCHES_PER_RUN", 0):
            self.run_lib()
        lib = library.load(Workspace(self.ws_path))
        self.assertEqual(lib["people"]["stu"]["openalex"]["status"], "unmatched")   # no doctorate, so no name search
        self.assertEqual(lib["people"]["m2"]["openalex"]["status"], "pending")      # waits for a name search
        res = self.run_lib()
        self.assertEqual(library.load(Workspace(self.ws_path))["people"]["m2"]["openalex"]["status"], "not-found")
        self.assertEqual(res["coverage"]["openalexPending"], 0)
        self.assertGreater(res["coverage"]["openalexUnmatched"], 0)
        self.assertTrue(all(library.load(Workspace(self.ws_path))["people"].values()))   # no empty records

    def test_people_expand_added_wait_for_a_doctorate_and_position(self):
        svc = Service(Workspace(self.ws_path))
        doctorate = {"doctorateStatus": "confirmed", "doctorateType": "PhD", "doctorateEvidence": "ORCID education record",
                     "doctorateSourceUrl": "https://orcid.org/0000-0000-0000-0001", "doctorateCheckedAt": TODAY}

        def added(name, **fields):          # added to the second degree by expand, through Chris
            pid = svc.create_contact({"fields": {"name": name, "tags": ["openalex-expand", "expand-second-degree"],
                                                 **fields}}, "test")["id"]
            svc.create_tie({"fields": {"from": "c1", "to": pid, "type": "coauthor",
                                       "evidence": "Named together on 2 works"}}, "test")
            return pid
        shown = added("Sky Shown", institution=LU, role="Professor", **doctorate)
        no_position = added("Ren Unplaced", institution=LU, **doctorate)
        no_doctorate = added("Lou Unconfirmed", institution=LU, role="Professor")
        kept = added("Kim Kept", institution=LU, role="Professor")
        lib = library.empty()
        lib["people"][kept] = {"openalex": {"status": "unmatched"}}      # already in the library
        svc = Service(Workspace(self.ws_path))
        people = library._people_in_scope(svc, lib)
        self.assertIn(shown, people)
        self.assertIn(kept, people)
        self.assertNotIn(no_position, people)
        self.assertNotIn(no_doctorate, people)
        self.assertIn("c1", people)                                      # everyone else as before
        library.save(Workspace(self.ws_path), lib)
        svc = Service(Workspace(self.ws_path))
        flags = {"maxDegree": "all", "doctorate": "any", "identified": "0"}
        self.assertTrue(svc.person(no_doctorate, flags)["librarySkipped"])     # the profile says why it has no papers
        self.assertFalse(svc.person(shown, flags)["librarySkipped"])
        self.assertFalse(svc.person(kept, flags)["librarySkipped"])
        # The degree is the library's, whatever relationships the view shows.
        self.assertTrue(svc.person(no_doctorate, dict(flags, types="mentor"))["librarySkipped"])

    def test_nih_profile_id_on_the_record_outranks_a_name_match(self):
        nb = notebook()
        for c in nb["contacts"]:
            if c["id"] == "m1":      # both Morgans sit at "Northfield" places, so a name match is held back
                c["sources"] = [{"url": "https://reporter.nih.gov/", "note": "NIH RePORTER PI profile ID 333"}]
        tmp = tempfile.mkdtemp(prefix="labnetwork-test-")
        try:
            ws = Workspace.create(os.path.join(tmp, "ws"), write_packet(tmp, nb), lab_name="Fixture Lab")
            library.run(ws.path, client=Client(transport=Fake()), log=lambda m: None)
            m1 = library.load(Workspace(ws.path))["people"]["m1"]["nih"]
            self.assertEqual((m1["status"], m1["profileIds"], m1["awards"]), ("matched", [333], ["nih:R21XX000003"]))
            self.assertIn("on their record", m1["basis"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_one_run_at_a_time(self):
        held = library._take_lock(Workspace(self.ws_path))          # another run holds the lock
        try:
            with self.assertRaises(library.LibraryError):
                self.run_lib()
        finally:
            library._release_lock(held)
        with open(os.path.join(self.ws_path, "library.lock"), "w") as f:
            f.write('{"pid": 1, "startedAt": "2026-01-01T00:00:00Z"}')   # left by a run that was killed
        self.run_lib()                                                  # a file nobody holds does not block

    def test_start_year_and_demo_lab(self):
        ws = Workspace(self.ws_path)
        ws.config["settings"]["demo"] = True
        ws.save_config()
        with self.assertRaises(library.LibraryError):
            self.run_lib()
        self.assertFalse(os.path.exists(os.path.join(self.ws_path, "library.json")))
        ws.config["settings"].pop("demo")
        ws.save_config()
        self.run_lib(since_year=2024)
        self.run_lib(since_year=2016)                   # a library keeps the year it was built with
        lib = library.load(Workspace(self.ws_path))
        self.assertEqual(lib["standard"]["sinceYear"], 2024)
        self.assertEqual(lib["people"]["pi"]["openalex"]["works"], ["W1"])

    def test_live_check_can_be_turned_off(self):
        from labnetwork import cli
        self.run_lib()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["config", self.ws_path, "--live-check", "off"]), 0)
        self.assertIn("Live check on profile pages: off", out.getvalue())
        svc = Service(Workspace(self.ws_path))

        def no_network(*args):
            raise AssertionError("the live check reached the network")
        svc._live_client = Client(transport=no_network)
        live = svc.latest("pi")
        self.assertTrue(live["skipped"])
        self.assertIn("off", live["notes"][0])
        self.assertFalse(svc.digests()["library"]["liveCheck"])

    def test_run_limit_follows_the_key(self):
        # Without a key the free $0.10 a day is shared by the network address; a key has its own $1 a day.
        with mock.patch.object(library.oa, "api_key", return_value=""):
            self.assertEqual(library.default_budget(), 0.08)
        with mock.patch.object(library.oa, "api_key", return_value="a-key"):
            self.assertEqual(library.default_budget(), 0.90)
        self.assertLessEqual(library.SEARCHES_PER_RUN * 0.001, 0.08 / 2)      # searches leave room for paper lists
        self.assertLessEqual(library.SEARCHES_PER_RUN_KEY * 0.001, 0.90 / 2)

    def test_an_api_key_never_reaches_files_or_messages(self):
        from labnetwork.connectors.http import HttpError
        secret = "k3y-that-must-not-leak"
        calls = []

        def transport(method, url, body, headers):
            calls.append(url)
            if "fail" in url:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return {"results": [], "meta": {"cost_usd": 0.0001}}, {}
        cache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cache, True)
        client = Client(cache_dir=cache, transport=transport)
        for _ in range(2):                      # the second answer comes from the cache
            client.get("https://api.openalex.org/works", {"filter": "x", "api_key": secret})
        self.assertIn(secret, calls[0])         # the key is sent ...
        with self.assertRaises(HttpError) as err:
            client.get("https://api.openalex.org/fail", {"api_key": secret}, use_cache=False)
        # ... but kept out of error messages, the record of sources and the cache files.
        self.assertNotIn(secret, str(err.exception) + err.exception.url)
        self.assertNotIn(secret, json.dumps(client.sources))
        for name in os.listdir(cache):
            with open(os.path.join(cache, name), encoding="utf-8") as f:
                self.assertNotIn(secret, f.read())

    def test_schedule_times(self):
        self.assertEqual(schedule.parse_time("7:05"), (7, 5))
        for bad in ("7pm", "24:00", "08:60", ""):
            with self.assertRaises(ValueError):
                schedule.parse_time(bad)

    def test_windows_task_runs_a_short_launcher(self):
        long_ws = "C:\\Users\\someone\\Documents\\Research Projects\\Network Maps\\Lab Network\\labs\\a-lab-with-a-long-name\\workspace"
        p = schedule.plan(long_ws, "week", job="library", day="sat", at="19:30", system="Windows")
        tr = p["install"][0][p["install"][0].index("/TR") + 1]
        self.assertLess(len(tr), 262)                         # Task Scheduler's limit for the command
        self.assertTrue(p["file"].endswith(".cmd"))
        self.assertIn("library", p["content"])
        self.assertIn("--update", p["content"])
        self.assertIn(">>", p["content"])                     # output goes to the log
        self.assertEqual(p["when"], "Saturdays at 19:30")

    @unittest.skipUnless(sys.platform == "darwin", "LaunchAgents are macOS only")
    def test_schedule_status_reads_the_launch_agent(self):
        home = tempfile.mkdtemp(prefix="labnetwork-home-")
        try:
            with mock.patch.dict(os.environ, {"HOME": home}):
                p = schedule.plan(self.ws_path, "week", job="library", day="sat", at="19:30")
                self.assertEqual(schedule.status(self.ws_path, "library"), "Not scheduled")
                os.makedirs(os.path.dirname(p["file"]))
                with open(p["file"], "w") as f:
                    f.write(p["content"])
                self.assertEqual(schedule.status(self.ws_path, "library"), "On: Saturdays at 19:30")
        finally:
            shutil.rmtree(home, ignore_errors=True)

    @unittest.skipIf(sys.platform == "darwin" or os.name == "nt", "cron is used on Linux and other Unix systems")
    def test_schedule_status_reads_the_crontab(self):
        p = schedule.plan(self.ws_path, "week", job="library", day="sat", at="19:30")
        with mock.patch.object(schedule.subprocess, "run", return_value=SimpleNamespace(stdout=p["content"] + "\n")):
            self.assertEqual(schedule.status(self.ws_path, "library"), "On: Saturdays at 19:30")

    def test_works_keep_their_publication_dates(self):
        self.run_lib()
        lib = library.load(Workspace(self.ws_path))
        self.assertEqual(lib["works"]["W1"]["date"], "2024-05-01")
        # A list stored without dates (by an earlier version) is read again, and the dates are filled in.
        lib["works"]["W2"]["date"] = ""
        lib["people"]["pi"]["openalex"].pop("datedAt")
        library.save(Workspace(self.ws_path), lib)
        self.run_lib(update=True)
        self.assertEqual(library.load(Workspace(self.ws_path))["works"]["W2"]["date"], "2023-03-01")

    def test_nsf_is_asked_by_full_name(self):
        self.run_lib()
        names = [parse_qs(urlsplit(u).query)["pdPIName"][0] for u in self.fake.calls if "api.nsf.gov" in u]
        self.assertIn("Pat Investigator", names)
        self.assertTrue(all(n.split()[0] not in ("True", "False") for n in names))

    def test_an_update_does_not_settle_a_held_back_nih_match(self):
        self.run_lib()
        self.assertEqual(library.load(Workspace(self.ws_path))["people"]["m1"]["nih"]["status"], "ambiguous")
        # Only one of the two Morgans gets a new award; the update must still see both and hold the match back.
        self.fake.projects.append(Fake.project("R01XX000010", 444, "Morgan Mentor", "NORTHFIELD INSTITUTE OF ART",
                                               "A newer award", added=TODAY + "T00:00:00"))
        res = self.run_lib(update=True)
        m1 = library.load(Workspace(self.ws_path))["people"]["m1"]["nih"]
        self.assertEqual(m1["status"], "ambiguous")
        with open(res["digest"][:-3] + ".json", encoding="utf-8") as f:
            self.assertNotIn("m1", json.load(f)["newAwards"])

    def test_a_first_nih_match_in_an_update_reads_the_whole_record(self):
        self.run_lib()
        self.assertEqual(library.load(Workspace(self.ws_path))["people"]["c1"]["nih"]["status"], "none")
        self.fake.projects += [
            Fake.project("R03XX000020", 555, "Chris Coauthor", "LAKESIDE UNIVERSITY", "An earlier award",
                         added="2020-03-01T00:00:00"),
            Fake.project("R01XX000021", 555, "Chris Coauthor", "LAKESIDE UNIVERSITY", "A new award",
                         added=TODAY + "T00:00:00")]
        res = self.run_lib(update=True)
        c1 = library.load(Workspace(self.ws_path))["people"]["c1"]["nih"]
        self.assertEqual((c1["status"], c1["profileIds"]), ("matched", [555]))
        self.assertEqual(sorted(c1["awards"]), ["nih:R01XX000021", "nih:R03XX000020"])   # the earlier award too
        with open(res["digest"][:-3] + ".json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["newAwards"].get("c1"), ["nih:R01XX000021"])    # only the new one

    def test_updates_ask_nih_afresh_even_with_a_response_cache(self):
        cache = os.path.join(self.tmp, "cache")
        run = lambda **kw: library.run(self.ws_path, client=Client(cache_dir=cache, transport=self.fake),
                                       log=lambda m: None, **kw)
        run()
        self.fake.projects.append(Fake.project("R01XX000030", 444, "Morgan Mentor", "NORTHFIELD INSTITUTE OF ART",
                                               "A newer award", added=TODAY + "T00:00:00"))
        run(update=True)                                   # still held back: both Morgans are at Northfield places
        # The other Morgan moves away; the next update must decide on current records, not the cached ones.
        for r in self.fake.projects:
            if r["principal_investigators"][0]["profile_id"] == 444:
                r["organization"]["org_name"] = "SOMEWHERE ELSE ENTIRELY"
        self.fake.projects.append(Fake.project("R21XX000031", 333, "Morgan Mentor", "NORTHFIELD INSTITUTE",
                                               "Another award", added=TODAY + "T00:00:00"))
        run(update=True)
        m1 = library.load(Workspace(self.ws_path))["people"]["m1"]["nih"]
        self.assertEqual((m1["status"], m1.get("profileIds")), ("matched", [333]))
        self.assertIn("nih:R21XX000031", m1["awards"])

    def test_an_old_award_with_a_recent_record_is_not_new(self):
        self.run_lib()
        # A later year of Pat's known award, and a transfer record of an older award not seen before.
        self.fake.projects += [
            Fake.project("R01XX000001", 111, "Pat Investigator", "LAKESIDE UNIVERSITY", "Twin sleep cohort",
                         added=TODAY + "T00:00:00", kind="5"),
            Fake.project("R01XX000040", 111, "Pat Investigator", "LAKESIDE UNIVERSITY", "An older award, moved",
                         added=TODAY + "T00:00:00", kind="7")]
        res = self.run_lib(update=True)
        lib = library.load(Workspace(self.ws_path))
        self.assertIn("nih:R01XX000040", lib["people"]["pi"]["nih"]["awards"])          # stored
        with open(res["digest"][:-3] + ".json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["newAwards"], {})                              # but not news

    def test_new_awards_without_a_type_1_number_are_reported(self):
        self.run_lib()
        # An R00 activation (application type 4) begins this year under a new number.
        r00 = Fake.project("R00XX000050", 111, "Pat Investigator", "LAKESIDE UNIVERSITY", "Independent phase",
                           added=TODAY + "T00:00:00", kind="4")
        r00["project_start_date"] = TODAY + "T00:00:00"
        self.fake.projects.append(r00)
        res = self.run_lib(update=True)
        with open(res["digest"][:-3] + ".json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["newAwards"], {"pi": ["nih:R00XX000050"]})

    def test_new_means_since_the_persons_own_last_check(self):
        self.run_lib()
        lib = library.load(Workspace(self.ws_path))
        month_ago = (dt.date.today() - dt.timedelta(days=30)).isoformat()
        lib["people"]["c1"]["nih"]["checkedAt"] = month_ago          # the last run stopped before reaching Chris
        library.save(Workspace(self.ws_path), lib)
        added = (dt.date.today() - dt.timedelta(days=20)).isoformat()
        self.fake.projects.append(Fake.project("R01XX000060", 555, "Chris Coauthor", "LAKESIDE UNIVERSITY",
                                               "An award from three weeks ago", added=added + "T00:00:00", kind="1"))
        res = self.run_lib(update=True)
        with open(res["digest"][:-3] + ".json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["newAwards"].get("c1"), ["nih:R01XX000060"])

    def test_lists_read_before_dates_were_kept_are_read_again_once(self):
        cache = os.path.join(self.tmp, "cache")
        run = lambda **kw: library.run(self.ws_path, client=Client(cache_dir=cache, transport=self.fake),
                                       log=lambda m: None, **kw)
        self.run_lib()                                      # built without a cache, as an earlier version did
        lib = library.load(Workspace(self.ws_path))        # and stored it: no dates
        for w in lib["works"].values():
            w["date"] = ""
        for r in lib["people"].values():
            (r.get("openalex") or {}).pop("datedAt", None)
        library.save(Workspace(self.ws_path), lib)
        self.fake.works = [w for w in self.fake.works if w["id"] != "https://openalex.org/W2"]   # OpenAlex drops W2
        run(update=True)
        self.fake.works.append(work("W11", TODAY, "Published this week", ["A1"]))
        run(update=True)
        pi = library.load(Workspace(self.ws_path))["people"]["pi"]["openalex"]
        self.assertEqual(pi["works"][0], "W11")             # later updates still find new papers
        self.assertIn("W2", pi["works"])                     # the dropped paper stays, without a date

    @unittest.skipIf(os.name == "nt", "the Windows lock is tested on Windows")
    def test_a_drive_that_cannot_lock_files_does_not_block_runs(self):
        import errno
        import fcntl
        with mock.patch.object(fcntl, "flock", side_effect=OSError(errno.ENOLCK, "No locks available")):
            self.run_lib()
        self.assertTrue(os.path.exists(os.path.join(self.ws_path, "library.json")))

    def test_windows_commands_quote_shell_characters(self):
        from labnetwork.service import _win_quote
        self.assertEqual(_win_quote("C:\\plain\\path"), "C:\\plain\\path")
        self.assertEqual(_win_quote("C:\\Users\\x\\R&D\\ws"), '"C:\\Users\\x\\R&D\\ws"')
        self.assertEqual(_win_quote("C:\\My Lab (2026)"), '"C:\\My Lab (2026)"')
        self.assertEqual(_win_quote('say "hi"'), '"say ""hi"""')

    def test_an_openalex_error_stops_openalex_but_keeps_the_run(self):
        self.fake.fail_search = True                      # the name search step gets a 403
        res = self.run_lib()
        self.assertIn("could not be reached", res["stopped"])
        lib = library.load(Workspace(self.ws_path))
        self.assertEqual(lib["people"]["pi"]["nih"]["awards"], ["nih:R01XX000001"])   # NIH and NSF still ran
        self.assertEqual(lib["people"]["pi"]["openalex"]["works"], ["W1", "W2"])

    def test_same_day_digests_are_listed_newest_first(self):
        self.run_lib()
        first = self.run_lib(update=True)["digest"]
        second = self.run_lib(update=True)["digest"]
        listed = [d for d in Service(Workspace(self.ws_path)).digests()["digests"] if d["kind"] == "library"]
        self.assertEqual(len(listed), 2)
        self.assertGreaterEqual(listed[0]["createdAt"], listed[1]["createdAt"])
        self.assertNotEqual(first, second)

    def test_institution_matching(self):
        self.assertTrue(library.org_match("Ashdown University", "ASHDOWN UNIVERSITY MEDICAL CENTER"))
        self.assertFalse(library.org_match("University of Minnesota", "University of Colorado"))


CHRIS, MIRA = "0000-0002-0000-0003", "0000-0002-0000-000X"
PAT, OTHER = "0000-0002-0000-0001", "0000-0002-0000-0009"


class SplitAuthorTests(unittest.TestCase):
    """One researcher whom OpenAlex splits across several author records."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="labnetwork-test-")
        self.fake = Fake()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def workspace(self, **links):
        """A fixture workspace whose records link these OpenAlex author records (Pat's to A1 unless given)."""
        links.setdefault("pi", ("A1",))
        nb = notebook()
        for c in nb["contacts"]:
            if c["id"] in links:
                c["sources"] = [{"url": f"https://openalex.org/{a}", "note": "OpenAlex author record"}
                                for a in links[c["id"]]]
        self.ws_path = Workspace.create(os.path.join(self.tmp, "ws"), write_packet(self.tmp, nb),
                                       lab_name="Fixture Lab").path

    def run_lib(self, **kw):
        return library.run(self.ws_path, client=Client(transport=self.fake), log=lambda m: None, **kw)

    def people(self):
        return library.load(Workspace(self.ws_path))["people"]

    def test_candidates_that_share_an_orcid_are_one_author(self):
        records = {"A3": author("A3", "Chris Coauthor", LU, orcid=CHRIS),             # Chris and a small fragment
                   "A4": author("A4", "Chris Coauthor", LU, works=3, orcid=CHRIS),
                   "A7": author("A7", "Mira Mentor", NI, works=30, orcid=MIRA),        # Mira and a small fragment
                   "A8": author("A8", "Mira Mentor", NI, works=2, orcid=MIRA.lower())}
        with mock.patch.dict(AUTHORS, records):
            self.fake = Fake()
            self.fake.works += [work("W20", "2020-02-01", "Chris under a second record", ["A1", "A4"]),
                                work("W21", "2025-03-01", "Under Mira's main record", ["A7"]),
                                work("W22", "2026-01-15", "Under Mira's fragment", ["A8"])]
            self.workspace()
            self.run_lib()
        p = self.people()
        # On Pat's papers, under two records with one ORCID iD: one author, so both records are Chris's.
        chris = p["c1"]["openalex"]
        self.assertEqual((chris["status"], chris["id"], chris["ids"]), ("linked", "A3", ["A3", "A4"]))
        self.assertEqual(chris["basis"], f"named with Pat Investigator on 2 recent OpenAlex papers "
                                         f"(2 OpenAlex records with ORCID {CHRIS})")
        self.assertEqual(chris["works"], ["W1", "W20"])
        self.assertEqual(chris["summary"]["worksCount"], 43)                       # 40 + 3
        # The name search finds two Mira Mentors at Northfield with one ORCID iD: the only author there, linked.
        mira = p["m2"]["openalex"]
        self.assertEqual((mira["status"], mira["ids"]), ("linked", ["A7", "A8"]))   # the larger record first
        self.assertEqual(mira["basis"], "the only OpenAlex author with this name at an institution on their record "
                                        f"(2 OpenAlex records with ORCID {MIRA})")
        self.assertEqual(mira["orcidGroup"], {"orcid": MIRA, "ids": ["A7", "A8"]})
        self.assertEqual(mira["works"], ["W22", "W21"])
        self.assertEqual((mira["summary"]["worksCount"], mira["summary"]["citedBy"]), (32, 1800))

    def test_candidates_with_different_or_no_orcids_stay_ambiguous(self):
        records = {"A3": author("A3", "Chris Coauthor", LU, orcid=CHRIS),
                   "A4": author("A4", "Chris Coauthor", LU, works=3, orcid="0000-0002-0000-0004"),   # another ORCID iD
                   "A7": author("A7", "Mira Mentor", NI, works=30, orcid=MIRA),
                   "A8": author("A8", "Mira Mentor", NI, works=2)}                                   # none
        with mock.patch.dict(AUTHORS, records):
            self.fake = Fake()
            self.fake.works.append(work("W20", "2020-02-01", "Chris under a second record", ["A1", "A4"]))
            self.workspace()
            self.run_lib()
        p = self.people()
        for pid, cands in (("c1", ["A3", "A4"]), ("m2", ["A7", "A8"])):
            o = p[pid]["openalex"]
            self.assertEqual((o["status"], o["candidates"]), ("ambiguous", cands))
            self.assertNotIn("ids", o)
            self.assertNotIn(pid, library.load(Workspace(self.ws_path))["works"].get("W20", {}).get("people", []))

    def test_a_record_already_linked_keeps_its_orcid_from_anyone_else(self):
        records = {"A5": author("A5", "Morgan Mentor", NI, orcid=MIRA),
                   "A7": author("A7", "Mira Mentor", NI, works=2, orcid=MIRA),     # under Morgan's ORCID iD
                   "A17": author("A17", "Morgan Mentor", NI, works=3, orcid=MIRA)}  # Morgan's, on no paper with Pat
        with mock.patch.dict(AUTHORS, records):
            self.fake = Fake()
            self.fake.works.append(work("W23", "2024-09-01", "Pat and Morgan", ["A1", "A5"]))
            self.workspace()
            self.run_lib()
            p = self.people()
            # Morgan is found on Pat's paper in this run, after the ORCID lookup; Mira's name search skips the record
            # with Morgan's ORCID iD.
            self.assertEqual(p["m1"]["openalex"]["ids"], ["A5"])
            self.assertEqual(p["m2"]["openalex"]["status"], "not-found")          # not linked to Morgan's fragment
            self.run_lib(update=True)                 # the next update looks up Morgan's ORCID iD
        p = self.people()
        m1 = p["m1"]["openalex"]
        # Morgan's other record joins; the one with Mira's name is held back for a person to decide.
        self.assertEqual((m1["ids"], m1["orcidGroup"]), (["A5", "A17"], {"orcid": MIRA, "ids": ["A5", "A17"]}))
        self.assertEqual(m1["basis"], f"named with Pat Investigator on 1 recent OpenAlex paper "
                                      f"(2 OpenAlex records with ORCID {MIRA})")
        self.assertEqual(m1["orcidHeld"], [{"id": "A7", "name": "Mira Mentor", "reason": "another name"}])
        self.assertEqual(p["m2"]["openalex"]["status"], "not-found")

    def orcid_requests(self, since=0):
        """The ORCID iDs asked for in each OpenAlex lookup by ORCID iD, after the first `since` requests."""
        return [parse_qs(urlsplit(u).query)["filter"][0].split(":", 1)[1].split("|")
                for u in self.fake.calls[since:] if "filter=orcid:" in u]

    def test_a_person_linked_on_their_record_gains_the_records_with_their_orcid(self):
        records = {"A1": author("A1", "Pat Investigator", LU, orcid=PAT),
                   "A2": author("A2", "Pat Investigator", LU, works=5, orcid=PAT)}    # a record OpenAlex split off
        with mock.patch.dict(AUTHORS, records):
            self.fake = Fake()
            self.fake.works.append(work("W30", "2026-02-01", "Under Pat's second record", ["A2"]))
            self.workspace()
            self.run_lib()
            pi = self.people()["pi"]["openalex"]
            self.assertEqual((pi["ids"], pi["basis"]), (["A1", "A2"], "OpenAlex link on their record, and 1 more "
                                                                      f"record with the same ORCID ({PAT})"))
            self.assertEqual(pi["orcidGroup"], {"orcid": PAT, "ids": ["A1", "A2"]})
            self.assertEqual(pi["works"], ["W30", "W1", "W2"])
            self.assertEqual((pi["summary"]["worksCount"], pi["summary"]["citedBy"]), (45, 1800))     # 40 + 5
            self.assertEqual(pi["orcidCheckedAt"], TODAY)
            # The combined summary came from the lookup's answer; A2 was not asked for again.
            self.assertFalse([u for u in self.fake.calls if "filter=openalex:" in u and "A2" in u])
            # A month later OpenAlex has split off another record. The update adds it and reads its papers, and only
            # the recent paper is news.
            lib = library.load(Workspace(self.ws_path))
            lib["people"]["pi"]["openalex"]["orcidCheckedAt"] = (dt.date.today() - dt.timedelta(days=31)).isoformat()
            library.save(Workspace(self.ws_path), lib)
            AUTHORS["A12"] = author("A12", "Pat Investigator", LU, works=2, orcid=PAT)
            self.fake.works += [work("W70", "2018-05-01", "An older paper under the newest record", ["A12"]),
                                work("W71", TODAY, "This week, under the newest record", ["A12"])]
            res = self.run_lib(update=True)
            pi = self.people()["pi"]["openalex"]
            self.assertEqual((pi["ids"], pi["summary"]["worksCount"]), (["A1", "A2", "A12"], 47))
            self.assertEqual(pi["basis"], f"OpenAlex link on their record, and 2 more records with the same ORCID ({PAT})")
            self.assertEqual(pi["works"], ["W71", "W30", "W1", "W2", "W70"])
            with open(res["digest"][:-3] + ".json", encoding="utf-8") as f:
                self.assertEqual(json.load(f)["newWorks"], {"pi": ["W71"]})
            # The records stay with Pat's link, and Pat is not looked up again within the month.
            calls = len(self.fake.calls)
            self.run_lib(update=True)
            self.assertEqual(self.people()["pi"]["openalex"]["ids"], ["A1", "A2", "A12"])
            self.assertEqual(self.orcid_requests(calls), [])

    def test_records_joined_by_orcid_leave_when_the_record_links_elsewhere(self):
        records = {"A1": author("A1", "Pat Investigator", LU, orcid=PAT),
                   "A2": author("A2", "Pat Investigator", LU, works=5, orcid=PAT),
                   "A12": author("A12", "Pam Investigator", LU, works=4, orcid=PAT),            # held back
                   "A16": author("A16", "Pat Investigator", "Elsewhere College", works=8),        # no ORCID iD
                   "A18": author("A18", "Pat Investigator", "Elsewhere College", works=6, orcid=OTHER)}
        nb = notebook()
        for c in nb["contacts"]:
            if c["id"] == "pi":
                c["profileSourceUrl"] = "https://openalex.org/A1"       # a link the app can change
        self.ws_path = Workspace.create(os.path.join(self.tmp, "ws"), write_packet(self.tmp, nb),
                                       lab_name="Fixture Lab").path

        def relink(aid):
            Service(Workspace(self.ws_path)).update_contact(
                "pi", {"set": {"profileSourceUrl": f"https://openalex.org/{aid}"}}, "test")
            calls = len(self.fake.calls)
            self.run_lib(update=True)
            return self.people()["pi"]["openalex"], self.orcid_requests(calls)
        with mock.patch.dict(AUTHORS, records):
            self.fake = Fake()
            self.run_lib()
            pi = self.people()["pi"]["openalex"]
            self.assertEqual((pi["ids"], [h["id"] for h in pi["orcidHeld"]]), (["A1", "A2"], ["A12"]))
            # Pat's record now links a record without an ORCID iD. The record that joined A1 for its ORCID iD leaves
            # with A1, and so does the record held back from it.
            pi, asked = relink("A16")
            self.assertEqual((pi["ids"], pi["basis"], asked), (["A16"], "OpenAlex link on their record", []))
            self.assertFalse({"orcidGroup", "orcidHeld"} & set(pi))
            self.assertNotIn("W1", pi["works"])
            # A newly linked record's ORCID iD is looked up in the same run.
            pi, asked = relink("A18")
        self.assertEqual((pi["ids"], asked), (["A18"], [[OTHER]]))

    def test_a_record_linked_to_someone_else_stays_theirs(self):
        records = {"A1": author("A1", "Pat Investigator", LU, orcid=PAT),
                   "A2": author("A2", "Pat Investigator", LU, works=5, orcid=PAT),
                   "A12": author("A12", "Pat Investigator", LU, works=2, orcid=PAT)}
        lines = []
        with mock.patch.dict(AUTHORS, records):
            self.fake = Fake()
            self.workspace(c3=("A2",))                  # Pat's second record, linked on Cory's record by mistake
            library.run(self.ws_path, client=Client(transport=self.fake), log=lines.append)
        p = self.people()
        self.assertEqual((p["pi"]["openalex"]["ids"], p["c3"]["openalex"]["ids"]), (["A1"], ["A2"]))
        # Two people hold records with Pat's ORCID iD, so neither gains the third record until someone sorts it out.
        self.assertFalse([pid for pid, r in p.items() if "A12" in library.author_ids(r.get("openalex") or {})])
        self.assertIn(f"ORCID {PAT} is on records linked to Cory Bridge and Pat Investigator", "\n".join(lines))

    def test_one_request_looks_up_many_people_and_other_orcids_never_join(self):
        records = {"A1": author("A1", "Pat Investigator", LU, orcid=PAT),
                   "A2": author("A2", "Pat Investigator", LU, works=5, orcid=OTHER),    # Pat's name, Cory's ORCID iD
                   "A9": author("A9", "Pat Investigator", LU, works=4),                 # Pat's name, no ORCID iD
                   "A3": author("A3", "Chris Coauthor", LU, orcid=CHRIS),
                   "A4": author("A4", "Chris Coauthor", LU, works=3, orcid=CHRIS),
                   "A7": author("A7", "Mira Mentor", NI, works=30, orcid=MIRA),
                   "A8": author("A8", "Mira Mentor", NI, works=2, orcid=MIRA),
                   "A16": author("A16", "Cory Bridge", LU, works=12, orcid=OTHER)}
        with mock.patch.dict(AUTHORS, records):
            self.fake = Fake()
            self.workspace(c1=("A3",), m2=("A8",), c3=("A16",))
            self.run_lib()
            self.assertEqual(self.orcid_requests(), [sorted([PAT, CHRIS, MIRA, OTHER])])   # one request for all four
            p = self.people()
            # The answer holds all four ORCID iDs' records; each person gains only those with their own, after the
            # record their own record links to (Mira's link is her smaller record). A2 has Pat's name and institution
            # but Cory's ORCID iD, so it never joins Pat.
            self.assertEqual([p[x]["openalex"]["ids"] for x in ("pi", "c1", "m2", "c3")],
                             [["A1"], ["A3", "A4"], ["A8", "A7"], ["A16"]])
            self.run_lib(update=True)
        self.assertEqual(len(self.orcid_requests()), 1)                                     # not again this month

    def test_a_record_with_another_name_or_no_shared_institution_is_held_back(self):
        merged = author("A19", "Pat Investigator", LU, works=100, orcid=PAT)    # namesakes OpenAlex merged into one
        merged["topics"] = [{"display_name": "Semiconductor materials", "count": 60}]
        records = {"A1": author("A1", "Pat Investigator", LU, orcid=PAT),
                   "A2": author("A2", "P. Investigator", LU, works=5, orcid=PAT),           # an initial: joins
                   "A12": author("A12", "Pam Investigator", LU, works=4, orcid=PAT),        # another person's name
                   "A14": author("A14", "Pat Investigator", "", works=3, orcid=PAT),        # no institution listed: joins
                   "A13": author("A13", "Pat Investigator", "Far Away University", works=2, country="GB", orcid=PAT),
                   "A15": author("A15", "Cory Bridge", LU, works=12, orcid=PAT),            # Cory's, under Pat's iD
                   "A19": merged,
                   "A20": author("A20", "Pat Investigator", LU, works=60, orcid=PAT)}       # larger, on Pat's topics
        lines = []
        with mock.patch.dict(AUTHORS, records):
            self.fake = Fake()
            self.workspace(c3=("A15",))
            library.run(self.ws_path, client=Client(transport=self.fake), log=lines.append)
        p = self.people()
        pi = p["pi"]["openalex"]
        self.assertEqual(pi["ids"], ["A1", "A20", "A2", "A14"])
        self.assertEqual(pi["orcidHeld"], [{"id": "A19", "name": "Pat Investigator",
                                            "reason": "no research topic in common"},
                                           {"id": "A12", "name": "Pam Investigator", "reason": "another name"},
                                           {"id": "A13", "name": "Pat Investigator",
                                            "reason": "no institution in common"}])
        self.assertIn("  Held back from Pat Investigator: A12 (Pam Investigator), another name", lines)
        # Cory's record has another name, so it is no claim on Pat's ORCID iD: it stays Cory's and blocks nothing.
        self.assertEqual(p["c3"]["openalex"]["ids"], ["A15"])
        self.assertFalse([m for m in lines if "is on records linked to" in m])

    def test_names_that_can_be_the_same_person(self):
        for ours, theirs in [("Hana L. Brooks", "H.L. Brooks"), ("Cora M. Vance", "CM Vance"),
                             ("Albert R. Pike", "R Pike Albert"), ("K. Lee Rowan", "Kenneth Lee Rowan"),
                             ("Henry (Rafe) Cole", "H. Rafe Cole"), ("Sari Laine", "S. Elina Laine"),
                             ("Rafael Ortega Medina", "Rafeal Ortega Medina"), ("Yuxi Wen", "Yuix Wen"),
                             ("William S. Thorn", "Wiliam S. Thorn"), ("Darius Okoye", "Arius Okoye"),
                             ("Matt Rivers", "Matthew Rivers"), ("Dana Satterly", "Dana Saterly")]:
            self.assertTrue(library.name_fits(ours, theirs), (ours, theirs))
        for ours, theirs in [("Bruno L. Hart", "Brenda Hart"), ("Rosa E. Lind", "Ruben C. Lind"),
                             ("Scott W. Lane", "Wesley A. Scott"), ("Mark Hale", "Mary Hale"),
                             ("Gregory P. Strand", "S Tobias Gregory"), ("Nicolaas Brandt", "Nick Brandt"),
                             ("Pat Investigator", "Investigator")]:
            self.assertFalse(library.name_fits(ours, theirs), (ours, theirs))

    def test_nothing_joins_when_their_own_record_no_longer_carries_the_orcid(self):
        with mock.patch.dict(AUTHORS, {"A3": author("A3", "Chris Coauthor", LU, orcid=CHRIS)}):
            self.fake = Fake()
            self.workspace()
            self.run_lib()                    # Chris is found on Pat's paper; the summary keeps A3's ORCID iD
        self.assertEqual(self.people()["c1"]["openalex"]["summary"]["orcid"], CHRIS)
        # OpenAlex has since moved the ORCID iD to another record. A build reuses Chris's summary, but the lookup
        # shows A3 no longer carries the iD, so the other record is not added.
        with mock.patch.dict(AUTHORS, {"A4": author("A4", "Chris Coauthor", LU, works=3, orcid=CHRIS)}):
            self.run_lib()
        self.assertEqual(self.orcid_requests(), [[CHRIS]])
        c1 = self.people()["c1"]["openalex"]
        self.assertEqual((c1["ids"], c1["orcidCheckedAt"]), (["A3"], TODAY))

    def test_orcid_lookups_ask_for_50_at_a_time_and_read_every_page(self):
        asked = []

        def transport(method, url, body, headers):
            q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            orcids, page = q["filter"].split(":", 1)[1].split("|"), int(q.get("page", 1))
            asked.append((len(orcids), page))
            total = 250 if orcids[0] == first else 1               # the first 50 iDs have 250 records between them
            return {"results": [{"id": f"https://openalex.org/A{i}"} for i in range((page - 1) * 200, min(page * 200, total))],
                    "meta": {"cost_usd": 0.0001, "count": total}}, {}
        orcids = [f"0000-0002-0001-{i:04d}" for i in range(120)]
        first = orcids[0]
        found = library.oa.authors_by_orcid(Client(transport=transport), orcids + ["https://orcid.org/" + first])
        self.assertEqual(asked, [(50, 1), (50, 2), (50, 1), (20, 1)])
        self.assertEqual(len(found), 252)

    def test_a_person_with_two_record_links_gets_the_papers_of_both_once(self):
        with mock.patch.dict(AUTHORS, {"A2": author("A2", "Pat Investigator", LU, works=5)}):
            self.fake = Fake()
            self.fake.works += [work("W30", "2025-02-01", "Under Pat's second record", ["A2"]),
                                work("W31", "2019-09-01", "A paper that names Pat under both records", ["A1", "A2"])]
            self.workspace(pi=("A1", "A2"))
            self.run_lib()
            pi = self.people()["pi"]["openalex"]
            self.assertEqual((pi["ids"], pi["basis"]), (["A1", "A2"], "2 OpenAlex links on their record"))
            self.assertEqual(pi["works"], ["W30", "W1", "W2", "W31"])         # both records, newest first, each once
            self.assertEqual(pi["summary"]["worksCount"], 45)                  # 40 + 5
            # An update reads both records as well; a new paper that names Pat under both is listed once.
            self.fake.works += [work("W32", TODAY, "New, under the second record", ["A2"]),
                                work("W33", TODAY, "New, under both records", ["A1", "A2"])]
            res = self.run_lib(update=True)
        lib = library.load(Workspace(self.ws_path))
        pi = lib["people"]["pi"]["openalex"]
        self.assertEqual(sorted(pi["works"][:2]), ["W32", "W33"])
        self.assertEqual(len(pi["works"]), len(set(pi["works"])))
        self.assertEqual((lib["works"]["W31"]["people"], lib["works"]["W33"]["people"]), (["pi"], ["pi"]))
        with open(res["digest"][:-3] + ".json", encoding="utf-8") as f:
            self.assertEqual(sorted(json.load(f)["newWorks"]["pi"]), ["W32", "W33"])
        rows = {r["id"]: r for r in Service(Workspace(self.ws_path)).people(
            {"maxDegree": "all", "doctorate": "any", "identified": "0"})["rows"]}
        self.assertEqual(rows["pi"]["works"], 45)

    def test_the_live_check_reads_every_record(self):
        with mock.patch.dict(AUTHORS, {"A2": author("A2", "Pat Investigator", LU, works=5)}):
            self.fake = Fake()
            self.workspace(pi=("A1", "A2"))
            self.run_lib()
            self.fake.works += [work("W40", TODAY, "Brand new, under the second record", ["A2"]),
                                work("W41", TODAY, "Brand new, under both records", ["A1", "A2"])]
            svc = Service(Workspace(self.ws_path))
            svc._live_client = Client(transport=self.fake)
            live = svc.latest("pi")
        self.assertEqual(sorted(w["id"] for w in live["works"]), ["W40", "W41"])          # each once
        asked = parse_qs(urlsplit([u for u in self.fake.calls if "/works" in u][-1]).query)["filter"][0]
        self.assertIn("authorships.author.id:A1|A2,", asked)                              # one request, both records
        view = svc.person("pi", {"maxDegree": "all", "doctorate": "any", "identified": "0"})["library"]["openalex"]
        self.assertEqual((view["id"], view["ids"]), ("A1", ["A1", "A2"]))
        self.assertEqual(view["urls"], ["https://openalex.org/A1", "https://openalex.org/A2"])

    def test_a_record_added_later_refills_and_a_replaced_one_starts_over(self):
        records = {"A2": author("A2", "Pat Investigator", LU, works=5),
                   "A9": author("A9", "Chris Coauthor", LU, works=7)}
        with mock.patch.dict(AUTHORS, records):
            self.fake = Fake()
            self.fake.works += [work("W30", "2019-02-01", "An older paper under Pat's second record", ["A2"]),
                                work("W50", "2022-02-01", "A paper under another Chris Coauthor record", ["A9"])]
            self.workspace()
            self.run_lib()
            p = self.people()
            self.assertEqual((p["pi"]["openalex"]["works"], p["c1"]["openalex"]["ids"]), (["W1", "W2"], ["A3"]))
            svc = Service(Workspace(self.ws_path))
            svc.add_source("pi", {"url": "https://openalex.org/A2", "note": "Pat's second OpenAlex record"}, "test")
            svc.add_source("c1", {"url": "https://openalex.org/A9", "note": "Chris's OpenAlex record"}, "test")
            res = self.run_lib(update=True)
        p = self.people()
        # Pat gained a record: the whole list is read again (an update alone reads only recent papers).
        pi = p["pi"]["openalex"]
        self.assertEqual((pi["ids"], pi["works"], pi["summary"]["worksCount"]), (["A1", "A2"], ["W1", "W2", "W30"], 45))
        self.assertNotIn("refill", pi)
        # Chris's record names a different OpenAlex record: what came from the old one is dropped.
        chris = p["c1"]["openalex"]
        self.assertEqual((chris["ids"], chris["works"]), (["A9"], ["W50"]))
        self.assertEqual(chris["basis"], "OpenAlex link on their record")
        self.assertEqual(chris["summary"]["worksCount"], 7)
        self.assertNotIn("c1", library.load(Workspace(self.ws_path))["works"]["W1"]["people"])
        with open(res["digest"][:-3] + ".json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["newWorks"], {})          # a record's older papers are not news

    def test_a_version_1_library_is_migrated_on_load(self):
        self.workspace()
        self.run_lib()
        path = os.path.join(self.ws_path, "library.json")
        with open(path, encoding="utf-8") as f:
            lib = json.load(f)
        # What version 1 wrote: one record per person, and Mira's name search decided without counting records
        # that share an ORCID iD as one author.
        lib["version"] = 1
        for rec in lib["people"].values():
            (rec.get("openalex") or {}).pop("ids", None)
        lib["people"]["m2"]["openalex"] = {"status": "ambiguous", "checkedAt": TODAY}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(lib, f)
        view = Service(Workspace(self.ws_path)).person("pi", {"maxDegree": "all", "doctorate": "any",
                                                               "identified": "0"})["library"]["openalex"]
        self.assertEqual(view["ids"], ["A1"])                      # the app reads version 1 as it is
        old = library.load(Workspace(self.ws_path))
        self.assertEqual((old["version"], old["people"]["pi"]["openalex"]["ids"]), (library.VERSION, ["A1"]))
        self.assertNotIn("checkedAt", old["people"]["m2"]["openalex"])
        records = {"A7": author("A7", "Mira Mentor", NI, works=30, orcid=MIRA),
                   "A8": author("A8", "Mira Mentor", NI, works=2, orcid=MIRA)}
        with mock.patch.dict(AUTHORS, records):
            self.run_lib()                                         # Mira is searched again, though checked today
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw["people"]["m2"]["openalex"]["ids"], ["A7", "A8"])
        # A version-1 reader still finds each person's main record under `id`.
        for rec in raw["people"].values():
            o = rec.get("openalex") or {}
            if o.get("ids"):
                self.assertEqual(o["id"], o["ids"][0])

    def test_a_combined_summary_adds_counts_and_keeps_the_largest_h_index(self):
        a = library.summarize_author(author("A1", "Pat Investigator", LU), 2016)
        b = library.summarize_author(author("A2", "Pat Investigator", "Elsewhere College", works=5, orcid=CHRIS), 2016)
        b.update({"hIndex": 3, "i10": 1, "topics": [["Twin studies", 2], ["Zebrafish", 1]]})
        s = library.combine_summaries([a, b])
        self.assertEqual((s["name"], s["worksCount"], s["citedBy"], s["hIndex"], s["i10"]),
                         ("Pat Investigator", 45, 1800, 12, 21))
        self.assertEqual(s["worksSince"], a["worksSince"] + b["worksSince"])
        self.assertEqual(s["topics"], [["Sleep and attention", 9], ["Twin studies", 8], ["Zebrafish", 1]])
        self.assertEqual((s["institutions"], s["orcid"]), ([LU, "Elsewhere College"], CHRIS))
        self.assertIs(library.combine_summaries([a]), a)

    def test_a_refresh_reads_every_record_and_never_adds_the_person_as_their_own_coauthor(self):
        from labnetwork import refresh
        c = {"sources": [{"url": "https://openalex.org/A1"}, {"url": "https://example.org/"},
                         {"url": "https://openalex.org/A2"}, {"url": "https://openalex.org/A1"}],
             "profileSourceUrl": "https://openalex.org/A5"}
        self.assertEqual(refresh._openalex_id(c), ["A1", "A2"])
        self.assertEqual(refresh._openalex_id({"sources": [], "profileSourceUrl": "https://openalex.org/A5"}), ["A5"])
        with mock.patch.dict(AUTHORS, {"A2": author("A2", "Pat Investigator", LU, works=5)}):
            self.fake = Fake()
            self.fake.works += [work("W60", TODAY, "New, under Pat's second record", ["A2"]),
                                work("W61", TODAY, "New, under both of Pat's records", ["A1", "A2"])]
            self.workspace(pi=("A1", "A2"))
            d = refresh.refresh(self.ws_path, scope=1, dry_run=True, client=Client(transport=self.fake),
                                log=lambda m: None)["digest"]
        self.assertEqual(sorted(x["title"] for x in d["papers"] if x["personId"] == "pi"),
                         ["New, under Pat's second record", "New, under both of Pat's records"])
        self.assertEqual(d["newPeople"], [])

    # ---------------------------------------------------------------- records marked as not someone's

    def test_a_record_joined_by_orcid_and_marked_not_theirs_leaves_for_good(self):
        cache = os.path.join(self.tmp, "cache")
        lines = []

        def run(**kw):
            return library.run(self.ws_path, client=Client(transport=self.fake, cache_dir=cache),
                               log=kw.pop("log", lambda m: None), **kw)
        with mock.patch.dict(AUTHORS, {"A1": author("A1", "Pat Investigator", LU, orcid=PAT)}):
            self.fake = Fake()
            self.workspace()
            run()                                       # Pat's paper list is read, and kept in the response cache
            # OpenAlex then lists a namesake's record under Pat's ORCID iD; it passes the guards. Pat publishes again.
            AUTHORS["A2"] = author("A2", "Pat Investigator", LU, works=5, orcid=PAT)
            self.fake.works += [work("W30", "2026-02-01", "The namesake's paper", ["A2"]),
                                work("W31", TODAY, "Pat's newest paper", ["A1"])]
            lib = library.load(Workspace(self.ws_path))
            lib["people"]["pi"]["openalex"]["orcidCheckedAt"] = "2000-01-01"
            library.save(Workspace(self.ws_path), lib)
            run(update=True)
            pi = self.people()["pi"]["openalex"]
            self.assertEqual((pi["ids"], pi["works"]), (["A1", "A2"], ["W31", "W30", "W1", "W2"]))
            svc = Service(Workspace(self.ws_path))
            res = svc.mark_not_theirs("pi", {"openalexId": "https://openalex.org/A2", "note": "A namesake"}, "Jon")
            self.assertEqual((res["openalexId"], res["notTheirs"], res["changed"], res["held"]), ("A2", True, True, True))
            # The app drops the record at once. Pat's summary and papers mixed both records, so they go with it.
            view = svc.person("pi", {"maxDegree": "all", "doctorate": "any", "identified": "0"})
            self.assertEqual((view["library"]["openalex"]["ids"], view["library"]["works"]), (["A1"], []))
            self.assertIsNone(view["library"]["openalex"]["summary"])
            self.assertEqual([(m["id"], m["note"], m["actor"]) for m in view["openalexNotTheirs"]],
                             [("A2", "A namesake", "Jon")])
            self.assertNotIn("W30", svc.library()["works"])          # nor does search find it under Pat
            calls = len(self.fake.calls)
            run(update=True, log=lines.append)
            pi = self.people()["pi"]["openalex"]
            # The next run takes the record out of the file. Pat's list is read from OpenAlex, not from the cached
            # copy of the first build (which lacks the newest paper), and the lookup by ORCID iD skips the record.
            self.assertEqual((pi["ids"], pi["basis"]), (["A1"], "OpenAlex link on their record"))
            self.assertEqual((pi["works"], pi["summary"]["worksCount"]), (["W31", "W1", "W2"], 40))
            self.assertEqual((pi["notTheirs"], self.orcid_requests(calls)), (["A2"], [[PAT]]))
            self.assertFalse({"orcidHeld", "orcidGroup", "reread"} & set(pi))   # decided, so not held back either
            self.assertNotIn("W30", library.load(Workspace(self.ws_path))["works"])
            self.assertIn("  Marked as not theirs, taken from Pat Investigator: A2; their papers are read again", lines)
            # A month later the lookup finds the record again; it stays out.
            lib = library.load(Workspace(self.ws_path))
            lib["people"]["pi"]["openalex"]["orcidCheckedAt"] = "2000-01-01"
            library.save(Workspace(self.ws_path), lib)
            run(update=True)
            self.assertEqual(self.people()["pi"]["openalex"]["ids"], ["A1"])
            # A withdrawn mark brings the lookup back at the next run, though Pat was looked up this month.
            svc = Service(Workspace(self.ws_path))
            self.assertTrue(svc.mark_not_theirs("pi", {"openalexId": "A2", "undo": True}, "Jon")["changed"])
            self.assertFalse(svc.mark_not_theirs("pi", {"openalexId": "A2", "undo": True}, "Jon")["changed"])
            run(update=True)
            pi = self.people()["pi"]["openalex"]
        self.assertEqual(pi["ids"], ["A1", "A2"])
        self.assertNotIn("notTheirs", pi)

    def test_held_back_records_are_listed_until_marked_not_theirs(self):
        records = {"A1": author("A1", "Pat Investigator", LU, orcid=PAT),
                   "A2": author("A2", "Pat Investigator", LU, works=5, orcid=PAT),                  # joins
                   "A12": author("A12", "Pam Investigator", LU, works=4, orcid=PAT),                # held back
                   "A13": author("A13", "Pat Investigator", "Far Away University", works=2, country="GB", orcid=PAT)}
        q = {"maxDegree": "all", "doctorate": "any", "identified": "0"}
        with mock.patch.dict(AUTHORS, records):
            self.fake = Fake()
            self.workspace()
            self.run_lib()
            svc = Service(Workspace(self.ws_path))
            o = svc.person("pi", q)["library"]["openalex"]
            self.assertEqual(o["ids"], ["A1", "A2"])
            self.assertEqual([(h["id"], h["name"], h["reason"], h["url"]) for h in o["held"]],
                             [("A12", "Pam Investigator", "another name", "https://openalex.org/A12"),
                              ("A13", "Pat Investigator", "no institution in common", "https://openalex.org/A13")])
            # A held-back record marked as not theirs leaves the list; Pat never held it, so nothing else changes.
            self.assertFalse(svc.mark_not_theirs("pi", {"openalexId": "A12"}, "test")["held"])
            view = svc.person("pi", q)
            self.assertEqual(([h["id"] for h in view["library"]["openalex"]["held"]], view["library"]["openalex"]["ids"]),
                             (["A13"], ["A1", "A2"]))
            self.assertEqual([w["id"] for w in view["library"]["works"]], ["W1", "W2"])
            self.assertEqual([m["name"] for m in view["openalexNotTheirs"]], ["Pam Investigator"])   # from the lookup
            # Marking the record that joined starts Pat over; the other held-back record stays listed to decide.
            svc.mark_not_theirs("pi", {"openalexId": "A2"}, "test")
            o = svc.person("pi", q)["library"]["openalex"]
            self.assertEqual((o["ids"], [h["id"] for h in o["held"]]), (["A1"], ["A13"]))
            self.run_lib(update=True)
        pi = self.people()["pi"]["openalex"]
        self.assertEqual((pi["ids"], pi["notTheirs"], pi["works"]), (["A1"], ["A12", "A2"], ["W1", "W2"]))
        self.assertEqual([h["id"] for h in pi["orcidHeld"]], ["A13"])      # the lookup made again lists it again

    def test_a_link_on_their_record_marked_not_theirs_no_longer_counts(self):
        from labnetwork import refresh
        with mock.patch.dict(AUTHORS, {"A2": author("A2", "Pat Investigator", LU, works=5)}):
            self.fake = Fake()
            self.fake.works.append(work("W30", "2025-02-01", "Under the other record", ["A2"]))
            self.workspace(pi=("A1", "A2"))           # sources cannot be taken off a record
            self.run_lib()
            svc = Service(Workspace(self.ws_path))
            svc.mark_not_theirs("pi", {"openalexId": "A2"}, "test")
            self.fake.works.append(work("W40", TODAY, "New, under the other record", ["A2"]))
            # The live check reads only Pat's own record, before and after the next library run.
            svc._live_client = Client(transport=self.fake)
            live = svc.latest("pi")
            asked = [parse_qs(urlsplit(u).query)["filter"][0] for u in self.fake.calls if "/works" in u][-1]
            self.assertTrue(asked.startswith("authorships.author.id:A1,"), asked)
            self.assertEqual(sorted(w["id"] for w in live["works"]), ["W1", "W2"])   # a list read just now
            raw = library.load(Workspace(self.ws_path))                       # the file still holds both records
            out = library.latest(raw, "pi", Client(transport=self.fake), not_theirs={"A2"})
            self.assertEqual(out["works"], [])
            self.assertIn("authorships.author.id:A1,", [u for u in self.fake.calls if "/works" in u][-1])
            self.run_lib(update=True)
            pi = self.people()["pi"]["openalex"]
            self.assertEqual((pi["ids"], pi["basis"], pi["works"]), (["A1"], "OpenAlex link on their record",
                                                                      ["W1", "W2"]))
            # A refresh reads Pat's own record only.
            d = refresh.refresh(self.ws_path, scope=1, dry_run=True, client=Client(transport=self.fake),
                                log=lambda m: None)["digest"]
            self.assertNotIn("New, under the other record", [x["title"] for x in d["papers"]])
            # Withdrawn, the link counts again.
            svc.mark_not_theirs("pi", {"openalexId": "A2", "undo": True}, "test")
            self.run_lib(update=True)
        self.assertEqual(self.people()["pi"]["openalex"]["ids"], ["A1", "A2"])

    def test_shared_papers_and_name_searches_skip_records_marked_not_theirs(self):
        records = {"A7": author("A7", "Mira Mentor", NI, works=30, orcid=MIRA),
                   "A8": author("A8", "Mira Mentor", NI, works=2)}
        cache = os.path.join(self.tmp, "cache")      # an update finds people on papers read in earlier runs
        self.run_lib = lambda **kw: library.run(self.ws_path, client=Client(transport=self.fake, cache_dir=cache),
                                                log=lambda m: None, **kw)
        with mock.patch.dict(AUTHORS, records):
            self.fake = Fake()
            self.workspace()
            self.run_lib()
            p = self.people()
            self.assertEqual([p[x]["openalex"].get("ids") for x in ("c1", "m1")], [["A3"], ["A5"]])
            self.assertEqual(p["m2"]["openalex"]["candidates"], ["A7", "A8"])          # ambiguous, searched today
            svc = Service(Workspace(self.ws_path))
            for pid, aid in (("c1", "A3"), ("m1", "A5"), ("m2", "A8")):
                svc.mark_not_theirs(pid, {"openalexId": aid}, "test")
            view = svc.person("c1", {"maxDegree": "all", "doctorate": "any", "identified": "0"})["library"]
            self.assertEqual((view["openalex"]["status"], view["works"]), ("unmatched", []))
            self.run_lib(update=True)
        p = self.people()
        lib = library.load(Workspace(self.ws_path))
        # Chris's only match on Pat's papers and Morgan's only match at Northfield are marked: their name searches
        # find no one else there, and their papers leave them.
        self.assertEqual([p[x]["openalex"]["status"] for x in ("c1", "m1")], ["not-found", "not-found"])
        self.assertEqual(lib["works"]["W1"]["people"], ["pi"])
        self.assertNotIn("W5", lib["works"])
        # Mira is searched again, though her search ran today; without the marked record one author is left.
        self.assertEqual((p["m2"]["openalex"]["status"], p["m2"]["openalex"]["ids"]), ("linked", ["A7"]))

    def test_marks_live_in_the_change_log_and_can_be_set_from_the_command_line(self):
        from labnetwork import cli, exporter
        from labnetwork.importer import Importer
        self.workspace()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["not-theirs", self.ws_path, "Pat Investigator", "https://openalex.org/A2",
                                       "--note", "A namesake", "--actor", "Jon"]), 0)
            self.assertEqual(cli.main(["not-theirs", self.ws_path, "pi", "A2"]), 0)        # again: nothing to change
            self.assertEqual(cli.main(["not-theirs", self.ws_path, "pi", "W123"]), 1)      # a work, not an author
            self.assertEqual(cli.main(["not-theirs", self.ws_path, "Nobody Here", "A2"]), 1)
        self.assertIn("Marked A2 as not Pat Investigator's.", out.getvalue())
        self.assertIn("Nothing to change: A2 is already marked as not Pat Investigator's.", out.getvalue())
        self.assertIn(f"Marked as not theirs: A2, by Jon on {TODAY}: A namesake", out.getvalue())
        self.assertIn("Give one OpenAlex author record", err.getvalue())
        svc = Service(Workspace(self.ws_path))
        ev = [e for e in svc.nb.events if e["op"] == "workspace.note"]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["actor"], ev[0]["data"]["kind"], ev[0]["data"]["person"], ev[0]["data"]["notTheirs"]),
                         ("Jon", "identity", "pi", True))
        # An export bundle carries the mark to another workspace on the same packet; library.json is not needed.
        fname, _, data = exporter.export(svc, "bundle")
        ws2 = Workspace.create(os.path.join(self.tmp, "ws2"), os.path.join(self.tmp, "packet"), lab_name="Copy")
        imp = Importer(Service(ws2))
        imp.apply(imp.preview(fname, data)["batchId"], {}, "test")
        self.assertEqual(list(Service(ws2).nb.openalex_not_theirs("pi")), ["A2"])
        # The latest note wins.
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["not-theirs", self.ws_path, "pi", "A2", "--undo"]), 0)
        self.assertIn("Withdrew the mark on A2.", out.getvalue())
        self.assertEqual(Service(Workspace(self.ws_path)).nb.openalex_not_theirs("pi"), {})


if __name__ == "__main__":
    unittest.main()
