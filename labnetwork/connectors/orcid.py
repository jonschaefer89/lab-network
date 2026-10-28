"""ORCID public API: education records (to confirm earned doctorates) and current employment.

https://info.orcid.org/documentation/api-tutorials/. Public data only; no key needed.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .http import Client

API = "https://pub.orcid.org/v3.0"
DOCTORATE = re.compile(r"\b(ph\.?\s?d|d\.?\s?phil|psy\.?\s?d|ed\.?\s?d|sc\.?\s?d|dr\.?\s?p\.?\s?h|m\.?\s?d|"
                       r"doctor of|doctorate|doctoral degree)\b", re.I)


def clean(orcid: str) -> str:
    return (orcid or "").strip().replace("https://orcid.org/", "").replace("http://orcid.org/", "")


def record_url(orcid: str) -> str:
    return f"https://orcid.org/{clean(orcid)}"


def _summaries(data: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    out = []
    for g in (data or {}).get("affiliation-group") or []:
        for s in g.get("summaries") or []:
            e = s.get(key) or {}
            if e:
                out.append(e)
    return out


def _year(d: Optional[Dict[str, Any]]) -> Optional[int]:
    try:
        return int(((d or {}).get("year") or {}).get("value"))
    except (TypeError, ValueError):
        return None


def _edu(e: Dict[str, Any]) -> Dict[str, Any]:
    return {"title": e.get("role-title") or "", "organization": (e.get("organization") or {}).get("name", ""),
            "department": e.get("department-name") or "", "endYear": _year(e.get("end-date"))}


def _emp(e: Dict[str, Any]) -> Dict[str, Any]:
    return {"title": e.get("role-title") or "", "organization": (e.get("organization") or {}).get("name", ""),
            "department": e.get("department-name") or "", "startYear": _year(e.get("start-date")),
            "endYear": _year(e.get("end-date"))}


def educations(client: Client, orcid: str) -> List[Dict[str, Any]]:
    return [_edu(e) for e in _summaries(client.get(f"{API}/{clean(orcid)}/educations"), "education-summary")]


def employments(client: Client, orcid: str) -> List[Dict[str, Any]]:
    return [_emp(e) for e in _summaries(client.get(f"{API}/{clean(orcid)}/employments"), "employment-summary")]


def activities(client: Client, orcid: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Education (with qualifications, where some people list degrees) and employment, in one request."""
    data = client.get(f"{API}/{clean(orcid)}/activities") or {}
    edus = [_edu(e) for e in _summaries(data.get("educations"), "education-summary") +
            _summaries(data.get("qualifications"), "qualification-summary")]
    return edus, [_emp(e) for e in _summaries(data.get("employments"), "employment-summary")]


def work_dois(client: Client, orcid: str) -> List[Tuple[str, Optional[int]]]:
    """(DOI, publication year) for each work the person lists on their ORCID record that has a DOI, in one request.
    DOIs are lowercase, without the https://doi.org/ prefix."""
    data = client.get(f"{API}/{clean(orcid)}/works") or {}
    out = []
    for g in data.get("group") or []:
        doi = ""
        for e in (g.get("external-ids") or {}).get("external-id") or []:
            if (e.get("external-id-type") or "").lower() == "doi":
                doi = ((e.get("external-id-normalized") or {}).get("value") or e.get("external-id-value") or "")
                break
        doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi.strip().lower())
        if doi:
            s = (g.get("work-summary") or [{}])[0]
            out.append((doi, _year(s.get("publication-date"))))
    return out


def doctorate(edus: List[Dict[str, Any]], orcid: str, checked: str) -> Optional[Dict[str, str]]:
    """A completed doctorate listed on the ORCID record, or None (which is not evidence of no doctorate)."""
    for e in sorted(edus, key=lambda x: -(x["endYear"] or 0)):
        m = DOCTORATE.search(e["title"])
        if m and e["endYear"]:
            return {"doctorateStatus": "confirmed", "doctorateType": e["title"][:120],
                    "doctorateEvidence": (f"ORCID education record: {e['title']}, {e['organization']}, "
                                          f"completed {e['endYear']}.")[:1800],
                    "doctorateSourceUrl": record_url(orcid), "doctorateCheckedAt": checked}
    return None


def current_position(emps: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    current = [e for e in emps if not e["endYear"] and e["title"]]
    if not current:
        return None
    current.sort(key=lambda e: -(e["startYear"] or 0))
    return current[0]
