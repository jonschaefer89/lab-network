"""NIH RePORTER: awards and the principal investigators named on them.

https://api.reporter.nih.gov. Free; about one request per second. RePORTER lists
contact and multiple PIs, not co-investigators, so only PI-level ties come from here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .http import Client

API = "https://api.reporter.nih.gov/v2/projects/search"
FIELDS = ["ApplId", "ProjectNum", "CoreProjectNum", "SubprojectId", "ProjectTitle", "PrincipalInvestigators",
          "Organization", "ProjectStartDate", "ProjectEndDate", "BudgetStart", "BudgetEnd", "FiscalYear",
          "AgencyIcAdmin", "ActivityCode", "IsActive"]
# A center grant (P50, P41, U54...) has one record for the award itself and one per core or project, all under the
# same core project number; the cores' records carry a subproject_id.


def project_url(appl_id: Any) -> str:
    return f"https://reporter.nih.gov/project-details/{appl_id}"


def projects(client: Client, profile_ids: Optional[List[int]] = None, first: str = "", last: str = "",
             org: str = "", since_fy: Optional[int] = None, limit: int = 500) -> List[Dict[str, Any]]:
    criteria: Dict[str, Any] = {}
    if profile_ids:
        criteria["pi_profile_ids"] = [int(p) for p in profile_ids]
    elif last:
        criteria["pi_names"] = [{"first_name": first, "last_name": last}]
        if org:
            criteria["org_names"] = [org]
    else:
        return []
    if since_fy:
        import datetime
        criteria["fiscal_years"] = list(range(int(since_fy), datetime.date.today().year + 2))
    out: List[Dict[str, Any]] = []
    offset = 0
    while True:
        data = client.post(API, {"criteria": criteria, "include_fields": FIELDS, "offset": offset, "limit": min(limit, 500)})
        batch = data.get("results") or []
        out.extend(batch)
        total = (data.get("meta") or {}).get("total", 0)
        offset += len(batch)
        if not batch or offset >= total or offset >= limit:
            break
    return out


def search(client: Client, criteria: Dict[str, Any], limit: int = 2000, use_cache: bool = True) -> List[Dict[str, Any]]:
    """Every project matching the criteria, page by page (RePORTER returns at most 500 at a time). Pass
    use_cache=False when the answer must be current, as in a weekly update."""
    out: List[Dict[str, Any]] = []
    offset = 0
    while True:
        data = client.post(API, {"criteria": criteria, "include_fields": FIELDS + ["DateAdded"], "offset": offset,
                                 "limit": 500}, use_cache=use_cache)
        batch = data.get("results") or []
        out.extend(batch)
        offset += len(batch)
        if not batch or offset >= (data.get("meta") or {}).get("total", 0) or offset >= limit:
            break
    return out


def by_names(client: Client, names: List[Dict[str, str]], added_since: str = "",
             use_cache: bool = True) -> List[Dict[str, Any]]:
    """Projects naming any of these PIs ({"first_name", "last_name"}), optionally only those added since a date."""
    criteria: Dict[str, Any] = {"pi_names": names}
    if added_since:
        criteria["date_added"] = {"from_date": added_since, "to_date": "2100-01-01"}
    return search(client, criteria, use_cache=use_cache)


def by_profiles(client: Client, profile_ids: List[int], added_since: str = "",
                use_cache: bool = True) -> List[Dict[str, Any]]:
    criteria: Dict[str, Any] = {"pi_profile_ids": [int(p) for p in profile_ids]}
    if added_since:
        criteria["date_added"] = {"from_date": added_since, "to_date": "2100-01-01"}
    return search(client, criteria, use_cache=use_cache)


def latest_by_core(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """One record per award (core project number): the most recent fiscal year, with the year range."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        core = r.get("core_project_num") or r.get("project_num")
        if core:
            groups.setdefault(core, []).append(r)
    out = {}
    for core, rs in groups.items():
        rs.sort(key=lambda r: (r.get("fiscal_year") or 0))
        latest = dict(rs[-1])
        latest["_fiscalYears"] = sorted({r.get("fiscal_year") for r in rs if r.get("fiscal_year")})
        pis = {}
        for r in rs:
            for p in r.get("principal_investigators") or []:
                if p.get("profile_id"):
                    pis[p["profile_id"]] = p
        latest["_allPis"] = list(pis.values())
        out[core] = latest
    return out


def pi_role(record: Dict[str, Any], profile_id: int) -> str:
    pis = record.get("principal_investigators") or []
    multi = len(pis) > 1
    for p in pis:
        if p.get("profile_id") == profile_id:
            if p.get("is_contact_pi"):
                return f"Contact PI (NIH FY{record.get('fiscal_year')} record)"
            return f"{'Multiple PI' if multi else 'PI'} (NIH FY{record.get('fiscal_year')} record)"
    return f"Named investigator (NIH FY{record.get('fiscal_year')} record)"
