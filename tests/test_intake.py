"""Slack intake: reading a channel, adding lab members' submissions, holding unclear ones, undo (no network)."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from labnetwork import intake
from labnetwork.connectors.http import Client
from labnetwork.service import Service
from labnetwork.store import Workspace
from tests.fixtures import notebook, write_packet

TOKEN = "xoxb-test-token"
MEMBERS = {"U_PAT": {"contactId": "pi", "name": "Pat Investigator"},
           "U_SAM": {"contactId": "stu", "name": "Sam Student"}}
LABELS = [("name", "Name"), ("institution", "Institution"), ("position", "Position"), ("relationship", "Relationship"),
          ("when", "When"), ("where", "Where"), ("notes", "Notes"), ("followUp", "Follow-up"), ("link", "Link")]


def form(ts, sender, **fields):
    """A message as the Slack workflow posts it."""
    lines = ["*Lab Network submission*", f"*From:* <@{sender}>"]
    lines += [f"*{label}:* {fields.get(key, '')}" for key, label in LABELS]
    return {"type": "message", "subtype": "bot_message", "bot_id": "B_WORKFLOW", "ts": ts, "text": "\n".join(lines)}


def typed(ts, user, text, **extra):
    return {"type": "message", "user": user, "ts": ts, "text": text, **extra}


class FakeSlack:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def __call__(self, method, url, body, headers):
        assert headers.get("Authorization") == f"Bearer {TOKEN}", "every Slack call carries the bot token"
        u = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        name = u.path.rsplit("/", 1)[1]
        self.calls.append(name)
        if name == "auth.test":
            return {"ok": True, "url": "https://example-lab.slack.com/", "user": "labbot"}
        if name == "conversations.history":
            oldest = float(q.get("oldest") or 0)
            msgs = [m for m in self.messages if float(m["ts"]) > oldest]
            return {"ok": True, "messages": list(reversed(msgs)), "has_more": False}
        if name == "conversations.members":
            return {"ok": True, "members": ["U_PAT", "U_SAM", "U_GUEST", "U_BOT"], "response_metadata": {}}
        if name == "users.info":
            people = {"U_PAT": "Pat Investigator", "U_SAM": "Samuel Student", "U_GUEST": "Gina Guest",
                      "U_BOT": "Some Bot"}
            return {"ok": True, "user": {"id": q["user"], "real_name": people[q["user"]], "is_bot": q["user"] == "U_BOT"}}
        if name == "users.conversations":
            return {"ok": True, "channels": [{"id": "G_LAB", "name": "lab-network", "is_private": True}]}
        raise AssertionError(f"unexpected Slack call {name}")


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="labnetwork-test-")
        ws = Workspace.create(os.path.join(self.tmp, "ws"), write_packet(self.tmp, notebook()), lab_name="Fixture Lab")
        self.ws_path = ws.path
        self.env = mock.patch.dict(os.environ, {"LABNETWORK_SLACK_TOKEN": TOKEN})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def setup_intake(self, mode="automatic", trusted=True):
        data = intake.empty()
        data.update({"channel": "G_LAB", "channelName": "lab-network", "members": MEMBERS, "mode": mode})
        if trusted:                                   # the lab has already trusted its form's app
            data["formBot"] = {"botId": "B_WORKFLOW", "appId": "", "name": "Add to Lab Network"}
        intake.save(Workspace(self.ws_path), data)

    def check(self, messages):
        svc = Service(Workspace(self.ws_path))
        fake = FakeSlack(messages)
        counts = intake.check(svc, client=Client(transport=fake))
        return svc, counts, fake

    def items(self):
        return intake.load(Workspace(self.ws_path))["items"]

    # ------------------------------------------------------------ reading

    def test_parse_reads_the_workflow_message(self):
        text = ("*Lab Network submission*\n*From:* <@U_PAT|pat>\n*Name:* Dana Park\n*Institution:* Hill &amp; Dale College\n"
                "*Relationship:* Collaborator\n*When:* <!date^1792195200^{date}|October 17, 2026>\n"
                "*Notes:* Talked about sleep\nand a shared dataset.\n*Link:* <https://example.org/dana|example.org/dana>")
        p = intake.parse(text)
        self.assertEqual(p["kind"], "form")
        f = p["fields"]
        self.assertEqual((f["from"], f["name"], f["institution"]), ("U_PAT", "Dana Park", "Hill & Dale College"))
        self.assertEqual(f["notes"], "Talked about sleep\nand a shared dataset.")
        self.assertEqual(f["link"], "https://example.org/dana")
        self.assertEqual(intake.parse_date(f["when"]), "2026-10-17")
        self.assertEqual(intake.parse("thanks, will do!")["kind"], "text")

    def test_rich_text_blocks_are_read_when_text_is_empty(self):
        msg = {"ts": "1.0", "text": "", "blocks": [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
            {"type": "text", "text": "Lab Network submission\nFrom: "}, {"type": "user", "user_id": "U_SAM"},
            {"type": "text", "text": "\nName: Lee Fox\n"}]}]}]}
        p = intake.parse(intake.message_text(msg))
        self.assertEqual((p["kind"], p["fields"]["from"], p["fields"]["name"]), ("form", "U_SAM", "Lee Fox"))

    def test_relationship_answers(self):
        self.assertEqual(intake.relationship("My mentor or advisor")[:2], ("mentor", "they-mentored-member"))
        self.assertEqual(intake.relationship("I’m on their committee")[:2], ("committee", "member-served-on-their-committee"))
        self.assertEqual(intake.relationship("Met them")[0], "met")
        self.assertEqual(intake.relationship("something else")[0], "")

    # ------------------------------------------------------------ automatic mode

    def test_automatic_mode_adds_a_new_person_with_relationship_and_note(self):
        self.setup_intake()
        svc, counts, _ = self.check([form("100.1", "U_PAT", name="Dana Park", institution="Hill College",
                                          position="Associate Professor", relationship="Collaborator",
                                          when="2026-10-01", where="SRCD poster session",
                                          notes="Shares our interest in sleep.", followUp="Send the preprint",
                                          link="https://example.org/dana")])
        self.assertEqual(counts["added"], 1)
        svc = Service(Workspace(self.ws_path))
        dana = next(c for c in svc.nb.records["contacts"].values() if c["name"] == "Dana Park")
        self.assertEqual((dana["institution"], dana["role"], dana["website"]),
                         ("Hill College", "Associate Professor", "https://example.org/dana"))
        self.assertIn("example-lab.slack.com/archives/G_LAB/p1001", dana["sources"][0]["url"])
        ties = [e for e in svc.nb.records["edges"].values() if dana["id"] in (e["from"], e["to"])]
        self.assertEqual([(e["type"], e["from"], e["provenance"]) for e in ties], [("collaborator", "pi", "reported")])
        notes = [m for m in svc.nb.records["meetings"].values() if m["contactId"] == dana["id"]]
        self.assertEqual((notes[0]["metBy"], notes[0]["context"], notes[0]["followUp"], notes[0]["date"]),
                         ("Pat Investigator", "SRCD poster session", "Send the preprint", "2026-10-01"))
        actors = {ev["actor"] for ev in svc.nb.events if ev.get("id") in (dana["id"], ties[0]["id"], notes[0]["id"])}
        self.assertEqual(actors, {"Pat Investigator (via Slack)"})        # written under the lab member's name

    def test_the_same_person_submitted_twice_is_one_person(self):
        self.setup_intake()
        first = dict(name="Dana Park", institution="Hill College", relationship="Collaborator", where="SRP 2026")
        self.check([form("100.1", "U_PAT", **first),
                    form("100.2", "U_PAT", **first),                                      # the same member again
                    form("100.3", "U_SAM", name="Dana Park", institution="Hill College", relationship="Met them",
                         where="AACAP 2026")])                                            # another member
        svc = Service(Workspace(self.ws_path))
        danas = [pid for pid, c in svc.nb.records["contacts"].items() if c["name"] == "Dana Park"]
        self.assertEqual(len(danas), 1)
        ties = sorted((e["type"], e["from"] if e["from"] != danas[0] else e["to"])
                      for e in svc.nb.records["edges"].values() if danas[0] in (e["from"], e["to"]))
        self.assertEqual(ties, [("collaborator", "pi"), ("met", "stu")])     # Pat's second report adds no record
        notes = sorted(m["context"] for m in svc.nb.records["meetings"].values() if m["contactId"] == danas[0])
        self.assertEqual(notes, ["AACAP 2026", "SRP 2026", "SRP 2026"])        # each submission keeps its note
        self.assertEqual([i["status"] for i in self.items().values()], ["added"] * 3)

    def test_met_them_links_an_existing_person_with_a_met_record(self):
        self.setup_intake()
        self.check([form("100.2", "U_SAM", name="Chris Coauthor", relationship="Met them", where="Conference dinner")])
        svc = Service(Workspace(self.ws_path))
        self.assertEqual(sum(1 for c in svc.nb.records["contacts"].values() if c["name"] == "Chris Coauthor"), 1)
        met = [e for e in svc.nb.records["edges"].values() if e["type"] == "met" and {e["from"], e["to"]} == {"stu", "c1"}]
        self.assertEqual(len(met), 1)
        self.assertEqual(met[0]["provenance"], "firsthand")

    def test_nicknames_find_the_person_and_shared_short_forms_wait(self):
        self.setup_intake()
        self.check([form("100.3", "U_PAT", name="Tobias Grandtrainee", relationship="Coauthor"),
                    form("100.31", "U_PAT", name="Christopher Coauthor", relationship="Coauthor")])
        items = self.items()
        self.assertEqual(items["100.3"]["status"], "held")        # Toby is a name of its own, not only Tobias
        self.assertEqual(items["100.31"]["status"], "held")       # Chris could be Christopher or Christina
        self.assertEqual(intake._given("Bill Lindqvist", "William G. Lindqvist"), 2)
        self.assertEqual(intake._given("Liz Carrow", "Elizabeth L. Carrow"), 2)
        self.assertEqual(intake._given("Kate Carrow", "Kathryn L. Carrow"), 1)   # Kate is a name of its own
        self.assertEqual(intake._given("Bill Lindqvist", "Robert Lindqvist"), 0)
        self.assertEqual(intake._given("Samantha Student", "Sam Student"), 1)
        self.assertEqual(intake._given("Dr. Dana Park, PhD", "Dana Park"), 3)
        self.assertEqual(intake.clean_name("Wen Jie Ma"), "Wen Jie Ma")         # Ma is a family name
        self.assertEqual(intake.clean_name("Dr. Wen Jie Ma, PhD"), "Wen Jie Ma")

    def test_an_unclear_person_is_held_not_guessed(self):
        self.setup_intake()
        _, counts, _ = self.check([form("100.4", "U_PAT", name="M. Mentor", relationship="My mentor or advisor")])
        self.assertEqual(counts["held"], 1)
        item = self.items()["100.4"]
        self.assertEqual(item["status"], "held")
        self.assertEqual({c["id"] for c in item["candidates"]}, {"m1", "m2"})
        svc = Service(Workspace(self.ws_path))
        self.assertFalse(any(c["name"] == "M. Mentor" for c in svc.nb.records["contacts"].values()))
        # The lab chooses; the relationship then runs from mentor to mentee.
        intake.decide_item(svc, "100.4", "add", "m2", "Pat Investigator")
        svc = Service(Workspace(self.ws_path))
        self.assertTrue(any(e["type"] == "mentor" and e["from"] == "m2" and e["to"] == "pi"
                            for e in svc.nb.records["edges"].values()))

    def test_details_for_someone_already_here_go_into_the_note(self):
        self.setup_intake()
        self.check([form("100.15", "U_SAM", name="Chris Coauthor", institution="Far Away University",
                         relationship="Met them", where="Conference dinner")])
        item = self.items()["100.15"]
        self.assertEqual(item["status"], "held")                    # same name, different institution: ask
        svc = Service(Workspace(self.ws_path))
        intake.decide_item(svc, "100.15", "add", "c1", "Pat")
        svc = Service(Workspace(self.ws_path))
        self.assertEqual(svc.nb.records["contacts"]["c1"]["institution"], "Lakeside University")   # unchanged
        note = [m for m in svc.nb.records["meetings"].values() if m["contactId"] == "c1" and m["metBy"] == "Sam Student"]
        self.assertIn("Institution given in Slack: Far Away University.", note[0]["learning"])

    def test_only_lab_members_can_submit(self):
        self.setup_intake()
        _, counts, _ = self.check([form("100.5", "U_GUEST", name="Eve Outsider", relationship="Collaborator"),
                                   typed("100.6", "U_GUEST", "Add Eve Outsider please")])
        self.assertEqual(counts["ignored"], 2)
        items = self.items()
        self.assertTrue(all(items[t]["status"] == "ignored" and "fields" not in items[t] for t in ("100.5", "100.6")))
        svc = Service(Workspace(self.ws_path))
        self.assertFalse(any(c["name"] == "Eve Outsider" for c in svc.nb.records["contacts"].values()))

    def test_a_typed_message_belongs_to_whoever_typed_it(self):
        self.setup_intake()
        text = "Lab Network submission\nFrom: <@U_PAT>\nName: Ivy Typed\nRelationship: Collaborator"
        self.check([typed("100.7", "U_SAM", text)])
        item = self.items()["100.7"]
        self.assertEqual((item["member"], item["status"]), ("stu", "added"))   # not Pat, whom the text names

    def test_plain_messages_wait_and_other_messages_are_skipped(self):
        self.setup_intake()
        _, counts, _ = self.check([
            typed("100.8", "U_SAM", "Met Dana at SRCD, she studies sleep in teens."),
            typed("100.9", "U_SAM", "a reply", thread_ts="100.8"),
            {"type": "message", "subtype": "channel_join", "user": "U_SAM", "ts": "101.0", "text": "joined"},
            {"type": "message", "subtype": "bot_message", "bot_id": "B_OTHER", "ts": "101.1", "text": "Reminder: lab meeting"}])
        self.assertEqual(counts["read"], 1)
        self.assertEqual(self.items()["100.8"]["status"], "held")
        svc = Service(Workspace(self.ws_path))
        intake.decide_item(svc, "100.8", "add", "c1", "Pat")
        svc = Service(Workspace(self.ws_path))
        note = [m for m in svc.nb.records["meetings"].values() if m["contactId"] == "c1" and m["metBy"] == "Sam Student"]
        self.assertIn("studies sleep", note[0]["learning"])

    def test_a_second_check_adds_nothing_twice(self):
        self.setup_intake()
        msgs = [form("100.10", "U_PAT", name="Dana Park", relationship="Collaborator")]
        self.check(msgs)
        _, counts, fake = self.check(msgs + [form("100.11", "U_PAT", name="Lee Fox", relationship="Coauthor")])
        self.assertEqual(counts["read"], 1)
        svc = Service(Workspace(self.ws_path))
        self.assertEqual(sum(1 for c in svc.nb.records["contacts"].values() if c["name"] == "Dana Park"), 1)
        self.assertNotIn("auth.test", fake.calls)                         # the workspace address is kept

    def test_undo_archives_what_a_submission_added(self):
        self.setup_intake()
        self.check([form("100.12", "U_PAT", name="Dana Park", relationship="Collaborator", notes="Sleep")])
        svc = Service(Workspace(self.ws_path))
        dana = next(pid for pid, c in svc.nb.records["contacts"].items() if c["name"] == "Dana Park")
        intake.decide_item(svc, "100.12", "undo", "", "Pat")
        svc = Service(Workspace(self.ws_path))
        self.assertFalse(svc.nb.is_active("contacts", dana))
        self.assertFalse(any(dana in (e["from"], e["to"]) for _, e in svc.nb.active("edges")))
        self.assertEqual(self.items()["100.12"]["status"], "undone")

    # ------------------------------------------------------------ review mode

    def test_review_mode_waits_for_a_decision(self):
        self.setup_intake(mode="review")
        _, counts, _ = self.check([form("100.13", "U_PAT", name="Dana Park", relationship="Collaborator"),
                                   form("100.14", "U_SAM", name="Chris Coauthor", relationship="Met them")])
        self.assertEqual(counts["pending"], 2)
        items = self.items()
        self.assertEqual((items["100.13"]["suggestion"], items["100.14"]["suggestion"]), ("new", "c1"))
        svc = Service(Workspace(self.ws_path))
        self.assertFalse(any(c["name"] == "Dana Park" for c in svc.nb.records["contacts"].values()))
        intake.decide_item(svc, "100.13", "add", "new", "Pat")
        intake.decide_item(svc, "100.14", "dismiss", "", "Pat")
        items = self.items()
        self.assertEqual((items["100.13"]["status"], items["100.13"]["decidedBy"]), ("added", "Pat"))
        self.assertEqual(items["100.14"]["status"], "dismissed")
        with self.assertRaises(intake.IntakeError):
            intake.decide_item(svc, "100.14", "add", "c1", "Pat")      # already handled

    def test_the_mode_switch_and_the_inbox_view(self):
        self.setup_intake(mode="review")
        svc = Service(Workspace(self.ws_path))
        self.assertEqual(svc.intake_settings({"mode": "automatic"})["mode"], "automatic")
        v = svc.intake()
        self.assertTrue(v["configured"] and v["hasToken"])
        self.assertNotIn(TOKEN, json.dumps(v))                              # the token never leaves this machine
        self.assertEqual([m["name"] for m in v["members"]], ["Pat Investigator", "Sam Student"])

    # ------------------------------------------------------------ setup and tokens

    def test_members_are_matched_to_the_lab_only_on_a_clear_name(self):
        svc = Service(Workspace(self.ws_path))
        mapped, unmatched = intake.map_members(svc, Client(transport=FakeSlack([])), TOKEN, "G_LAB")
        self.assertEqual({k: v["contactId"] for k, v in mapped.items()}, {"U_PAT": "pi"})
        # "Samuel Student" might be lab member Sam Student, or not: the admin adds such accounts by hand.
        self.assertEqual(unmatched, ["Samuel Student (U_SAM)", "Gina Guest (U_GUEST)"])

    def test_setup_command_finds_the_channel_and_keeps_hand_made_entries(self):
        import contextlib
        import io
        from labnetwork import cli
        from labnetwork.connectors import http as http_mod
        fake = FakeSlack([])
        real = http_mod.Client
        with mock.patch.object(http_mod, "Client", lambda **kw: real(transport=fake)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["intake", self.ws_path, "--setup", "--channel", "#lab-network",
                                       "--mode", "automatic", "--member", "U_GUEST=pd", "--member", "U_SAM=stu"]), 0)
            data = intake.load(Workspace(self.ws_path))
            self.assertEqual((data["channel"], data["mode"]), ("G_LAB", "automatic"))
            self.assertEqual({k: v["contactId"] for k, v in data["members"].items()},
                             {"U_PAT": "pi", "U_SAM": "stu", "U_GUEST": "pd"})
            self.assertEqual(cli.main(["intake", self.ws_path, "--setup", "--channel", "#lab-network"]), 0)
        self.assertIn("U_GUEST", intake.load(Workspace(self.ws_path))["members"])   # kept on a second setup
        first = out.getvalue().split("Reading #lab-network")[1]
        self.assertNotIn("Gina Guest (U_GUEST)", first.split("not matched")[-1] if "not matched" in first else "")
        second = out.getvalue().split("Reading #lab-network")[-1]
        self.assertIn("Sam Student (Slack U_SAM)", second)                           # the whole roster is listed
        self.assertIn("Postdoc", second)
        self.assertNotIn(TOKEN, out.getvalue())

    def test_members_can_be_added_or_corrected_by_hand(self):
        self.setup_intake()
        svc = Service(Workspace(self.ws_path))
        v = svc.intake_settings({"members": {"U_DANA": "pd", "U_SAM": ""}})
        self.assertEqual({m["slackId"]: m["contactId"] for m in v["members"]}, {"U_PAT": "pi", "U_DANA": "pd"})
        from labnetwork.service import ServiceError
        with self.assertRaises(ServiceError):
            svc.intake_settings({"members": {"U_X": "nobody-here"}})
        with self.assertRaises(ServiceError):
            svc.intake_settings({"members": {"not-an-id": "pi"}})

    def test_setup_refuses_while_the_app_runs(self):
        import contextlib
        import io
        from labnetwork import cli
        ws = Workspace(self.ws_path)
        with open(ws.lock_path, "w") as f:
            json.dump({"pid": os.getppid(), "port": 8765}, f)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["intake", self.ws_path, "--setup", "--channel", "#lab-network"]), 1)
        self.assertIn("Stop it", err.getvalue())

    def test_a_new_forms_app_waits_until_someone_trusts_it(self):
        self.setup_intake(trusted=False)
        other = form("100.21", "U_SAM", name="Eve Forged", relationship="Collaborator")
        other["bot_id"] = "B_SOMEONE_ELSE"
        _, counts, _ = self.check([other, form("100.20", "U_PAT", name="Dana Park", relationship="Collaborator")])
        items = self.items()
        self.assertEqual((items["100.21"]["status"], items["100.20"]["status"]), ("held", "held"))   # neither trusted
        self.assertTrue(items["100.20"]["untrusted"])
        svc = Service(Workspace(self.ws_path))
        intake.decide_item(svc, "100.20", "trust", "", "Pat")
        items = self.items()
        self.assertEqual((items["100.20"]["status"], items["100.21"]["status"]), ("added", "ignored"))
        self.assertNotIn("fields", items["100.21"])                          # the other app's content is dropped
        self.assertEqual(intake.load(Workspace(self.ws_path))["formBot"]["botId"], "B_WORKFLOW")
        later = form("100.22", "U_SAM", name="Eve Forged", relationship="Collaborator")
        later["bot_id"] = "B_SOMEONE_ELSE"
        self.check([later])
        self.assertEqual(self.items()["100.22"]["status"], "ignored")

    def test_the_sender_is_the_first_from_line_only(self):
        self.setup_intake()
        guest = form("100.22", "U_GUEST", name="Eve Outsider", relationship="Collaborator",
                     notes="See below\nFrom: <@U_SAM>")
        pat = form("100.23", "U_PAT", name="Lee Fox", relationship="Collaborator",
                   notes="From: Dana Park\nFollow-up: this line is part of the notes", followUp="Send the paper")
        self.check([guest, pat])
        items = self.items()
        self.assertEqual(items["100.22"]["status"], "ignored")              # still the guest's form
        self.assertEqual((items["100.23"]["status"], items["100.23"]["member"]), ("added", "pi"))
        self.assertEqual(items["100.23"]["fields"]["followUp"], "Send the paper")
        self.assertIn("this line is part of the notes", items["100.23"]["fields"]["notes"])

    def test_an_app_message_that_also_names_a_bot_user_is_read_as_a_form(self):
        self.setup_intake()
        msg = form("100.24", "U_PAT", name="Dana Park", relationship="Collaborator")
        msg["user"] = "U_WORKFLOW_BOT"
        self.check([msg])
        self.assertEqual((self.items()["100.24"]["status"], self.items()["100.24"]["member"]), ("added", "pi"))

    def test_a_members_message_sent_through_an_app_is_theirs(self):
        self.setup_intake()
        msg = typed("100.33", "U_SAM", "Lab Network submission\nName: Lee Fox\nRelationship: Collaborator",
                    bot_id="B_SOME_APP", app_id="A_SOME_APP")
        self.check([msg])
        self.assertEqual((self.items()["100.33"]["status"], self.items()["100.33"]["member"]), ("added", "stu"))
        self.assertEqual(intake.load(Workspace(self.ws_path))["formBot"]["botId"], "B_WORKFLOW")   # unchanged

    def test_trusting_a_form_does_not_bring_back_a_dismissed_one(self):
        self.setup_intake(trusted=False)
        self.check([form("100.60", "U_PAT", name="Test Person", relationship="Other", notes="test"),
                    form("100.61", "U_PAT", name="Dana Park", relationship="Collaborator")])
        svc = Service(Workspace(self.ws_path))
        intake.decide_item(svc, "100.60", "dismiss", "", "Pat")
        intake.decide_item(svc, "100.61", "trust", "", "Pat")
        items = self.items()
        self.assertEqual((items["100.60"]["status"], items["100.61"]["status"]), ("dismissed", "added"))
        self.assertFalse(any(c["name"] == "Test Person" for c in Service(Workspace(self.ws_path)).nb.records["contacts"].values()))

    def test_a_failed_retry_stays_open(self):
        from labnetwork.service import ServiceError
        self.setup_intake()
        with mock.patch.object(Service, "create_note", side_effect=ServiceError("No")):
            self.check([form("100.62", "U_PAT", name="Christopher Coauthorr", relationship="Met them", where="Lunch")])
            svc = Service(Workspace(self.ws_path))
            with self.assertRaises(intake.IntakeError):
                intake.decide_item(svc, "100.62", "add", "c1", "Pat")   # another person, and it fails again
        self.assertEqual(self.items()["100.62"]["status"], "held")
        intake.decide_item(Service(Workspace(self.ws_path)), "100.62", "add", "c1", "Pat")
        self.assertEqual(self.items()["100.62"]["status"], "added")

    def test_dismiss_takes_back_a_partial_addition(self):
        from labnetwork.service import ServiceError
        self.setup_intake()
        with mock.patch.object(Service, "create_note", side_effect=ServiceError("The record did not pass validation",
                                                                                 ["meeting.context: too long"])):
            self.check([form("100.50", "U_PAT", name="Dana Park", relationship="Met them", where="Lunch")])
        item = self.items()["100.50"]
        self.assertEqual(item["status"], "held")
        self.assertIn("meeting.context: too long", item["reason"])            # the detail is shown
        svc = Service(Workspace(self.ws_path))
        dana = item["created"][0][1]
        intake.decide_item(svc, "100.50", "dismiss", "", "Pat")
        self.assertFalse(Service(Workspace(self.ws_path)).nb.is_active("contacts", dana))

    def test_a_retry_can_choose_someone_already_here(self):
        from labnetwork.service import ServiceError
        self.setup_intake()
        with mock.patch.object(Service, "create_note", side_effect=ServiceError("No")):
            self.check([form("100.51", "U_PAT", name="Christopher Coauthorr", relationship="Met them", where="Lunch")])
        half = self.items()["100.51"]["created"][0][1]
        svc = Service(Workspace(self.ws_path))
        intake.decide_item(svc, "100.51", "add", "c1", "Pat")             # it was Chris Coauthor after all
        svc = Service(Workspace(self.ws_path))
        self.assertFalse(svc.nb.is_active("contacts", half))
        self.assertTrue(any(m["contactId"] == "c1" and m["metBy"] == "Pat Investigator" for _, m in svc.nb.active("meetings")))

    def test_overlong_answers_are_shortened_not_refused(self):
        self.setup_intake()
        self.check([form("100.52", "U_PAT", name="Dana Park", relationship="Met them", where="x" * 3000)])
        svc = Service(Workspace(self.ws_path))
        self.assertEqual(self.items()["100.52"]["status"], "added")
        dana = self.items()["100.52"]["person"]
        note = next(m for _, m in svc.nb.active("meetings") if m["contactId"] == dana)
        self.assertEqual(len(note["context"]), 2000)

    def test_nicknames_that_are_names_of_their_own_wait(self):
        self.setup_intake()
        svc = Service(Workspace(self.ws_path))
        for name in ("William Smith", "John Lee", "Md. Abdul Karim"):
            svc.create_contact({"fields": {"name": name}, "confirmNotDuplicate": True}, "test")
        self.check([form("100.53", "U_PAT", name="Liam Smith", relationship="Collaborator"),
                    form("100.54", "U_PAT", name="Jack Lee", relationship="Collaborator"),
                    form("100.55", "U_PAT", name="Bill Smith", relationship="Collaborator"),
                    form("100.56", "U_PAT", name="Md. Abdul Karim", relationship="Collaborator"),
                    form("100.57", "U_PAT", name="John Smithers Jr.", relationship="Collaborator")])
        items = self.items()
        self.assertEqual((items["100.53"]["status"], items["100.54"]["status"]), ("held", "held"))
        svc = Service(Workspace(self.ws_path))
        self.assertEqual(svc.nb.records["contacts"][items["100.55"]["person"]]["name"], "William Smith")   # Bill
        self.assertEqual(svc.nb.records["contacts"][items["100.56"]["person"]]["name"], "Md. Abdul Karim")
        self.assertEqual(svc.nb.records["contacts"][items["100.57"]["person"]]["name"], "John Smithers Jr.")

    def test_a_form_in_another_order_or_without_some_questions(self):
        text = ("*Lab Network submission*\n*From:* <@U_PAT>\n*Name:* Lee Fox\n*Relationship:* Collaborator\n"
                "*Institution:* Hill College\n*Notes:* We met last spring.\nWhen: that was in April\n"
                "*Follow-up:* Send the paper\nand the protocol\n*Link:* https://example.org/lee")
        f = intake.parse(text, ordered=True)["fields"]
        self.assertEqual((f["from"], f["relationship"], f["institution"]), ("U_PAT", "Collaborator", "Hill College"))
        self.assertNotIn("when", f)                                          # this form has no When question
        self.assertEqual(f["notes"], "We met last spring.\nWhen: that was in April")
        self.assertEqual(f["followUp"], "Send the paper\nand the protocol")

    def test_a_typed_message_keeps_label_like_lines_in_its_notes(self):
        text = ("Lab Network submission\nName: Lee Fox\nWhen: 2026-05-01\nNotes: Talked about the grant.\n"
                "When: we meet again in June\nFollow up: maybe a call\nFollow-up: Send the paper\nLink:\n\n"
                "For Relationship, use one of: Met them, Other.")
        f = intake.parse(text)["fields"]
        self.assertEqual(f["when"], "2026-05-01")
        self.assertEqual(f["notes"], "Talked about the grant.\nWhen: we meet again in June\nFollow up: maybe a call")
        self.assertEqual((f["followUp"], f["link"]), ("Send the paper", ""))

    def test_the_pinned_template_is_ignored(self):
        self.setup_intake()
        template = ("To add someone to Lab Network, copy the lines below.\n\nLab Network submission\nName:\n"
                    "Institution:\nPosition:\nRelationship:\nWhen:\nWhere:\nNotes:\nFollow-up:\nLink:\n\n"
                    "For Relationship, use one of: Met them, Collaborator, Other.")
        _, counts, _ = self.check([typed("100.25", "U_PAT", template)])
        self.assertEqual((counts["ignored"], self.items()["100.25"]["reason"]), (1, "an empty template"))

    def test_typed_messages_take_answers_in_any_order_before_the_notes(self):
        self.setup_intake()
        text = ("Lab Network submission\nRelationship: Collaborator\nName: Lee Fox\nWhere: SRCD,\nin the poster hall\n"
                "Notes: Studies sleep.\n\nA second paragraph.\nWhen: we meet again in June\n"
                "Link: https://example.org/lee\n\nFor Relationship, use one of: Met them, Other.")
        self.check([typed("100.26", "U_SAM", text)])
        f = self.items()["100.26"]["fields"]
        self.assertEqual((f["relationship"], f["name"], f["where"], f["link"]),
                         ("Collaborator", "Lee Fox", "SRCD,\nin the poster hall", "https://example.org/lee"))
        self.assertEqual(f["notes"], "Studies sleep.\nA second paragraph.\nWhen: we meet again in June")
        self.assertNotIn("when", f)                                          # a When line in the notes stays there
        last = intake.parse("Lab Network submission\nName: Lee Fox\nNotes: One.\n\nTwo.\n\n"
                            "For Relationship, use one of: Met them, Other.")["fields"]
        self.assertEqual(last["notes"], "One.\nTwo.")                         # the template's own text is left out

    def test_dates(self):
        self.assertEqual(intake.parse_date("2026-09-31"), "")                # no such day
        self.assertEqual(intake.parse_date("October 17th, 2026"), "2026-10-17")
        p = intake.parse("Lab Network submission\nName: X Y\nWhen: <!date^1792195200^{date_long}|Saturday, October 17th>",
                         ordered=True)
        self.assertEqual(p["fields"]["when"], "2026-10-17")

    def test_undo_keeps_a_person_that_later_records_mention(self):
        self.setup_intake()
        self.check([form("100.27", "U_PAT", name="Dana Park", relationship="Collaborator"),
                    form("100.28", "U_SAM", name="Dana Park", relationship="Met them", where="Lunch")])
        svc = Service(Workspace(self.ws_path))
        dana = next(pid for pid, c in svc.nb.records["contacts"].items() if c["name"] == "Dana Park")
        intake.decide_item(svc, "100.27", "undo", "", "Pat")
        svc = Service(Workspace(self.ws_path))
        self.assertTrue(svc.nb.is_active("contacts", dana))                 # Sam's note still refers to her
        self.assertFalse(any(e["type"] == "collaborator" and dana in (e["from"], e["to"]) for _, e in svc.nb.active("edges")))
        self.assertTrue(any(m["contactId"] == dana and m["metBy"] == "Sam Student" for _, m in svc.nb.active("meetings")))
        self.assertIn("Dana Park stays", self.items()["100.27"]["reason"])

    def test_a_partial_addition_can_be_added_again_without_duplicates(self):
        from labnetwork.service import ServiceError
        self.setup_intake()
        real = Service.create_note
        calls = {"n": 0}

        def flaky(svc_self, payload, actor):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ServiceError("Describe how you met (context)")
            return real(svc_self, payload, actor)
        with mock.patch.object(Service, "create_note", flaky):
            self.check([form("100.29", "U_PAT", name="Dana Park", relationship="Met them", where="Lunch")])
            item = self.items()["100.29"]
            self.assertEqual(item["status"], "held")
            self.assertEqual([c for c, _ in item["created"]], ["contacts"])  # what was written is on record
            svc = Service(Workspace(self.ws_path))
            intake.decide_item(svc, "100.29", "add", "new", "Pat", confirm_new=True)
        svc = Service(Workspace(self.ws_path))
        self.assertEqual(sum(1 for c in svc.nb.records["contacts"].values() if c["name"] == "Dana Park"), 1)
        self.assertEqual(self.items()["100.29"]["status"], "added")

    def test_a_waiting_submission_sees_people_added_since(self):
        self.setup_intake(mode="review")
        self.check([form("100.30", "U_PAT", name="Dana Park", relationship="Collaborator"),
                    form("100.32", "U_SAM", name="Dana Park", relationship="Met them")])
        svc = Service(Workspace(self.ws_path))
        intake.decide_item(svc, "100.30", "add", "new", "Pat", confirm_new=True)
        svc = Service(Workspace(self.ws_path))
        dana = next(pid for pid, c in svc.nb.records["contacts"].items() if c["name"] == "Dana Park")
        second = next(i for i in svc.intake()["items"] if i["ts"] == "100.32")
        self.assertEqual((second["suggestion"], second["candidates"][0]["id"]), (dana, dana))
        with self.assertRaises(intake.IntakeError):
            intake.decide_item(svc, "100.32", "add", "new", "Pat")      # a stale "someone new"
        intake.decide_item(svc, "100.32", "add", dana, "Pat")
        svc = Service(Workspace(self.ws_path))
        self.assertEqual(sum(1 for c in svc.nb.records["contacts"].values() if c["name"] == "Dana Park"), 1)

    def test_automatic_additions_need_more_than_an_initial_or_a_prefix(self):
        self.setup_intake()
        svc = Service(Workspace(self.ws_path))
        for name in ("Danielle Lee", "Junjie Tran", "J. Smith", "Dana Park"):
            svc.create_contact({"fields": {"name": name}, "confirmNotDuplicate": True}, "test")
        self.check([form("100.40", "U_PAT", name="Dan Lee", relationship="Collaborator"),
                    form("100.41", "U_PAT", name="Jun Tran", relationship="Collaborator"),
                    form("100.42", "U_PAT", name="Jennifer Smith", relationship="Collaborator"),
                    form("100.43", "U_PAT", name="Dr. Dana Park", relationship="Collaborator")])
        items = self.items()
        self.assertEqual([items[t]["status"] for t in ("100.40", "100.41", "100.42")], ["held"] * 3)
        svc = Service(Workspace(self.ws_path))
        self.assertEqual(svc.nb.records["contacts"][items["100.43"]["person"]]["name"], "Dana Park")

    def test_token_problems_never_quote_the_file(self):
        secret = "xoxp-very-secret-other-token"
        with mock.patch.dict(os.environ, {"LABNETWORK_SLACK_TOKEN": ""}):
            secrets = os.path.join(self.tmp, "secrets.json")
            with open(secrets, "w") as f:
                json.dump({"OTHER_API_KEY": secret, "SLACK_BOT_TOKEN": "xoxb-good-token-123"}, f)
            data = intake.empty()
            data["token"] = {"file": secrets, "key": ""}
            tok, problem = intake.token_status(data)
            self.assertEqual(tok, "")
            self.assertIn("--token-key", problem)
            self.assertNotIn(secret, problem)
            notes = os.path.join(self.tmp, "slack-token")
            with open(notes, "w") as f:
                f.write("# the lab bot\nxoxb-good-token-123\n")
            data["token"] = {"file": notes, "key": ""}
            self.assertEqual(intake.token(data), "xoxb-good-token-123")
            with open(notes, "w") as f:
                f.write("not a token at all\n")
            tok, problem = intake.token_status(data)
            self.assertEqual(tok, "")
            self.assertNotIn("not a token at all", problem)

    def test_the_inbox_shows_every_waiting_submission(self):
        self.setup_intake()
        data = intake.load(Workspace(self.ws_path))
        data["items"]["1.0"] = {"ts": "1.0", "kind": "text", "status": "held", "member": "pi", "submitter": "Pat",
                                "text": "an old question", "fields": {}}
        for n in range(250):
            ts = f"{1000 + n}.0"
            data["items"][ts] = {"ts": ts, "kind": "form", "status": "added", "member": "pi", "fields": {"name": "X"}}
        intake.save(Workspace(self.ws_path), data)
        v = Service(Workspace(self.ws_path)).intake()
        self.assertIn("1.0", [i["ts"] for i in v["items"]])

    def test_token_sources(self):
        data = intake.empty()
        self.assertEqual(intake.token(data), TOKEN)                         # from the environment
        with mock.patch.dict(os.environ, {"LABNETWORK_SLACK_TOKEN": ""}):
            secrets = os.path.join(self.tmp, "secrets.json")
            with open(secrets, "w") as f:
                json.dump({"OTHER": "x", "SLACK_BOT_TOKEN": "xoxb-from-json"}, f)
            data["token"] = {"file": secrets, "key": "SLACK_BOT_TOKEN"}
            self.assertEqual(intake.token(data), "xoxb-from-json")
            plain = os.path.join(self.tmp, "slack-token")
            with open(plain, "w") as f:
                f.write("xoxb-from-file\n")
            data["token"] = {"file": plain, "key": ""}
            self.assertEqual(intake.token(data), "xoxb-from-file")

    def test_a_failed_check_is_recorded(self):
        self.setup_intake()
        svc = Service(Workspace(self.ws_path))

        def broken(method, url, body, headers):
            return {"ok": False, "error": "not_in_channel"}
        with self.assertRaises(intake.IntakeError):
            intake.check(svc, client=Client(transport=broken))
        self.assertIn("not_in_channel", intake.load(Workspace(self.ws_path))["lastError"])


if __name__ == "__main__":
    unittest.main()
