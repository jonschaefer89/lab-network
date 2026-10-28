"""Import files into a workspace without silent overwrites or silent drops.

Supported inputs:
  Lab Network bundle (.json)     produced by `export`; replays its change history
  Notebook (.json)              the packet format: {schemaVersion, contacts, edges, meetings, ...}
  Contacts CSV, Notes CSV, Relationships CSV   (columns documented in docs/data-format.md)

Every import is a two-step operation. preview() classifies each incoming row
as new, unchanged, fill (adds a value we lack), conflict (differs from a value
we hold), possible duplicate (no ID match but the same name, email or profile),
or rejected (with the reason). apply() writes only what the preview showed,
using the decisions the user made. Conflicting values are never written over
current values; each becomes an open conflict that keeps both values until
someone resolves it.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .model import KIND_TO_COLL, LIST_FIELDS, list_item_key, name_key, norm_text
from .packet import (check_contact, check_edge, check_meeting, entity_schema, sha256_bytes,
                     validate_schema)
from .store import utc_now

MAX_BYTES = 25 * 1024 * 1024     # for files sent through the app; the import command and expand read any size

CONTACT_COLUMNS = {
    "id": "id", "name": "name", "fullname": "name", "institution": "institution", "affiliation": "institution",
    "role": "role", "position": "role", "title": "role", "lab": "lab", "labname": "labName", "email": "email",
    "website": "website", "homepage": "website", "tags": "tags", "notes": "notes",
    "researchsummary": "researchSummary", "researchinterests": "researchSummary", "interests": "researchSummary",
    "researchsourceurl": "researchSourceUrl", "researchcheckedat": "researchCheckedAt",
    "profilesourceurl": "profileSourceUrl", "profileurl": "profileSourceUrl",
    "profilecheckedat": "profileCheckedAt", "profileconfidence": "profileConfidence",
    "photourl": "photoUrl", "photosourceurl": "photoSourceUrl", "photoattribution": "photoAttribution",
    "photocheckedat": "photoCheckedAt", "photoconfidence": "photoConfidence",
    "doctoratestatus": "doctorateStatus", "doctoratetype": "doctorateType",
    "doctorateevidence": "doctorateEvidence", "doctoratesourceurl": "doctorateSourceUrl",
    "doctoratecheckedat": "doctorateCheckedAt",
}
NOTE_COLUMNS = {
    "id": "id", "contactid": "contactId", "personid": "contactId", "contactname": "_contactName",
    "person": "_contactName", "name": "_contactName", "metby": "metBy", "date": "date", "context": "context",
    "howwemet": "context", "learning": "learning", "discussed": "learning", "whatwediscussed": "learning",
    "followup": "followUp", "recordedby": "recordedBy",
}
TIE_COLUMNS = {
    "id": "id", "from": "from", "to": "to", "type": "type", "provenance": "provenance",
    "confidence": "confidence", "status": "status", "sourcetitle": "sourceTitle", "sourceurl": "sourceUrl",
    "evidence": "evidence", "checkedat": "checkedAt", "relationshipstart": "relationshipStart",
    "relationshipend": "relationshipEnd", "relationshipstatus": "relationshipStatus",
    "relationshiplabel": "relationshipLabel", "projectid": "projectId", "projecttitle": "projectTitle",
    "attributionscope": "attributionScope",
}
CONTACT_DEFAULTS = {"institution": "", "role": "", "lab": "", "email": "", "website": "", "tags": [],
                    "notes": ""}
SKIP_COMPARE = {"id", "createdAt"}


class ImportError_(Exception):
    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details


def _hdr(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def parse(filename: str, data: bytes, size_limit: bool = True) -> Dict[str, Any]:
    if size_limit and len(data) > MAX_BYTES:
        raise ImportError_(f"File is larger than {MAX_BYTES // (1024 * 1024)} MB. Stop the app and import it from the "
                           "command line instead: python3 labnetwork.py import <workspace> <file>")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportError_("The file is not UTF-8 text. Save it as UTF-8 (CSV UTF-8 in Excel) and try again.")
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            obj = json.loads(text)
        except ValueError as exc:
            raise ImportError_(f"The JSON could not be parsed: {exc}")
        return _parse_json(obj)
    return _parse_csv(text)


def _parse_json(obj: Any) -> Dict[str, Any]:
    out = {"format": "", "contacts": [], "edges": [], "meetings": [], "events": None, "baseSha256": None,
           "rowErrors": [], "meta": {}}
    # "fieldnotes-bundle" is this app's earlier name for the same format.
    if isinstance(obj, dict) and obj.get("format") in ("labnetwork-bundle", "fieldnotes-bundle"):
        out["format"] = "bundle"
        nb = obj.get("notebook") or {}
        out["baseSha256"] = (obj.get("basePacket") or {}).get("sha256")
        out["events"] = (obj.get("labnetwork") or obj.get("fieldnotes") or {}).get("events") or []
        out["meta"] = {"exportedAt": obj.get("exportedAt"), "workspaceId": obj.get("workspaceId"),
                       "labName": obj.get("labName")}
    elif isinstance(obj, dict) and "contacts" in obj:
        out["format"] = "notebook"
        nb = obj
    elif isinstance(obj, list):
        out["format"] = "contacts-json"
        nb = {"contacts": obj}
    else:
        raise ImportError_("Unrecognised JSON: expected a Lab Network bundle, a notebook with 'contacts', "
                           "or a list of contacts")
    for coll in ("contacts", "edges", "meetings"):
        items = nb.get(coll) or []
        if not isinstance(items, list):
            raise ImportError_(f"'{coll}' must be a list")
        for i, rec in enumerate(items):
            if not isinstance(rec, dict):
                out["rowErrors"].append({"collection": coll, "row": i + 1, "errors": ["not an object"]})
                continue
            r = copy.deepcopy(rec)
            r["_row"] = i + 1
            out[coll].append(r)
    return out


def _parse_csv(text: str) -> Dict[str, Any]:
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ImportError_("The CSV file is empty")
    headers = [_hdr(h) for h in rows[0]]
    hs = set(headers)
    if "context" in hs or "howwemet" in hs:
        fmt, mapping, coll = "notes-csv", NOTE_COLUMNS, "meetings"
    elif {"from", "to", "type"} <= hs:
        fmt, mapping, coll = "ties-csv", TIE_COLUMNS, "edges"
    elif "name" in hs or "fullname" in hs:
        fmt, mapping, coll = "contacts-csv", CONTACT_COLUMNS, "contacts"
    else:
        raise ImportError_("Could not tell what this CSV holds. Contacts need a 'name' column, notes a "
                           "'context' column, relationships 'from', 'to' and 'type' columns.")
    unknown = [rows[0][i] for i, h in enumerate(headers) if h and h not in mapping]
    out = {"format": fmt, "contacts": [], "edges": [], "meetings": [], "events": None, "baseSha256": None,
           "rowErrors": [], "meta": {"ignoredColumns": unknown}}
    for n, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        rec: Dict[str, Any] = {"_row": n}
        for i, cell in enumerate(row):
            if i >= len(headers):
                continue
            field = mapping.get(headers[i])
            if not field:
                continue
            val = cell.strip()
            if len(val) > 1 and val[0] == "'" and val[1] in "=+@\t\r":
                val = val[1:]
            if field == "tags":
                rec["tags"] = [t.strip() for t in re.split(r"[;|]", val) if t.strip()]
            elif val:
                rec[field] = val
        out[coll].append(rec)
    return out


# ---------------------------------------------------------------------------


class Importer:
    def __init__(self, svc):
        self.svc = svc
        self.nb = svc.nb

    # -- preview ---------------------------------------------------------------

    def preview(self, filename: str, data: bytes, size_limit: bool = True) -> Dict[str, Any]:
        parsed = parse(filename, data, size_limit)
        digest = sha256_bytes(data)
        batch_id = ("imp-" + utc_now().replace(":", "").replace("-", "")[:15] + "-" + digest[:6] +
                    uuid.uuid4().hex[:4])
        previous = [dict(b, batchId=k) for k, b in self.nb.batches.items() if b.get("sha256") == digest]
        same_base = (parsed["format"] == "bundle" and parsed["baseSha256"] == self.svc.packet.sha256)
        items: List[Dict[str, Any]] = []
        for err in parsed["rowErrors"]:
            items.append({"key": f"{err['collection']}:{err['row']}", "collection": err["collection"],
                          "row": err["row"], "status": "rejected", "errors": err["errors"]})
        if same_base:
            items += self._preview_events(parsed["events"] or [])
            mode = "history"
        else:
            items += self._preview_records(parsed)
            mode = "records"
        summary: Dict[str, Dict[str, int]] = {}
        for it in items:
            s = summary.setdefault(it["collection"], {})
            s[it["status"]] = s.get(it["status"], 0) + 1
        needs = [it["key"] for it in items if it["status"] in ("possible-duplicate", "id-collision")]
        preview = {
            "batchId": batch_id, "mode": mode,
            "file": {"name": filename, "sha256": digest, "bytes": len(data), "format": parsed["format"]},
            "alreadyImported": previous, "meta": parsed["meta"], "summary": summary, "items": items,
            "needsDecision": needs,
            "explain": _explain(mode, parsed["format"], same_base, parsed.get("baseSha256")),
            "revision": self.nb.revision,
        }
        self.svc.pending_imports[batch_id] = {"preview": preview, "data": data, "parsed": parsed}
        return preview

    def _current_value(self, coll, rid, field):
        rec = self.nb.records[coll].get(rid)
        return rec.get(field) if rec else None

    def _validate_incoming(self, coll: str, rec: Dict[str, Any]) -> List[str]:
        clean = {k: v for k, v in rec.items() if not k.startswith("_")}
        errs = [f"{e['path'].replace('$.', '').replace('$', 'record')}: {e['message']}"
                for e in validate_schema(clean, entity_schema(self.svc.packet.schema, coll))]
        issues: List[Dict[str, str]] = []
        if coll == "contacts":
            check_contact(clean, issues)
        elif coll == "edges":
            check_edge(clean, issues)
        else:
            check_meeting(clean, issues)
        errs += [f"{i['field']}: {i['message']}" for i in issues if i["level"] == "error"]
        return errs

    def _field_changes(self, coll: str, rid: str, rec: Dict[str, Any]) -> List[Dict[str, Any]]:
        cur = self.nb.records[coll][rid]
        changes = []
        for f, v in rec.items():
            if f.startswith("_") or f in SKIP_COMPARE:
                continue
            c = cur.get(f)
            if f in LIST_FIELDS and isinstance(v, list):
                have = {list_item_key(f, x) for x in (c or [])}
                new_items, seen = [], set(have)
                for x in v:
                    k = list_item_key(f, x)
                    if k not in seen:
                        new_items.append(x)
                        seen.add(k)
                if new_items:
                    changes.append({"field": f, "action": "append", "items": new_items, "count": len(new_items)})
                continue
            if c == v or _empty(v):
                continue
            if _empty(c):
                changes.append({"field": f, "action": "fill", "current": c, "incoming": v})
            else:
                already = any(cf["status"] == "open" and cf.get("collection") == coll and cf.get("recordId") == rid
                              and cf.get("field") == f and cf.get("incoming") == v
                              for cf in self.nb.conflicts.values())
                changes.append({"field": f, "action": "already-open" if already else "conflict",
                                "current": c, "incoming": v})
        return changes

    def _preview_records(self, parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        incoming_contact_ids = set()
        new_name_rows: Dict[str, int] = {}
        for rec in parsed["contacts"]:
            row = rec.get("_row")
            key = f"contacts:{row}"
            rid = rec.get("id")
            target = self.nb.canonical_id(rid) if rid else None
            exists = target is not None and target in self.nb.records["contacts"]
            candidate = copy.deepcopy(rec)
            if not exists:
                for k, v in CONTACT_DEFAULTS.items():
                    candidate.setdefault(k, copy.deepcopy(v))
                candidate.setdefault("createdAt", utc_now())
                candidate.setdefault("id", "pending-id")
            else:
                merged = copy.deepcopy(self.nb.records["contacts"][target])
                merged.update({k: v for k, v in rec.items() if not _empty(v) and not k.startswith("_")})
                candidate = merged
            errs = self._validate_incoming("contacts", candidate)
            if errs:
                items.append({"key": key, "collection": "contacts", "row": row, "status": "rejected",
                              "id": rid, "name": rec.get("name", ""), "errors": errs})
                continue
            if exists:
                changes = self._field_changes("contacts", target, rec)
                status = ("conflict" if any(c["action"] == "conflict" for c in changes)
                          else "update" if any(c["action"] in ("fill", "append") for c in changes)
                          else "unchanged")
                items.append({"key": key, "collection": "contacts", "row": row, "status": status, "id": target,
                              "incomingId": rid, "name": self.nb.records["contacts"][target]["name"],
                              "changes": changes})
                incoming_contact_ids.add(rid)
                continue
            dups = self.svc.possible_duplicates({**rec, "id": rid})
            nk = name_key(rec.get("name", ""))
            if nk and nk in new_name_rows:
                dups = dups + [{"id": None, "row": new_name_rows[nk], "name": rec.get("name", ""),
                                "reasons": [f"same name as row {new_name_rows[nk]} of this file"]}]
            elif nk:
                new_name_rows[nk] = row
            status = "possible-duplicate" if dups else "new"
            items.append({"key": key, "collection": "contacts", "row": row, "status": status, "id": rid,
                          "name": rec.get("name", ""), "matches": dups, "record": _public(rec)})
            if rid:
                incoming_contact_ids.add(rid)
        known = set(self.nb.records["contacts"]) | incoming_contact_ids | set(self.nb.aliases)
        for rec in parsed["edges"]:
            row = rec.get("_row")
            key = f"edges:{row}"
            missing = [rec.get(f) for f in ("from", "to") if rec.get(f) not in known]
            cand = {k: v for k, v in rec.items() if not k.startswith("_")}
            cand.setdefault("createdAt", utc_now())
            cand.setdefault("checkedAt", "")
            cand.setdefault("sourceTitle", "")
            cand.setdefault("sourceUrl", "")
            for f, d in (("provenance", "reported"), ("confidence", "unverified"), ("status", "proposed")):
                cand.setdefault(f, d)
            cand.setdefault("id", "pending-id")
            errs = self._validate_incoming("edges", cand)
            if missing:
                errs.append("unknown person ID(s): " + ", ".join(str(m) for m in missing) +
                            " (import those contacts first or in the same file)")
            if errs:
                items.append({"key": key, "collection": "edges", "row": row, "status": "rejected",
                              "id": rec.get("id"), "errors": errs})
                continue
            rid = rec.get("id")
            if rid and rid in self.nb.records["edges"]:
                changes = self._field_changes("edges", rid, rec)
                status = ("conflict" if any(c["action"] == "conflict" for c in changes)
                          else "update" if changes else "unchanged")
                items.append({"key": key, "collection": "edges", "row": row, "status": status, "id": rid,
                              "changes": changes, "record": _public(cand)})
                continue
            mapped = dict(cand)
            mapped["from"] = self.nb.canonical_id(mapped["from"])
            mapped["to"] = self.nb.canonical_id(mapped["to"])
            dup = self.svc.duplicate_edge(mapped) if (mapped["from"] in self.nb.records["contacts"]
                                                      and mapped["to"] in self.nb.records["contacts"]) else None
            items.append({"key": key, "collection": "edges", "row": row,
                          "status": "duplicate" if dup else "new", "id": rid, "duplicateOf": dup,
                          "record": _public(cand)})
        for rec in parsed["meetings"]:
            row = rec.get("_row")
            key = f"meetings:{row}"
            cand = {k: v for k, v in rec.items() if not k.startswith("_")}
            errs = []
            if not cand.get("contactId") and rec.get("_contactName"):
                matches = [rid for rid, c in self.nb.active("contacts")
                           if norm_text(c["name"]) == norm_text(rec["_contactName"])]
                if len(matches) == 1:
                    cand["contactId"] = matches[0]
                elif not matches:
                    errs.append(f"no contact named '{rec['_contactName']}'; add the contact first or give contactId")
                else:
                    errs.append(f"{len(matches)} contacts are named '{rec['_contactName']}'; give contactId "
                                f"(one of {', '.join(matches)})")
            for f in ("metBy", "date", "context", "learning", "followUp", "recordedBy"):
                cand.setdefault(f, "")
            cand.setdefault("createdAt", utc_now())
            cand.setdefault("id", "pending-id")
            if cand.get("contactId") and cand["contactId"] not in known:
                errs.append(f"unknown person ID '{cand['contactId']}'")
            errs += self._validate_incoming("meetings", cand)
            if errs:
                items.append({"key": key, "collection": "meetings", "row": row, "status": "rejected",
                              "id": rec.get("id"), "errors": errs})
                continue
            rid = rec.get("id")
            if rid and rid in self.nb.records["meetings"]:
                changes = self._field_changes("meetings", rid, cand)
                status = ("conflict" if any(c["action"] == "conflict" for c in changes)
                          else "update" if changes else "unchanged")
                items.append({"key": key, "collection": "meetings", "row": row, "status": status, "id": rid,
                              "changes": changes})
                continue
            cid = self.nb.canonical_id(cand["contactId"])
            dup = None
            for mid, m in self.nb.active("meetings"):
                if (self.nb.canonical_id(m.get("contactId", "")) == cid and (m.get("date") or "") == cand["date"]
                        and norm_text(m.get("context", "")) == norm_text(cand["context"])):
                    dup = mid
                    break
            items.append({"key": key, "collection": "meetings", "row": row,
                          "status": "duplicate" if dup else "new", "id": rid, "duplicateOf": dup,
                          "record": _public(cand)})
        return items

    def _preview_events(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        sim_versions: Dict[Tuple[str, str, str], str] = {}
        sim_values: Dict[Tuple[str, str, str], Any] = {}
        sim_created = set()
        sim_creator: Dict[Tuple[str, str], str] = {}
        skipped_ids = set()

        def exists(coll, rid):
            return rid in self.nb.records[coll] or (coll, rid) in sim_created

        def version(coll, rid, f):
            if (coll, rid, f) in sim_versions:
                return sim_versions[(coll, rid, f)]
            if (coll, rid) in sim_creator:
                return sim_creator[(coll, rid)]
            return self.nb.version(coll, rid, f)

        def value(coll, rid, f):
            if (coll, rid, f) in sim_values:
                return sim_values[(coll, rid, f)]
            return self._current_value(coll, rid, f)

        for ev in sorted(events, key=lambda e: e.get("seq", 0)):
            key = f"events:{ev.get('uuid')}"
            base_item = {"key": key, "collection": "events", "op": ev.get("op"), "id": ev.get("id"),
                         "uuid": ev.get("uuid"), "actor": ev.get("actor", ""), "ts": ev.get("ts", "")}
            if not ev.get("uuid") or not ev.get("op"):
                items.append({**base_item, "status": "rejected", "errors": ["event without uuid or op"]})
                continue
            if ev["uuid"] in self.nb.uuids:
                items.append({**base_item, "status": "unchanged"})
                continue
            kind, _, action = ev["op"].partition(".")
            data = ev.get("data") or {}
            if kind not in KIND_TO_COLL:
                if ev["op"] in ("import.batch", "conflict.open", "conflict.resolve", "workspace.note"):
                    items.append({**base_item, "status": "new", "history": True})
                else:
                    items.append({**base_item, "status": "rejected", "errors": [f"unknown op {ev['op']}"]})
                continue
            coll = KIND_TO_COLL[kind]
            rid = ev.get("id")
            if (coll, rid) in skipped_ids:
                items.append({**base_item, "status": "rejected",
                              "errors": ["depends on a record that this import cannot create"]})
                continue
            if action == "create":
                if exists(coll, rid):
                    items.append({**base_item, "status": "id-collision",
                                  "errors": [f"a different {coll[:-1]} already uses ID '{rid}'"],
                                  "record": _public(data.get("record") or {})})
                    skipped_ids.add((coll, rid))
                    continue
                rec = data.get("record") or {}
                errs = self._validate_incoming(coll, rec)
                if errs:
                    items.append({**base_item, "status": "rejected", "errors": errs})
                    skipped_ids.add((coll, rid))
                    continue
                sim_created.add((coll, rid))
                sim_creator[(coll, rid)] = ev["uuid"]
                for f, v in rec.items():
                    sim_versions[(coll, rid, f)] = ev["uuid"]
                    sim_values[(coll, rid, f)] = v
                items.append({**base_item, "status": "new", "name": rec.get("name", ""), "record": _public(rec)})
                continue
            if not exists(coll, rid):
                items.append({**base_item, "status": "rejected", "errors": [f"no {coll[:-1]} '{rid}' to change"]})
                continue
            if action == "update":
                ok, conflicts, converged = {}, {}, {}
                for f, v in (data.get("set") or {}).items():
                    base = (data.get("base") or {}).get(f)
                    if base == version(coll, rid, f):
                        ok[f] = v
                    elif value(coll, rid, f) == v:
                        converged[f] = v
                    else:
                        conflicts[f] = {"current": value(coll, rid, f), "incoming": v}
                for f, v in ok.items():
                    sim_versions[(coll, rid, f)] = ev["uuid"]
                    sim_values[(coll, rid, f)] = v
                status = "conflict" if conflicts else "new"
                items.append({**base_item, "status": status, "fields": sorted(ok), "converged": sorted(converged),
                              "conflicts": conflicts,
                              "name": self.nb.records["contacts"].get(rid, {}).get("name", "") if coll == "contacts" else ""})
                continue
            items.append({**base_item, "status": "new"})
        return items

    # -- apply ---------------------------------------------------------------------

    def apply(self, batch_id: str, decisions: Dict[str, str], actor: str) -> Dict[str, Any]:
        pending = self.svc.pending_imports.get(batch_id)
        if pending is None:
            raise ImportError_("This preview has expired. Preview the file again.")
        preview = pending["preview"]
        if preview.get("revision") is not None and preview["revision"] != self.nb.revision:
            raise ImportError_("The notebook changed after this preview. Preview the file again.")
        missing = [k for k in preview["needsDecision"] if k not in decisions]
        if missing:
            raise ImportError_(f"{len(missing)} item(s) need a decision before importing",
                               {"needsDecision": missing})
        stored = self.svc.ws.save_import_copy(batch_id, preview["file"]["name"], pending["data"])
        batch_ev = {"op": "import.batch", "id": batch_id, "actor": actor,
                    "data": {"batchId": batch_id, "filename": preview["file"]["name"],
                             "sha256": preview["file"]["sha256"], "format": preview["file"]["format"],
                             "bytes": preview["file"]["bytes"], "mode": preview["mode"],
                             "summary": preview["summary"],
                             "storedCopy": stored.replace(self.svc.ws.path, ".")}}
        if preview["mode"] == "history":
            events, report = self._apply_events(pending["parsed"]["events"], preview, decisions, batch_id, actor)
        else:
            events, report = self._apply_records(pending["parsed"], preview, decisions, batch_id, actor)
        batch_ev["data"]["applied"] = report
        written = self.svc._append([batch_ev] + events)
        del self.svc.pending_imports[batch_id]
        full_report = {"batchId": batch_id, "applied": report, "events": len(written) - 1,
                       "file": preview["file"], "decisions": decisions}
        self.svc.ws.save_import_report(batch_id, {"preview": preview, "result": full_report})
        return full_report

    def _conflict_event(self, batch_id, n, coll, rid, field, current, incoming, source, actor):
        cid = f"conf-{batch_id[4:]}-{n}"
        return {"op": "conflict.open", "id": cid, "actor": actor, "batch": batch_id,
                "data": {"conflictId": cid, "batchId": batch_id, "collection": coll, "recordId": rid,
                         "field": field, "current": copy.deepcopy(current), "incoming": copy.deepcopy(incoming),
                         "source": source}}

    def _apply_records(self, parsed, preview, decisions, batch_id, actor):
        events: List[Dict[str, Any]] = []
        report = {"created": 0, "updated": 0, "conflictsOpened": 0, "skipped": 0, "merged": 0, "rejected": 0,
                  "unchanged": 0, "duplicates": 0}
        by_key = {it["key"]: it for it in preview["items"]}
        id_map: Dict[str, str] = {}
        taken = set()
        n_conf = 0
        src = {"file": preview["file"]["name"], "batchId": batch_id, "format": preview["file"]["format"]}

        def new_id(name, wanted):
            if wanted and not self.nb.id_taken("contacts", wanted) and wanted not in taken:
                taken.add(wanted)
                return wanted
            base = self.svc.new_contact_id(name)
            rid, i = base, 2
            while rid in taken:
                rid = f"{base}-{i}"
                i += 1
            taken.add(rid)
            return rid

        def emit_changes(coll, kind, rid, changes):
            nonlocal n_conf
            sets = {c["field"]: c["incoming"] for c in changes if c["action"] == "fill"}
            cur = self.nb.records[coll][rid]
            if sets:
                events.append({"op": f"{kind}.update", "id": rid, "actor": actor, "batch": batch_id,
                               "data": {"set": copy.deepcopy(sets), "prev": {f: cur.get(f) for f in sets},
                                        "base": {f: self.nb.version(coll, rid, f) for f in sets}}})
            for c in changes:
                if c["action"] == "append":
                    events.append({"op": f"{kind}.append", "id": rid, "actor": actor, "batch": batch_id,
                                   "data": {"field": c["field"], "items": copy.deepcopy(c["items"])}})
                elif c["action"] == "conflict":
                    n_conf += 1
                    events.append(self._conflict_event(batch_id, n_conf, coll, rid, c["field"], c["current"],
                                                       c["incoming"], src, actor))
                    report["conflictsOpened"] += 1
            if sets or any(c["action"] == "append" for c in changes):
                report["updated"] += 1

        for rec in parsed["contacts"]:
            it = by_key.get(f"contacts:{rec.get('_row')}")
            if it is None:
                continue
            st = it["status"]
            dec = decisions.get(it["key"], "apply" if st in ("update", "conflict") else "create")
            if st == "rejected":
                report["rejected"] += 1
                continue
            if st == "unchanged":
                report["unchanged"] += 1
                continue
            if dec == "skip":
                report["skipped"] += 1
                continue
            clean = {k: v for k, v in rec.items() if not k.startswith("_")}
            if st in ("update", "conflict"):
                emit_changes("contacts", "contact", it["id"], it["changes"])
                if rec.get("id") and rec["id"] != it["id"]:
                    id_map[rec["id"]] = it["id"]
                continue
            if dec.startswith("merge:"):
                target = dec.split(":", 1)[1]
                if target not in self.nb.records["contacts"]:
                    raise ImportError_(f"Cannot merge into unknown person '{target}'")
                emit_changes("contacts", "contact", target, self._field_changes("contacts", target, clean))
                if rec.get("id") and rec["id"] != target and not self.nb.id_taken("contacts", rec["id"]):
                    events.append({"op": "contact.alias", "id": target, "actor": actor, "batch": batch_id,
                                   "data": {"alias": rec["id"]}})
                if rec.get("id"):
                    id_map[rec["id"]] = target
                report["merged"] += 1
                continue
            rid = new_id(clean.get("name", ""), clean.get("id"))
            if clean.get("id") and clean["id"] != rid:
                id_map[clean["id"]] = rid
            record = {**copy.deepcopy(CONTACT_DEFAULTS), **clean, "id": rid}
            record.setdefault("createdAt", utc_now())
            events.append({"op": "contact.create", "id": rid, "actor": actor, "batch": batch_id,
                           "data": {"record": record}})
            report["created"] += 1

        def remap(x):
            return id_map.get(x) or self.nb.canonical_id(x)

        created_ids = {e["id"] for e in events if e["op"] == "contact.create"}

        def exists_after(x):
            return x in created_ids or x in self.nb.records["contacts"]

        for rec in parsed["edges"]:
            it = by_key.get(f"edges:{rec.get('_row')}")
            if it is None:
                continue
            st = it["status"]
            if st == "rejected":
                report["rejected"] += 1
                continue
            if st in ("unchanged",):
                report["unchanged"] += 1
                continue
            if st == "duplicate":
                report["duplicates"] += 1
                continue
            if decisions.get(it["key"]) == "skip":
                report["skipped"] += 1
                continue
            if st in ("update", "conflict"):
                emit_changes("edges", "edge", it["id"], it["changes"])
                continue
            record = dict(it["record"])
            record["from"], record["to"] = remap(record["from"]), remap(record["to"])
            if not (exists_after(record["from"]) and exists_after(record["to"])):
                report["rejected"] += 1
                report.setdefault("rejectedAtApply", []).append(
                    {"key": it["key"], "reason": "a person it connects was skipped or rejected in this import"})
                continue
            wanted = record.get("id")
            if not wanted or wanted == "pending-id" or wanted in self.nb.records["edges"]:
                record["id"] = self.svc._edge_id(record["type"], record["from"], record["to"])
            events.append({"op": "edge.create", "id": record["id"], "actor": actor, "batch": batch_id,
                           "data": {"record": record}})
            report["created"] += 1

        for rec in parsed["meetings"]:
            it = by_key.get(f"meetings:{rec.get('_row')}")
            if it is None:
                continue
            st = it["status"]
            if st == "rejected":
                report["rejected"] += 1
                continue
            if st == "unchanged":
                report["unchanged"] += 1
                continue
            if st == "duplicate":
                report["duplicates"] += 1
                continue
            if decisions.get(it["key"]) == "skip":
                report["skipped"] += 1
                continue
            if st in ("update", "conflict"):
                emit_changes("meetings", "meeting", it["id"], it["changes"])
                continue
            record = dict(it["record"])
            record["contactId"] = remap(record["contactId"])
            if not exists_after(record["contactId"]):
                report["rejected"] += 1
                report.setdefault("rejectedAtApply", []).append(
                    {"key": it["key"], "reason": "the person this note is about was skipped or rejected"})
                continue
            wanted = record.get("id")
            if not wanted or wanted == "pending-id" or wanted in self.nb.records["meetings"]:
                record["id"] = f"note-{record['contactId']}-{record.get('date') or 'undated'}-{batch_id[-6:]}"[:100]
            events.append({"op": "meeting.create", "id": record["id"], "actor": actor, "batch": batch_id,
                           "data": {"record": record}})
            report["created"] += 1
        return events, report

    def _apply_events(self, bundle_events, preview, decisions, batch_id, actor):
        events: List[Dict[str, Any]] = []
        report = {"eventsApplied": 0, "alreadyPresent": 0, "conflictsOpened": 0, "rejected": 0, "skipped": 0}
        by_uuid = {it["uuid"]: it for it in preview["items"] if it.get("collection") == "events"}
        n_conf = 0
        for ev in sorted(bundle_events, key=lambda e: e.get("seq", 0)):
            it = by_uuid.get(ev.get("uuid"))
            if it is None:
                continue
            st = it["status"]
            if st == "unchanged":
                report["alreadyPresent"] += 1
                continue
            if st == "rejected":
                report["rejected"] += 1
                continue
            if st == "id-collision":
                report["skipped"] += 1
                continue
            if decisions.get(it["key"]) == "skip":
                report["skipped"] += 1
                continue
            copy_ev = {k: copy.deepcopy(v) for k, v in ev.items() if k not in ("seq",)}
            copy_ev["importedIn"] = batch_id
            copy_ev["origSeq"] = ev.get("seq")
            if ev["op"].endswith(".update"):
                data = copy_ev["data"]
                keep = set(it.get("fields") or [])
                if it.get("conflicts"):
                    coll = KIND_TO_COLL[ev["op"].split(".")[0]]
                    for f, cv in it["conflicts"].items():
                        n_conf += 1
                        events.append(self._conflict_event(
                            batch_id, n_conf, coll, ev["id"], f, cv["current"], cv["incoming"],
                            {"batchId": batch_id, "file": preview["file"]["name"], "eventUuid": ev["uuid"],
                             "actor": ev.get("actor", ""), "ts": ev.get("ts", "")}, actor))
                        report["conflictsOpened"] += 1
                if not keep:
                    # Nothing applicable; record the event id so a re-import treats it as seen.
                    copy_ev = {"op": "workspace.note", "id": ev["id"], "uuid": ev["uuid"], "ts": ev.get("ts"),
                               "actor": ev.get("actor", ""), "importedIn": batch_id,
                               "data": {"note": "update not applied: every field conflicted or already matched",
                                        "originalOp": ev["op"]}}
                else:
                    data["set"] = {f: v for f, v in data["set"].items() if f in keep}
                    data["prev"] = {f: v for f, v in (data.get("prev") or {}).items() if f in keep}
                    data["base"] = {f: v for f, v in (data.get("base") or {}).items() if f in keep}
            events.append(copy_ev)
            report["eventsApplied"] += 1
        return events, report


def _public(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in rec.items() if not k.startswith("_")}


def _explain(mode: str, fmt: str, same_base: bool, base_sha: Optional[str]) -> str:
    if mode == "history":
        return ("This bundle was exported from a workspace built on the same packet, so its change history is "
                "replayed event by event. Changes you already have are skipped. A field that both copies "
                "changed becomes an open conflict; your current value stays until you choose.")
    if fmt == "bundle":
        return ("This bundle was built on a different packet (base " + (base_sha or "unknown")[:12] + "...), so "
                "its records are compared one by one with yours. Its change history is not replayed.")
    return ("Records are matched by stable ID first, then checked for possible duplicates by name, email and "
            "profile URL. New values fill empty fields; values that differ from yours become open conflicts "
            "and nothing is overwritten.")
