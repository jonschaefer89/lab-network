# Data formats

Lab Network keeps two things apart: a **packet** (read-only starting data) and a **workspace** (every change since).

```
my-lab/
  lab.json                 onboarding answers (only for labs built by onboard)
  packet/                  read-only; never edited
    notebook.json          people, relationships, notes, coverage
    lab-roots.json         the lab members, by stable ID
    notebook.schema.json   JSON Schema for notebook.json
    source-manifest.json   status, counts, SHA-256 of notebook.json, limitations
    packet-checksums.json  SHA-256 of every packet file
  workspace/
    workspace.json         which packet (path and SHA-256), lab name, settings
    changes.jsonl          append-only change log, one JSON event per line
    imports/               a copy of every imported file and its preview report
    exports/               files written by export
    digests/               refresh, expand and library summaries (Markdown and JSON)
    library.json           the research library: derived, rebuildable, not part of the change log
    library.lock           locked by a library run while it works, so two runs never overlap
    conferences.json       imported conference programs: derived, rebuildable, not part of the change log
    intake.json            the Slack intake: channel, mode, lab roster and each submission (see slack-intake.md)
    logs/                  output of the weekly update
```

`library.json` (written by `labnetwork.py library`) holds `people` (per person: `openalex` with the identity, its
basis, a summary and up to 50 recent work IDs; `nih` with matched profile IDs and award keys; `nsf` with award keys),
`works` (title, date, DOI, byline, author count, and the people each belongs to), `awards` (NIH by core project
number, NSF by award ID, with each person's role), the `standard` used, and a log of `runs`. Delete it to start the
library over; nothing else depends on it. An OpenAlex `status` is `linked`, `pending` (a run has not reached them),
`unmatched` (no method identified them; the next update tries again), `not-found` (a name search found no author at
their institutions) or `ambiguous` (a name search or shared papers found several different authors; `candidates` lists
their records). An optional `openalex.url` replaces the OpenAlex profile link; the demo uses it to point at example.org.

OpenAlex often splits one researcher across several author records, so a linked person can hold several: `openalex.ids`
lists them all, the main one first, and `openalex.id` is the main one. A person holds every OpenAlex author URL in their
record's sources. Records with the same ORCID iD count as one author, whether a name search or shared papers find them
or a lookup by ORCID iD does: for each linked person whose records list an ORCID iD, a run asks OpenAlex for every
record with that iD (50 iDs to a request) and adds the ones not linked to anyone else, after the records the person
already holds. OpenAlex also lists some coauthors' and namesakes' records under a researcher's ORCID iD, and sometimes
merges several namesakes into one large record. So a record is added only when its name can be the person's
(initials, another word order and a one-letter typo are allowed), it shares an institution with them when both list
institutions, and, if it has more works than all of the person's records together, it shares one of their main
research topics. The others are kept in `orcidHeld` (`id`, `name` and `reason`) for a person to decide; adding a
record's URL to the person's sources links it. Once a person's sources link any OpenAlex record, the library uses only
those links, so for someone whose sources link none yet, add the records they hold first (`ids`, the main one first);
the profile's **Theirs** button does this. You can also mark any OpenAlex record as not a person's, on their
profile page, with `labnetwork.py not-theirs` or through the local API. The mark is an identity note in the change log
(below), so it outlasts `library.json`. No run links a marked record to that person, even when their record's sources
link it (sources cannot be taken off a record): it never joins by ORCID iD, is not held back for them, and is no
candidate on shared papers or in a name search. A person who holds the record when it is marked starts over. The app
drops it at once, with their summary and paper list. The next run does the same in `library.json`, then links the
records their own record links to and reads their papers from OpenAlex again (`reread`: not from the response cache),
or identifies them again without it. `notTheirs` lists the marks a run has applied; after a mark is withdrawn, the next
run repeats the lookup by ORCID iD and the name search. `orcidCheckedAt` is the date of the lookup;
it is repeated after 30 days, or in the next run when the person's records change. An ORCID iD on records of two
people adds nothing to either, and the run's log names them. `orcidGroup` (`orcid` and `ids`) records which records were joined because they share an ORCID iD; they stay with
the person while their record links to one of them. Records with different ORCID iDs, or without one, count as
different authors. Paper lists, weekly updates and the live check read all of a person's records, and each paper is listed once.
The `summary` adds works, works since the start year, citations and `i10` over the records and keeps the largest
record's `hIndex`, which is a lower bound for the combined record. `refill` marks a paper list that is read again in
full because the person gained a record.

`library.json` is version 2. A run upgrades a version-1 file (one `id` per person) when it loads it: it adds `ids` and
searches again for people a version-1 name search left `ambiguous`. A version-1 reader still finds each person's main
record in `id`.

`conferences.json` (written by `labnetwork.py conference`, onboarding and the Conferences page) holds `series`, the
conferences the lab follows (`id`, `short`, `name`, `url`, `frequency`), and `conferences`, the imported programs.
Only programs whose `series` is on the list are shown or searched; taking a conference off the list keeps its
programs. Each program has an `id`, its `series`, `name`, `short`
name, `year`, `dates`, `location`, the program `url`, a `note` on how the file was made, `addedAt`, `addedBy` and
`appearances` (`name`, `institution`, `role`, `title`, `session`, `when`, `sourceUrl`) as the program prints them.
Names are matched to the notebook whenever the file is read, so a person added later is found: the same full name and
a matching institution, the only person with that full name, or initials with a matching institution and one
candidate. A name that fits several people is left unmatched. Importing a program with an existing `id` replaces it.
A program file is a CSV with a header row (`name` required; `institution` or `affiliation`, `role`, `title`,
`session`, `when`, `sourceUrl` optional) or a JSON list of objects with those keys. "Last, First" names and degrees
after a comma ("Jane Doe, PhD"), and titles or military ranks ("Dr.", "LTC", "USAF"), are read as plain names.
The role `Author` means a co-author on an abstract, who may not have attended. The Conferences page and profiles
count a year as presenting only for the other roles (`presentingYears`), unless "Include co-authors" is ticked.

`intake.json` (written by `labnetwork.py intake --setup` and the Slack inbox) holds:

- `channel`, `channelName`: the Slack channel, by ID and name.
- `mode`: `automatic` or `review`.
- `members`: Slack user ID mapped to `{contactId, name}`; only these people can submit.
- `token`: optional `{file, key}` naming where the bot token is kept. The token itself is never stored here.
- `formBot`: the app trusted to post the lab's form (`botId`, `appId`, `name`), set when someone chooses "Trust this
  form"; empty until then.
- `pollMinutes`: how often the open app reads the channel.
- `lastTs`, `lastCheck`, `lastError`: the last message read, when, and the last error.
- `items`: each submission by its Slack timestamp. An item holds its sender, `kind` (`form` or `text`), the form's
  `fields`, its Slack `permalink`, its `status` (`added`, `held`, `pending`, `dismissed`, `undone` or `ignored`),
  the person it was attached to, and `created`, the records it added, which undo archives. A form from an app no
  one has trusted yet has `untrusted` and the app's identity in `app`.

`workspace.json` settings that change the app:

| Setting | Meaning |
|---|---|
| `defaultFilters` | The people filters a new visit starts with |
| `degreeLimit` | The furthest degree shown. Set it to the deepest degree whose connections were collected (`labnetwork.py config WORKSPACE --degree-limit 2`, with the server stopped). |
| `liveCheck` | `false` stops profile pages from checking OpenAlex and NIH for anything newer (`config WORKSPACE --live-check off`) |
| `contactEmail` | Sent to OpenAlex with each request, from `lab.json` |
| `demo` | Marks the fictional demo lab: no library runs, weekly updates or live checks |

On every start, Lab Network checks that `notebook.json` still matches the SHA-256 recorded in `workspace.json`, and
refuses to run if it does not.

## `notebook.json`

The full field list, with types and limits, is [`labnetwork/schema/notebook.schema.json`](../labnetwork/schema/notebook.schema.json).
The main collections:

- **`contacts`**: one record per person, keyed by a stable `id`. Professional fields (`institution`, `role`, `lab`),
  identity and photo sources with checked dates and confidence, `doctorateStatus` (`confirmed`, `not-yet-earned` or
  `unknown`; absent means not classified), `researchSummary` with its source, a `sources` ledger, and `papers` and
  `grants` lists. `seed`, `networkDegree` and `expansionStatus` describe how the record was collected; they are never
  used as the degree of connection.
- **`edges`**: one record per sourced relationship. `from` and `to` are contact IDs. `type` is one of `mentor`
  (mentor to mentee), `postdoc` (postdoc to supervisor), `committee` (committee member to student), `training`,
  `coauthor`, `grant`, `collaborator`, `labmate`, `met`, `event` or `irta_colleague`. Each record has `provenance`
  (`public`, `reported` or `firsthand`), `confidence` (`confirmed`, `likely` or `unverified`), `status` (`reviewed`,
  `proposed` or `rejected`), `sourceTitle`, `sourceUrl`, `evidence`, `checkedAt`, and optional timing and labels.
  For display, types fall into three groups, in this order of precedence: supervision (`mentor`, `postdoc`,
  `committee`), research (`coauthor`, `grant`, `collaborator`) and association (`training`, `labmate`,
  `irta_colleague`, `met`, `event`). A pair with records of several kinds is drawn and listed under the first group.
- **`meetings`**: notes about people the lab met. `contactId` (whom), `metBy`, `date` (may be blank), `context` (how
  you met), `learning` (what you discussed), `followUp`, `recordedBy`.
- **`coverage`**: what the collection covered and left out.

Besides the schema, these rules are checked: unique IDs, every reference points to an existing contact, no
self-relationships, absolute http(s) URLs, real `YYYY-MM-DD` dates, a source URL on every public relationship and
every paper and grant, evidence and a source for every confirmed or not-yet-earned doctorate, a source page for every
confirmed photo, and a source for every research summary, abstract and relationship narrative.

## How degrees and paths are computed

- A step is one included relationship record between two people. Degree is the fewest steps from any lab member.
- Which records count is set by the Relationships menu: types, evidence kinds, and whether proposed records count.
  Rejected records never count. Changing these recalculates every degree and path.
- **Any direction** (the default) walks each record either way and still describes it in its stored direction.
- **Recorded direction** walks mentor, postdoc and committee records only from their `from` person to their `to`
  person. Mutual types are walked both ways. Training records are left out because their stored order is not
  consistent across sources.
- All equally short paths are listed; longer ones are not.

The people filters (doctorate, institution and position, degree range) only change who is listed or drawn. They do
not change degrees. When a hidden person links someone shown to the lab, the map draws them as a hollow marker.

## The change log (`changes.jsonl`)

Each line is one event:

```json
{"seq": 12, "uuid": "9f2c...", "ts": "2026-10-16T19:20:11Z", "actor": "Maria Rivera",
 "op": "contact.update", "id": "joan-elder",
 "data": {"set": {"institution": "Hill College"}, "prev": {"institution": "Example State University"},
          "base": {"institution": "packet"}}}
```

| Operation | Meaning |
|---|---|
| `contact.create`, `edge.create`, `meeting.create` | A new record (`data.record`) |
| `*.update` | Changed fields (`set`), their previous values (`prev`) and the version each change was made on (`base`) |
| `*.append` | Items added to a list field such as `papers`, `grants`, `sources` or `tags` |
| `*.archive`, `*.restore` | Hide or restore a record created in this workspace (packet records cannot be archived) |
| `contact.alias` | An imported ID that refers to an existing person |
| `meeting.meta` | Follow-up status |
| `import.batch` | One import: file name, SHA-256, format and results |
| `conflict.open`, `conflict.resolve` | A differing incoming value, and the decision about it |
| `workspace.note` | A note about the workspace. With `data.kind` = `collection`, it records whose connections a run of `expand` collected: `data.people` maps each person to a status (`collected`, `not-found`, `ambiguous`, `not-looked-up`, `stopped`, `undone`) with counts and the identity basis; `data.standard` gives the rule; `data.batchId` names the import batch. The latest note for a person wins. With `data.kind` = `identity`, it marks the OpenAlex author record `data.openalexId` as not the person `data.person`'s (`data.notTheirs`: `true`, or `false` to withdraw an earlier mark), with the name on the record (`data.name`), a `data.note` and the `data.date`. The latest note for a person and record wins. |

The log is only ever appended to. If the last line was cut off by a crash, it is moved to `changes.partial-*.jsonl`
at the next start and everything before it loads normally.

## Export formats

| Format | Contents | Use |
|---|---|---|
| Bundle (`labnetwork-bundle.json`) | The current notebook, archived records, the full change log, conflicts, aliases and follow-up status | Backups, and moving or sharing a workspace. Importing it into a workspace on the same packet replays the history and reproduces it exactly. |
| Notebook (`notebook.json`) | Current records only, in the packet format | Other tools, or a new packet |
| Contacts, notes and relationships (`contacts.csv`, `notes.csv`, `relationships.csv`) | One row per record, UTF-8 with a byte-order mark for Excel | Spreadsheets |

## Importing

Imports always show a preview first. Each row is classified as:

| Status | Meaning |
|---|---|
| New | Not in the notebook; it will be added |
| Adds missing values | Same stable ID; it fills fields that are empty here or adds list items |
| Conflicts with current values | Same stable ID with a different value; the current value stays and the incoming one is kept as an open conflict |
| Already here, no change | Same stable ID and the same values; nothing changes |
| Possibly someone already here | No ID match, but the same name, email or profile page. You choose: same person (merge), a different person, or skip. |
| Duplicate of an existing record | The same relationship or note already exists under another ID |
| ID already used by a different record | A bundle would create a record under an ID that a different record here already uses; it is skipped |
| Rejected | Fails validation; the reason is shown and the row is not imported |

Importing the same file twice adds nothing. The second preview shows every row as already present and notes that the
file was imported before.

### CSV columns

Download templates from the Data page. Column names ignore case and spaces.

- **Contacts:** `id` (optional), `name` (required), `institution`, `role` or `position`, `lab`, `email`, `website`,
  `tags` (separated by `;`), `notes`, `researchSummary`, `researchSourceUrl`, `profileSourceUrl`, `photoUrl`,
  `photoSourceUrl`, `doctorateStatus`, `doctorateEvidence`, `doctorateSourceUrl`, `doctorateCheckedAt`.
- **Notes:** `contactId` or `contactName`, `metBy`, `date`, `context` (required), `learning`, `followUp`, `recordedBy`.
  A `contactName` must match exactly one person.
- **Relationships:** `from`, `to`, `type` (required), `evidence` (required), `provenance`, `confidence`, `status`,
  `sourceTitle`, `sourceUrl`, `relationshipLabel`, `relationshipStart`, `relationshipEnd`, `relationshipStatus`,
  `checkedAt`. Without a `status`, imported relationships are stored as "proposed", so they do not count as
  connections until someone reviews them.

## The local API

The app's own JSON API on http://127.0.0.1:8765 is described in [AGENTS.md](../AGENTS.md#help-with-an-existing-lab).
Requests must be addressed to localhost, and writes must be JSON from the app's own origin.
