# Lab Network: instructions for AI assistants

This file is for AI coding assistants (Claude Code, Codex, Cursor and similar) that a researcher has pointed at
this repository. Your job is usually one of two things: set Lab Network up for the researcher's lab, or help them use
a lab that is already set up. Read the ground rules first; they apply to every task.

## Ground rules

1. **Keep the lab's data private.** Never upload, publish, commit or send the lab folder, its packet, its workspace or
   its exports anywhere. Lab folders live outside this repository (default `~/LabNetwork/<lab-slug>`). If the user
   asks you to push this repository, check first that no lab folder, workspace, export, `lab.json` or shared copy
   (`site`) is inside it.
2. **Ask before contacting outside services.** Onboarding, the research library, expand and refresh send names, ORCID
   iDs and NIH profile IDs to OpenAlex, ORCID, NIH RePORTER and NSF. Say so and get a yes before the first run.
3. **Never invent people or relationships.** Record a relationship only when the user states it or a public record
   shows it. Working at the same institution or on the same topic is not a relationship. If you are unsure, ask.
4. **A person confirms every identity.** When `onboard` lists several OpenAlex authors for a lab member, show the
   candidates to the user and let them choose. Never choose between same-name candidates yourself. An ORCID iD is
   accepted once the user has given or confirmed it; show any iD you looked up before building (Step 3 of setup).
5. **Do not scrape Google Scholar.** It has no API and its terms forbid automated reading. Use a Scholar URL only to
   help the user confirm who is who (you may open it in a browser so the user can look).
6. **Read websites only with permission.** With the user's OK you may read the lab's people page or faculty pages to
   draft the member list, then show the list to the user before writing it to `lab.json`.
7. **Install nothing that keeps running without a yes.** `schedule` installs a weekly or monthly job. Show the plan it
   prints and install it (`--yes`) only after the user agrees. `onboard --non-interactive` never installs one; it
   prints the command instead.
8. **Do not edit packet files.** A packet is the read-only starting data. Changes go through the app, the CLI or the
   local API, which record who made each change and when.
9. **Never handle credentials.** Do not ask for passwords or API keys, and never type, paste or copy a token.
   Only the optional Slack intake needs one: the user creates a Slack bot token and saves it themselves (see
   [docs/slack-intake.md](docs/slack-intake.md)). OpenAlex responses say an assistant can register a key with the
   user's email; do not do that. Mention it to the user and stay within the free daily allowance (`expand` stops
   before reaching it).
10. **Never post to Slack.** The Slack intake only reads its channel. Do not create channels, invite people or post
    messages for the user unless they ask for that specific action in so many words.
11. **A lab's shared copy goes online only behind a sign-in.** `site` builds a read-only copy of the whole notebook,
    meeting notes included. Deploy it only to a host where a sign-in admits just the people the user lists, and only
    after a signed-out request is refused ([docs/hosting.md](docs/hosting.md)). Never publish it openly. The
    fictional demo (`site --demo`) is the only copy that may go online without a sign-in.

## Check the environment

```bash
python3 --version            # needs 3.9 or newer
python3 -m unittest discover -s tests -t .     # optional; about 10 seconds, offline
python3 labnetwork.py demo --open              # optional: show the user the fictional demo lab
```

Run long-lived commands such as `serve` and `demo` in the background. The app listens on http://127.0.0.1:8765.

## Set up a new lab

Ask as few questions as you can. The user should answer twice: once to start, and once to confirm the member list.
Identity questions come only when a name matches several researchers. Everything else has a default; do not ask
about anything the user can change later.

### Step 1. Ask two things, in one message

> I'll set up Lab Network for your lab. I need two things:
>
> 1. Your lab's website (the people or team page is best), or your name and institution.
> 2. Your OK to read your lab's web pages and to look up the people in your network in public records: OpenAlex,
>    ORCID, NIH RePORTER and NSF. Only names and research IDs are sent.

If the user's first message already gives both, skip this step. Without the OK in item 2, stop: nothing can be built
(rules 2 and 6).

### Step 2. Draft the lab without asking

- Read the people page and the PI's faculty page. List the current members with their roles. Leave out alumni unless
  the page lists them as current.
- For each member, search ORCID's public API (`https://pub.orcid.org/v3.0/expanded-search/?q=...`) by name and
  institution. Record an ORCID iD only when exactly one record matches both; otherwise leave it empty and let onboard
  find the OpenAlex author.
- Plan `<LABDIR>/lab.json` (default `LABDIR`: `~/LabNetwork/<lab-slug>`) with these defaults in `settings`: papers
  from ten years ago (`sinceYear`), coauthors of coauthors on (`secondDegree: true`), the research library on
  (`library: true`), and `weeklyUpdate: false`. Leave photos out unless the user agrees in Step 3. Write the file only after the user
  confirms the list (rule 6). [`lab.example.json`](lab.example.json) shows the common fields and
  [docs/onboarding.md](docs/onboarding.md#labjson-reference) lists and explains every field.

`lab.json` rules:

- Give each member an `id` (lowercase, hyphenated name, for example `maria-lopez`).
- Each entry in `relationships` names a lab member by `member` id and the other person by `name`.
- `type` is one of `mentor`, `postdoc`, `committee`, `training`, `collaborator`, `labmate`, `irta_colleague`, `met`,
  `event`, `coauthor`, `grant`.
- Direction words: for `mentor` use `"direction": "they-mentored-member"` or `"member-mentored-them"`; for `postdoc`
  use `"member-was-their-postdoc"` or `"they-were-member-postdoc"`; for `committee` use
  `"they-served-on-member-committee"` or `"member-served-on-their-committee"`.
- `evidence` is one plain sentence in the user's words, such as "PhD advisor at Duke, 2013–2019".
- Photos: set `photoUrl` to the image file and `photoSourceUrl` to the page that shows it is that person.
- The weekly update goes in `settings`: `"weeklyUpdate": {"day": "mon", "at": "08:00"}` (a three-letter day and a
  24-hour time).
- Conferences go in a top-level `"conferences"` list: `{"short": "SRP", "name": "…", "url": "…"}` for each.

### Step 3. Confirm in one message

Show the member list (name, role, and the ORCID iD you found, with its link) and say what happens next. The user's
confirmation of the list confirms those iDs (rule 4). In the same message,
offer the optional extras as one list the user can answer or ignore:

> Is this list right? Add or remove anyone, then say "go".
>
> Optional, all in one reply if you like:
> - Relationships that papers won't show: PhD advisors, postdoc supervisors, trainees, committees, fellowship
>   cohorts, collaborators without shared papers (who, which lab member, and one sentence of evidence).
> - Conferences the lab attends.
> - May I use the photos on your people page?
> - Shall I set up a weekly check for new papers and awards on this computer (Mondays at 08:00)? I'll show you
>   what it installs before installing it.
>
> A free OpenAlex key makes the first build finish in one run instead of over several days. If you want one, register
> at openalex.org/settings/api and save it as one line in `~/.config/lab-network/openalex-key`.

Do not register a key for the user (rule 9). Treat "go", or any confirmation of the list, as the answer, and do not
ask again about extras the user skipped.

### Step 4. Write `lab.json` and build

Write `<LABDIR>/lab.json` with the confirmed list and the answers the user gave, then:

```bash
python3 labnetwork.py onboard <LABDIR> --non-interactive
```

Run it in the background: the network takes a few minutes and the research library 10 to 40 minutes more.

| Exit code | Meaning | Next step |
|---|---|---|
| 0 | Packet, workspace and library are ready | Step 5 |
| 2 | No `lab.json` yet; a blank one was written | Fill it in (Step 2) and run again |
| 3 | Some members need identity confirmation | Read `<LABDIR>/identity-candidates.json`. Ask about all of them in one message. For each member show every candidate as the file gives it: name, last known institutions and affiliation history, number of works, topics, ORCID and the OpenAlex link. Write each choice to `members[].openalexId` in `lab.json`, or `"none"` if none match, and run again. Never choose yourself (rule 4). |
| 1 | An error | Read the message; fix `lab.json` or run again (responses are cached, so retries are cheap) |

### Step 5. Make sure the first and second degree are complete

Onboard keeps each lab member's most frequent coauthors (40 by default) and their frequent coauthors. `expand`
adds the rest: every coauthor on each member's papers with 30 or fewer authors, and each first-degree person's
coauthors on two or more such papers since the start year. It counts only papers that connect to what is known about
the person, because OpenAlex sometimes files two researchers with the same name under one record. A lab member's
papers listed on the ORCID record whose iD is on their record always count, so record members' ORCID iDs (Step 2).
The digest lists any iD that OpenAlex shows for a member but that their record lacks; show it to the user, and add it
only once they confirm it (rule 4). So the first dry run after onboarding usually lists people to add:

```bash
python3 labnetwork.py expand <LABDIR>/workspace --dry-run
```

If the dry run lists anyone to add, run `expand` without `--dry-run` (the OK from Step 1 covers it) and plan again. Stop when a
dry run plans nothing. If OpenAlex's daily allowance stops a run, tell the user when the next run can continue, or
that a key would let it finish now. Do the same for the library: `python3 labnetwork.py library <LABDIR>/workspace`
until its last line says `0 still to fetch`.

Then check the packet:

```bash
python3 labnetwork.py validate <LABDIR>/packet
```

The last line must be `RESULT: PASS`. Report, in a few lines: the number of people at each degree (the map's count
line shows them), relationships, papers and awards; and anything left for a later run. The `limitations` list in
`<LABDIR>/packet/source-manifest.json` describes the starting build, before `expand` added to it.

### Step 6. Start the app and show it

```bash
python3 labnetwork.py serve <LABDIR>/workspace --open
```

Point out the map and its degree buttons, one person's page with their route to the lab, a topic search, and how to
add a note after a meeting. Mention that coauthor identities come from OpenAlex and are marked "uncertain" until
someone checks them, and that a new network shows everyone because positions and doctorates are often unknown.

### Step 7. The weekly update, if the user said yes

```bash
python3 labnetwork.py schedule <LABDIR>/workspace --every week --day mon --at 08:00
```

It prints what it would install (a LaunchAgent on macOS, a crontab line on Linux, a scheduled task on Windows). Show
the user that plan and run the same command with `--yes` only after they agree to it (rule 7). `--every status`
shows whether it is on; `--every off` removes it.

### Extras to offer once, in one line each

Offer these after Step 6 and set up only the ones the user asks for:

- **Lab members add from Slack**: a private channel and a form; see [docs/slack-intake.md](docs/slack-intake.md).
  The user does the Slack steps; you run `python3 labnetwork.py intake <LABDIR>/workspace --setup --channel '#name'`
  with the server stopped, show the roster it prints, and ask whether submissions are added automatically
  (`--mode automatic`) or after review (the default). With a form, the first one waits until the user chooses
  **Trust this form**; never trust it for them.
- **A read-only copy for the lab, online behind a sign-in**: see [docs/hosting.md](docs/hosting.md) and rule 11.
- **Conference programs**: for each conference on the list, and with the user's OK to read its website, build one
  CSV per year from the public program (`name,institution,role,title,session,when,sourceUrl`, one row per person per
  appearance). Take every row from text you read on the program; never add a name it does not print (rule 3). Keep
  the extracted text beside each file and check that every name occurs in it. Tell the user which years are out of
  reach (behind a login, a paywall or a JavaScript-only app). Then import each file:

  ```bash
  python3 labnetwork.py conference <LABDIR>/workspace srp-2025.csv --series srp --name "SRP 2025 Annual Meeting" \
    --short SRP --year 2025 --url <program URL> --note "speakers, chairs and poster first authors from the program PDF"
  ```

- **The older scheduled refresh**, which adds new papers to profiles and lab members' new coauthors as people:
  `python3 labnetwork.py refresh <LABDIR>/workspace --scope 1 --dry-run`, and `schedule … --job refresh` with the
  user's yes.

## Help with an existing lab

When the server is running you can use its local API (JSON over http://127.0.0.1:8765). Writes need the header
`Content-Type: application/json`; set `X-LabNetwork-Actor` to the person's name (URL-encoded) so changes are
attributed to them. Before any write, show the user the exact change and get their OK.

| Task | How |
|---|---|
| Who works on a topic | `GET /api/search?q=ADHD` (or `q=ADHD AND ABCD`: people every term matches; items that match all terms have `allTerms`) returns `profiles`, `papers`, `grants`, `notes`, `events` (conference program appearances) and `people`, each with person IDs, degrees and sources, and `peopleSummary` (one entry per person, with position, institution and match counts). Report each person's matches by kind. Each paper or grant lists how each person is linked (`basis`: `profile` for a paper on their profile, `record` when a coauthor or shared-grant record names them, `openalex` for a paper in their OpenAlex record, `nih` or `nsf` for an award in the research library). A paper on someone's profile is a recorded association; do not call anyone an expert. |
| How someone connects to the lab | `GET /api/person/<id>` (`connection.paths`) or `GET /api/path?from=lab&to=<id>` |
| Whether someone's connections were collected | `GET /api/person/<id>` (`collection`: status, date, rule, identity basis) |
| Someone's publication record and awards | `GET /api/person/<id>` (`library`: OpenAlex summary and identity basis, recent works, NIH and NSF awards); `GET /api/person/<id>/latest` checks the sources live for anything newer (`sources` says which it read and when each last updated that person; `skipped: true` when the live check is off or the lab is the demo) |
| An OpenAlex record that is not someone's | Only on the user's decision (rule 4). `POST /api/contacts/<id>/not-theirs` with `{"openalexId": "A…", "note": "…"}`, or `{"openalexId": "A…", "undo": true}` to withdraw it; or `python3 labnetwork.py not-theirs <workspace> <id> <record> --actor "<name>"`. `GET /api/person/<id>` lists the marks (`openalexNotTheirs`) and the records held back after the lookup by ORCID iD (`library.openalex.held`). The library, refresh and expand never link a marked record to the person. To accept a held-back record instead, also only on the user's decision, add its URL with `POST /api/contacts/<id>/sources`. If their sources link no OpenAlex record yet, first add the URLs of the records they hold (`library.openalex.urls`, the main one first): once the sources link any record, the library uses only those links. |
| Whether the library and weekly update are set up | `GET /api/digests` (`library`: last run and coverage, schedule, counts, and the commands to build, update and schedule it) |
| Lab members' Slack submissions | `GET /api/intake` (mode, roster, last check, each submission and its status: `added`, `held`, `pending`, `dismissed`, `undone`). `POST /api/intake/check` reads the channel now. `POST /api/intake/items/<ts>` with `{"action": "add", "person": "<id or new>"}`, `"dismiss"` or `"undo"`. |
| Who is at an institution, by department and lab | `GET /api/affiliations` (or `?inst=<key>` for one institution's people). Departments and labs are inferred; say so. A shared institution is not a relationship. |
| Shortest path between two people | `GET /api/path?from=<id>&to=<id>&mode=any` (or `mode=directed`) |
| How someone outside the network connects | `GET /api/outside?name=<name>` lists OpenAlex author records (`person` is set when one is already in the network); show them to the user to choose (rule 4). `GET /api/outside/<A…>` lists the people in the network who coauthored their papers (30 or fewer authors), with degrees; `degree` is one more than the closest. Nothing is saved. Each lookup uses about $0.001 of the OpenAlex allowance. |
| Who is on a conference's program | Import it first, with the user's OK: `python3 labnetwork.py conference <workspace> <file.csv> --name "…" --short AACAP --year 2026 --url <program page>`. Build the file only from the public program (rule 3: never add a name the program does not print). Then search the short name (`q=AACAP`, or `q=ADHD AND AACAP`). `GET /api/conferences` lists programs and how many names are in the network; `?id=<id>` lists every appearance with its match. |
| Record a meeting | `POST /api/notes` with `{"fields": {"contactId", "metBy", "date", "context", "learning", "followUp", "recordedBy"}, "tie": {"from": "<lab member id>"}}` |
| Add a person | `POST /api/contacts` with `{"fields": {"name", "institution", "role", ...}}`. A 409 response lists possible duplicates: ask the user. |
| Import a spreadsheet | `python3 labnetwork.py import <workspace> <file>` shows a preview. Possible duplicates need a decision (`--decisions`, a JSON file mapping each item key to `merge:<id>`, `create` or `skip`). Apply with `--yes` only after the user agrees. |
| Back up | `python3 labnetwork.py export <workspace>` writes a bundle that reproduces the workspace exactly |
| Candidates for a mentorship or advisory team | Search the topic, keep people with a confirmed doctorate, and give each one's route to the lab and the evidence. Say which evidence is a public record and which is reported. |

## Troubleshooting

- **Port in use:** `serve ... --port 8780`.
- **"Another Lab Network server (pid …, port …) is using this workspace":** a server is already running for it; use that one or stop it.
- **"notebook.json no longer matches":** the packet changed after the workspace was created. Restore the packet, or
  build a new one and import the old workspace's export bundle into a new workspace.
- **OpenAlex or RePORTER errors:** wait a minute and run again; finished requests are cached.
- **"Stopped before finishing … run again tomorrow":** `expand` reached its spending limit for the day. Cached
  responses make the next run cheap.
- **"The library is not complete yet":** the library used up OpenAlex's free daily allowance. The weekly update, or
  `library <workspace>` the next day, continues where it stopped.
- **"Another library run is in progress":** the weekly update or another `library` command is running. Wait for it to
  finish. The operating system releases the lock when that run's process exits, even if it is killed, so there is
  no file to delete.
- **The map is crowded:** use "1st degree", the people filters, or the Relationships menu. Smaller
  `maxCoauthorsPerMember` and `maxNewPerBridge` values in `lab.json` shrink only the starting build; `expand` adds
  the rest of each member's coauthors, and `expand --per-person N` adds at most N people for each first-degree person.
