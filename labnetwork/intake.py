"""Slack intake: lab members add people, relationships and meeting notes from a private Slack channel.

A lab member posts a submission in the lab's private channel: through a Slack Workflow form (paid Slack plans), or
by filling in a pinned template and posting it. Lab Network reads the channel with a bot token, never posts to it,
and turns each submission into records in the workspace, written under the lab member's name, so the change history
shows who added what.

Two modes, set in workspace/intake.json ("mode") or on the Data page:
  automatic  a submission is added as soon as it is read. It waits for a decision only when its person could be
             someone already in the notebook under a different form of the name, or when it is plain text rather
             than a submission.
  review     every submission waits in the Slack inbox on the Data page until someone adds or dismisses it.
Each addition can be undone from the inbox. Undo archives what the submission created and nothing else: a person it
added stays when later records refer to them.

Who can submit: only the Slack accounts listed under "members", each mapped to a lab member. A typed message belongs
to whoever posted it, even when an app sent it for them. A form posted by an app names its sender on its first
"From:" line, and is read only from the app the lab has chosen to trust ("formBot"): the first form from an app
waits in the inbox until someone chooses "Trust this form", and posts from any other app are then ignored. Messages
from anyone off the roster are ignored and their content is not kept, and so is anything that is not a top-level
message.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from .connectors import slack
from .connectors.http import Client, HttpError
from .expand import family_key, name_parts, name_score
from .model import norm_text
from .store import Workspace, utc_now

INTAKE = "intake.json"
MARKER = "lab network submission"
DEFAULT_TOKEN_FILE = "~/.config/lab-network/slack-token"
MODES = ("automatic", "review")
TOKEN_RX = re.compile(r"^(?:xoxe\.)?xox[a-z]-[A-Za-z0-9-]{6,}$")

# Labels in a submission ("Name: ..."), and the order a form writes them in.
LABELS = {"from": "from", "submitted by": "from", "name": "name", "person": "name", "institution": "institution",
          "position": "position", "role": "position", "relationship": "relationship",
          "how you know them": "relationship", "when": "when", "date": "when", "where": "where",
          "where or how you met": "where", "notes": "notes", "what you talked about": "notes",
          "follow-up": "followUp", "follow up": "followUp", "followup": "followUp", "link": "link"}
EARLY = ["from", "name", "institution", "position", "relationship", "when", "where"]   # written before the notes
MULTILINE = {"notes", "where", "followUp"}   # answers that may run over several lines; the rest are one line
# Field lengths the notebook accepts; longer answers are shortened rather than refused.
LIMITS = {"name": 160, "institution": 200, "position": 300, "link": 2000, "where": 2000, "notes": 4800,
          "followUp": 2000, "relationship": 180}

# The "Relationship" answers: (start of the answer, relationship type, direction, label). In the notebook a mentor
# record runs from mentor to mentee and a committee record from committee member to student.
RELATIONSHIPS = [
    ("met", "met", "", "Met"),
    ("collaborator", "collaborator", "", "Collaborator"),
    ("coauthor", "coauthor", "", "Coauthor"),
    ("my mentor", "mentor", "they-mentored-member", "Mentor or advisor"),
    ("my advisor", "mentor", "they-mentored-member", "Mentor or advisor"),
    ("my mentee", "mentor", "member-mentored-them", "Mentee or trainee"),
    ("my trainee", "mentor", "member-mentored-them", "Mentee or trainee"),
    ("on my committee", "committee", "they-served-on-member-committee", "On the lab member's committee"),
    ("i'm on their committee", "committee", "member-served-on-their-committee", "The lab member is on their committee"),
    ("i am on their committee", "committee", "member-served-on-their-committee", "The lab member is on their committee"),
    ("colleague", "labmate", "", "Colleague in the same lab or department"),
    ("other", "", "", "Other"),
]

# Given names that are the same name (Bill and William). A short form in one group only ("bill") identifies the
# name; one in several groups ("sam": Samuel or Samantha) does not.
_NICKNAMES = [
    "william bill billy will liam", "robert bob bobby rob robbie bert", "richard rick dick rich ricky",
    "katherine kathryn catherine kathleen kate katie kathy cathy kat", "elizabeth liz beth betsy lizzie eliza betty",
    "james jim jimmy jamie", "michael mike mikey mick", "david dave davey", "christopher chris topher",
    "christina christine kristina kristin chris tina", "thomas tom tommy", "margaret maggie meg peggy marge",
    "jennifer jen jenny", "susan sue susie suzanne", "andrew andy drew", "anthony tony", "edward ed eddie ted ned",
    "theodore ted teddy theo", "joseph joe joey", "nicholas nick nicky", "alexander alex sasha xander",
    "alexandra alex sasha lexi", "steven stephen steve", "gregory greg", "daniel dan danny", "benjamin ben benji",
    "patrick pat", "patricia pat patty trish", "john jack johnny", "henry hank harry", "charles charlie chuck",
    "lawrence larry", "gerald jerry", "jerome jerry", "terrence terence terry", "theresa teresa terry tess",
    "rebecca becky becca", "deborah debbie deb", "victoria vicky tori", "abigail abby", "samuel sam sammy",
    "samantha sam sammy", "jacob jake", "joshua josh", "zachary zach zack", "timothy tim", "kenneth ken kenny",
    "ronald ron", "donald don", "raymond ray", "douglas doug", "frederick fred freddie", "albert al bert",
    "allison alison ali allie", "alicia ali", "amanda mandy", "melissa missy mel", "pamela pam", "sandra sandy",
    "cynthia cindy", "judith judy", "barbara barb", "dorothy dot dottie", "virginia ginny", "jessica jess jessie",
    "stephanie steph", "nathaniel nathan nate nat", "vincent vince vinny", "phillip philip phil", "peter pete",
    "walter walt", "leonard leo len lenny", "eugene gene", "howard howie", "harold hal harry", "francis frank",
    "franklin frank", "isabella isabel izzy bella", "madeline madeleine maddie", "emily em emmy", "gabriel gabe",
    "gabrielle gabby gabi", "jacqueline jackie", "caroline carrie", "carolyn carol carrie", "valerie val",
    "cassandra cassie", "mattias matias", "matthew matt", "jonathan jon jonny", "nathalie natalie nat",
    "tobias toby",
]
_GROUPS: Dict[str, List[frozenset]] = {}
for _g in _NICKNAMES:
    _set = frozenset(_g.split())
    for _n in _set:
        _GROUPS.setdefault(_n, []).append(_set)
# Short forms that are also given names in their own right (Jack is not always John): they match only weakly.
_STANDALONE = {"liam", "jack", "will", "max", "alex", "sam", "ben", "leo", "theo", "drew", "nat", "kate", "jamie",
               "jess", "jessie", "tina", "kat", "harry", "frank", "ray", "mel", "sandy", "tess", "al", "bert", "gene",
               "hal", "toby", "ali", "dan", "danny", "johnny", "billy", "sammy", "bella", "lexi", "tori", "eliza",
               "beth", "betty", "meg", "maggie", "carrie", "carol", "teddy", "sasha", "xander", "nate", "allie",
               "izzy", "jackie", "val", "chris", "pat", "terry", "jerry"}
_HONORIFICS = {"dr", "prof", "professor", "mr", "mrs", "ms", "mx"}               # dropped from the front of a name
_DEGREES = {"phd", "md", "mph", "msw", "psyd", "edd", "lcsw", "dphil", "mba"}   # and from the end (not "Ma", a name)


class IntakeError(Exception):
    pass


_lock = threading.RLock()    # one check or decision at a time in this process


# --------------------------------------------------------------------------- storage and settings

def empty() -> Dict[str, Any]:
    return {"format": "labnetwork-intake", "version": 1, "mode": "review", "channel": "", "channelName": "",
            "token": {"file": "", "key": ""}, "members": {}, "formBot": {}, "pollMinutes": 10, "teamUrl": "",
            "lastTs": "", "lastCheck": "", "lastError": "", "items": {}}


def path_of(ws: Workspace) -> str:
    return os.path.join(ws.path, INTAKE)


def load(ws: Workspace) -> Dict[str, Any]:
    p = path_of(ws)
    if not os.path.exists(p):
        return empty()
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    for k, v in empty().items():
        data.setdefault(k, v)
    return data


def save(ws: Workspace, data: Dict[str, Any]) -> None:
    p = path_of(ws)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def token_status(data: Dict[str, Any]) -> Tuple[str, str]:
    """(token, problem). The token comes from LABNETWORK_SLACK_TOKEN, else from the file named in intake.json (with
    "key" for a JSON file of several secrets), else from ~/.config/lab-network/slack-token. A problem never quotes
    the file's contents."""
    env = os.environ.get("LABNETWORK_SLACK_TOKEN", "").strip()
    if env:
        return (env, "") if TOKEN_RX.match(env) else ("", "LABNETWORK_SLACK_TOKEN does not look like a Slack token "
                                                          "(Slack tokens start with xoxb-)")
    spec = data.get("token") or {}
    path = os.path.expanduser(spec.get("file") or DEFAULT_TOKEN_FILE)
    if not os.path.isfile(path):
        return "", (f"no Slack token: save the bot token as one line in {DEFAULT_TOKEN_FILE}, set "
                    "LABNETWORK_SLACK_TOKEN, or name a token file during setup")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except (OSError, UnicodeDecodeError):
        return "", f"the token file {path} could not be read"
    key = spec.get("key") or ""
    if key:
        try:
            value = str(json.loads(raw).get(key) or "").strip()
        except (ValueError, AttributeError):
            return "", f"the token file {path} is not a JSON file of secrets, so a key does not apply"
        if not value:
            return "", f"the token file {path} has no value under {key}"
        return (value, "") if TOKEN_RX.match(value) else ("", f"the value under {key} in {path} does not look like "
                                                                "a Slack token (they start with xoxb-)")
    if raw.lstrip().startswith("{"):
        return "", (f"the token file {path} holds several values; name the one to use with --token-key")
    for line in raw.splitlines():
        if TOKEN_RX.match(line.strip()):
            return line.strip(), ""
    return "", f"the token file {path} has no Slack token in it (a line that starts with xoxb-)"


def token(data: Dict[str, Any]) -> str:
    return token_status(data)[0]


def configured(data: Dict[str, Any]) -> bool:
    return bool(data.get("channel") and data.get("members"))


def _safe(exc: BaseException, tok: str = "") -> str:
    """An error message fit to show and store: never one that could carry the token."""
    msg = str(exc)
    if (tok and tok in msg) or "Bearer" in msg or "xox" in msg:
        return f"{type(exc).__name__} (details withheld because they could include the token)"
    return msg[:300]


# --------------------------------------------------------------------------- reading messages

def _unescape(s: str) -> str:
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _inline(s: str) -> str:
    """Slack markup to plain text: <@U1|name> -> @U1, <https://x|label> -> https://x, dates to their fallback text."""
    s = re.sub(r"<!date\^\d+\^[^|>]*\|([^>]*)>", r"\1", s)
    s = re.sub(r"<@([UW][A-Z0-9_]+)(?:\|[^>]*)?>", r"@\1", s)
    s = re.sub(r"<mailto:([^|>]+)(?:\|[^>]*)?>", r"\1", s)
    s = re.sub(r"<(https?://[^|>]+)(?:\|[^>]*)?>", r"\1", s)
    s = re.sub(r"<#([CG][A-Z0-9]+)(?:\|([^>]*))?>", lambda m: "#" + (m.group(2) or m.group(1)), s)
    s = re.sub(r"<!(here|channel|everyone)[^>]*>", r"@\1", s)
    return _unescape(s)


def _flatten(blocks: List[Dict[str, Any]]) -> str:
    """Plain text of a message's blocks (rich text, or section blocks with mrkdwn), for messages without `text`."""
    out: List[str] = []

    def walk(el: Any) -> None:
        if isinstance(el, list):
            for x in el:
                walk(x)
            return
        if not isinstance(el, dict):
            return
        t = el.get("type")
        if t == "text":
            out.append(el.get("text", ""))
        elif t in ("mrkdwn", "plain_text"):
            out.append(el.get("text", "") + "\n")
        elif t == "user":
            out.append(f"<@{el.get('user_id', '')}>")
        elif t == "link":
            out.append(f"<{el.get('url', '')}>")
        elif t == "date":
            out.append(f"<!date^{el.get('timestamp', 0)}^{{date}}|{el.get('fallback', '')}>")
        elif t in ("rich_text_section", "rich_text_preformatted", "rich_text_quote"):
            walk(el.get("elements"))
            out.append("\n")
        elif t == "rich_text_list":
            for item in el.get("elements") or []:
                walk(item)
        elif t == "rich_text":
            walk(el.get("elements"))
        elif t == "section":
            walk(el.get("text"))
            walk(el.get("fields"))
    walk(blocks)
    return "".join(out)


def message_text(m: Dict[str, Any]) -> str:
    text = m.get("text") or ""
    if MARKER not in norm_text(_inline(text)) and m.get("blocks"):
        flat = _flatten(m["blocks"])
        if flat.strip():
            text = flat
    return text


_TEMPLATE_TEXT = re.compile(r"^\s*(for relationship, use one of|to add someone to lab network)", re.I)
_LABEL_RX = re.compile(r"^\s*[*_~>]*\s*([A-Za-z][A-Za-z '\-]{0,40}?)\s*[*_~]*\s*:\s*[*_~]*\s*(.*?)\s*[*_~]*\s*$")


def _label(line: str) -> Optional[Tuple[str, str]]:
    m = _LABEL_RX.match(_inline(line))
    key = LABELS.get(m.group(1).strip().lower()) if m else None
    return (key, m.group(2)) if key else None


def parse(text: str, ordered: bool = False) -> Dict[str, Any]:
    """The submission's fields, or {"kind": "text"} for a message without the "Lab Network submission" line.

    A typed message takes each label where it first appears, in any order. A form posted by an app (`ordered`)
    always writes the labels in the same order, so its labels are found by position: the one-line answers before the
    notes, the notes after them, then the last Follow-up and Link lines. Text in the notes that looks like a label
    therefore stays in the notes, and the sender comes only from the first From line.
    """
    raw = text or ""
    plain = _inline(raw)
    if MARKER not in norm_text(plain):
        return {"kind": "text", "fields": {}, "text": plain.strip()}
    lines = raw.splitlines()
    first = next(i for i, ln in enumerate(lines) if MARKER in norm_text(_inline(ln)))
    lines = [ln for ln in lines[first + 1:] if not _TEMPLATE_TEXT.match(_inline(ln))]
    labels = [_label(ln) for ln in lines]
    def first(key, lo=0, hi=None):
        return next((i for i in range(lo, len(lines) if hi is None else hi) if labels[i] and labels[i][0] == key), None)

    def last(key, lo=0):
        return next((i for i in range(len(lines) - 1, lo - 1, -1) if labels[i] and labels[i][0] == key), None)

    starts: Dict[str, int] = {}
    notes = first("notes")
    if notes is not None:
        starts["notes"] = notes
    # The one-line answers: where each first appears before the notes, in any order. After the notes start, a line
    # that looks like a label is part of the notes (until Follow-up or Link).
    for key in EARLY:
        i = first(key, 0, notes)
        if i is not None:
            starts[key] = i
    # Follow-up and Link come after the notes: the last such line, so the same words inside the notes stay there.
    for key in ("followUp", "link"):
        i = last(key, notes + 1 if notes is not None else 0)
        if i is not None and i not in starts.values():
            starts[key] = i
    order = sorted(starts.items(), key=lambda kv: kv[1])
    fields: Dict[str, str] = {}
    for n, (key, i) in enumerate(order):
        if key == "from":
            mention = re.search(r"<@([UW][A-Z0-9_]+)", lines[i])
            fields["from"] = mention.group(1) if mention else ""
            continue
        value = _date_value(lines[i]) if key == "when" else labels[i][1]
        if key in MULTILINE:             # runs until the next answer's label; repeated labels stay in the text
            stop = order[n + 1][1] if n + 1 < len(order) else len(lines)
            more = [_inline(ln).strip() for ln in lines[i + 1:stop]]
            value = "\n".join([value] + [x for x in more if x]).strip()
        fields[key] = value.strip()
    return {"kind": "form", "fields": fields, "text": plain.strip()}


def _date_value(raw_line: str) -> str:
    """The When answer: a Slack date token gives an exact date; anything else is kept as written."""
    token_ = re.search(r"<!date\^(\d+)\^[^|>]*\|([^>]*)>", raw_line)
    if token_:
        shown = parse_date(token_.group(2))
        if shown:
            return shown
        # A picked date is stored as midnight somewhere; noon UTC after it falls on that same date almost everywhere.
        return (_dt.datetime.fromtimestamp(int(token_.group(1)), _dt.timezone.utc)
                + _dt.timedelta(hours=12)).date().isoformat()
    lab = _label(raw_line)
    return lab[1] if lab else ""


def parse_date(value: str) -> str:
    """An ISO date for an answer such as 2026-10-17, October 17, 2026 or 10/17/2026; "" for anything else,
    including dates that do not exist."""
    v = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", (value or "").strip())
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", v)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return ""
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%A, %B %d, %Y", "%a, %b %d, %Y", "%m/%d/%Y", "%d %B %Y", "%B %d %Y",
                "%b %d %Y"):
        try:
            return _dt.datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def relationship(answer: str) -> Tuple[str, str, str]:
    """(type, direction, label) for the relationship answer; type is "" for none."""
    a = norm_text(answer or "").replace("’", "'")
    for start, typ, direction, label in RELATIONSHIPS:
        if a.startswith(norm_text(start)):
            return typ, direction, label
    return "", "", ""


# --------------------------------------------------------------------------- matching people

def clean_name(name: str) -> str:
    """A name without a title in front or degrees at the end: "Dr. Dana Park, PhD" -> "Dana Park". Everything else
    stays, such as "Md." at the start of a name or "Jr." at the end."""
    words = [w for w in re.split(r"\s+", (name or "").strip()) if w]
    key = lambda w: re.sub(r"[^a-z]", "", w.lower())
    while len(words) > 2 and key(words[0]) in _HONORIFICS:
        words.pop(0)
    while len(words) > 2 and key(words[-1]) in _DEGREES:
        words.pop()
    return " ".join(words).strip().rstrip(",").strip()


def _given(a: str, b: str) -> int:
    """How well two names' given names agree: 3 the same name, 2 a nickname of it (Bill and William), 1 compatible
    but uncertain (an initial, a prefix such as Jun and Junjie, or a short form two names share), 0 different."""
    a, b = clean_name(a), clean_name(b)
    s = name_score(a, b)
    if s == 2:
        return 3
    ga = next((g for g in name_parts(a)[0] if len(g) > 1), "")
    gb = next((g for g in name_parts(b)[0] if len(g) > 1), "")
    if ga and gb and ga != gb:
        short, full = (ga, gb) if len(ga) <= len(gb) else (gb, ga)
        groups = _GROUPS.get(short, [])
        if len(short) <= 5 and len(groups) == 1 and full in groups[0] and short not in _STANDALONE:
            return 2
        if any(full in g for g in groups) or any(short in g for g in _GROUPS.get(full, [])):
            return 1
    return 1 if s else 0


def candidates(svc, name: str, institution: str = "") -> List[Dict[str, Any]]:
    """People already in the notebook who could be this person: the same family name, with how well the given name
    and the institution agree."""
    from .library import org_match
    name = clean_name(name)
    fam = family_key(name)
    out = []
    with svc.lock:                                       # an import may be changing the notebook
        people = [(pid, c["name"], c.get("institution") or "") for pid, c in svc.nb.active("contacts")]
    for pid, nm, inst in people:
        if not fam or family_key(nm) != fam:
            continue
        g = _given(name, nm)
        out.append({"id": pid, "name": nm, "institution": inst, "given": g,
                    "sameInstitution": bool(institution and inst and org_match(institution, inst)),
                    "otherInstitution": bool(institution and inst and not org_match(institution, inst))})
    out.sort(key=lambda x: (-x["given"], -x["sameInstitution"], x["name"]))
    return out


def decide(cands: List[Dict[str, Any]]) -> Tuple[str, str]:
    """("existing", id), ("new", "") or ("hold", reason), for adding without asking anyone. A submission is attached
    to someone only on the same name or a nickname of it; an initial or a prefix needs the same institution too."""
    def pick(xs):
        if len(xs) == 1:
            return xs[0]
        same = [c for c in xs if c["sameInstitution"]]
        return same[0] if len(same) == 1 else None
    for level in (3, 2):
        xs = [c for c in cands if c["given"] == level]
        if xs:
            p = pick(xs)
            if p and not p["otherInstitution"]:
                return "existing", p["id"]
            if len(xs) > 1:
                return "hold", f"{len(xs)} people already here have this name"
            return "hold", f"{xs[0]['name']} is already here, at a different institution"
    weak = [c for c in cands if c["given"] == 1]
    if weak:
        same = [c for c in weak if c["sameInstitution"]]
        if len(same) == 1:
            return "existing", same[0]["id"]
        others = len(weak) - 1
        return "hold", f"the person could be {weak[0]['name']}" + (f" or {others} other{'s' if others > 1 else ''}"
                                                                   if others else "") + " already here"
    if any(c["sameInstitution"] for c in cands):
        return "hold", "someone with this family name is already here, at the same institution"
    return "new", ""


# --------------------------------------------------------------------------- adding a submission

def _actor(item: Dict[str, Any]) -> str:
    return f"{item.get('submitter') or 'A lab member'} (via Slack)"


def apply_item(svc, item: Dict[str, Any], person: str, decided_by: str, confirm_new: bool = False) -> None:
    """Write a submission into the notebook: its person (new or existing), a meeting note, a relationship.

    Each record is listed in item["created"] as soon as it is written, so a submission that fails part way can be
    undone, or added again without writing anything twice.
    """
    from .service import EditConflict, NotFound, ServiceError
    created: List[List[str]] = item.setdefault("created", [])
    steps: Dict[str, str] = item.setdefault("steps", {})
    member, f, who = item["member"], item.get("fields") or {}, item.get("submitter") or "A lab member"
    actor = _actor(item)
    posted = (item.get("postedAt") or utc_now())[:10]

    def done(step: str, coll: str, rid: str) -> None:
        steps[step] = rid
        created.append([coll, rid])

    def clip(key: str, value: str) -> str:
        return (value or "").strip()[:LIMITS.get(key, 2000)]

    if steps.get("contact") and person not in ("", "new", steps["contact"]):
        # An earlier attempt added a new person, and now someone already here is chosen: take back that attempt,
        # leaving the submission open in case this one fails too.
        status, reason = item.get("status"), item.get("reason", "")
        undo_item(svc, item, decided_by)
        item.update({"status": status, "reason": reason})
        created, steps = item.setdefault("created", []), item.setdefault("steps", {})
        created.clear()
        steps.clear()

    try:
        if item["kind"] == "text":
            if not person or person == "new":
                raise IntakeError("Choose who this note is about")
            pid, _ = svc._require_contact(person)
            if "note" not in steps:
                res = svc.create_note({"fields": {"contactId": pid, "metBy": who, "context": "Shared in Slack",
                                                  "learning": item.get("text", "")[:4000], "recordedBy": who,
                                                  "date": posted}}, actor)
                done("note", "meetings", res["id"])
        else:
            name = clean_name(f.get("name") or "")
            if steps.get("contact") and svc.nb.is_active("contacts", steps["contact"]):
                pid = steps["contact"]                   # added by an earlier attempt
            elif person == "new" or not person:
                if not name:
                    raise IntakeError("The submission has no name")
                if not confirm_new:
                    same = [c for c in candidates(svc, name, f.get("institution", "")) if c["given"] >= 2]
                    if same:
                        raise IntakeError(f"{same[0]['name']} is already in the notebook. Choose them, or choose "
                                          "Someone new again to add a separate person.")
                fields: Dict[str, Any] = {"name": clip("name", name), "institution": clip("institution", f.get("institution", "")),
                                          "role": clip("position", f.get("position", ""))}
                if re.match(r"^https?://", f.get("link", "")) and len(f["link"]) <= LIMITS["link"]:
                    fields["website"] = f["link"]
                src = item.get("permalink") or ""
                fields["sources"] = [{"url": src if src.startswith("https://") else "",
                                      "note": f"Added from a Slack submission by {who} on {posted}"}]
                pid = svc.create_contact({"fields": fields, "confirmNotDuplicate": True}, actor)["id"]
                done("contact", "contacts", pid)
            else:
                pid, _ = svc._require_contact(person)
            typ, direction, label = relationship(f.get("relationship", ""))
            when = parse_date(f.get("when", ""))
            notes = (f.get("notes") or "").strip()
            if person and person != "new" and not steps.get("contact"):
                # Someone already here keeps their details; what the submission says about them goes in the note.
                cur = svc.nb.records["contacts"][pid]
                given = [(lab, f.get(key, "").strip()) for lab, key, field in
                         (("Institution", "institution", "institution"), ("Position", "position", "role"))
                         if f.get(key, "").strip() and norm_text(f.get(key, "")) != norm_text(cur.get(field) or "")]
                if given:
                    notes = (notes + "\n" if notes else "") + "; ".join(f"{k} given in Slack: {v}" for k, v in given) + "."
            if (typ == "met" or f.get("where") or notes or f.get("followUp")) and "note" not in steps:
                context = clip("where", f.get("where", "")) or f"Reported in Slack by {who}"
                payload: Dict[str, Any] = {"fields": {"contactId": pid, "metBy": who[:200], "date": when,
                                                      "context": context, "learning": clip("notes", notes),
                                                      "followUp": clip("followUp", f.get("followUp", "")),
                                                      "recordedBy": who[:200]}}
                if typ == "met" and member != pid:
                    payload["tie"] = {"from": member, "label": "Met"}
                res = svc.create_note(payload, actor)
                done("note", "meetings", res["id"])
                if res.get("tie"):
                    done("metTie", "edges", res["tie"])
            if typ and typ != "met" and member != pid and "tie" not in steps:
                a, b = (pid, member) if direction in ("they-mentored-member", "they-served-on-member-committee") \
                    else (member, pid)
                evidence = f"{who} reported this in Slack on {posted}: {label.lower()}." + (f" {notes}" if notes else "")
                # A lab member who reports the same relationship again adds a note, not a second record.
                again = any(e.get("from") == a and e.get("to") == b and e.get("type") == typ and
                            e.get("sourceTitle") == "Slack submission" for _, e in svc.nb.active("edges"))
                try:
                    if again:
                        raise EditConflict("This lab member already reported this relationship")
                    res = svc.create_tie({"fields": {"from": a, "to": b, "type": typ, "provenance": "reported",
                                                     "evidence": evidence[:3000], "sourceTitle": "Slack submission",
                                                     "sourceUrl": item.get("permalink", ""),
                                                     "relationshipStatus": "unknown", "relationshipLabel": label,
                                                     "relationshipStart": when}}, actor)
                    done("tie", "edges", res["id"])
                except EditConflict:
                    steps["tie"] = ""                    # the same record is already here
    except (ServiceError, EditConflict, NotFound) as exc:
        details = getattr(exc, "details", None)
        extra = "; ".join(str(d) for d in details) if isinstance(details, list) else ""
        raise IntakeError(str(exc) + (f": {extra}" if extra else ""))
    item.update({"status": "added", "person": pid, "decidedAt": utc_now(), "decidedBy": decided_by, "reason": ""})


def undo_item(svc, item: Dict[str, Any], actor: str) -> None:
    """Archive what a submission added, newest first. A person it added stays when other records refer to them."""
    from .service import NotFound, ServiceError
    mine = {(c, r) for c, r in item.get("created") or []}
    kept = []
    for coll, rid in reversed(item.get("created") or []):
        if rid in svc.nb.archived.get(coll, set()):
            continue
        if coll == "contacts":
            refs = [1 for eid, e in svc.nb.active("edges") if rid in (e["from"], e["to"]) and ("edges", eid) not in mine]
            refs += [1 for mid, m in svc.nb.active("meetings") if m.get("contactId") == rid and ("meetings", mid) not in mine]
            if refs:
                kept.append(f"{svc.nb.records['contacts'][rid]['name']} stays, because {len(refs)} other record"
                            f"{'s' if len(refs) > 1 else ''} refer to them")
                continue
        try:
            svc.archive(coll, rid, actor)
        except (NotFound, ServiceError):
            continue
    item.update({"status": "undone", "decidedAt": utc_now(), "decidedBy": actor, "reason": "; ".join(kept)})


# --------------------------------------------------------------------------- reading the channel

def _ignored(item: Dict[str, Any], reason: str) -> Dict[str, Any]:
    item.update({"status": "ignored", "reason": reason})
    return item                                          # its content is not kept


def _new_item(m: Dict[str, Any], data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ts = m.get("ts", "")
    if m.get("thread_ts") and m.get("thread_ts") != ts:
        return None                                       # a reply in a thread
    if m.get("subtype") not in (None, "bot_message", "thread_broadcast", "file_share"):
        return None                                       # joins, topic changes, pins, deletions
    # A lab member's own message is theirs, even when an app sent it for them (messages posted with a person's
    # token carry the app's bot_id). Anything else from an app is read as the lab's form.
    poster = m.get("user") or ""
    from_app = (bool(m.get("bot_id")) or m.get("subtype") == "bot_message") and poster not in (data.get("members") or {})
    parsed = parse(message_text(m), ordered=from_app)
    posted = _dt.datetime.fromtimestamp(float(ts or 0), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    item: Dict[str, Any] = {"ts": ts, "kind": parsed["kind"], "postedAt": posted,
                            "permalink": slack.permalink(data.get("teamUrl", ""), data.get("channel", ""), ts)}
    app: Dict[str, str] = {}
    if from_app:
        if parsed["kind"] != "form":
            return None                                   # another app's message
        prof = m.get("bot_profile") or {}
        app = {"botId": m.get("bot_id", ""), "appId": m.get("app_id") or prof.get("app_id", ""),
               "name": prof.get("name", "")}
        if data.get("formBot") and not _same_app(app, data["formBot"]):
            item["from"] = ""
            return _ignored(item, "posted by an app other than the lab's form")
        sender = parsed["fields"].get("from", "")
    else:
        sender = poster
    item["from"] = sender
    member = (data.get("members") or {}).get(sender)
    if not member:
        return _ignored(item, "sent by someone who is not on the lab roster")
    if parsed["kind"] == "form" and not any(v for k, v in parsed["fields"].items() if k != "from"):
        return _ignored(item, "an empty template")
    item.update({"member": member["contactId"], "submitter": member.get("name", ""), "fields": parsed["fields"],
                 "text": parsed["text"][:4000], "status": "new"})
    if from_app and not data.get("formBot"):
        # Any channel member can make an app post here, so Lab Network trusts an app only when a person says so.
        item.update({"status": "held", "untrusted": True, "app": app,
                     "reason": "it came from an app Lab Network does not trust yet. If it is your lab's form, choose "
                               "Trust this form"})
    return item


def _same_app(a: Dict[str, str], trusted: Dict[str, str]) -> bool:
    return a.get("botId") == trusted.get("botId") and (not trusted.get("appId") or a.get("appId") == trusted["appId"])


def _settle(svc, data: Dict[str, Any], item: Dict[str, Any]) -> None:
    """Decide what to do with a newly read submission, and in automatic mode add it."""
    automatic = data.get("mode") == "automatic"
    if item["kind"] == "text":
        item.update({"status": "held" if automatic else "pending",
                     "reason": "it is a plain message rather than a submission, so it does not say who it is about"})
        return
    f = item.get("fields") or {}
    if not clean_name(f.get("name") or ""):
        item.update({"status": "held" if automatic else "pending", "reason": "the submission has no name"})
        return
    cands = candidates(svc, f["name"], f.get("institution", ""))
    action, value = decide(cands)
    item["candidates"] = [{k: c[k] for k in ("id", "name", "institution")} for c in cands[:6]]
    if not automatic:
        item.update({"status": "pending", "suggestion": value if action == "existing" else ("new" if action == "new" else ""),
                     "reason": value if action == "hold" else ""})
        return
    if action == "hold":
        item.update({"status": "held", "reason": value})
        return
    try:
        apply_item(svc, item, value if action == "existing" else "new", "added automatically", confirm_new=True)
    except IntakeError as exc:                            # keep the submission, and what it wrote, for a decision
        item.update({"status": "held", "reason": f"it could not be added ({exc})"})


def check(svc, client: Optional[Client] = None) -> Dict[str, Any]:
    """Read new messages from the channel and settle each one. Returns counts of what happened."""
    with _lock:
        data = load(svc.ws)
        if (svc.ws.config.get("settings") or {}).get("demo"):
            raise IntakeError("The demo lab is fictional, so it does not read Slack.")
        if not data.get("channel"):
            raise IntakeError("Slack intake is not set up: no channel")
        tok, problem = token_status(data)
        if not tok:
            data["lastError"] = problem
            save(svc.ws, data)
            raise IntakeError(problem)
        client = client or Client(cache_dir=None)
        counts = {"read": 0, "added": 0, "held": 0, "pending": 0, "ignored": 0}
        try:
            if not data.get("teamUrl"):
                data["teamUrl"] = slack.auth_test(client, tok).get("url", "")
            messages = slack.history(client, tok, data["channel"], oldest=data.get("lastTs") or "")
            data["lastError"] = ""
        except Exception as exc:                          # nothing read; say why, without the token
            data["lastError"] = _safe(exc, tok)
            data["lastCheck"] = utc_now()
            save(svc.ws, data)
            raise IntakeError(data["lastError"])
        try:
            for m in messages:
                ts = m.get("ts", "")
                if not ts or ts in data["items"]:
                    continue
                if float(ts) > float(data.get("lastTs") or 0):
                    data["lastTs"] = ts
                try:
                    item = _new_item(m, data)
                except Exception as exc:
                    item = {"ts": ts, "status": "ignored", "reason": f"could not be read ({type(exc).__name__})"}
                if item is None:
                    continue
                counts["read"] += 1
                data["items"][ts] = item
                if item["status"] == "new":
                    try:
                        _settle(svc, data, item)
                    except Exception as exc:              # never leave a submission unseen
                        item.update({"status": "held", "reason": f"it could not be added ({_safe(exc, tok)})"})
                counts[item["status"]] = counts.get(item["status"], 0) + 1
        finally:
            data["lastCheck"] = utc_now()
            save(svc.ws, data)
        return counts


def decide_item(svc, ts: str, action: str, person: str, actor: str, confirm_new: bool = False) -> Dict[str, Any]:
    """A person's decision on one submission: add (with the chosen person), dismiss, or undo."""
    with _lock:
        data = load(svc.ws)
        item = (data.get("items") or {}).get(ts)
        if not item or item.get("status") == "ignored":
            raise IntakeError("No such submission")
        try:
            if action == "add":
                if item["status"] not in ("pending", "held"):
                    raise IntakeError("This submission was already handled")
                apply_item(svc, item, person, actor, confirm_new=confirm_new)
            elif action == "dismiss":
                if item["status"] not in ("pending", "held"):
                    raise IntakeError("This submission was already handled")
                if item.get("created"):                   # a partial addition: remove what it wrote
                    undo_item(svc, item, actor)
                item.pop("untrusted", None)
                item.update({"status": "dismissed", "decidedAt": utc_now(), "decidedBy": actor})
            elif action == "undo":
                if not (item["status"] == "added" or (item["status"] in ("pending", "held") and item.get("created"))):
                    raise IntakeError("Only an added submission can be undone")
                undo_item(svc, item, actor)
            elif action == "trust":
                if not item.get("untrusted"):
                    raise IntakeError("This submission is not waiting for that")
                data["formBot"] = dict(item["app"])
                for other in sorted(data["items"].values(), key=lambda x: float(x.get("ts") or 0)):
                    if not other.get("untrusted") or other.get("status") != "held":
                        continue
                    other.pop("untrusted", None)
                    if _same_app(other.get("app") or {}, data["formBot"]):
                        other.update({"status": "new", "reason": ""})
                        _settle(svc, data, other)
                    else:                                 # another app: no longer a candidate
                        for k in ("fields", "text", "member", "submitter"):
                            other.pop(k, None)
                        other.update({"status": "ignored", "reason": "posted by an app other than the lab's form"})
            else:
                raise IntakeError(f"Unknown action {action}")
        finally:
            save(svc.ws, data)                            # what an attempt wrote is kept, even when it failed
        return item


def map_members(svc, client: Client, tok: str, channel: str,
                skip: Any = ()) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    """Slack accounts in the channel matched to lab members by name; also the accounts that matched no one.
    Only the same given name or a nickname of it counts (an initial or a shared short form does not). Accounts in
    `skip` (already on the roster) are left as they are."""
    roots = [(pid, svc.nb.records["contacts"][pid]["name"]) for pid in svc.nb.root_ids
             if pid in svc.nb.records["contacts"]]
    mapped: Dict[str, Dict[str, str]] = {}
    unmatched: List[str] = []
    for uid in slack.members(client, tok, channel):
        if uid in skip:
            continue
        u = slack.user(client, tok, uid)
        if u["bot"] or u["deleted"]:
            continue
        hits = [(pid, nm) for pid, nm in roots if family_key(nm) == family_key(u["name"]) and _given(u["name"], nm) >= 2]
        if len(hits) == 1:
            mapped[uid] = {"contactId": hits[0][0], "name": hits[0][1], "slackName": u["name"]}
        else:
            unmatched.append(f"{u['name'] or 'no name'} ({uid})")
    return mapped, unmatched


def set_members(svc, pairs: Dict[str, str]) -> Dict[str, Any]:
    """Add or correct Slack accounts on the roster: {Slack user ID: person ID}; an empty person ID removes one."""
    with _lock:
        data = load(svc.ws)
        members = dict(data.get("members") or {})
        for uid, pid in pairs.items():
            uid = (uid or "").strip()
            if not re.fullmatch(r"[UW][A-Z0-9_]+", uid):
                raise IntakeError(f"{uid!r} is not a Slack user ID (they start with U, as in U0123ABCD)")
            if not pid:
                members.pop(uid, None)
                continue
            if not svc.nb.is_active("contacts", pid):
                raise IntakeError(f"No person with ID {pid!r} in the notebook")
            members[uid] = {"contactId": pid, "name": svc.nb.records["contacts"][pid]["name"],
                            "slackName": (members.get(uid) or {}).get("slackName", "")}
        data["members"] = members
        save(svc.ws, data)
        return data


def view(svc) -> Dict[str, Any]:
    """The inbox for the Data page: settings (never the token), every submission waiting for a decision with its
    possible matches checked again now, the most recent others, and counts."""
    data = load(svc.ws)
    names = {pid: c["name"] for pid, c in svc.nb.records["contacts"].items()}
    items = sorted((data.get("items") or {}).values(), key=lambda i: float(i.get("ts") or 0), reverse=True)
    counts: Dict[str, int] = {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    waiting, others = [], []
    for i in items:
        if i["status"] == "ignored":
            continue
        x = {k: v for k, v in i.items() if k not in ("created", "steps")}
        x["personName"] = names.get(i.get("person", ""), "")
        x["memberName"] = names.get(i.get("member", ""), i.get("submitter", ""))
        x["createdCount"] = len(i.get("created") or [])
        if i["status"] in ("pending", "held"):
            if i.get("kind") == "form" and clean_name((i.get("fields") or {}).get("name") or ""):
                cands = candidates(svc, i["fields"]["name"], i["fields"].get("institution", ""))
                x["candidates"] = [{k: c[k] for k in ("id", "name", "institution")} for c in cands[:6]]
                action, value = decide(cands)
                x["suggestion"] = value if action == "existing" else ("new" if action == "new" else "")
            waiting.append(x)
        elif len(others) < 200:
            others.append(x)
    tok, problem = token_status(data)
    return {"configured": configured(data), "hasToken": bool(tok), "tokenProblem": problem,
            "mode": data.get("mode", "review"), "channel": data.get("channel", ""),
            "channelName": data.get("channelName", ""), "formApp": (data.get("formBot") or {}).get("name", "")
            or ("trusted" if data.get("formBot") else ""),
            "members": [{"slackId": k, **v} for k, v in sorted((data.get("members") or {}).items(),
                                                                  key=lambda kv: kv[1].get("name", ""))],
            "lastCheck": data.get("lastCheck", ""), "lastError": data.get("lastError", ""),
            "pollMinutes": data.get("pollMinutes", 10), "counts": counts, "items": waiting + others}


def forget_form(ws: Workspace) -> Dict[str, Any]:
    """Stop trusting the app that posts the lab's form; its next form waits for "Trust this form" again."""
    with _lock:
        data = load(ws)
        data["formBot"] = {}
        save(ws, data)
        return data


def set_mode(ws: Workspace, mode: str) -> Dict[str, Any]:
    if mode not in MODES:
        raise IntakeError(f"mode must be one of {', '.join(MODES)}")
    with _lock:
        data = load(ws)
        data["mode"] = mode
        save(ws, data)
        return data
