"""NSF award search: awards and the principal investigator named on each.

https://www.research.gov/common/webapi/awardapisearch-v1.htm. Free, no key. NSF gives no stable person ID that can be
searched, so each award is matched to a person by name and by the institution that received it.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .http import Client

API = "https://api.nsf.gov/services/v1/awards.json"
FIELDS = "id,title,startDate,expDate,awardeeName,piFirstName,piLastName,pdPIName,coPDPI,fundsObligatedAmt,date"


def award_url(award_id: str) -> str:
    return f"https://www.nsf.gov/awardsearch/showAward?AWD_ID={award_id}"


def by_pi(client: Client, first: str, last: str, since: str = "", use_cache: bool = True) -> List[Dict[str, Any]]:
    """Awards whose PI matches the name. `since` (YYYY-MM-DD) limits them to awards dated on or after it."""
    params: Dict[str, Any] = {"pdPIName": f"{first} {last}".strip(), "printFields": FIELDS, "rpp": 25, "offset": 1}
    if since:
        y, m, d = since.split("-")
        params["dateStart"] = f"{m}/{d}/{y}"
    out: List[Dict[str, Any]] = []
    while True:
        data = client.get(API, params, use_cache=use_cache)
        batch = ((data or {}).get("response") or {}).get("award") or []
        out.extend(batch)
        if len(batch) < 25 or len(out) >= 200:
            break
        params = dict(params, offset=params["offset"] + 25)
    return out
