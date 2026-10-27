"""Exports: a complete bundle for round trips, the packet format, and CSV views."""
from __future__ import annotations

import copy
import csv
import io
import json
import re
from typing import Any, Dict, List, Tuple

from .store import utc_now

FORMATS = {
    "bundle": ("labnetwork-bundle.json", "application/json"),
    "notebook": ("notebook.json", "application/json"),
    "contacts-csv": ("contacts.csv", "text/csv"),
    "notes-csv": ("notes.csv", "text/csv"),
    "ties-csv": ("relationships.csv", "text/csv"),
}

_FORMULA_START = ("=", "+", "@", "\t", "\r")


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        v = "; ".join(str(x) for x in v)
    s = str(v)
    # Spreadsheet formula guard; the CSV importer strips this quote again.
    return "'" + s if s.startswith(_FORMULA_START) else s


def _csv(rows: List[List[Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow([_cell(x) for x in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def bundle(svc) -> Dict[str, Any]:
    nb = svc.nb
    archived = {coll: [copy.deepcopy(nb.records[coll][rid]) for rid in sorted(nb.archived[coll])]
                for coll in ("contacts", "edges", "meetings")}
    return {
        "format": "labnetwork-bundle",
        "formatVersion": 1,
        "exportedAt": utc_now(),
        "app": {"name": "Lab Network", "version": svc.state()["app"]["version"]},
        "workspaceId": svc.ws.config.get("workspaceId"),
        "labName": svc.ws.config.get("labName", ""),
        "basePacket": {"sha256": svc.packet.sha256, "bytes": svc.packet.size,
                       "datasetStatus": svc.report.get("datasetStatus"), "savedAt": svc.report.get("savedAt")},
        "roots": copy.deepcopy(svc.packet.roots),
        "counts": nb.counts(),
        "notebook": nb.to_notebook(),
        "archived": archived,
        "labnetwork": {
            "events": copy.deepcopy(nb.events),
            "conflicts": list(copy.deepcopy(nb.conflicts).values()),
            "noteMeta": copy.deepcopy(dict(nb.note_meta)),
            "aliases": dict(nb.aliases),
        },
    }


def export(svc, fmt: str) -> Tuple[str, str, bytes]:
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    fname, ctype = FORMATS[fmt]
    stamp = utc_now()[:10]
    lab = re.sub(r"[^a-z0-9]+", "-", (svc.ws.config.get("labName") or "labnetwork").lower()).strip("-") or "labnetwork"
    fname = f"{lab}-{stamp}-{fname}"
    with svc.lock:
        nb = svc.nb
        if fmt == "bundle":
            return fname, ctype, json.dumps(bundle(svc), ensure_ascii=False, indent=1).encode("utf-8")
        if fmt == "notebook":
            return fname, ctype, json.dumps(nb.to_notebook(), ensure_ascii=False, indent=1).encode("utf-8")
        names = svc.names()
        if fmt == "contacts-csv":
            dist, _ = svc.lab_dist()
            cols = ["id", "name", "institution", "role", "lab", "email", "website", "tags", "doctorateStatus",
                    "researchSummary", "researchSourceUrl", "profileSourceUrl", "photoUrl", "photoSourceUrl"]
            rows = [cols + ["computedDegree", "origin", "editedFields"]]
            for rid, c in nb.active("contacts"):
                rows.append([c.get(k, "") for k in cols] +
                            [dist.get(rid, ""), nb.origin("contacts", rid), nb.edited_fields("contacts", rid)])
            return fname, ctype, _csv(rows)
        if fmt == "notes-csv":
            cols = ["id", "contactId", "metBy", "date", "context", "learning", "followUp", "recordedBy", "createdAt"]
            rows = [cols + ["contactName", "followUpStatus"]]
            for mid, m in nb.active("meetings"):
                rows.append([m.get(k, "") for k in cols] +
                            [names.get(m.get("contactId"), ""), nb.note_meta.get(mid, {}).get("followUpStatus", "")])
            return fname, ctype, _csv(rows)
        cols = ["id", "from", "to", "type", "provenance", "confidence", "status", "relationshipLabel",
                "relationshipStart", "relationshipEnd", "relationshipStatus", "sourceTitle", "sourceUrl",
                "evidence", "checkedAt", "projectId", "projectTitle", "attributionScope"]
        rows = [cols + ["fromName", "toName"]]
        for eid, e in nb.active("edges"):
            rows.append([e.get(k, "") for k in cols] + [names.get(e["from"], ""), names.get(e["to"], "")])
        return fname, ctype, _csv(rows)


TEMPLATES = {
    "contacts.csv": [["id", "name", "institution", "role", "email", "website", "researchSummary",
                      "researchSourceUrl", "profileSourceUrl", "photoUrl", "photoSourceUrl", "doctorateStatus",
                      "doctorateEvidence", "doctorateSourceUrl", "doctorateCheckedAt", "tags", "notes"],
                     ["", "Example Person (delete this row)", "Example University", "Associate Professor",
                      "", "https://example.edu/people/example", "Studies example topics.",
                      "https://example.edu/people/example", "https://example.edu/people/example", "", "",
                      "", "", "", "", "met-at-conference; example", ""]],
    "notes.csv": [["contactId", "contactName", "metBy", "date", "context", "learning", "followUp", "recordedBy"],
                  ["", "Example Person (delete this row)", "Your Name", "2026-10-10",
                   "Poster session at a conference", "Their new longitudinal cohort", "Send our preprint",
                   "Your Name"]],
    "relationships.csv": [["from", "to", "type", "provenance", "confidence", "status", "evidence", "sourceTitle",
                           "sourceUrl", "relationshipLabel", "relationshipStart", "relationshipEnd",
                           "relationshipStatus", "checkedAt"],
                          ["person-id-a", "person-id-b", "collaborator", "reported", "likely", "proposed",
                           "Delete this example row. Describe what shows the two people are connected.", "",
                           "", "", "", "", "unknown", ""]],
}


def template(name: str) -> bytes:
    if name not in TEMPLATES:
        raise KeyError(name)
    return _csv(TEMPLATES[name])
