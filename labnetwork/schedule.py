"""Opt-in weekly or monthly updates (the research library, or the older refresh) using the system's own scheduler.

Nothing is installed unless you pass --yes. Without it, the command prints the
exact file or command it would install, so you can review it first.
  macOS    a LaunchAgent in ~/Library/LaunchAgents (runs while you are logged in)
  Linux    one line in your crontab, marked with a comment
  Windows  a Task Scheduler task that runs a short launcher file (the task's command line is limited to 261
           characters, which a synced-drive path can pass)
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(HERE, "labnetwork.py")


DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
FULL_DAYS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday",
             "sat": "Saturday", "sun": "Sunday"}
JOBS = ("library", "refresh")


def parse_time(at: str) -> tuple:
    """'08:00' -> (8, 0); anything else is a ValueError with a message for people."""
    parts = (at or "").strip().split(":")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        hour, minute = int(parts[0]), int(parts[1])
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    raise ValueError(f"the time must be HH:MM on a 24-hour clock, such as 08:00 or 19:30 (got {at!r})")


def _when(every: str, day: str, hour: int, minute: int) -> str:
    return (f"{FULL_DAYS[day]}s at {hour:02d}:{minute:02d}" if every == "week"
            else f"the 1st of each month at {hour:02d}:{minute:02d}")


def _tag(ws_path: str) -> str:
    return hashlib.sha1(os.path.abspath(ws_path).encode("utf-8")).hexdigest()[:10]


def _python() -> str:
    """The python3 on PATH (a link package managers keep current across upgrades), else this interpreter. On Windows
    always this interpreter: python3 there can be the Microsoft Store stub, which opens the Store instead of running."""
    if os.name == "nt":
        return sys.executable
    return shutil.which("python3") or sys.executable


def _command(ws_path: str, scope: int, job: str = "refresh") -> List[str]:
    if job == "library":
        return [_python(), LAUNCHER, "library", os.path.abspath(ws_path), "--update", "--quiet"]
    return [_python(), LAUNCHER, "refresh", os.path.abspath(ws_path), "--scope", str(scope), "--quiet"]


def _label(job: str, tag: str) -> str:
    return f"org.labnetwork.{job}.{tag}"


def _system() -> str:
    return "macOS" if sys.platform == "darwin" else ("Windows" if os.name == "nt" else "cron")


def plan(ws_path: str, every: str, scope: int = 1, job: str = "refresh", day: str = "mon", at: str = "08:00",
         system: str = "") -> Dict[str, Any]:
    """What would be installed. `job` is library (papers and awards) or refresh; `day` and `at` set the weekly time.
    `system` (macOS, Windows or cron) defaults to this computer's; tests pass it to check the other plans."""
    system = system or _system()
    if every not in ("week", "month"):
        raise ValueError("every must be week or month")
    if job not in JOBS:
        raise ValueError(f"job must be one of {JOBS}")
    if day not in DAYS:
        raise ValueError(f"day must be one of {DAYS}")
    hour, minute = parse_time(at)
    tag = _tag(ws_path)
    cmd = _command(ws_path, scope, job)
    logs = os.path.join(os.path.abspath(ws_path), "logs")
    log = os.path.join(logs, f"{job}.log")
    when = _when(every, day, hour, minute)
    if system == "macOS":
        label = _label(job, tag)
        # launchd numbers weekdays from Sunday = 0; a run missed while the Mac was asleep happens when it wakes.
        cal = ({"Weekday": (DAYS.index(day) + 1) % 7, "Hour": hour, "Minute": minute} if every == "week"
               else {"Day": 1, "Hour": hour, "Minute": minute})
        plist = {"Label": label, "ProgramArguments": cmd, "StartCalendarInterval": cal, "RunAtLoad": False,
                 "StandardOutPath": log, "StandardErrorPath": log}
        path = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
        return {"platform": "macOS", "label": label, "file": path, "content": plistlib.dumps(plist).decode("utf-8"),
                "install": [["launchctl", "bootstrap", f"gui/{os.getuid()}", path]],
                "remove": [["launchctl", "bootout", f"gui/{os.getuid()}/{label}"]], "logs": logs, "when": when,
                "job": job}
    if system == "Windows":
        name = f"Lab Network {job} {tag}"
        sched = ["/SC", "WEEKLY", "/D", day.upper()[:3]] if every == "week" else ["/SC", "MONTHLY", "/D", "1"]
        launcher = _launcher_path(job, tag)
        # cmd.exe reads a batch file in the console's code page; chcp 65001 makes it read the paths as UTF-8.
        content = ("@echo off\r\nchcp 65001 >nul\r\n" + " ".join(f'"{c}"' for c in cmd) + f' >> "{log}" 2>&1\r\n')
        return {"platform": "Windows", "label": name, "file": launcher, "content": content,
                "install": [["schtasks", "/Create", "/F", "/TN", name, "/TR", f'"{launcher}"',
                             "/ST", f"{hour:02d}:{minute:02d}"] + sched],
                "remove": [["schtasks", "/Delete", "/F", "/TN", name]], "logs": logs, "when": when, "job": job}
    cron = (f"{minute} {hour} * * {(DAYS.index(day) + 1) % 7}" if every == "week" else f"{minute} {hour} 1 * *")
    line = cron + " " + " ".join(f"'{c}'" for c in cmd) + f" >> '{log}' 2>&1  # labnetwork:{job}:{tag}"
    return {"platform": "Linux/Unix (cron)", "label": f"labnetwork:{job}:{tag}", "file": None, "content": line,
            "install": "crontab-append", "remove": "crontab-remove", "logs": logs, "when": when, "job": job}


def _launcher_path(job: str, tag: str) -> str:
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(root, "labnetwork", "tasks", f"{job}-{tag}.cmd")


def install(p: Dict[str, Any]) -> None:
    os.makedirs(p["logs"], exist_ok=True)
    if p["platform"] == "macOS":
        os.makedirs(os.path.dirname(p["file"]), exist_ok=True)
        with open(p["file"], "w", encoding="utf-8") as f:
            f.write(p["content"])
        subprocess.run(p["remove"][0], capture_output=True)
        subprocess.run(p["install"][0], check=True)
    elif p["platform"] == "Windows":
        os.makedirs(os.path.dirname(p["file"]), exist_ok=True)
        with open(p["file"], "w", encoding="utf-8", newline="") as f:
            f.write(p["content"])
        subprocess.run(p["install"][0], check=True)
    else:
        current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        lines = [l for l in current.splitlines() if not l.endswith(p["label"])]
        lines.append(p["content"])
        subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)


def uninstall(ws_path: str, job: str = "refresh") -> str:
    tag = _tag(ws_path)
    if sys.platform == "darwin":
        label = _label(job, tag)
        path = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
        if os.path.exists(path):
            os.remove(path)
        return f"Removed {label}"
    if os.name == "nt":
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", f"Lab Network {job} {tag}"], capture_output=True)
        if os.path.exists(_launcher_path(job, tag)):
            os.remove(_launcher_path(job, tag))
        return "Removed the scheduled task"
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    lines = [l for l in current.splitlines() if not l.endswith(f"labnetwork:{job}:{tag}")]
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
    return "Removed the crontab line"


def status(ws_path: str, job: str = "refresh") -> str:
    """"On: Saturdays at 19:30" when the job is installed (with its time when the scheduler shows it), else "Not scheduled"."""
    tag = _tag(ws_path)
    if sys.platform == "darwin":
        path = os.path.expanduser(f"~/Library/LaunchAgents/{_label(job, tag)}.plist")
        if not os.path.exists(path):
            return "Not scheduled"
        try:
            with open(path, "rb") as f:
                cal = plistlib.load(f).get("StartCalendarInterval") or {}
            if "Weekday" in cal:
                return "On: " + _when("week", DAYS[(int(cal["Weekday"]) + 6) % 7], int(cal["Hour"]), int(cal["Minute"]))
            return "On: " + _when("month", "mon", int(cal["Hour"]), int(cal["Minute"]))
        except (OSError, ValueError, KeyError, plistlib.InvalidFileException):
            return f"On ({path})"
    if os.name == "nt":
        r = subprocess.run(["schtasks", "/Query", "/TN", f"Lab Network {job} {tag}"], capture_output=True, text=True)
        return "On (Task Scheduler)" if r.returncode == 0 else "Not scheduled"
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.endswith(f"labnetwork:{job}:{tag}"):
            f = line.split()
            try:
                if f[2] == "*":
                    return "On: " + _when("week", DAYS[(int(f[4]) + 6) % 7], int(f[1]), int(f[0]))
                return "On: " + _when("month", "mon", int(f[1]), int(f[0]))
            except (IndexError, ValueError):
                return "On (crontab)"
    return "Not scheduled"
