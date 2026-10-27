"""Structured onboarding: lab.json questionnaire -> confirmed identities -> packet -> workspace -> research library
-> weekly update.

Each step is skipped when its output already exists, so the command can be re-run
safely. With --non-interactive (for AI agents) the command never prompts; it stops
with a clear message and writes identity-candidates.json when a person has to choose,
and it never installs the weekly update (it prints the command instead).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

from . import library as libmod
from . import schedule
from .builder import BuildError, build, load_lab, resolve_members, save_lab, settings_of
from .connectors import openalex as oa
from .connectors.http import Client, HttpError
from .store import Workspace

TEMPLATE = {
    "lab": {"name": "", "institution": "", "website": "", "contactEmail": ""},
    "members": [
        {"name": "", "role": "", "institution": "", "orcid": "", "scholarUrl": "", "facultyPage": "",
         "nihProfileId": "", "photoUrl": "", "photoSourceUrl": "", "researchSummary": "", "openalexId": ""}
    ],
    "relationships": [
        {"member": "(id of a lab member, filled in after the first run)", "name": "", "type": "mentor",
         "direction": "they-mentored-member", "evidence": "", "start": "", "end": "", "status": "historical",
         "label": "", "institution": "", "url": ""}
    ],
    "settings": {"sinceYear": None, "maxCoauthorsPerMember": 40, "secondDegree": True, "maxNewPerBridge": 5,
                 "maxPeople": 1500, "library": True, "weeklyUpdate": {"day": "mon", "at": "08:00"}},
}


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or default


def ask_yes(prompt: str, default: str = "y") -> bool:
    return ask(prompt + " (y/n)", default).lower().startswith("y")


def confirm(prompt: str) -> bool:
    """A yes typed by a person: Enter accepts, but closed input (a script, a pipe that ran dry) never does."""
    try:
        val = input(f"{prompt} (y/n) [y]: ").strip().lower()
    except EOFError:
        print()
        return False
    return val in ("", "y", "yes")


def ask_day(prompt: str, default: str = "mon") -> str:
    while True:
        day = ask(prompt + " (" + ", ".join(schedule.DAYS) + ")", default).lower()[:3]
        if day in schedule.DAYS:
            return day
        print("    Type the first three letters of a day, such as mon.")


def ask_time(prompt: str, default: str = "08:00") -> str:
    while True:
        at = ask(prompt + " (24-hour HH:MM)", default)
        try:
            hour, minute = schedule.parse_time(at)
            return f"{hour:02d}:{minute:02d}"
        except ValueError:
            print("    Type a time such as 08:00 or 19:30.")


def library_questions(settings: Dict[str, Any]) -> None:
    print("\nThe research library keeps each researcher's OpenAlex publication record, their recent papers, and their")
    print("NIH and NSF awards, for the researchers in the network. To build it, Lab Network sends their names to")
    print("OpenAlex, NIH RePORTER and NSF.")
    settings["library"] = ask_yes("Build the research library")
    if not settings["library"]:
        settings["weeklyUpdate"] = False
        return
    if not oa.api_key():
        print("Without an OpenAlex API key, OpenAlex allows $0.10 of requests a day (about 1,000 paper lists), so a")
        print("large library fills over several runs: each run stops before the day's allowance runs out, and each")
        print("weekly update continues it. A free key raises the allowance to $1 a day, enough for most libraries in")
        print(f"one run. Register at openalex.org, then save the key as one line in {oa.KEY_FILE}.")
    if ask_yes("Check for new papers and awards once a week on this computer"):
        settings["weeklyUpdate"] = {"day": ask_day("  Day"), "at": ask_time("  Time")}
    else:
        settings["weeklyUpdate"] = False


def questionnaire() -> Dict[str, Any]:
    print("\nLab Network onboarding. Press Enter to skip any optional question.\n")
    lab = {"lab": {}, "members": [], "relationships": [], "settings": {}}
    lab["lab"]["name"] = ask("Lab name (for example: Rivera Lab)")
    lab["lab"]["institution"] = ask("Institution")
    lab["lab"]["website"] = ask("Lab website URL (optional)")
    lab["lab"]["contactEmail"] = ask("Your email, sent to OpenAlex so it can contact you about heavy use (optional)")
    print("\nLab members. Start with the PI. Leave the name empty to finish.")
    while True:
        name = ask(f"  Member {len(lab['members']) + 1} full name")
        if not name:
            break
        m = {"name": name, "role": ask("    Current position (for example: Associate Professor)"),
             "orcid": ask("    ORCID iD (best way to find their papers; optional)"),
             "scholarUrl": ask("    Google Scholar profile URL (optional)"),
             "facultyPage": ask("    Faculty or lab profile page URL (optional)"),
             "nihProfileId": ask("    NIH RePORTER PI profile ID (optional)"),
             "photoUrl": ask("    Photo image URL (optional)")}
        if m["photoUrl"]:
            m["photoSourceUrl"] = ask("    Page that shows the photo is them (optional)")
        lab["members"].append({k: v for k, v in m.items() if v})
    if not lab["members"]:
        raise BuildError("At least one lab member is needed.")
    since = ask("\nInclude papers since which year", str(_dt.date.today().year - 10))
    lab["settings"]["sinceYear"] = int(since) if since.isdigit() else None
    lab["settings"]["secondDegree"] = ask("Also add coauthors of your coauthors (y/n)", "y").lower().startswith("y")
    print("\nPeople you know that papers will not show: mentors, trainees, committee members, fellowship peers.")
    print("Leave the name empty to finish. You can also add these later in the app.")
    names = [m["name"] for m in lab["members"]]
    while True:
        other = ask("  Their full name")
        if not other:
            break
        who = ask(f"    Which lab member knows them ({'; '.join(names)})", names[0])
        typ = ask("    Relationship (mentor, postdoc, committee, training, collaborator, labmate, irta_colleague, met)", "mentor")
        rel = {"memberName": who, "name": other, "type": typ}
        if typ == "mentor":
            rel["direction"] = "they-mentored-member" if ask("    Did they mentor the lab member (y/n)", "y").lower().startswith("y") else "member-mentored-them"
        elif typ == "postdoc":
            rel["direction"] = "member-was-their-postdoc" if ask("    Was the lab member their postdoc (y/n)", "y").lower().startswith("y") else "they-were-member-postdoc"
        elif typ == "committee":
            rel["direction"] = "they-served-on-member-committee" if ask("    Did they serve on the lab member's committee (y/n)", "y").lower().startswith("y") else "member-served-on-their-committee"
        rel["evidence"] = ask("    In a sentence, what is the connection (for example: PhD advisor at Duke)")
        rel["start"] = ask("    Start year (optional)")
        rel["end"] = ask("    End year (optional)")
        rel["institution"] = ask("    Their current institution (optional)")
        lab["relationships"].append({k: v for k, v in rel.items() if v})
    library_questions(lab["settings"])
    lab["conferences"] = conference_questions()
    return lab


def conference_questions() -> List[Dict[str, str]]:
    """The conferences the lab attends. The Conferences page lists who in the network is on their programs."""
    print("\nConferences your lab attends or is considering, such as SRP or AACAP. The Conferences page shows who in")
    print("your network is on each one's program, once programs are imported. Leave the name empty to finish; you can")
    print("change the list later on the Conferences page.")
    out = []
    while True:
        short = ask("  Short name (for example: SRP)")
        if not short:
            return out
        c = {"short": short, "name": ask("    Full name (optional)"), "url": ask("    Website (optional)")}
        out.append({k: v for k, v in c.items() if v})


def apply_conferences(lab: Dict[str, Any], ws_dir: str, log=print) -> None:
    """Put lab.json's `conferences` on the workspace's list; ones already there keep their details."""
    from . import conferences as conf
    wanted = [c for c in lab.get("conferences") or [] if isinstance(c, dict) and c.get("short")]
    ws = Workspace(ws_dir)
    have = {x["id"] for x in conf.load(ws).get("series", [])}
    added = []
    for c in wanted:
        if conf.slug(c["short"]) in have:
            continue
        try:
            conf.series_add(ws, c["short"], c.get("name", ""), c.get("url", ""), c.get("frequency", ""), "onboarding")
            added.append(c["short"])
        except ValueError as exc:
            log(f"Conference {c['short']} left off the list: {exc}")
    if added:
        log("Conferences on the lab's list: " + ", ".join(added) + ". Import their programs with the conference "
            "command (see AGENTS.md).")
    elif not wanted and not have:
        log("No conferences listed. Add the ones your lab attends on the Conferences page, or under \"conferences\" "
            "in lab.json.")


def choose_interactively(member: Dict[str, Any], cands: List[Dict[str, Any]]) -> Optional[str]:
    print(f"\nWhich OpenAlex author is {member['name']}?")
    for i, c in enumerate(cands[:8], 1):
        print(f"  {i}. {c['name']}  {', '.join(c['lastKnownInstitutions'][:2]) or 'no institution'}  "
              f"{c['worksCount']} works  ORCID {c['orcid'] or 'none'}  topics: {', '.join(c['topics'][:3])}  {c['url']}")
    print("  0. None of these")
    pick = ask("Number (Enter to decide later)")
    if pick == "0":
        return "none"
    if pick.isdigit() and 1 <= int(pick) <= len(cands[:8]):
        return cands[int(pick) - 1]["openalexId"]
    return None


def run(labdir: str, interactive: bool = True, rebuild: bool = False, client: Optional[Client] = None,
        log=print) -> int:
    labdir = os.path.abspath(labdir)
    injected = client is not None
    os.makedirs(labdir, exist_ok=True)
    lab_path = os.path.join(labdir, "lab.json")
    if not os.path.exists(lab_path):
        if not interactive:
            with open(lab_path, "w", encoding="utf-8") as f:
                json.dump(TEMPLATE, f, indent=2)
            log(f"Wrote a blank {lab_path}. Fill it in (docs/onboarding.md explains each field) and run onboard again.")
            return 2
        save_lab(lab_path, questionnaire())
        log(f"Saved {lab_path}")
    lab = load_lab(lab_path)
    _weekly_time(lab.get("settings") or {})        # a mistyped day or time stops here, before any lookups
    _link_relationship_members(lab)
    client = client or Client(cache_dir=os.path.join(labdir, "cache"), mailto=(lab.get("lab") or {}).get("contactEmail", ""))
    log("Matching lab members to OpenAlex authors")
    report = resolve_members(lab, client, choose=choose_interactively if interactive else None)
    save_lab(lab_path, lab)
    for r in report["resolved"]:
        log(f"  {r['member']}: {r['openalexId']} (by {r['by']})")
    if report["needsConfirmation"]:
        cand_path = os.path.join(labdir, "identity-candidates.json")
        with open(cand_path, "w", encoding="utf-8") as f:
            json.dump(report["needsConfirmation"], f, indent=2, ensure_ascii=False)
        log(f"\n{len(report['needsConfirmation'])} member(s) need a person to confirm which OpenAlex author they are.")
        log(f"Candidates are in {cand_path}. Put the chosen ID in lab.json as members[].openalexId "
            "(or \"none\"), then run onboard again.")
        return 3
    packet_dir = os.path.join(labdir, "packet")
    if os.path.exists(os.path.join(packet_dir, "notebook.json")) and not rebuild:
        log("A packet already exists; skipping the build (use --rebuild to make a new one).")
    else:
        if os.path.exists(packet_dir) and rebuild:
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            os.replace(packet_dir, packet_dir + "-" + stamp)
            log(f"Kept the previous packet as packet-{stamp}")
        log("Building the packet (this reads public records; it can take a few minutes)")
        summary = build(lab, client, packet_dir, log=lambda m: log("  " + m))
        save_lab(lab_path, lab)
        log(f"Packet: {summary['people']} people, {summary['relationships']} relationship records "
            f"({summary['requests']} requests, {summary['cached']} from cache)")
    ws_dir = os.path.join(labdir, "workspace")
    if not os.path.exists(os.path.join(ws_dir, "workspace.json")):
        ws = Workspace.create(ws_dir, packet_dir, lab_name=lab["lab"]["name"])
        # Built networks rarely know doctorates or positions, so their default view does not filter on them.
        ws.config["settings"]["defaultFilters"] = {"maxDegree": "2", "doctorate": "any", "identified": "0"}
        # Show only the degrees the build collected: coauthors, and their coauthors unless turned off.
        second = ((lab.get("settings") or {}).get("secondDegree", True) is not False)
        ws.config["settings"]["degreeLimit"] = 2 if second else 1
        if (lab.get("lab") or {}).get("contactEmail"):
            ws.config["settings"]["contactEmail"] = lab["lab"]["contactEmail"]
        ws.save_config()
        log(f"Workspace created at {ws_dir}")
    else:
        log("Workspace already exists.")
    apply_conferences(lab, ws_dir, log)
    settings = settings_of(lab)                    # the start year defaults to ten years back, as in the build
    if settings.get("library", True) is False:
        log("\nResearch library: skipped (\"library\": false in lab.json). Build it later with:\n"
            f"  python3 labnetwork.py library \"{ws_dir}\"")
    else:
        build_library(ws_dir, settings, client if injected else None, log)
        weekly_update(ws_dir, settings, interactive, log)
    log("\nNext: python3 labnetwork.py serve \"" + ws_dir + "\" --open")
    return 0


def build_library(ws_dir: str, settings: Dict[str, Any], client: Optional[Client], log=print) -> Dict[str, Any]:
    """Build the research library, or continue a build that stopped; skip it when the last run finished."""
    lib = libmod.load(Workspace(ws_dir))
    last = (lib.get("runs") or [None])[-1]
    if last and not last.get("openalexStopped") and not (last.get("coverage") or {}).get("openalexPending"):
        log("\nResearch library: already built.")
        return {"skipped": True}
    log("\n" + ("Continuing" if last else "Building") + " the research library: OpenAlex publication records and recent "
        "papers, NIH and NSF awards.\nFor a large network this takes 10 to 40 minutes. You can stop it with Ctrl+C; if "
        "you run onboard\nor library again, it continues where it stopped.")
    try:
        res = libmod.run(ws_dir, since_year=settings.get("sinceYear"), client=client, log=lambda m: log("  " + m))
    except (libmod.LibraryError, HttpError) as exc:
        log(f"Research library: stopped ({exc}). Run onboard or library again to continue.")
        return {"error": str(exc)}
    cov = res["coverage"]
    if res["stopped"] or cov["openalexPending"]:
        log(f"The library is not complete yet: {cov['openalexPending']} people still to fetch from OpenAlex. "
            "The weekly update continues it,\nor run this tomorrow, after OpenAlex's daily allowance resets:\n"
            f"  python3 labnetwork.py library \"{ws_dir}\"")
    return res


def _weekly_time(settings: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """lab.json settings.weeklyUpdate: false, true, "sat 19:30" or {"day": "sat", "at": "19:30"}; None when unset."""
    w = settings.get("weeklyUpdate")
    if w is None or w is False:
        return w
    if isinstance(w, str):
        parts = w.split()
        w = {"day": parts[0] if parts else "mon", "at": parts[1] if len(parts) > 1 else "08:00"}
    if not isinstance(w, dict):
        w = {}
    day = str(w.get("day") or "mon").lower()[:3]
    if day not in schedule.DAYS:
        raise BuildError(f"settings.weeklyUpdate.day must be one of {', '.join(schedule.DAYS)}")
    try:
        hour, minute = schedule.parse_time(str(w.get("at") or "08:00"))
    except ValueError as exc:
        raise BuildError(f"settings.weeklyUpdate.at: {exc}")
    return {"day": day, "at": f"{hour:02d}:{minute:02d}"}


def weekly_update(ws_dir: str, settings: Dict[str, Any], interactive: bool, log=print) -> str:
    """Offer the weekly library update. Installs only in an interactive run, after showing the plan and asking."""
    try:
        current = schedule.status(ws_dir, "library")
    except Exception:                          # no scheduler on this system
        current = "unknown"
    if current.startswith("On"):
        log(f"\nWeekly update: {current}")
        return "on"
    w = _weekly_time(settings)
    when = w or {"day": "mon", "at": "08:00"}
    cmd = f"python3 labnetwork.py schedule \"{ws_dir}\" --every week --day {when['day']} --at {when['at']}"
    if w is False:
        log(f"\nWeekly update: off. To turn it on later:\n  {cmd} --yes")
        return "off"
    if not interactive:
        log("\nWeekly update: not installed (onboard installs nothing in non-interactive mode). To check for new "
            f"papers and awards every week,\nshow the user the plan this prints:\n  {cmd}\n"
            "and if they agree, run it again with --yes.")
        return "not-installed"
    p = schedule.plan(ws_dir, "week", job="library", day=when["day"], at=when["at"])
    log(f"\nWeekly update: runs {p['when']} on this computer, adds new papers and awards to the library, and writes "
        "a digest\nyou can read on the Data page."
        + (" A run missed while the Mac is asleep happens when it wakes." if p["platform"] == "macOS" else
           " A run missed while the computer is off or asleep waits for the next week."))
    if p.get("file"):
        log(f"It writes {p['file']}")
    elif p.get("content"):
        log(f"It adds this line to your crontab:\n  {p['content']}")
    elif isinstance(p.get("install"), list):
        log("It runs: " + " ".join(p["install"][0]))
    if not confirm("Install the weekly update now"):
        log(f"Not installed. To turn it on later:\n  {cmd} --yes")
        return "declined"
    try:
        schedule.install(p)
    except (OSError, subprocess.CalledProcessError) as exc:
        log(f"Could not install it ({exc}). To try again:\n  {cmd} --yes")
        return "failed"
    log(f"Installed. Logs go to {p['logs']}. To turn it off:\n"
        f"  python3 labnetwork.py schedule \"{ws_dir}\" --every off")
    return "installed"


def _link_relationship_members(lab: Dict[str, Any]) -> None:
    """Questionnaire answers name the member; the builder wants their ID."""
    from .service import slugify
    by_name = {m["name"].lower(): m for m in lab["members"]}
    for m in lab["members"]:
        m.setdefault("id", slugify(m["name"]))
    for r in lab.get("relationships") or []:
        if r.get("memberName") and not r.get("member"):
            m = by_name.get(r["memberName"].lower())
            if m:
                r["member"] = m["id"]
