"""Workspace storage: an append-only change log kept beside an immutable packet.

A workspace directory contains:
  workspace.json   which packet it builds on (path + SHA-256), lab roots, settings
  changes.jsonl    one JSON event per line; never rewritten, only appended
  imports/         a copy of every imported file and its preview report
  exports/         files written by `labnetwork export`

The current notebook is the packet with every event applied in order
(see model.py). Nothing in the packet directory is ever modified.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import uuid
from typing import Any, Dict, Iterable, List, Optional

from .packet import Packet, PacketError, load_packet, sha256_file

CONFIG_NAME = "workspace.json"
LOG_NAME = "changes.jsonl"
LOCK_NAME = ".labnetwork.lock"


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today() -> str:
    return _dt.date.today().isoformat()


class WorkspaceError(Exception):
    pass


class Workspace:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.config_path = os.path.join(self.path, CONFIG_NAME)
        self.log_path = os.path.join(self.path, LOG_NAME)
        self.imports_dir = os.path.join(self.path, "imports")
        self.exports_dir = os.path.join(self.path, "exports")
        self.lock_path = os.path.join(self.path, LOCK_NAME)
        if not os.path.isfile(self.config_path):
            raise WorkspaceError(f"{self.path} is not a Lab Network workspace (no {CONFIG_NAME}). "
                                 f"Create one with: labnetwork init <packet> <workspace>")
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config: Dict[str, Any] = json.load(f)
        self.quarantined: List[str] = []
        self._next_seq: Optional[int] = None

    # -- creation -----------------------------------------------------------

    @classmethod
    def create(cls, path: str, packet_dir: str, lab_name: str = "",
               roots: Optional[List[Dict[str, Any]]] = None) -> "Workspace":
        path = os.path.abspath(path)
        if os.path.exists(os.path.join(path, CONFIG_NAME)):
            raise WorkspaceError(f"{path} already holds a workspace")
        packet = load_packet(packet_dir, roots_override=roots)
        if os.path.commonpath([path, packet.path]) == packet.path:
            raise WorkspaceError("The workspace must not live inside the packet directory")
        os.makedirs(path, exist_ok=True)
        cfg = {
            "format": "labnetwork-workspace",
            "formatVersion": 1,
            "workspaceId": uuid.uuid4().hex,
            "createdAt": utc_now(),
            "labName": lab_name,
            "packet": {
                "path": os.path.relpath(packet.path, path),
                "sha256": packet.sha256,
                "bytes": packet.size,
            },
            "roots": roots,
            "settings": {},
        }
        with open(os.path.join(path, CONFIG_NAME), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        open(os.path.join(path, LOG_NAME), "a").close()
        return cls(path)

    # -- packet -------------------------------------------------------------

    def packet_dir(self) -> str:
        p = self.config["packet"]["path"]
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(self.path, p))

    def load_packet(self) -> Packet:
        packet = load_packet(self.packet_dir(), roots_override=self.config.get("roots"))
        expected = self.config["packet"]["sha256"]
        if packet.sha256 != expected:
            raise PacketError(
                "notebook.json no longer matches the copy this workspace was built on "
                f"(expected {expected[:12]}..., found {packet.sha256[:12]}...). The packet must stay "
                "unchanged; restore it, or start a new workspace for the new packet.")
        return packet

    def save_config(self) -> None:
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, self.config_path)

    # -- change log ---------------------------------------------------------

    def read_events(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.log_path):
            return []
        events = []
        with open(self.log_path, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
        bad_tail = []
        for n, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                # Only an interrupted final write is recoverable; corruption
                # earlier in the file stops the load so nothing is lost silently.
                if any(x.strip() for x in lines[n + 1:]):
                    raise WorkspaceError(f"{self.log_path} line {n + 1} is not valid JSON; "
                                         "the change log needs manual repair")
                bad_tail.append(line)
        if bad_tail:
            self._quarantine_tail(bad_tail, events)
        self._next_seq = (events[-1]["seq"] + 1) if events else 1
        return events

    def _quarantine_tail(self, bad_tail: List[str], good: List[Dict[str, Any]]) -> None:
        stamp = utc_now().replace(":", "")
        qpath = os.path.join(self.path, f"changes.partial-{stamp}.jsonl")
        with open(qpath, "w", encoding="utf-8") as f:
            f.write("\n".join(bad_tail) + "\n")
        tmp = self.log_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for ev in good:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.log_path)
        self.quarantined.append(qpath)

    def append(self, events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if self._next_seq is None:
            self.read_events()
        out = []
        with open(self.log_path, "a", encoding="utf-8") as f:
            for ev in events:
                ev = dict(ev)
                ev["seq"] = self._next_seq
                ev.setdefault("uuid", uuid.uuid4().hex)
                ev.setdefault("ts", utc_now())
                self._next_seq += 1
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                out.append(ev)
            f.flush()
            os.fsync(f.fileno())
        return out

    # -- imports / exports --------------------------------------------------

    def save_import_copy(self, batch_id: str, filename: str, data: bytes) -> str:
        os.makedirs(self.imports_dir, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in os.path.basename(filename))[:80]
        dest = os.path.join(self.imports_dir, f"{batch_id}--{safe or 'upload'}")
        with open(dest, "wb") as f:
            f.write(data)
        return dest

    def save_import_report(self, batch_id: str, report: Dict[str, Any]) -> str:
        os.makedirs(self.imports_dir, exist_ok=True)
        dest = os.path.join(self.imports_dir, f"{batch_id}.report.json")
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1, ensure_ascii=False)
        return dest

    # -- single-writer lock -------------------------------------------------

    def acquire_lock(self, port: int) -> None:
        if os.path.exists(self.lock_path):
            try:
                with open(self.lock_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                pid = int(info.get("pid", 0))
            except (OSError, ValueError):
                pid = 0
            if pid and pid != os.getpid() and _pid_alive(pid):
                raise WorkspaceError(f"Another Lab Network server (pid {pid}, port {info.get('port')}) "
                                     "is using this workspace. Stop it first.")
        with open(self.lock_path, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "port": port, "since": utc_now()}, f)

    def server_running(self) -> Optional[Dict[str, Any]]:
        """The lock's details when a live server holds this workspace, else None."""
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                info = json.load(f)
            pid = int(info.get("pid", 0))
        except (OSError, ValueError):
            return None
        return info if pid and pid != os.getpid() and _pid_alive(pid) else None

    def release_lock(self) -> None:
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                if json.load(f).get("pid") == os.getpid():
                    os.remove(self.lock_path)
        except (OSError, ValueError):
            pass

    def backup_log(self) -> Optional[str]:
        """Copy changes.jsonl to backups/ (called before risky bulk operations)."""
        if not os.path.exists(self.log_path):
            return None
        bdir = os.path.join(self.path, "backups")
        os.makedirs(bdir, exist_ok=True)
        dest = os.path.join(bdir, f"changes-{utc_now().replace(':', '')}.jsonl")
        shutil.copy2(self.log_path, dest)
        return dest

    def log_fingerprint(self) -> str:
        return sha256_file(self.log_path) if os.path.exists(self.log_path) else ""


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
