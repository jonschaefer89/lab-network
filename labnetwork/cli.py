"""Command line: labnetwork <command> ...  (run `python3 labnetwork.py --help`)."""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys

from .packet import PacketError, load_packet, validation_report
from .store import Workspace, WorkspaceError

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_PACKET = os.path.join(HERE, "demo", "packet")
DEMO_LIBRARY = os.path.join(HERE, "demo", "library.json")
DEMO_DIGESTS = os.path.join(HERE, "demo", "digest-samples")


def _print_report(rep):
    print(f"Packet: {rep['packetPath']}")
    print(f"notebook.json SHA-256: {rep['sha256']}  ({rep['bytes']} bytes)")
    if rep.get("datasetStatus"):
        print(f"Dataset status: {rep['datasetStatus']}  saved {rep.get('savedAt')}")
    for c in rep["checks"]:
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['check']}: {c['detail']}")
    counts = rep["counts"]
    print("Loaded counts: " + ", ".join(f"{k}={v}" for k, v in counts.items() if not isinstance(v, dict)))
    print("Relationship types: " + ", ".join(f"{k}={v}" for k, v in counts["relationship_types"].items()))
    for r in rep["roots"]:
        others = [x for x in r["sameFirstNameCandidates"] if x != r["id"]]
        print(f"  root {r['id']}: {'found' if r['found'] else 'MISSING'} -> {r['name']} | {r['role']}"
              + (f"  (first name also matches: {', '.join(others)})" if others else ""))
    errs = [i for i in rep["ruleIssues"] if i["level"] == "error"]
    for i in errs[:20]:
        print(f"  rule error: {i['collection']} {i['id']} {i['field']}: {i['message']}")
    for e in rep["schemaErrors"][:20]:
        print(f"  schema error: {e['path']}: {e['message']}")
    print("RESULT: " + ("PASS" if rep["ok"] else "FAIL"))


def cmd_validate(args):
    rep = validation_report(load_packet(args.packet))
    if args.json:
        print(json.dumps(rep, indent=1, ensure_ascii=False))
    else:
        _print_report(rep)
    return 0 if rep["ok"] else 1


def cmd_init(args):
    ws = Workspace.create(args.workspace, args.packet, lab_name=args.lab_name or "")
    rep = validation_report(ws.load_packet())
    _print_report(rep)
    print(f"\nWorkspace created at {ws.path}")
    print(f"Start it with:  python3 \"{os.path.join(HERE, 'labnetwork.py')}\" serve \"{ws.path}\"")
    return 0


def cmd_serve(args):
    from .server import serve
    from .service import Service
    ws = Workspace(args.workspace)
    svc = Service(ws)
    if not svc.report["ok"]:
        print("WARNING: the packet did not pass every validation check:")
        for c in svc.report["checks"]:
            if not c["ok"]:
                print(f"  FAIL {c['check']}: {c['detail']}")
    serve(svc, port=args.port, open_browser=args.open)
    return 0


def cmd_export(args):
    from . import exporter
    from .service import Service
    svc = Service(Workspace(args.workspace))
    fname, _, data = exporter.export(svc, args.format)
    out = args.out or os.path.join(svc.ws.exports_dir, fname)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "wb") as f:
        f.write(data)
    print(f"Wrote {out} ({len(data)} bytes)")
    return 0


def cmd_import(args):
    ws = Workspace(args.workspace)
    if not args.yes:
        return _import(ws, args)
    ws.acquire_lock(0)          # one writer at a time: refused while the app is running
    try:
        return _import(ws, args)
    finally:
        ws.release_lock()


def _import(ws, args):
    from .importer import Importer
    from .service import Service
    svc = Service(ws)
    imp = Importer(svc)
    with open(args.file, "rb") as f:
        data = f.read()
    prev = imp.preview(os.path.basename(args.file), data, size_limit=False)
    print(prev["explain"])
    if prev["alreadyImported"]:
        print("This exact file was imported before: " + ", ".join(b["batchId"] for b in prev["alreadyImported"]))
    for coll, counts in prev["summary"].items():
        print(f"  {coll}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for it in prev["items"]:
        if it["status"] == "rejected":
            print(f"  rejected {it['key']}: {'; '.join(it.get('errors', []))}")
    decisions = {}
    if args.decisions:
        with open(args.decisions, "r", encoding="utf-8") as f:
            decisions = json.load(f)
    if prev["needsDecision"] and not all(k in decisions for k in prev["needsDecision"]):
        print("These items need a decision (merge:<id>, create, or skip). Pass --decisions FILE with a JSON "
              "object, or use the web app's import screen:")
        for it in prev["items"]:
            if it["key"] in prev["needsDecision"]:
                print(f"  {it['key']}: {it.get('name', '')} possibly {[(m.get('id'), m.get('reasons')) for m in it.get('matches', [])]}")
        return 2
    if not args.yes:
        print("Preview only. Re-run with --yes to apply.")
        return 0
    res = imp.apply(prev["batchId"], decisions, args.actor or "command line")
    print("Applied: " + json.dumps(res["applied"]))
    return 0


def cmd_onboard(args):
    from .builder import BuildError
    from .onboard import run
    try:
        return run(args.labdir, interactive=not args.non_interactive, rebuild=args.rebuild)
    except BuildError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_refresh(args):
    from .refresh import refresh
    log = (lambda m: None) if args.quiet else print
    res = refresh(args.workspace, scope=args.scope, since=args.since, dry_run=args.dry_run, log=log)
    if args.quiet and not res["dryRun"]:
        print(f"Lab Network refresh finished; digest {res['digestPath']}")
    return 0


def cmd_expand(args):
    from .expand import expand
    only = [x.strip() for x in (args.only or "").split(",") if x.strip()] or None
    expand(args.workspace, since_year=args.since, per_person=args.per_person, min_shared=args.min_shared,
           only=only, dry_run=args.dry_run, budget_usd=args.budget)
    return 0


def cmd_library(args):
    from .connectors.http import HttpError
    from .library import LibraryError, run
    try:
        res = run(args.workspace, update=args.update, budget_usd=args.budget, use_nih=not args.no_nih,
                  use_nsf=not args.no_nsf, log=(lambda m: None) if args.quiet else print)
    except (LibraryError, HttpError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.quiet:
        print(f"Lab Network library {'update' if args.update else 'build'} finished"
              + (f"; digest {res['digest']}" if res.get("digest") else ""))
    return 0


def _server_call(ws: Workspace, path: str, payload: dict):
    """POST to the running server for this workspace, so only one process writes its change log."""
    import urllib.parse
    import urllib.request
    from .refresh import _running_port
    import urllib.error
    port = _running_port(ws)
    if not port:
        return None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "X-LabNetwork-Actor": urllib.parse.quote("command line")})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:     # the server explains what was wrong
        try:
            msg = json.loads(exc.read().decode("utf-8")).get("error", "")
        except ValueError:
            msg = ""
        raise intake_error(msg or f"the running Lab Network server answered {exc.code}")


def intake_error(msg: str):
    from .intake import IntakeError
    return IntakeError(msg)


def _member_pairs(items):
    pairs = {}
    for item in items or []:
        uid, _, pid = item.partition("=")
        pairs[uid.strip()] = pid.strip()
    return pairs


def cmd_intake(args):
    from . import intake
    from .connectors import slack
    from .connectors.http import Client, HttpError
    from .service import Service
    ws = Workspace(args.workspace)
    try:
        if args.setup:
            running = ws.server_running()
            if running:
                print(f"Error: a Lab Network server (port {running.get('port')}) is using this workspace. Stop it, "
                      "run setup, then start it again.", file=sys.stderr)
                return 1
            data = intake.load(ws)
            if args.token_file:
                data["token"] = {"file": args.token_file, "key": args.token_key or ""}
            tok, problem = intake.token_status(data)
            if not tok:
                print(f"Error: {problem}.", file=sys.stderr)
                return 1
            if not args.channel:
                print("Error: --setup needs --channel, the private channel's name, such as '#lab-network'.",
                      file=sys.stderr)
                return 1
            client = Client(cache_dir=None)
            data["teamUrl"] = slack.auth_test(client, tok).get("url", "")
            ch = slack.find_channel(client, tok, args.channel)
            data.update({"channel": ch["id"], "channelName": ch["name"]})
            svc = Service(ws)
            # Roster entries made earlier (including by hand, for names that do not match) stay while the person is
            # in the channel; everyone else in the channel is matched by name.
            in_channel = set(slack.members(client, tok, ch["id"]))
            kept = {k: v for k, v in (data.get("members") or {}).items() if k in in_channel}
            mapped, unmatched = intake.map_members(svc, client, tok, ch["id"], skip=set(kept))
            data["members"] = {**mapped, **kept}
            if args.mode:
                data["mode"] = args.mode
            intake.save(ws, data)
            if args.member:
                data = intake.set_members(svc, _member_pairs(args.member))
            unmatched = [u for u in unmatched if not any(u.endswith(f"({uid})") for uid in data["members"])]
            print(f"Reading #{ch['name']}{' (private)' if ch['private'] else ' (public: anyone in the workspace can read it)'}."
                  f" Mode: {data['mode']}.")
            print("Lab members who can submit:")
            for uid, m in sorted(data["members"].items(), key=lambda kv: kv[1]["name"]):
                slack_name = m.get("slackName") or ""
                print(f"  {m['name']}" + (f" (Slack: {slack_name}, {uid})" if slack_name and slack_name != m["name"]
                                          else f" (Slack {uid})"))
            if unmatched:
                print("In the channel but not matched to a lab member (their messages are ignored): " + ", ".join(unmatched))
            return 0
        if (args.mode or args.member or args.forget_form) and not args.check:
            payload = {**({"mode": args.mode} if args.mode else {}),
                       **({"members": _member_pairs(args.member)} if args.member else {}),
                       **({"forgetForm": True} if args.forget_form else {})}
            res = _server_call(ws, "/api/intake/settings", payload)
            if res is None:
                if args.mode:
                    intake.set_mode(ws, args.mode)
                if args.member:
                    intake.set_members(Service(ws), payload["members"])
                if args.forget_form:
                    intake.forget_form(ws)
            v = intake.view(Service(ws))
            print(f"Mode: {v['mode']}. Lab members who can submit: " +
                  (", ".join(f"{m['name']} ({m['slackId']})" for m in v["members"]) or "none"))
            return 0
        if args.check:
            res = _server_call(ws, "/api/intake/check", {})
            if res is None:
                ws.acquire_lock(0)
                try:
                    counts = intake.check(Service(ws))
                finally:
                    ws.release_lock()
            else:
                counts = res.get("checked", {})
            print("Read {read} new message(s): {added} added, {held} waiting for a decision, {pending} waiting for "
                  "review, {ignored} ignored.".format(**{k: counts.get(k, 0) for k in
                                                        ("read", "added", "held", "pending", "ignored")}))
            return 0
        v = intake.view(Service(ws))
        if not v["channel"]:
            print("Slack intake is not set up. See docs/slack-intake.md, then run: intake WORKSPACE --setup --channel '#name'")
            return 0
        print(f"Channel #{v['channelName'] or v['channel']}; mode {v['mode']}; token {'found' if v['hasToken'] else 'MISSING'};"
              f" last checked {v['lastCheck'] or 'never'}.")
        if v["lastError"]:
            print("Last error: " + v["lastError"])
        print("Submissions: " + (", ".join(f"{n} {k}" for k, n in sorted(v["counts"].items())) or "none yet"))
        for i in v["items"]:
            if i["status"] in ("held", "pending"):
                print(f"  waiting: {i.get('submitter')}: {(i.get('fields') or {}).get('name') or i.get('text', '')[:60]}"
                      f" ({i.get('reason') or 'review'})")
        return 0
    except (intake.IntakeError, slack.SlackError, HttpError) as exc:
        print(f"Error: {intake._safe(exc)}", file=sys.stderr)
        return 1


def cmd_undo_import(args):
    import urllib.parse
    import urllib.request
    from .refresh import _running_port
    from .service import Service
    ws = Workspace(args.workspace)
    port = _running_port(ws)
    if port:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/import/undo",
                                     data=json.dumps({"batchId": args.batch}).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-LabNetwork-Actor": urllib.parse.quote(args.actor)})
        with urllib.request.urlopen(req, timeout=300) as r:
            res = json.loads(r.read().decode("utf-8"))
    else:
        ws.acquire_lock(0)
        try:
            res = Service(ws).undo_import(args.batch, args.actor)
        finally:
            ws.release_lock()
    print(f"Archived {res['archived']} records created by {res['batchId']} and removed {res.get('removedItems', 0)} "
          f"items it added to existing records. {res['keptUpdates']} changes that filled in values stay (see each "
          "record's history).")
    return 0


def _person_id(svc, ref: str):
    """A person's ID from their ID or their exact name (one person only)."""
    from .model import norm_text
    pid = svc.nb.canonical_id(ref)
    if pid in svc.nb.records["contacts"]:
        return pid
    same = [rid for rid, c in svc.nb.active("contacts") if norm_text(c["name"]) == norm_text(ref)]
    return same[0] if len(same) == 1 else None


def cmd_not_theirs(args):
    """Mark an OpenAlex author record as not a person's, withdraw the mark, or show what is recorded for them."""
    import urllib.error
    import urllib.parse
    import urllib.request
    from .refresh import _running_port
    from .service import Service, ServiceError
    ws = Workspace(args.workspace)
    svc = Service(ws)
    pid = _person_id(svc, args.person)
    if pid is None:
        print(f"Error: no single person with the ID or name '{args.person}'.", file=sys.stderr)
        return 1
    name = svc.nb.records["contacts"][pid]["name"]
    if args.record:
        payload = {"openalexId": args.record, "note": args.note or "", "undo": args.undo}
        port = _running_port(ws)
        try:
            if port:                          # through the running app, so only one process writes the change log
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/contacts/{urllib.parse.quote(pid)}/not-theirs",
                    data=json.dumps(payload).encode("utf-8"), method="POST",
                    headers={"Content-Type": "application/json", "X-LabNetwork-Actor": urllib.parse.quote(args.actor)})
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        res = json.loads(r.read().decode("utf-8"))
                except urllib.error.HTTPError as exc:
                    try:
                        msg = json.loads(exc.read().decode("utf-8")).get("error", "")
                    except ValueError:
                        msg = ""
                    raise ServiceError(msg or f"the running Lab Network server answered {exc.code}")
            else:
                ws.acquire_lock(0)
                try:
                    res = Service(ws).mark_not_theirs(pid, payload, args.actor)
                finally:
                    ws.release_lock()
        except ServiceError as exc:
            print(f"Error: {exc.message}", file=sys.stderr)
            return 1
        aid = res["openalexId"]
        if not res["changed"]:
            print(f"Nothing to change: {aid} is {'already' if res['notTheirs'] else 'not'} marked as not {name}'s.")
        elif res["notTheirs"]:
            print(f"Marked {aid} as not {name}'s. The research library, refresh and expand will not link it to them." +
                  (" The app drops it from their profile now, with their summary and papers; the next library run takes "
                   "it out of library.json and reads their papers again." if res.get("held") else ""))
        else:
            print(f"Withdrew the mark on {aid}. The next library run may link it to {name} again.")
        svc = Service(ws)
    print(f"{name} ({pid})")
    lib = svc._library_view(pid)
    o = (lib or {}).get("openalex") or {}
    if o.get("ids"):
        print(f"  OpenAlex records in the research library: {', '.join(o['ids'])} ({o.get('basis') or 'linked'})")
    elif lib:
        print(f"  OpenAlex records in the research library: none ({o.get('status', 'pending')})")
    else:
        print("  Not in the research library yet.")
    for h in o.get("held") or []:
        print(f"  Held back after the lookup by ORCID iD: {h['id']} {h.get('name', '')} ({h.get('reason', '')})")
    for m in svc._not_theirs_view(pid):
        print(f"  Marked as not theirs: {m['id']} {m.get('name', '')}".rstrip() +
              f", by {m.get('actor') or 'someone'} on {m.get('date', '')}" + (f": {m['note']}" if m.get("note") else ""))
    return 0


def cmd_conference(args):
    """Import a conference program, list the imported ones, or remove one."""
    from . import conferences as conf
    from .model import Notebook
    ws = Workspace(args.workspace)
    if args.follow:
        try:
            ser = conf.series_add(ws, args.follow, args.name or "", args.url or "", args.frequency or "", args.actor)
        except ValueError as err:
            print(f"Error: {err}", file=sys.stderr)
            return 1
        print(f"{ser['short']} ({ser['id']}) is on the lab's conference list.")
    if args.unfollow:
        try:
            kept = conf.series_remove(ws, args.unfollow)
        except KeyError:
            print(f"Error: {args.unfollow} is not on the lab's conference list.", file=sys.stderr)
            return 1
        print(f"Took {args.unfollow} off the list" + (f"; its {kept} imported programs are kept and come back if you "
                                                       "add it again." if kept else "."))
    if args.remove:
        if not conf.remove(ws, args.remove):
            print(f"Error: no imported program with the ID {args.remove}.", file=sys.stderr)
            return 1
        print(f"Removed {args.remove}.")
        return 0
    if args.file:
        if not (args.name and args.year):
            print("Error: --name and --year are required with a program file.", file=sys.stderr)
            return 1
        try:
            rows = conf.read_rows(args.file)
            res = conf.add(ws, name=args.name, year=args.year, rows=rows, short=args.short or "", dates=args.dates or "",
                           location=args.location or "", url=args.url or "", note=args.note or "", actor=args.actor,
                           conf_id=args.id or "", series=args.series or "")
        except (OSError, ValueError, json.JSONDecodeError) as err:
            print(f"Error: {err}", file=sys.stderr)
            return 1
        print(f"{'Replaced' if res['replaced'] else 'Added'} {res['id']}: {res['appearances']} appearances.")
    packet = ws.load_packet()
    nb = Notebook(packet, ws.read_events(), packet.root_ids)
    data = conf.load(ws)
    confs = conf.matched(nb, data)
    listed = data.get("series", [])
    print("The lab's conferences: " + (", ".join(f"{x['short']} ({x['id']})" for x in listed) if listed else
                                       "none yet (add one with --follow SHORT --name NAME --url WEBSITE)."))
    if not confs:
        print("No programs imported for them yet.")
        return 0
    for c in confs:
        print(f"{c['id']}: {c['name']} ({c['year']}{', ' + c['location'] if c.get('location') else ''}). "
              f"{c['count']} appearances by {c['names']} people; {c['inNetwork']} of them are in the network"
              + (f"; {c['ambiguous']} names fit several people" if c["ambiguous"] else "") + ".")
        if args.file and c["id"] == res["id"]:
            for a in c["appearances"]:
                if a["basis"] == "ambiguous":
                    names = ", ".join(nb.contact(x)["name"] for x in a["candidates"])
                    print(f"  ambiguous: {a['name']} ({a.get('institution') or 'no institution'}) could be {names}")
    return 0


def cmd_schedule(args):
    from . import schedule
    # Turning updates on needs a real workspace; turning them off works even after the workspace is gone.
    if args.every in ("week", "month") and (Workspace(args.workspace).config.get("settings") or {}).get("demo"):
        print("Error: this is the fictional demo lab; there is nothing to update.", file=sys.stderr)
        return 1
    if args.every == "off":
        print(schedule.uninstall(args.workspace, args.job))
        return 0
    if args.every == "status":
        print(schedule.status(args.workspace, args.job))
        return 0
    try:
        p = schedule.plan(args.workspace, args.every, args.scope, job=args.job, day=args.day, at=args.at)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    what = ("library update: new papers and awards for people within the degree limit" if args.job == "library"
            else f"refresh for people within {args.scope} degree(s)")
    print(f"Platform: {p['platform']}. Runs {p['when']}: {what}.")
    if p.get("file"):
        print(f"File to write: {p['file']}\n{p['content']}")
    elif p.get("content"):
        print(f"Crontab line to add:\n{p['content']}")
    if isinstance(p["install"], list):
        print("Command: " + " ".join(p["install"][0]))
    if not args.yes:
        print("\nNothing was installed. Re-run with --yes to install it; turn it off later with --every off.")
        return 0
    schedule.install(p)
    print("Installed. Logs go to " + p["logs"])
    return 0


def cmd_config(args):
    ws = Workspace(args.workspace)
    settings = ws.config.setdefault("settings", {})
    if args.degree_limit is not None or args.live_check is not None:
        running = ws.server_running()
        if running:
            print(f"Error: a Lab Network server (port {running.get('port')}) is using this workspace. "
                  "Stop it, change the setting, then start it again.", file=sys.stderr)
            return 1
    if args.live_check is not None:
        settings["liveCheck"] = args.live_check == "on"
        ws.save_config()
        print("Saved.")
    if args.degree_limit is not None:
        if args.degree_limit == "none":
            settings.pop("degreeLimit", None)
        elif args.degree_limit.isdigit() and int(args.degree_limit) >= 1:
            settings["degreeLimit"] = int(args.degree_limit)
        else:
            print("Error: --degree-limit takes a whole number of at least 1, or none", file=sys.stderr)
            return 1
        ws.save_config()
        print("Saved.")
    limit = settings.get("degreeLimit")
    print(f"Degree limit: {limit if limit else 'none (every degree is shown)'}")
    print(f"Live check on profile pages: {'off' if settings.get('liveCheck') is False else 'on'}")
    return 0


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def demo_workspace(target: str) -> Workspace:
    """The demo lab's workspace: created from the bundled packet, with the bundled research library and digest.

    The demo is marked as fictional, which turns off library runs, schedules and live checks for it. A folder that
    holds another lab's workspace is refused; a demo workspace made from an earlier demo packet is set aside and made
    again.
    """
    from .packet import load_packet
    if os.path.exists(os.path.join(target, "workspace.json")):
        ws = Workspace(target)
        packet = os.path.realpath(ws.packet_dir())       # as the workspace resolves it, then without symlinks
        if packet != os.path.realpath(DEMO_PACKET):       # decided by the packet, never by a setting alone
            raise WorkspaceError(f"{target} holds the workspace of a real lab. Run the demo in a new folder, or "
                                 "leave the folder out to use the demo-workspace folder in this kit.")
        if (ws.config.get("packet") or {}).get("sha256") != load_packet(DEMO_PACKET).sha256:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            os.replace(target, f"{target}-old-{stamp}")
            print(f"The demo has changed since {target} was made; kept the old one as {target}-old-{stamp}")
    if not os.path.exists(os.path.join(target, "workspace.json")):
        Workspace.create(target, DEMO_PACKET, lab_name="Demo Lab (fictional data)")
        print(f"Created demo workspace at {target}")
    ws = Workspace(target)
    if not ws.config.setdefault("settings", {}).get("demo"):
        ws.config["settings"]["demo"] = True
        ws.save_config()
    copies = [(DEMO_LIBRARY, os.path.join(target, "library.json"))]
    if os.path.isdir(DEMO_DIGESTS):
        copies += [(os.path.join(DEMO_DIGESTS, n), os.path.join(target, "digests", n)) for n in sorted(os.listdir(DEMO_DIGESTS))]
    for src, dst in copies:
        if os.path.isfile(src) and (not os.path.exists(dst) or _read(src) != _read(dst)):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
    return ws


def cmd_site(args):
    import tempfile
    import urllib.error
    from .site import SiteError, build
    if not args.demo and not args.workspace:
        print("Error: name the workspace to share, or use --demo for the fictional demo lab.", file=sys.stderr)
        return 2
    tmp = None
    try:
        target = args.workspace
        if args.demo:              # a fresh demo, so nothing typed into the demo app is published with it
            tmp = tempfile.mkdtemp(prefix="labnetwork-demo-")
            target = os.path.join(tmp, "demo-workspace")
            demo_workspace(target)
        res = build(target, args.out, how_to_add=args.how_to_add or "", force=args.force)
    except (SiteError, WorkspaceError, PacketError, OSError, urllib.error.URLError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    if not args.demo:
        print("This copy holds the lab's notebook. Put it online only behind a sign-in that admits just your lab")
        print("(see docs/hosting.md), and check that a signed-out browser cannot open it.")
    print(f"To look at it on this computer first: python3 labnetwork.py site-preview {res['outDir']}")
    return 0


def cmd_site_preview(args):
    from .site import SiteError, preview
    try:
        return preview(args.folder, port=args.port, open_browser=args.open)
    except SiteError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_demo(args):
    target = os.path.abspath(args.workspace or os.path.join(HERE, "demo-workspace"))
    demo_workspace(target)
    args.workspace = target
    return cmd_serve(args)


def main(argv=None):
    p = argparse.ArgumentParser(prog="labnetwork", description="A private map and record of a lab's professional network.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("validate", help="check a data packet's integrity, counts and references")
    s.add_argument("packet")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("init", help="create a workspace for edits beside a read-only packet")
    s.add_argument("packet")
    s.add_argument("workspace")
    s.add_argument("--lab-name", default="")
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("serve", help="run the app on http://127.0.0.1")
    s.add_argument("workspace")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="open the browser")
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("export", help="write an export file")
    s.add_argument("workspace")
    s.add_argument("--format", default="bundle",
                   choices=["bundle", "notebook", "contacts-csv", "notes-csv", "ties-csv"])
    s.add_argument("--out")
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("import", help="preview (and with --yes apply) an import file")
    s.add_argument("workspace")
    s.add_argument("file")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--decisions", help="JSON file mapping item keys to merge:<id>, create or skip")
    s.add_argument("--actor", default="")
    s.set_defaults(fn=cmd_import)
    s = sub.add_parser("onboard", help="set up a new lab: questionnaire, identity checks, build, workspace")
    s.add_argument("labdir", help="folder for this lab's files (lab.json, packet/, workspace/)")
    s.add_argument("--non-interactive", action="store_true", help="never prompt (for AI agents)")
    s.add_argument("--rebuild", action="store_true", help="build a new packet even if one exists")
    s.set_defaults(fn=cmd_onboard)
    s = sub.add_parser("refresh", help="add new papers, coauthors and awards for people near the lab")
    s.add_argument("workspace")
    s.add_argument("--scope", type=int, default=1, choices=[1, 2], help="degrees of connection to update (default 1)")
    s.add_argument("--since", help="YYYY-MM-DD; default is the last refresh")
    s.add_argument("--dry-run", action="store_true", help="show what would change without saving")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_refresh)
    s = sub.add_parser("expand", help="add every coauthor of the lab members, and each first-degree person's "
                       "coauthors on two or more works since the start year")
    s.add_argument("workspace")
    s.add_argument("--since", type=int, default=2016, help="count works from this year (default 2016)")
    s.add_argument("--per-person", type=int, default=None,
                   help="at most this many new people per first-degree person (default: no limit)")
    s.add_argument("--min-shared", type=int, default=2, help="shared works a new person needs (default 2)")
    s.add_argument("--only", help="comma-separated IDs or names of first-degree people to expand")
    s.add_argument("--budget", type=float, default=None,
                   help="stop before OpenAlex spending passes this many US dollars (default 0.90 with an OpenAlex key, "
                        "else 0.08: within the daily allowance)")
    s.add_argument("--dry-run", action="store_true", help="show what would change without saving")
    s.set_defaults(fn=cmd_expand)
    s = sub.add_parser("library", help="build or update the research library: OpenAlex records and recent papers, "
                       "NIH and NSF awards for people within the degree limit")
    s.add_argument("workspace")
    s.add_argument("--update", action="store_true", help="add what is new since the last run and write a digest")
    s.add_argument("--budget", type=float, default=None, help="OpenAlex spending limit for this run in US dollars "
                   "(default 0.08, or 0.90 with an OpenAlex API key); the run also stops before today's free "
                   "allowance runs out")
    s.add_argument("--no-nih", action="store_true", help="skip NIH RePORTER")
    s.add_argument("--no-nsf", action="store_true", help="skip NSF awards")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_library)
    s = sub.add_parser("intake", help="add lab members' Slack submissions: set up, check now, or show status")
    s.add_argument("workspace")
    s.add_argument("--setup", action="store_true", help="connect a Slack channel (needs --channel and a bot token)")
    s.add_argument("--channel", help="the private channel's name, such as '#lab-network'")
    s.add_argument("--token-file", help="file that holds the bot token (default ~/.config/lab-network/slack-token)")
    s.add_argument("--token-key", help="with --token-file: the key that holds the token in a JSON file")
    s.add_argument("--mode", choices=["automatic", "review"],
                   help="automatic: add submissions as they arrive; review: each waits on the Data page")
    s.add_argument("--check", action="store_true", help="read the channel now")
    s.add_argument("--forget-form", action="store_true",
                   help="stop trusting the app that posts the lab's form (its next form waits for Trust this form)")
    s.add_argument("--member", action="append", metavar="SLACK_ID=PERSON_ID",
                   help="add or correct a lab member's Slack account, such as U0123ABCD=maria-rivera (repeatable; "
                        "leave the person empty to remove someone)")
    s.set_defaults(fn=cmd_intake)
    s = sub.add_parser("undo-import", help="archive what one import batch created and remove what it added")
    s.add_argument("workspace")
    s.add_argument("batch", help="the batch ID, such as imp-...")
    s.add_argument("--actor", default="undo-import")
    s.set_defaults(fn=cmd_undo_import)
    s = sub.add_parser("not-theirs", help="mark an OpenAlex author record as not a person's (or show their records)")
    s.add_argument("workspace")
    s.add_argument("person", help="the person's ID, or their exact name")
    s.add_argument("record", nargs="?", help="the OpenAlex author record's ID or URL; leave it out to list the "
                   "person's records, the records held back from them and the records marked as not theirs")
    s.add_argument("--note", default="", help="why it is not theirs, such as 'her husband's record'")
    s.add_argument("--undo", action="store_true", help="withdraw an earlier mark")
    s.add_argument("--actor", default="command line", help="who made the decision (recorded in the change log)")
    s.set_defaults(fn=cmd_not_theirs)
    s = sub.add_parser("conference", help="import a conference program (CSV or JSON), list programs, or remove one")
    s.add_argument("workspace")
    s.add_argument("file", nargs="?", help="the program: one row per appearance with name, and optionally institution, "
                   "role, title, session, when and sourceUrl")
    s.add_argument("--name", help="the meeting's full name, such as 'AACAP 70th Annual Meeting'")
    s.add_argument("--short", help="its short name, such as AACAP")
    s.add_argument("--year", type=int)
    s.add_argument("--dates", help="such as 2026-10-26 to 2026-10-31")
    s.add_argument("--location")
    s.add_argument("--url", help="the program's web page, used as the source of every appearance without its own")
    s.add_argument("--note", help="how the file was made, such as 'speakers and chairs from the online program'")
    s.add_argument("--id", help="the program's ID (default: from the short name and year); an existing ID is replaced")
    s.add_argument("--remove", metavar="ID", help="remove an imported program")
    s.add_argument("--series", help="the conference on the lab's list this program belongs to (default: its short name)")
    s.add_argument("--follow", metavar="SHORT", help="put a conference on the lab's list (with --name, --url and "
                   "--frequency); the Conferences page lists only these")
    s.add_argument("--unfollow", metavar="ID", help="take a conference off the list; its programs are kept")
    s.add_argument("--frequency", help="such as 'annual' or 'biennial, odd years'")
    s.add_argument("--actor", default="command line", help="who imported it")
    s.set_defaults(fn=cmd_conference)
    s = sub.add_parser("schedule", help="turn weekly or monthly updates on or off")
    s.add_argument("workspace")
    s.add_argument("--every", required=True, choices=["week", "month", "off", "status"])
    s.add_argument("--job", default="library", choices=["library", "refresh"],
                   help="library: new papers and awards (default); refresh: the older profile update")
    s.add_argument("--day", default="mon", choices=["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    s.add_argument("--at", default="08:00", help="time of day, HH:MM (default 08:00)")
    s.add_argument("--scope", type=int, default=1, choices=[1, 2], help="degrees for --job refresh")
    s.add_argument("--yes", action="store_true", help="install it (otherwise only print the plan)")
    s.set_defaults(fn=cmd_schedule)
    s = sub.add_parser("config", help="show or change workspace settings")
    s.add_argument("workspace")
    s.add_argument("--degree-limit", help="furthest degree the app shows (a number, or none). Set it to the deepest "
                   "degree whose connections were collected.")
    s.add_argument("--live-check", choices=["on", "off"], help="whether a profile page checks OpenAlex and NIH for "
                   "papers and awards newer than the last library update (default on)")
    s.set_defaults(fn=cmd_config)
    s = sub.add_parser("site", help="build a read-only copy of the lab to share online behind a sign-in")
    s.add_argument("workspace", nargs="?", help="the workspace to share (or --demo)")
    s.add_argument("--out", required=True, help="a new or empty folder for the copy, outside the Lab Network folder")
    s.add_argument("--how-to-add", default="", help="one sentence the copy shows about where lab members send "
                   "additions, for example: Post new people and notes in #lab-network on Slack.")
    s.add_argument("--demo", action="store_true", help="share the fictional demo lab instead (safe to make public)")
    s.add_argument("--force", action="store_true", help="replace an earlier copy in the same folder")
    s.set_defaults(fn=cmd_site)
    s = sub.add_parser("site-preview", help="look at a shared copy on this computer before putting it online")
    s.add_argument("folder")
    s.add_argument("--port", type=int, default=8790)
    s.add_argument("--open", action="store_true")
    s.set_defaults(fn=cmd_site_preview)
    s = sub.add_parser("demo", help="run the app on the bundled fictional demo lab")
    s.add_argument("workspace", nargs="?")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true")
    s.set_defaults(fn=cmd_demo)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (PacketError, WorkspaceError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
