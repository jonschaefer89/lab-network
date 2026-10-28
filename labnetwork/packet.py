"""Load and validate a Lab Network data packet.

A packet is a directory that holds notebook.json and, optionally,
lab-roots.json, notebook.schema.json, source-manifest.json and
packet-checksums.json. Lab Network reads packets and never writes to them;
all edits live in a separate workspace (see store.py).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLED_SCHEMA = os.path.join(HERE, "schema", "notebook.schema.json")
_bundled: Dict[str, Any] = {}


def bundled_schema() -> Dict[str, Any]:
    """This kit's schema. A packet keeps the copy it was built with, which an older kit may have made stricter (it
    allowed 3,000 people and 10,000 relationships); the notebook as edited since holds records this kit wrote."""
    if not _bundled:
        _bundled.update(_read_json(BUNDLED_SCHEMA))
    return _bundled


class PacketError(Exception):
    """The packet cannot be loaded safely."""


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@dataclass
class Packet:
    path: str
    notebook: Dict[str, Any]
    roots: List[Dict[str, Any]]
    manifest: Optional[Dict[str, Any]]
    checksums: Optional[Dict[str, str]]
    schema: Dict[str, Any]
    sha256: str
    size: int

    @property
    def root_ids(self) -> List[str]:
        return [r["id"] for r in self.roots]


def find_packet_dir(path: str) -> str:
    """Accept either the packet directory or a parent that contains input/."""
    path = os.path.abspath(path)
    if os.path.isfile(os.path.join(path, "notebook.json")):
        return path
    inner = os.path.join(path, "input")
    if os.path.isfile(os.path.join(inner, "notebook.json")):
        return inner
    raise PacketError(f"No notebook.json found in {path} or {inner}")


def load_packet(path: str, roots_override: Optional[List[Dict[str, Any]]] = None) -> Packet:
    pdir = find_packet_dir(path)
    nb_path = os.path.join(pdir, "notebook.json")
    digest = sha256_file(nb_path)
    size = os.path.getsize(nb_path)
    try:
        notebook = _read_json(nb_path)
    except (OSError, ValueError) as exc:
        raise PacketError(f"notebook.json could not be parsed: {exc}") from exc
    if not isinstance(notebook, dict):
        raise PacketError("notebook.json must contain a JSON object")

    def optional(name):
        p = os.path.join(pdir, name)
        return _read_json(p) if os.path.isfile(p) else None

    manifest = optional("source-manifest.json")
    checksums = optional("packet-checksums.json")
    schema = optional("notebook.schema.json") or _read_json(BUNDLED_SCHEMA)
    roots = roots_override if roots_override is not None else optional("lab-roots.json")
    if roots is None:
        seed_ids = (notebook.get("coverage") or {}).get("seedIds") or []
        roots = [{"id": s} for s in seed_ids]
    if not roots:
        raise PacketError("No lab roots: add lab-roots.json to the packet or roots to workspace.json")
    return Packet(pdir, notebook, roots, manifest, checksums, schema, digest, size)


# ---------------------------------------------------------------------------
# JSON Schema subset. Covers every keyword used by notebook.schema.json; an
# unknown assertion keyword is reported instead of being ignored silently.

_KNOWN_KEYWORDS = {
    "$schema", "title", "description", "type", "properties", "required",
    "additionalProperties", "items", "enum", "const", "minLength", "maxLength",
    "minItems", "maxItems", "minimum", "maximum", "anyOf", "$id", "default",
}


def _type_ok(value: Any, typ: str) -> bool:
    if typ == "string":
        return isinstance(value, str)
    if typ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if typ == "boolean":
        return isinstance(value, bool)
    if typ == "array":
        return isinstance(value, list)
    if typ == "object":
        return isinstance(value, dict)
    if typ == "null":
        return value is None
    return False


def validate_schema(value: Any, schema: Dict[str, Any], path: str = "$",
                    errors: Optional[List[Dict[str, str]]] = None,
                    limit: int = 500) -> List[Dict[str, str]]:
    if errors is None:
        errors = []
    if len(errors) >= limit:
        return errors

    def err(msg):
        if len(errors) < limit:
            errors.append({"path": path, "message": msg})

    unknown = set(schema) - _KNOWN_KEYWORDS
    if unknown:
        err("unsupported schema keywords: " + ", ".join(sorted(unknown)))
    if "anyOf" in schema:
        if not any(not validate_schema(value, sub, path, [], limit) for sub in schema["anyOf"]):
            err("value matches none of the allowed forms")
            return errors
    typ = schema.get("type")
    if typ is not None:
        types = typ if isinstance(typ, list) else [typ]
        if not any(_type_ok(value, t) for t in types):
            err(f"expected {'/'.join(types)}, got {type(value).__name__}")
            return errors
    if "const" in schema and value != schema["const"]:
        err(f"must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        err(f"{value!r} is not one of {schema['enum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            err(f"shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            err(f"longer than {schema['maxLength']} characters")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            err(f"below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            err(f"above maximum {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            err(f"fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            err(f"more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                validate_schema(item, schema["items"], f"{path}[{i}]", errors, limit)
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                err(f"missing required field '{key}'")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    err(f"unexpected field '{key}'")
        for key, sub in props.items():
            if key in value:
                validate_schema(value[key], sub, f"{path}.{key}", errors, limit)
    return errors


def entity_schema(schema: Dict[str, Any], collection: str) -> Dict[str, Any]:
    """Item schema for one collection (contacts, edges, meetings, research)."""
    return schema["properties"][collection]["items"]


# ---------------------------------------------------------------------------
# Application-level rules from the data dictionary.

CONTACT_URL_FIELDS = ["website", "labSourceUrl", "primaryInstitutionSourceUrl", "doctorateSourceUrl",
                      "photoUrl", "photoSourceUrl", "profileSourceUrl", "researchSourceUrl"]
CONTACT_DATE_FIELDS = ["labCheckedAt", "primaryInstitutionCheckedAt", "doctorateCheckedAt",
                       "photoCheckedAt", "profileCheckedAt", "researchCheckedAt"]
PAPER_URL_FIELDS = ["url", "abstractSourceUrl", "sourceUrl", "evidenceSourceUrl"]
PAPER_DATE_FIELDS = ["checkedAt", "abstractCheckedAt"]
GRANT_URL_FIELDS = ["url", "abstractSourceUrl", "sourceUrl"]
GRANT_DATE_FIELDS = ["checkedAt", "abstractCheckedAt", "projectStartDate", "projectEndDate",
                     "budgetStartDate", "budgetEndDate", "start", "end"]
EDGE_URL_FIELDS = ["sourceUrl", "relationshipNarrativeSourceUrl"]
EDGE_DATE_FIELDS = ["checkedAt", "relationshipNarrativeCheckedAt"]

_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_calendar_date(value: str) -> bool:
    if not _DATE_RX.match(value):
        return False
    try:
        _dt.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def is_http_url(value: str) -> bool:
    try:
        u = urlparse(value)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.netloc) and " " not in value


def check_contact(c: Dict[str, Any], issues: List[Dict[str, str]], cid: str = "") -> None:
    cid = cid or c.get("id", "?")

    def add(field, msg, level="error"):
        issues.append({"level": level, "collection": "contacts", "id": cid, "field": field, "message": msg})

    for f in CONTACT_URL_FIELDS:
        v = c.get(f) or ""
        if v and not is_http_url(v):
            add(f, f"not an absolute http(s) URL: {v[:80]}")
    for f in CONTACT_DATE_FIELDS:
        v = c.get(f) or ""
        if v and not is_calendar_date(v):
            add(f, f"not a real YYYY-MM-DD date: {v}")
    for i, s in enumerate(c.get("sources") or []):
        if s.get("url") and not is_http_url(s["url"]):
            add(f"sources[{i}].url", f"not an absolute http(s) URL: {s['url'][:80]}")
    status = c.get("doctorateStatus")
    if status in ("confirmed", "not-yet-earned"):
        for f in ("doctorateEvidence", "doctorateSourceUrl", "doctorateCheckedAt"):
            if not (c.get(f) or "").strip():
                add(f, f"doctorateStatus '{status}' requires {f}")
    if c.get("photoUrl") and c.get("photoConfidence") == "confirmed" and not c.get("photoSourceUrl"):
        add("photoSourceUrl", "a confirmed portrait needs an identity-verifying source URL")
    if (c.get("researchSummary") or "").strip() and not c.get("researchSourceUrl"):
        add("researchSourceUrl", "a research summary needs its source URL")
    for kind, items, urls, dates in (("papers", c.get("papers") or [], PAPER_URL_FIELDS, PAPER_DATE_FIELDS),
                                     ("grants", c.get("grants") or [], GRANT_URL_FIELDS, GRANT_DATE_FIELDS)):
        for i, rec in enumerate(items):
            where = f"{kind}[{i}]"
            for f in urls:
                v = rec.get(f) or ""
                if v and not is_http_url(v):
                    add(f"{where}.{f}", f"not an absolute http(s) URL: {v[:80]}")
            for f in dates:
                v = rec.get(f) or ""
                if v and not is_calendar_date(v):
                    add(f"{where}.{f}", f"not a real YYYY-MM-DD date: {v}")
            if not (rec.get("url") or rec.get("sourceUrl")):
                add(where, "research record needs a source URL")
            if (rec.get("abstract") or "").strip() and not (rec.get("abstractSourceUrl") and rec.get("abstractCheckedAt")):
                add(f"{where}.abstract", "an abstract needs its own source URL and checked date")


def check_edge(e: Dict[str, Any], issues: List[Dict[str, str]], contact_ids=None) -> None:
    eid = e.get("id", "?")

    def add(field, msg, level="error"):
        issues.append({"level": level, "collection": "edges", "id": eid, "field": field, "message": msg})

    if contact_ids is not None:
        for f in ("from", "to"):
            if e.get(f) not in contact_ids:
                add(f, f"refers to unknown contact '{e.get(f)}'")
    if e.get("from") and e.get("from") == e.get("to"):
        add("to", "a relationship cannot connect a person to themselves")
    for f in EDGE_URL_FIELDS:
        v = e.get(f) or ""
        if v and not is_http_url(v):
            add(f, f"not an absolute http(s) URL: {v[:80]}")
    for f in EDGE_DATE_FIELDS:
        v = e.get(f) or ""
        if v and not is_calendar_date(v):
            add(f, f"not a real YYYY-MM-DD date: {v}")
    if e.get("provenance") == "public" and not e.get("sourceUrl"):
        add("sourceUrl", "a public-record relationship needs a source URL")
    if (e.get("relationshipNarrative") or "").strip() and not (
            e.get("relationshipNarrativeSourceUrl") and e.get("relationshipNarrativeCheckedAt")):
        add("relationshipNarrative", "a relationship narrative needs its own source URL and checked date")


def check_meeting(m: Dict[str, Any], issues: List[Dict[str, str]], contact_ids=None) -> None:
    mid = m.get("id", "?")
    if contact_ids is not None and m.get("contactId") not in contact_ids:
        issues.append({"level": "error", "collection": "meetings", "id": mid, "field": "contactId",
                       "message": f"refers to unknown contact '{m.get('contactId')}'"})
    d = m.get("date") or ""
    if d and not is_calendar_date(d):
        issues.append({"level": "error", "collection": "meetings", "id": mid, "field": "date",
                       "message": f"not a real YYYY-MM-DD date: {d}"})


def validate_rules(nb: Dict[str, Any]) -> List[Dict[str, str]]:
    issues: List[Dict[str, str]] = []
    for coll in ("contacts", "edges", "meetings", "research"):
        seen = Counter(x.get("id") for x in nb.get(coll) or [])
        for rid, n in seen.items():
            if n > 1:
                issues.append({"level": "error", "collection": coll, "id": rid, "field": "id",
                               "message": f"ID used {n} times"})
    contact_ids = {c.get("id") for c in nb.get("contacts") or []}
    for c in nb.get("contacts") or []:
        check_contact(c, issues)
    for e in nb.get("edges") or []:
        check_edge(e, issues, contact_ids)
    for m in nb.get("meetings") or []:
        check_meeting(m, issues, contact_ids)
    for r in nb.get("research") or []:
        if r.get("contactId") not in contact_ids:
            issues.append({"level": "error", "collection": "research", "id": r.get("id"), "field": "contactId",
                           "message": f"refers to unknown contact '{r.get('contactId')}'"})
    for s in (nb.get("coverage") or {}).get("seedIds") or []:
        if s not in contact_ids:
            issues.append({"level": "error", "collection": "coverage", "id": s, "field": "seedIds",
                           "message": "coverage seed ID is not a contact"})
    return issues


def count_summary(nb: Dict[str, Any]) -> Dict[str, Any]:
    contacts = nb.get("contacts") or []
    edges = nb.get("edges") or []
    return {
        "people": len(contacts),
        "relationships": len(edges),
        "meeting_notes": len(nb.get("meetings") or []),
        "research_requests": len(nb.get("research") or []),
        "photo_urls": sum(1 for c in contacts if c.get("photoUrl")),
        "confirmed_photo_urls": sum(1 for c in contacts if c.get("photoUrl") and c.get("photoConfidence") == "confirmed"),
        "paper_associations": sum(len(c.get("papers") or []) for c in contacts),
        "grant_associations": sum(len(c.get("grants") or []) for c in contacts),
        "applied_seed_records": len(nb.get("appliedSeeds") or []),
        "relationship_types": dict(Counter(e.get("type") for e in edges)),
        "relationship_provenance": dict(Counter(e.get("provenance") for e in edges)),
        "relationship_review_status": dict(Counter(e.get("status") for e in edges)),
        "relationship_confidence": dict(Counter(e.get("confidence") for e in edges)),
    }


def _first_name(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return n.split()[0] if n.split() else ""


def resolve_roots(nb: Dict[str, Any], roots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id = {c["id"]: c for c in nb.get("contacts") or []}
    out = []
    for r in roots:
        c = by_id.get(r["id"])
        expected_name = r.get("name")
        first = _first_name(expected_name or (c or {}).get("name", ""))
        same_first = sorted(x["id"] for x in by_id.values() if first and _first_name(x["name"]) == first)
        out.append({
            "id": r["id"],
            "found": c is not None,
            "name": c["name"] if c else None,
            "expectedName": expected_name,
            "nameMatches": (c is not None and (expected_name is None or c["name"] == expected_name)),
            "role": c.get("role") if c else None,
            "institution": c.get("institution") if c else None,
            "sameFirstNameCandidates": same_first,
        })
    return out


def validation_report(packet: Packet) -> Dict[str, Any]:
    """Everything a person needs to trust (or distrust) a packet, in one dict."""
    nb = packet.notebook
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    if packet.checksums:
        bad = []
        for fname, expected in packet.checksums.items():
            p = os.path.join(packet.path, fname)
            if not os.path.isfile(p) or sha256_file(p) != expected:
                bad.append(fname)
        check("packet-checksums.json matches every listed file", not bad,
              "mismatch: " + ", ".join(bad) if bad else f"{len(packet.checksums)} files verified")
    if packet.manifest and packet.manifest.get("delivered_sha256"):
        check("notebook.json matches the manifest's delivered_sha256",
              packet.manifest["delivered_sha256"] == packet.sha256, packet.sha256)
        if packet.manifest.get("delivered_utf8_bytes"):
            check("notebook.json size matches the manifest",
                  packet.manifest["delivered_utf8_bytes"] == packet.size,
                  f"{packet.size} bytes")
    schema_errors = validate_schema(nb, packet.schema)
    check("notebook.json conforms to notebook.schema.json", not schema_errors,
          f"{len(schema_errors)} schema errors" if schema_errors else "0 errors")
    rule_issues = validate_rules(nb)
    rule_errors = [i for i in rule_issues if i["level"] == "error"]
    check("cross-field, URL, date and reference rules", not rule_errors,
          f"{len(rule_errors)} errors" if rule_errors else "0 errors")
    counts = count_summary(nb)
    expected = (packet.manifest or {}).get("counts")
    count_diffs = {}
    if expected:
        for k, v in expected.items():
            if counts.get(k) != v:
                count_diffs[k] = {"expected": v, "loaded": counts.get(k)}
        check("loaded counts match the manifest", not count_diffs,
              "all counts match" if not count_diffs else json.dumps(count_diffs))
    roots = resolve_roots(nb, packet.roots)
    check("every lab root resolves to exactly one stored contact ID",
          all(r["found"] and r["nameMatches"] for r in roots),
          ", ".join(f"{r['id']}={'ok' if r['found'] else 'MISSING'}" for r in roots))
    status = (packet.manifest or {}).get("dataset_status", "")
    live_ok = not re.search(r"seed-only|not live", status, re.I)
    check("dataset is not labeled seed-only or NOT LIVE", live_ok, status or "no manifest status")
    return {
        "ok": all(c["ok"] for c in checks),
        "packetPath": packet.path,
        "sha256": packet.sha256,
        "bytes": packet.size,
        "datasetStatus": status,
        "savedAt": (packet.manifest or {}).get("saved_at"),
        "checks": checks,
        "counts": counts,
        "expectedCounts": expected,
        "countDifferences": count_diffs,
        "schemaErrors": schema_errors[:50],
        "ruleIssues": rule_issues[:200],
        "roots": roots,
        "limitations": (packet.manifest or {}).get("limitations", []),
    }
