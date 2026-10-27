"""Packet validation, graph distances, path rules, descriptions and search."""
import copy
import itertools
import os
import sys
import tempfile
import unittest
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from labnetwork import describe  # noqa: E402
from labnetwork.graph import Graph, TieFilter  # noqa: E402
from labnetwork.packet import load_packet, validate_rules, validate_schema, validation_report  # noqa: E402
from labnetwork.service import PeopleFilter, Service  # noqa: E402
from tests import fixtures  # noqa: E402


def independent_bfs(nb_dict, src, mode="any", allowed_status=("reviewed",)):
    """Deliberately separate implementation used to check Graph results."""
    one_way = {"mentor", "postdoc", "committee"}
    adj = {}
    for e in nb_dict["edges"]:
        if e["status"] not in allowed_status:
            continue
        a, b, t = e["from"], e["to"], e["type"]
        if mode == "any":
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        elif t in one_way:
            adj.setdefault(a, set()).add(b)
        elif t == "training":
            continue
        else:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
    dist = {src: 0}
    q = deque([src])
    while q:
        u = q.popleft()
        for v in sorted(adj.get(u, ())):
            if v not in dist:
                dist[v] = dist[u] + 1
                q.append(v)
    return dist


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_fixture_passes(self):
        pdir = fixtures.write_packet(self.tmp)
        rep = validation_report(load_packet(pdir))
        self.assertTrue(rep["ok"], [c for c in rep["checks"] if not c["ok"]])
        self.assertEqual(rep["counts"]["people"], 17)
        self.assertEqual(rep["counts"]["relationships"], 18)

    def test_rule_errors_are_reported(self):
        nb = fixtures.notebook()
        nb["edges"].append(copy.deepcopy(nb["edges"][0]))                       # duplicate edge ID
        nb["edges"].append(fixtures.edge("e-ghost", "pi", "nobody", "coauthor"))  # unknown endpoint
        nb["edges"].append(fixtures.edge("e-self", "pi", "pi", "coauthor"))       # self-edge
        bad = fixtures.edge("e-nosrc", "pi", "c1", "collaborator")
        bad["sourceUrl"] = ""                                                    # public without source
        nb["edges"].append(bad)
        nb["contacts"][3]["website"] = "example.org/no-scheme"                   # not absolute URL
        nb["contacts"][4]["profileCheckedAt"] = "2026-02-30"                     # not a real date
        nb["contacts"][5]["doctorateEvidence"] = ""                              # confirmed w/o evidence
        msgs = [(i["id"], i["field"], i["message"]) for i in validate_rules(nb)]
        text = "\n".join(str(m) for m in msgs)
        self.assertIn("ID used 2 times", text)
        self.assertIn("unknown contact 'nobody'", text)
        self.assertIn("themselves", text)
        self.assertIn("public-record relationship needs a source URL", text)
        self.assertIn("not an absolute http(s) URL", text)
        self.assertIn("not a real YYYY-MM-DD date", text)
        self.assertIn("requires doctorateEvidence", text)

    def test_schema_and_checksum_failures(self):
        nb = fixtures.notebook()
        nb["edges"][0]["type"] = "friend"
        pdir = fixtures.write_packet(self.tmp, nb)
        packet = load_packet(pdir)
        errs = validate_schema(packet.notebook, packet.schema)
        self.assertTrue(any("not one of" in e["message"] for e in errs))
        with open(os.path.join(pdir, "notebook.json"), "a") as f:
            f.write(" ")
        rep = validation_report(load_packet(pdir))
        sha_check = [c for c in rep["checks"] if "delivered_sha256" in c["check"]][0]
        self.assertFalse(sha_check["ok"])


class GraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.ws = fixtures.new_workspace()
        cls.svc = Service(cls.ws)
        cls.nb_dict = fixtures.notebook()

    def test_lab_degrees(self):
        dist, preds = self.svc.lab_dist()
        expect = {"pi": 0, "stu": 0, "pd": 0, "m1": 1, "m2": 1, "c1": 1, "c2": 1, "g1": 1, "t1": 1,
                  "irta": 1, "cm": 1, "pdsup": 1, "c3": 2, "tg": 2, "far": 3, "jose": 4}
        self.assertEqual({k: dist.get(k) for k in expect}, expect)
        self.assertNotIn("iso", dist, "a rejected record must not connect anyone")
        self.assertEqual(preds["c3"], ["c1", "c2"])

    def test_every_pair_matches_independent_bfs(self):
        g = self.svc.graph()
        ids = [c["id"] for c in self.nb_dict["contacts"]]
        for mode in ("any", "directed"):
            for a, b in itertools.product(ids, ids):
                expected = independent_bfs(self.nb_dict, a, mode).get(b)
                got = g.shortest_paths([a], b, mode)
                self.assertEqual(got["distance"], expected, f"{mode} {a}->{b}")
                for p in got["paths"]:
                    self.assertEqual(len(p) - 1, expected)
                    for st in g.path_steps(p, mode):
                        self.assertTrue(st["records"], f"step {st['from']}->{st['to']} has no record")
                        for rec in st["records"]:
                            self.assertIn(rec["id"], g.edges)

    def test_equal_paths_listed_longer_ones_not(self):
        g = self.svc.graph()
        res = g.shortest_paths(["pi", "stu", "pd"], "c3")
        self.assertEqual(res["distance"], 2)
        self.assertEqual(sorted(map(tuple, res["paths"])), [("pi", "c1", "c3"), ("stu", "c2", "c3")])
        res = g.shortest_paths(["m1"], "c3")
        self.assertEqual(res["paths"], [["m1", "pi", "c1", "c3"]])
        self.assertEqual(res["pathCount"], 1)

    def test_zero_step_and_no_path(self):
        g = self.svc.graph()
        self.assertEqual(g.shortest_paths(["c3"], "c3")["status"], "same-person")
        self.assertEqual(g.shortest_paths(["c3"], "c3")["distance"], 0)
        self.assertEqual(g.shortest_paths(["pi"], "iso")["status"], "no-path")

    def test_directed_rules(self):
        g = self.svc.graph()
        self.assertEqual(g.shortest_paths(["m1"], "pi", "directed")["distance"], 1)       # mentor -> mentee
        self.assertEqual(g.shortest_paths(["pi"], "m1", "directed")["status"], "no-path")  # not reversed
        self.assertEqual(g.shortest_paths(["pd"], "pdsup", "directed")["distance"], 1)    # postdoc -> supervisor
        self.assertEqual(g.shortest_paths(["pdsup"], "pd", "directed")["status"], "no-path")
        self.assertEqual(g.shortest_paths(["cm"], "stu", "directed")["distance"], 1)      # committee -> student
        self.assertEqual(g.shortest_paths(["pi"], "t1", "directed")["status"], "no-path")  # training left out
        self.assertEqual(g.shortest_paths(["pi"], "t1", "any")["distance"], 1)
        # Mutual types go both ways in directed mode.
        self.assertEqual(g.shortest_paths(["c1"], "pi", "directed")["distance"], 1)
        self.assertEqual(g.shortest_paths(["stu"], "pi", "directed")["distance"], 4)

    def test_relationship_filter_changes_traversal(self):
        only_coauthor = TieFilter(types=frozenset(["coauthor"]))
        g = Graph(self.svc.nb, only_coauthor)
        dist, _ = g.lab_distances()
        self.assertEqual(dist.get("c3"), 2)        # pi-c1-c3 is all coauthor
        self.assertNotIn("m1", dist)               # mentor records excluded
        with_rejected = TieFilter(status=frozenset(["reviewed", "rejected"]))
        self.assertEqual(Graph(self.svc.nb, with_rejected).lab_distances()[0].get("iso"), 1)


class DescribeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.ws = fixtures.new_workspace()
        cls.svc = Service(cls.ws)
        cls.names = cls.svc.names()
        cls.edges = {e["id"]: e for e in fixtures.notebook()["edges"]}

    def s(self, eid):
        return describe.sentence(self.edges[eid], self.names)

    def test_sentences_follow_direction_and_tense(self):
        self.assertEqual(self.s("e-m1-pi"), "Morgan Mentor mentored Pat Investigator")
        self.assertEqual(self.s("e-pi-stu"), "Pat Investigator mentors Sam Student")
        self.assertEqual(self.s("e-pd-pdsup"), "Dana Postdoc was a postdoc supervised by Pax Supervisor")
        self.assertEqual(self.s("e-cm-stu"), "Cam Committee is recorded as a committee member for Sam Student")
        self.assertEqual(self.s("e-pi-irta"), "Pat Investigator and Ira Fellow were NIH IRTAs together")
        self.assertEqual(describe.perspective(self.edges["e-pi-stu"], "stu"), "Mentor")      # pi is stu's mentor
        self.assertEqual(describe.perspective(self.edges["e-pi-stu"], "pi"), "Mentee")
        self.assertEqual(describe.perspective(self.edges["e-pd-pdsup"], "pd"), "Postdoc supervisor")
        self.assertEqual(describe.perspective(self.edges["e-cm-stu"], "stu"), "Committee member")

    def test_connection_summary(self):
        p = self.svc.person("c3", {})
        self.assertEqual(p["connection"]["distance"], 2)
        self.assertIn("through Chris Coauthor and Casey Coauthor", p["connection"]["headline"])
        p = self.svc.person("irta", {})
        self.assertIn("NIH IRTA colleagues", p["connection"]["headline"])
        recs = p["ties"][0]["records"]
        self.assertEqual(recs[0]["provenanceLabel"], "Reported")
        p = self.svc.person("iso", {})
        self.assertEqual(p["connection"]["status"], "no-path")
        self.assertIn("not evidence that no relationship exists", p["connection"]["coverageNote"])


class SearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.ws = fixtures.new_workspace()
        cls.svc = Service(cls.ws)

    def q(self, text, **params):
        return self.svc.search({"q": text, **params})

    def test_categories_are_distinct(self):
        r = self.q("ADHD")
        self.assertEqual(sorted(p["personId"] for p in r["profiles"]), ["far", "pi"])
        self.assertEqual(len(r["papers"]), 1)
        self.assertIn("attention-deficit/hyperactivity disorder", r["aliasesUsed"])
        self.assertEqual(sorted(a["personId"] for a in r["papers"][0]["associations"]), ["c3", "pi"])
        self.assertEqual(r["grants"], [])
        self.assertEqual(sorted(n["kind"] for n in r["notes"]), ["contact-note", "meeting"])
        far = [p for p in r["profiles"] if p["personId"] == "far"][0]
        self.assertFalse(far["shown"])                       # 3rd degree: outside default view
        self.assertEqual(far["degree"], 3)

    def test_grant_associations_grouped_by_award(self):
        r = self.q("twin")
        self.assertEqual(len(r["grants"]), 1)
        roles = sorted(a["role"] for a in r["grants"][0]["associations"])
        self.assertEqual(roles, ["Co-Investigator", "Contact PI"])

    def test_matching_rules(self):
        self.assertEqual([p["personId"] for p in self.q("Nunez")["people"]], ["jose"])   # accents ignored
        self.assertEqual([p["personId"] for p in self.q("zebra")["profiles"]], ["far"])  # word prefix
        self.assertEqual(self.q("HD")["counts"]["profiles"], 0)                           # no mid-word match
        none = self.q("quantum chromodynamics")
        self.assertEqual(sum(v for k, v in none["counts"].items()), 0)
        quoted = self.q('"ADHD"')
        self.assertEqual(quoted["aliasesUsed"], [])
        self.assertEqual(quoted["counts"]["papers"], 0)


    def test_and_keeps_people_every_term_matches(self):
        from labnetwork.search import split_and
        self.assertEqual(split_and('ADHD AND "drug AND alcohol"'), ["ADHD", '"drug AND alcohol"'])
        self.assertEqual(split_and("drug and alcohol"), ["drug and alcohol"])     # lower-case and is a word
        everyone = {"maxDegree": "all", "doctorate": "any", "identified": "0"}
        adhd = {p["personId"] for p in self.q("ADHD", **everyone)["peopleSummary"]}
        sleep = {p["personId"] for p in self.q("sleep", **everyone)["peopleSummary"]}
        r = self.q("ADHD AND sleep", **everyone)
        self.assertEqual({p["personId"] for p in r["peopleSummary"]}, adhd & sleep)
        self.assertEqual(r["andTerms"], ["ADHD", "sleep"])
        both = [e for e in r["papers"] if e["allTerms"]]
        self.assertEqual(len(both), 1)                       # "Sleep and attention-deficit/hyperactivity disorder"
        self.assertEqual(sorted(both[0]["matchedTerms"]), ["ADHD", "sleep"])
        self.assertIn("attention-deficit/hyperactivity disorder", r["aliasesUsed"])
        self.assertEqual(self.q("zebra AND twin", **everyone)["peopleSummary"], [])


class FilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.ws = fixtures.new_workspace()
        cls.svc = Service(cls.ws)

    def test_default_view(self):
        net = self.svc.network({})
        shown = {n["id"] for n in net["nodes"] if not n["ghost"]}
        self.assertEqual(shown, {"pi", "stu", "pd", "m1", "m2", "c1", "g1", "irta", "cm", "pdsup", "c3", "tg"})
        dist, _ = self.svc.lab_dist()
        flags = {pid: self.svc.person_flags(pid, dist, PeopleFilter()) for pid in ("c2", "t1", "far", "iso")}
        self.assertEqual(flags["c2"]["hiddenBy"], ["doctorate", "placement"])
        self.assertEqual(flags["t1"]["hiddenBy"], ["doctorate"])
        self.assertEqual(flags["far"]["hiddenBy"], ["degree"])
        self.assertEqual(flags["iso"]["hiddenBy"], ["degree"])

    def test_filters_widen_and_narrow(self):
        one = {n["id"] for n in self.svc.network({"maxDegree": "1"})["nodes"]}
        self.assertNotIn("c3", one)
        everyone = self.svc.network({"maxDegree": "all", "doctorate": "any", "identified": "0"})
        self.assertEqual(len(everyone["nodes"]), 17)
        self.assertEqual(everyone["counts"]["unreachable"], 1)

    def test_hidden_bridge_becomes_ghost(self):
        # tg (shown) reaches the lab only through t1, whose doctorate is not classified (hidden).
        net = self.svc.network({})
        ghosts = {n["id"] for n in net["nodes"] if n["ghost"]}
        self.assertEqual(ghosts, {"t1"})
        tg = [n for n in net["nodes"] if n["id"] == "tg"][0]
        self.assertEqual(tg["parent"], "t1")
        c3 = [n for n in net["nodes"] if n["id"] == "c3"][0]
        self.assertEqual(c3["parent"], "c1")      # a shown bridge is preferred over a hidden one
        self.assertEqual(net["counts"]["shown"], 12)


if __name__ == "__main__":
    unittest.main()
