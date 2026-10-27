"""OpenAlex: author identities, works with full author lists, affiliations and topics.

https://docs.openalex.org. Free; no key needed. Without a key, requests share a free daily allowance per network
address ($0.10; a filtered list costs $0.0001, a name search $0.001); the client reads what is left from each response.
"""
from __future__ import annotations

import os
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional

from .http import Client

API = "https://api.openalex.org"
WORK_FIELDS = "id,doi,title,display_name,publication_year,publication_date,type,primary_location,authorships"


def short_id(value: str) -> str:
    """'https://openalex.org/A123' -> 'A123'."""
    m = re.search(r"([AWISCT]\d+)$", (value or "").strip())
    return m.group(1) if m else (value or "").strip()


def author_url(aid: str) -> str:
    return f"https://openalex.org/{short_id(aid)}"


def find_author_ids(text: str) -> List[str]:
    return re.findall(r"openalex\.org/(A\d+)", text or "")


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"[^a-z0-9 ]+", " ", "".join(c for c in s if not unicodedata.combining(c)).lower()).strip()


KEY_FILE = os.path.expanduser("~/.config/lab-network/openalex-key")


def api_key() -> str:
    """An OpenAlex API key, if the user added one (OPENALEX_API_KEY, or one line in ~/.config/lab-network/openalex-key).

    None is needed. A free key raises the daily allowance from $0.10 to $1; get one at https://openalex.org/settings/api.
    """
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if not key and os.path.isfile(KEY_FILE):
        with open(KEY_FILE, "r", encoding="utf-8") as f:
            key = f.read().strip()
    return key


def _params(client: Client, extra: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(extra)
    if client.mailto:
        p["mailto"] = client.mailto
    key = api_key()
    if key:
        p["api_key"] = key
    return p


def author(client: Client, aid: str) -> Optional[Dict[str, Any]]:
    try:
        return client.get(f"{API}/authors/{short_id(aid)}", _params(client, {}))
    except Exception:
        return None


def author_by_orcid(client: Client, orcid: str) -> Optional[Dict[str, Any]]:
    o = orcid.strip().replace("https://orcid.org/", "")
    try:
        return client.get(f"{API}/authors/orcid:{o}", _params(client, {}))
    except Exception:
        return None


def summarize(a: Dict[str, Any], institution_hint: str = "") -> Dict[str, Any]:
    insts = [i.get("display_name", "") for i in (a.get("last_known_institutions") or [])]
    history = []
    for aff in a.get("affiliations") or []:
        inst = (aff.get("institution") or {}).get("display_name", "")
        years = aff.get("years") or []
        if inst:
            history.append({"institution": inst, "from": min(years) if years else None, "to": max(years) if years else None})
    topics = [t.get("display_name", "") for t in (a.get("topics") or [])][:5]
    hint = _fold(institution_hint)
    hint_match = bool(hint) and any(hint in _fold(x) or _fold(x) in hint for x in insts + [h["institution"] for h in history])
    return {
        "openalexId": short_id(a.get("id", "")), "url": author_url(a.get("id", "")), "name": a.get("display_name", ""),
        "orcid": (a.get("orcid") or "").replace("https://orcid.org/", ""), "worksCount": a.get("works_count", 0),
        "citedByCount": a.get("cited_by_count", 0), "lastKnownInstitutions": insts, "affiliationHistory": history[:8],
        "topics": topics, "institutionMatches": hint_match,
    }


def search_authors(client: Client, name: str, institution: str = "", limit: int = 8) -> List[Dict[str, Any]]:
    data = client.get(f"{API}/authors", _params(client, {"search": name, "per-page": limit}))
    cands = [summarize(a, institution) for a in data.get("results", [])]
    cands.sort(key=lambda c: (not c["institutionMatches"], -c["worksCount"]))
    return cands


def get_authors(client: Client, ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    ids = [short_id(i) for i in ids if i]
    out: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        data = client.get(f"{API}/authors", _params(client, {"filter": "openalex:" + "|".join(chunk), "per-page": 50}))
        for a in data.get("results", []):
            out[short_id(a["id"])] = a
    return out


def authors_by_orcid(client: Client, orcids: Iterable[str], use_cache: bool = True) -> List[Dict[str, Any]]:
    """Every author record that carries one of these ORCID iDs, 50 iDs to a request. OpenAlex can split one researcher
    across several author records, each with the same ORCID iD."""
    orcids = list(dict.fromkeys(o.strip().replace("https://orcid.org/", "") for o in orcids if o and o.strip()))
    out: List[Dict[str, Any]] = []
    for start in range(0, len(orcids), 50):
        flt = "orcid:" + "|".join(orcids[start:start + 50])
        for page in range(1, 51):                  # 200 records a page; the first is almost always the only one
            data = client.get(f"{API}/authors", _params(client, {"filter": flt, "per-page": 200,
                                                                  **({"page": page} if page > 1 else {})}),
                              use_cache=use_cache)
            out += data.get("results", [])
            if not data.get("results") or page * 200 >= ((data.get("meta") or {}).get("count") or 0):
                break
    return out


def works_for_author(client: Client, aid: Any, since_year: int, max_works: int = 200,
                     since_date: str = "") -> List[Dict[str, Any]]:
    """Works by one author record, or by a list of records (one researcher split across several) read together."""
    ids = "|".join(short_id(a) for a in ([aid] if isinstance(aid, str) else aid))
    flt = f"author.id:{ids},from_publication_date:{since_date or f'{since_year}-01-01'}"
    works: List[Dict[str, Any]] = []
    cursor = "*"
    while cursor and len(works) < max_works:
        per = min(200, max_works - len(works))
        data = client.get(f"{API}/works", _params(client, {"filter": flt, "sort": "publication_date:desc",
                                                            "per-page": per, "select": WORK_FIELDS, "cursor": cursor}))
        batch = data.get("results", [])
        works.extend(batch)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not batch:
            break
    return works[:max_works]


def work_link(w: Dict[str, Any]) -> str:
    return w.get("doi") or w.get("id") or ""


def work_venue(w: Dict[str, Any]) -> str:
    return (((w.get("primary_location") or {}).get("source") or {}).get("display_name")) or ""


def work_title(w: Dict[str, Any]) -> str:
    return (w.get("title") or w.get("display_name") or "").strip()


def work_authors(w: Dict[str, Any], limit: int = 12) -> str:
    names = [((a.get("author") or {}).get("display_name") or a.get("raw_author_name") or "") for a in w.get("authorships") or []]
    names = [n for n in names if n]
    return ", ".join(names[:limit]) + (f", and {len(names) - limit} more" if len(names) > limit else "")


def coauthors(w: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for a in w.get("authorships") or []:
        au = a.get("author") or {}
        if au.get("id"):
            out.append({"id": short_id(au["id"]), "name": au.get("display_name", ""),
                        "orcid": (au.get("orcid") or "").replace("https://orcid.org/", ""),
                        "institutions": [i.get("display_name", "") for i in a.get("institutions") or []]})
    return out
