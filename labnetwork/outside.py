"""Look up someone outside the network: who in it has published with them.

The notebook holds the lab, its first and second degree. Asked about anyone else, Lab Network searches OpenAlex for
their author record (`find`), reads their papers with 30 or fewer authors, the same rule `expand` uses, and lists the
people in the research library who are authors on them (`connect`). The person's degree is one more than the closest
of those coauthors'. Nothing is saved: the lookup changes no record, and adding the person is a separate step.

A name search costs $0.001 of the OpenAlex daily allowance and the paper list $0.0001.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .connectors import openalex as oa
from .connectors.http import Client

MAX_AUTHORS = 30          # the same limit as expand: large-team papers do not count as working together
MAX_WORKS = 200           # their newest papers with 30 or fewer authors, in one request
SELECT = "id,doi,title,publication_year,authorships"


def find(client: Client, name: str, institution: str = "", limit: int = 8) -> List[Dict[str, Any]]:
    """OpenAlex author records for a name, those at the given institution first, then by number of works."""
    return oa.search_authors(client, name, institution, limit)


def library_index(lib: Dict[str, Any]) -> Dict[str, str]:
    """OpenAlex author ID -> person ID, for every person the library has linked."""
    from .library import author_ids
    out: Dict[str, str] = {}
    for pid, rec in (lib.get("people") or {}).items():
        o = (rec or {}).get("openalex") or {}
        if o.get("status") == "linked":
            for aid in author_ids(o):
                out.setdefault(aid, pid)
    return out


def connect(client: Client, lib: Dict[str, Any], aid: str) -> Dict[str, Any]:
    """The research library's people who are authors on this OpenAlex author's papers, most shared papers first.

    `person` is set instead when the record already belongs to someone in the network.
    """
    aid = oa.short_id(aid)
    index = library_index(lib)
    record = oa.get_authors(client, [aid]).get(aid)
    summary = oa.summarize(record) if record else {"openalexId": aid, "url": oa.author_url(aid), "name": ""}
    if aid in index:
        return {"author": summary, "person": index[aid], "bridges": [], "worksRead": 0}
    flt = f"authorships.author.id:{aid},authors_count:<{MAX_AUTHORS + 1}"
    data = client.get(f"{oa.API}/works", oa._params(client, {"filter": flt, "sort": "publication_date:desc",
                                                             "per-page": MAX_WORKS, "select": SELECT}))
    works = data.get("results", [])
    bridges: Dict[str, Dict[str, Any]] = {}
    for w in works:
        seen = set()
        for a in w.get("authorships") or []:
            pid = index.get(oa.short_id((a.get("author") or {}).get("id", "")))
            if not pid or pid in seen:
                continue
            seen.add(pid)
            b = bridges.setdefault(pid, {"personId": pid, "shared": 0, "works": []})
            b["shared"] += 1
            if len(b["works"]) < 3:            # newest first, as OpenAlex returned them
                b["works"].append({"title": oa.work_title(w), "year": w.get("publication_year"),
                                   "url": w.get("doi") or w.get("id") or "",
                                   "authors": len(w.get("authorships") or [])})
    ranked = sorted(bridges.values(), key=lambda b: (-b["shared"], -(b["works"][0]["year"] or 0)))
    return {"author": summary, "person": None, "bridges": ranked, "worksRead": len(works),
            "moreWorks": len(works) >= MAX_WORKS}
