# Setting up your lab

Onboarding turns a short questionnaire into a starting network and a research library. It takes about twenty minutes
of your time, most of it spent listing people and confirming identities, and then 10 to 40 minutes unattended while
the library is built.

```bash
python3 labnetwork.py onboard ~/LabNetwork/my-lab
```

The command works in six steps and skips any step that is already done, so you can stop and run it again.

1. **Questionnaire.** It asks for the lab, its members, the relationships papers will not show, the research library
   and weekly update, and the conferences the lab attends, then saves your answers as `lab.json`. You can also write
   `lab.json` yourself; [`lab.example.json`](../lab.example.json) shows the common fields, and the
   [reference below](#labjson-reference) lists them all.
2. **Identity check.** Each member is matched to an OpenAlex author. A member with an ORCID iD is matched by it.
   Without one, you choose from a list of same-name authors that shows each one's institutions, topics and number of
   works. Lab Network never chooses for you.
3. **Build.** Lab Network reads public records and writes a packet: the read-only starting data.
4. **Workspace.** It creates the folder where your edits, notes and imports are kept.
5. **Research library.** It reads each researcher's OpenAlex publication record and recent papers, and their NIH and
   NSF awards (see below). If OpenAlex's free daily allowance runs out first, the library is saved as it stands, and
   the weekly update or the next `library` run continues it. You can stop the build with Ctrl+C and run the command
   again later to continue it.
6. **Weekly update.** If you asked for one, it shows what it would install on your computer (a LaunchAgent on macOS, a
   crontab line on Linux, a scheduled task on Windows) and asks before installing it. Each week the update adds new
   papers and awards to the library and writes a digest, which the Data page lists under Updates. On macOS, a run
   missed while the computer is asleep happens when it wakes; on Linux and Windows it waits for the next week.

When it finishes:

```bash
python3 labnetwork.py serve ~/LabNetwork/my-lab/workspace --open
```

## What the build reads

- **Lab members' papers** from OpenAlex, since the year you choose (ten years back by default).
- **Coauthors.** Up to 40 per member, keeping the most frequent, from papers with 30 or fewer authors. Each becomes a
  first-degree person, linked to the member by one coauthor record that lists the shared works. You confirm each
  member's OpenAlex record, not each paper on it, and OpenAlex sometimes files two researchers with the same name under
  one record. So a member's paper counts only when it names another lab member, someone in the relationships you list,
  or someone named on another member's papers that count; is on the ORCID record whose iD you gave for the member; or
  lists the member at their institution or at an institution where two such papers list them. The `limitations` list
  in `packet/source-manifest.json` says how many of each member's papers were left out, and `expand` checks them again
  later against everything recorded by then.
- **Coauthors of coauthors**, unless you turn this off. Up to 5 per first-degree person, again the most frequent, from
  papers with 30 or fewer authors that count for that person. As for lab members, a paper counts when it connects to
  what is known about them: it lists them at their institution or at an institution where two such papers list them,
  it is one of their papers with a lab member, or its authors include another first-degree person or a lab member for
  whom it counts. These are second-degree people. Their ties are marked "likely" rather than "confirmed", because both
  identities come from OpenAlex's algorithm.
- **ORCID records** for people who list an ORCID iD. A completed doctorate on the record is stored as confirmed, with
  the record as its source. A current employment entry fills in an empty position.
- **NIH awards** for members with a RePORTER PI profile ID. Other principal investigators named on the same award are
  linked to the member by a shared-grant record.
- **Your reported relationships**, stored as "reported" evidence with the date of onboarding.

The build caps the network at 1,500 people by default so the map stays readable. The `limitations` list in
`packet/source-manifest.json` says what each cap left out, for example "kept the 40 most frequent of 133 coauthors".

## What the research library reads

The library covers lab members and the researchers in the network: people with a confirmed doctorate, a coauthor,
shared-grant or collaborator record, or an OpenAlex link. Of the people `expand` adds to the second degree, it covers
only those with a confirmed doctorate and a known institution and position, because there can be thousands of them. People the library already holds stay in it.

- **OpenAlex.** Each person's author record (works, citations, h-index, main topics) and up to 50 of their most recent
  works since the start year with 30 or fewer authors. A person is identified by the OpenAlex links on their record; by
  their name on a recent paper of someone they have a recorded relationship with; or, if they have a confirmed
  doctorate, as the only OpenAlex author with their name at an institution on their record. Anyone else is listed as
  not matched, and each weekly update tries again. OpenAlex often splits one researcher across several author records:
  records with the same ORCID iD count as one author (once a month the library looks up each linked person's ORCID iD
  for records not yet linked), and a person whose record links several OpenAlex records holds all of them. Their
  papers come from every record, and their counts are added together.
- **NIH RePORTER.** Awards under the RePORTER profile ID on the person's record (a lab member's `nihProfileId`, or the
  ID on an award that named them); otherwise awards whose principal investigator has their name, at an institution on
  their record. A name that matches investigators at several such institutions is held back.
- **NSF.** Awards whose principal investigator has their name, made to an institution on their record.

Without an API key, OpenAlex allows $0.10 of requests a day for each network address, which is about 1,000 paper
lists. Each run stops before the allowance runs out, and a library update also stops after $0.08 (about 800 paper
lists), so a large library fills over several runs: the first build, then each weekly update. A free key raises the
allowance to $1 a day, and a run with a key may use up to $0.90 of it: register at
[openalex.org/settings/api](https://openalex.org/settings/api) and save the key as one line in
`~/.config/lab-network/openalex-key`.

## `lab.json` reference

### `lab`

| Field | Meaning |
|---|---|
| `name` | The lab's name, shown in the app |
| `institution` | Used for members without their own `institution` and to rank same-name candidates |
| `website` | The lab's people page; used as a source for membership and photos |
| `contactEmail` | Optional. Sent to OpenAlex with each request so they can contact you about heavy use. Stays in `lab.json` and the workspace settings. |

### `members` (one entry per current lab member)

| Field | Meaning |
|---|---|
| `id` | Stable ID, for example `maria-rivera`. Filled in for you if missing. |
| `name`, `role`, `institution` | As the lab lists them |
| `orcid` | The member's ORCID iD; the most reliable way to find their papers |
| `scholarUrl`, `facultyPage` | Kept as sources so you can check identities. Scholar is never read automatically. |
| `nihProfileId` | NIH RePORTER PI profile ID, for awards |
| `photoUrl`, `photoSourceUrl`, `photoAttribution` | The image file, the page that shows it is this person, and a credit line |
| `researchSummary`, `researchSourceUrl` | A sentence about their research and where it comes from |
| `doctorate` | Optional `{status, type, evidence, sourceUrl}`. Without a source link it is stored as "unknown". |
| `openalexId` | Filled in by the identity check, or `"none"` to skip paper lookups for this member |

### `relationships` (people papers will not show)

| Field | Meaning |
|---|---|
| `member` | The lab member's `id` |
| `name` | The other person's full name. If they are already in the network (for example as a coauthor), the record attaches to them. |
| `type` | `mentor`, `postdoc`, `committee`, `training`, `collaborator`, `labmate`, `irta_colleague`, `met`, `event`, `coauthor` or `grant` |
| `direction` | For `mentor`: `they-mentored-member` or `member-mentored-them`. For `postdoc`: `member-was-their-postdoc` or `they-were-member-postdoc`. For `committee`: `they-served-on-member-committee` or `member-served-on-their-committee`. |
| `evidence` | One sentence describing the connection, such as "PhD advisor at Duke, 2013–2019" |
| `start`, `end` | Years, as precise as you know them |
| `status` | `current`, `historical` or `unknown` |
| `label`, `institution`, `url`, `sourceUrl` | Optional detail |

### `settings`

| Field | Default | Meaning |
|---|---|---|
| `sinceYear` | ten years ago | Earliest publication year read |
| `maxCoauthorsPerMember` | 40 | First-degree coauthors kept per member |
| `secondDegree` | true | Whether to add coauthors of coauthors |
| `maxWorksPerBridge` | 60 | Recent works read for each first-degree person |
| `maxNewPerBridge` | 5 | Second-degree people added per first-degree person |
| `maxPeople` | 1500 | Upper limit for the whole network |
| `maxPapersPerPerson` | 12 | Recent papers stored on each profile |
| `orcidLookups`, `maxOrcidLookups` | true, 250 | ORCID checks for doctorates and positions |
| `nihAwards` | true | NIH award lookups for members with a profile ID |
| `library` | true | Build the research library at the end of onboarding |
| `weeklyUpdate` | `{"day": "mon", "at": "08:00"}` | When the weekly library update runs: a three-letter day and a 24-hour time, or `false` for none. Onboarding installs it only after you say yes. |

### `conferences` (meetings the lab attends)

| Field | Meaning |
|---|---|
| `short` | Short name, such as `SRP`. Required. |
| `name` | Full name |
| `url` | The conference's website |
| `frequency` | Optional, such as `annual` or `biennial, odd years` |

Onboarding puts these on the Conferences page's list. Import a year's program with the `conference` command.

## Rebuilding

A workspace is tied to the exact packet it was created from. To rebuild with new settings, run
`onboard ... --rebuild`; the old packet is kept beside the new one. Then export a bundle from the old workspace,
create a workspace for the new packet, and import the bundle into it. Your notes and edits come across as records,
and anything that conflicts with the new data becomes an open conflict.

For routine updates there is no need to rebuild: see [updates.md](updates.md).

## Starting from your own data instead

If you already have a list of contacts, skip onboarding: write a packet by hand (see [data-format.md](data-format.md))
or start from the demo packet's format, then import contacts and notes from CSV on the Data page.
