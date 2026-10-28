# Privacy

Lab Network holds notes about real people, so it is built to stay on one computer. The one exception is a read-only
copy for your lab, which goes online only if you build and deploy it yourself ([hosting.md](hosting.md)).

## Kept on your computer

- The packet, the workspace, every note and edit, and every export. They are ordinary files in the folders you chose.
- The app itself. It listens only on 127.0.0.1 (this computer), answers only requests addressed to localhost, and
  accepts changes only as JSON from its own page, so other websites open in the same browser cannot read or change
  your notebook.
- There are no accounts, analytics or telemetry, and no cloud copy unless you deploy a read-only copy.

## Sent to other services

| Occasion | Data sent | Recipient |
|---|---|---|
| `onboard`, `expand`, `library` and `refresh` | Names, ORCID iDs and OpenAlex author IDs of the people being looked up; your email if you set `contactEmail` | OpenAlex (api.openalex.org); `onboard`, `expand` and `refresh` also ask ORCID (pub.orcid.org) |
| `library`, its weekly update, and `onboard` (which builds the library) | Names of people within the degree limit, and NIH profile IDs of matched investigators | NIH RePORTER (api.reporter.nih.gov) and NSF (api.nsf.gov) |
| Opening a person's page | Their OpenAlex author ID and NIH profile ID, to check for papers and awards newer than the last update. Off in the demo, and after `config WORKSPACE --live-check off`. | OpenAlex and NIH RePORTER |
| `onboard` and `refresh` | NIH RePORTER profile IDs of lab members | NIH RePORTER (api.reporter.nih.gov) |
| Slack intake, if set up (when the app starts, every ten minutes while it is open, and `intake --check`) | The bot token, and requests to read one channel's messages and its members' names. Nothing is posted. | Slack (slack.com) |
| Viewing the app | Requests for each photo shown, sent by your browser to the site that hosts it, without a referrer | The photo's host (usually a university website) |
| Opening a source link | An ordinary visit to that page | That site |
| `site`, the first time | A download of Pyodide, the in-browser Python the read-only copy uses (nothing about your lab) | npm (registry.npmjs.org) |
| Deploying a read-only copy, only if you do it ([hosting.md](hosting.md)) | The whole notebook except Slack submissions, imports, exports and logs | The host you choose, behind its sign-in |

Notes, follow-ups, relationships you reported and your edits are never sent anywhere, except in a read-only copy you
deploy, which holds their current versions. With the Slack intake, lab
members' submissions are in your Slack workspace already. Lab Network copies each one's answers into
`workspace/intake.json`, and keeps nothing from people who are not on the lab roster. Lookups only send identifiers
of people the build, expansion, library or refresh is reading. `expand` and `library` cache responses on this
computer, outside the lab folder (`~/Library/Caches/lab-network` on macOS, `~/.cache/labnetwork` on Linux,
`%LOCALAPPDATA%\labnetwork\cache` on Windows), so a repeated run sends almost nothing. `onboard` keeps its cache in the
lab folder (`cache/`); `refresh` and the live check on a person's page keep none.

Without an API key, OpenAlex counts requests against a free daily allowance shared by everyone on your network
address, and its responses suggest that an assistant register a key with your email. Lab Network does not register
one; `expand` and `library` stop before reaching the allowance (`--budget`, default $0.08 of the $0.10 day, or $0.90
of the $1 a day with a key).

The weekly update runs on your computer under your user account and sends the same lookups as
`library WORKSPACE --update`. The demo lab sends nothing: its library comes with it, and its lookups are off.

## Habits that keep it private

- Keep lab folders outside this repository. The repository's `.gitignore` excludes `lab.json`, `packet/`,
  `workspace/` and `exports/`, but a folder elsewhere is safer.
- Treat export bundles like the notes themselves: they contain everything.
- Treat a read-only copy the same way. Put it online only behind a sign-in that admits the people you list, and check
  that a signed-out browser is refused before you deploy it ([hosting.md](hosting.md)).
- A cloud-synced folder (Dropbox, OneDrive, iCloud) works, but run the app on one computer at a time; Lab Network
  refuses to start a second server on the same workspace.
- Before sharing a bundle with a colleague, remember that it includes every note and its author.

## Contact notes and consent

Notes record your professional impressions of people who have not agreed to be in a database. Keep them factual
(where you met, what you discussed, what you promised to send), and follow your institution's rules on personal data.
