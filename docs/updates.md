# Keeping the network current

## The weekly library update

Onboarding offers to install the weekly update. For a lab set up another way, or to change its day and time:

```bash
python3 labnetwork.py library ~/LabNetwork/my-lab/workspace --update
python3 labnetwork.py schedule ~/LabNetwork/my-lab/workspace --every week --day sat --at 19:30          # plan only
python3 labnetwork.py schedule ~/LabNetwork/my-lab/workspace --every week --day sat --at 19:30 --yes    # install
```

Each update, for everyone in the research library:

- **Papers.** Asks OpenAlex for works published in the 60 days before the last update or later, 50 author records per
  request, so papers OpenAlex adds late are still caught. New ones (30 or fewer authors) join the person's recent
  papers. A person who gained an OpenAlex record since the last update has their list read again in full.
- **Summaries.** Refreshes each person's OpenAlex counts, h-index and topics.
- **Split records.** Once a month for each person, asks OpenAlex for every author record with the same ORCID iD as
  their records, 50 iDs per request (about $0.003 for 1,500 people). It adds the records not linked to anyone else
  whose name can be the person's and that share an institution with them (when both list one); a record with more
  works than all of theirs must also share a research topic. Their papers are read in full, and only papers from the
  last 60 days are listed as new. The log names the records held back. A record marked as not theirs never joins (see
  below).
- **NIH awards.** Asks RePORTER for awards added since two weeks before the last check: by NIH profile for people
  already matched or with a profile ID on their record, by name for everyone else (accepted only at an institution
  on their record). The digest lists an award as new when it opened or was renewed recently, not when an older award
  adds a year, moves to another institution or receives a supplement.
- **NSF awards.** Asks NSF for each person's awards dated since a month before the last check.
- **People still to fetch.** Continues the first build where an earlier run stopped, and tries again to identify
  people listed as not matched.

It writes `workspace/digests/library-YYYY-MM-DD.md` with the new papers and awards, nearest people first (a second
update on the same day gets its own file). OpenAlex
usually lists a paper within days of publication; NIH posts awards in batches, often weeks after the award notice.
Opening a person's page checks both sources live, so that person is always current; `config WORKSPACE --live-check
off` turns this off.

Once the library is full, a weekly update costs about $0.01 of OpenAlex's free daily allowance. Until then, each
update also fetches the paper lists still missing, nearest people first, and stops after $0.08 (about 800 lists), or
$0.90 with an OpenAlex API key, or before the allowance runs out; the next run continues. On macOS a run missed while the computer was asleep happens when it wakes. Only one
library run works on a workspace at a time; a second one stops with a message.

The Data page, under Updates, shows when the library last ran, whether the weekly update is on and when, and the
commands above with your folders filled in. `schedule WORKSPACE --every status` prints the same status, and
`--every off` removes the weekly update. Logs go to `workspace/logs/library.log`.

## Records that are not someone's

OpenAlex sometimes lists a coauthor's, spouse's or namesake's author record under a researcher's ORCID iD, and a match
on shared papers or by name search can pick the wrong record. When a record is not someone's, mark it: on their profile
page, under Research, open "OpenAlex records" and choose **Not theirs** beside it, or run

```bash
python3 labnetwork.py not-theirs ~/LabNetwork/my-lab/workspace maria-lopez A5000000001 --note "Her coauthor's record" --actor "Maria Rivera"
python3 labnetwork.py not-theirs ~/LabNetwork/my-lab/workspace maria-lopez          # their records, held-back records and marks
python3 labnetwork.py not-theirs ~/LabNetwork/my-lab/workspace maria-lopez A5000000001 --undo
```

The same list shows the records held back after the lookup by ORCID iD, each with the reason, so they can be marked too.
The mark goes in the change log under the name of whoever made it, so it outlasts the research library. From then on:

- **The research library** never links the record to them, even when their record's sources link it. If they hold it,
  the profile drops it at once, with their summary and paper list. The next library run takes it out of
  `library.json`, links the records their own record links to and reads their papers again, or looks for them again
  without it.
- **The live check** on their profile page does not read it.
- **Refresh and expand** do not read it for them, and do not match a coauthor with that record to them by name; expand
  lists such a coauthor as held back. Expand also stops reading the records the research library joined to the marked
  one by ORCID iD, until the library's next run makes a new match.

**Undo** beside a mark withdraws it, and the next library run may link the record again.

To accept a held-back record instead, choose **Theirs** beside it: its URL is added to their record's sources, and the
next library run links it. If their sources link no OpenAlex record yet, the records the library linked to them are
added first, because once their sources link one, the library uses only those links.

## Refresh and expand

A refresh looks up recent public records for people near the lab and adds what is new. To fill in a network the
first time, or after adding people by hand, use `expand` instead (see the README): it collects every first-degree
person's coauthors on two or more papers since the start year, while a refresh adds only papers and coauthors that
appeared since the last run.

```bash
python3 labnetwork.py refresh ~/LabNetwork/my-lab/workspace --scope 1 --dry-run
python3 labnetwork.py refresh ~/LabNetwork/my-lab/workspace --scope 1
```

## What a refresh does

For each person within the chosen degree of the lab (`--scope 1` or `--scope 2`) whose record has an OpenAlex link:

- **New papers** since the last refresh (with 45 days of overlap, because indexing lags) are added to their profile.
- **New coauthors of lab members** are added as new people, each with a coauthor record citing the paper.
- **New ties between people already in the notebook** are added when they appear on a new paper together.
- **Only papers with 30 or fewer authors** add people or ties; a larger paper still goes on the profile. A new person,
  or a tie with a lab member, comes only from a paper that counts for that member by the rule `expand` uses (see
  [Fill in the first and second degree](../README.md#fill-in-the-first-and-second-degree)), because OpenAlex sometimes
  files two researchers with the same name under one record. To apply the rule, each refresh reads all of the lab
  members' OpenAlex works and the ORCID records whose iDs are on their records. The digest lists lab members' new
  papers that added no coauthors, and any ORCID iD for you to check. A later refresh does not read those papers again;
  once one counts, `expand` adds its coauthors. If a record the check needs cannot be read (a lab member's OpenAlex
  works or ORCID record, or the research library), the refresh records none of the papers it could not check and keeps
  the date of the last refresh, so the next refresh reads them again.
- **A changed affiliation** is not applied. If OpenAlex lists a different institution, it becomes an open conflict on
  the Data page, and your recorded value stays until you choose.
- **New NIH awards** for lab members with a RePORTER profile ID are added to their profiles.

Everything goes through the normal importer, so its rules apply. Nothing is overwritten. A new person who looks like
someone already in the notebook is held for review instead of being added. A second refresh on the same day adds
nothing.

Each run writes a digest to `workspace/digests/YYYY-MM-DD.md`. The Data page lists digests under Updates.

If the app is running, the refresh sends its changes through the app, so the two never write at the same time.

## Turning on a schedule

```bash
python3 labnetwork.py schedule ~/LabNetwork/my-lab/workspace --every week --scope 1          # shows the plan
python3 labnetwork.py schedule ~/LabNetwork/my-lab/workspace --every week --scope 1 --yes    # installs it
python3 labnetwork.py schedule ~/LabNetwork/my-lab/workspace --every status
python3 labnetwork.py schedule ~/LabNetwork/my-lab/workspace --every off
```

Without `--yes`, nothing is installed: you see the exact file or command first.

| System | Installed item | Schedule |
|---|---|---|
| macOS | A LaunchAgent in `~/Library/LaunchAgents` | Mondays at 08:00 (`week`) or the 1st of the month (`month`), while you are logged in |
| Linux and other Unix | One crontab line, marked `# labnetwork:refresh:<id>` | Same times |
| Windows | A Task Scheduler task named "Lab Network refresh <id>" | Same times |

The job writes its output to `workspace/logs/refresh.log`. On Linux and Windows, a run missed while the computer
is off or asleep is skipped until the next scheduled time; on macOS it runs when the computer wakes.

## Choosing the scope

- `--scope 1` checks the lab members and the people directly connected to them: usually a few dozen to a few hundred
  people, and a few minutes of lookups.
- `--scope 2` also checks second-degree people. It takes longer and finds more distant activity.

A weekly scope-1 refresh plus an occasional scope-2 run is a reasonable default.

## People without an OpenAlex link

A refresh only checks people whose records include an OpenAlex author link (in their sources or profile URL). Labs
built by `onboard` have these links. For a network assembled another way, add the link to a person's sources from
their page in the app, and the next refresh will include them. The digest says how many people in range were skipped
for lack of a link. When OpenAlex splits a researcher across several author records, add each record's link to their
sources: refresh, expand and the research library then read all of them as one person. The research library also adds
the records with the same ORCID iD as a linked record, and expand reads the records the library holds for each person;
refresh reads only the links on the record. None of them reads a link that was marked as not theirs.
