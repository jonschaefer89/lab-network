# Adding to Lab Network from Slack

Lab members can add people, relationships and meeting notes without opening Lab Network. They post a short
submission in a private Slack channel, and Lab Network reads the channel and records each submission under their
name. On a paid Slack plan the submission can be a form; on the free plan, which cannot build forms, lab members fill
in a pinned template instead. Nobody
outside your computer can reach Lab Network itself: it only reads from Slack, and it never posts there.

## What lab members do

With a form, a lab member opens it from a bookmark at the top of the channel. Without one, they copy the pinned
template, fill it in and post it. Either way they answer:

| Question | Answer |
|---|---|
| Their full name | Required |
| Their institution | Optional |
| Their position | Optional |
| How do you know them? | Required, from a list |
| When | Optional date |
| Where or how you met | Optional |
| What you talked about, or why they matter | Optional |
| Follow-up | Optional |
| Link to their profile page | Optional |

Lab Network turns one submission into up to three records:

- **The person.** If they are already in the notebook, the submission attaches to them. Otherwise a new person is
  added, with the institution, position and link from the form.
- **A meeting note.** The note is added when the answer is "Met them" or when the form says where they met, what was
  discussed, or a follow-up. It is written by the lab member and shows on the Notes page.
- **A relationship** between the lab member and the person, for every answer except "Other". "Met them" becomes a
  firsthand meeting record. The other answers become reported relationships: collaborator, coauthor, mentor or
  trainee (in the right direction), committee member, or lab colleague.

Each change is stored under "*Name* (via Slack)", so the change history shows who added what.

## Automatic or reviewed

The **Slack inbox** on the Data page has a switch, "Add lab members' submissions automatically". The `intake`
command sets the same thing with `--mode automatic` or `--mode review`.

- **Review** (the default for a new lab): every submission waits in the inbox until you add it, choosing who it is
  about, or dismiss it.
- **Automatic:** a submission is added as soon as Lab Network reads it. It waits in the inbox only when Lab Network
  cannot tell who the person is:
  - Several people already in the notebook have that name.
  - The name is close to one person's but not the same, and the institutions differ.
  - Someone with that family name works at the same institution.
  - The message is plain text rather than the form.

In automatic mode, Lab Network attaches a submission to someone already in the notebook only when the names agree:
the same given name, ignoring titles such as "Dr.", or a nickname of it ("Bill" for "William", "Matt" for "Matthew").
An initial, a name that only starts the same way ("Jun" and "Junjie"), a short form that fits several names ("Sam"
for Samuel or Samantha), or a short form that is also a name of its own ("Jack", "Liam", "Kate") counts only when the
institutions agree too; otherwise the submission waits for you.

The same person submitted again is not added twice: the submission attaches to them and adds its meeting note. A
second report of the same relationship by the same lab member adds no second record; a report by another lab member
adds their own.

In either mode, every addition can be undone from the inbox. Undo archives the records the submission created. A
person it added stays if later records, such as another lab member's note, refer to them.

## Who can submit

Only lab members can add anything. Three things enforce this:

1. **The channel is private.** Only the people you invite can see it or use its form.
2. **Senders must be on the lab roster.** Lab Network accepts submissions only from Slack accounts matched to lab
   members, and setup prints that list for you to check. Messages from anyone else are ignored, and their content is
   not kept.
3. **The sender comes from Slack.** For a typed message, the sender is whoever posted it, even if an app sent it
   for them. A form names its sender on its first `From:` line, which the workflow fills in itself. Because any
   channel member could make an app post a message, the first form from an app waits in the inbox until you choose
   **Trust this form**. After that, Lab Network reads forms only from that app and ignores posts from any other.
   `intake <workspace> --forget-form` withdraws the trust.

Lab Network ignores thread replies, people joining the channel, topic changes, and other apps' messages. It never
posts to the channel.

## Setting it up

Plan on about fifteen minutes. Do steps 1 to 4 in Slack, then step 5 in a terminal on the computer with Lab Network.

### 1. A private channel

Create a private channel, for example `#lab-network`, and invite the lab members. Invite only people who should be
able to add to the notebook.

### 2. A bot that can read the channel

Lab Network reads the channel with a Slack bot token. You can use an app your workspace already has, if it has the
scopes below. Otherwise, make one at <https://api.slack.com/apps>: choose **Create New App**, then **From scratch**,
and give it these **Bot Token Scopes** under **OAuth & Permissions**:

| Scope | Why |
|---|---|
| `groups:history` | Read messages in private channels the bot is in |
| `groups:read` | Find the channel by name during setup |
| `users:read` | Match channel members to lab members by name |

Install the app to your workspace and copy its **Bot User OAuth Token**, which starts with `xoxb-`. Then invite the
bot to the channel: in the channel, type `/invite @` followed by the app's name.

### 3. Save the token

Save the token as a single line in `~/.config/lab-network/slack-token`, and make the file readable only by you:

```bash
mkdir -p ~/.config/lab-network && pbpaste > ~/.config/lab-network/slack-token && chmod 600 ~/.config/lab-network/slack-token
```

`pbpaste` is macOS. On another system, paste the token into the file with any editor.

You have two alternatives:

- Set the environment variable `LABNETWORK_SLACK_TOKEN`.
- If the token already lives in a JSON file of secrets, run setup with `--token-file PATH --token-key KEY`, and Lab
  Network reads it from there.

Lab Network never copies the token, and never shows it in the app or in its files.

### 4. The form

In Slack, open **Workflow Builder** (under **Tools**, then **Workflows**) and create a workflow that starts from a
link in Slack. Add the link as a bookmark in the channel so lab members can find it. Then add two steps:

1. **Collect info in a form.** Add the nine questions from the table above, using these exact labels. "How do you
   know them?" is a select list with these exact options:
   - Met them
   - Collaborator
   - Coauthor
   - My mentor or advisor
   - My mentee or trainee
   - On my committee
   - I'm on their committee
   - Colleague in my lab or department
   - Other
2. **Send a message to a channel**, to your private channel. Use the text below. Type each label exactly as shown,
   and insert each answer with Workflow Builder's **Insert a variable** button. The `From:` line takes the variable
   for the person who submitted the form.

```text
Lab Network submission
From: {person who submitted the form}
Name: {Their full name}
Institution: {Their institution}
Position: {Their position}
Relationship: {How do you know them?}
When: {When}
Where: {Where or how you met}
Notes: {What you talked about, or why they matter}
Follow-up: {Follow-up}
Link: {Link to their profile page}
```

If your Slack plan has no Workflow Builder (the free plan cannot create workflows), pin a template like this one in
the channel and ask lab members to copy it:

```text
To add someone to Lab Network, copy the lines below, fill in what you know, and post them here. Only the first line,
Name and Relationship are needed.

Lab Network submission
Name:
Institution:
Position:
Relationship:
When:
Where:
Notes:
Follow-up:
Link:

For Relationship, use one of: Met them, Collaborator, Coauthor, My mentor or advisor, My mentee or trainee, On my
committee, I'm on their committee, Colleague in my lab or department, Other.
```

Lab Network ignores the pinned template itself, because its fields are empty, and leaves out the template's own
instructions if they are pasted along. In a typed message, the answers above Notes can come in any order.
Everything after Notes, up to Follow-up or Link, belongs to the notes, even a line that looks like a label. Each
answer is one line, except Where, Notes and Follow-up, which can run over several lines.

### 5. Connect Lab Network

Stop the app first, so only one process writes the workspace. Then run:

```bash
python3 labnetwork.py intake ~/LabNetwork/my-lab/workspace --setup --channel '#lab-network' --mode review
```

The setup finds the channel and matches its members to the lab members by name. It prints who can submit and who in
the channel matched no one; check that list. Then start the app as usual. It reads the channel a few seconds after
it starts, and every ten minutes while it is open. To read the channel without the app, run
`python3 labnetwork.py intake <workspace> --check`.

Lab Network also reads messages posted in the channel before setup. Typed messages wait in the inbox in either mode,
so earlier conversation is never added on its own.

## What is kept where

- **In Slack:** the submissions stay in the channel, under your workspace's message-retention settings.
- **On your computer:** `workspace/intake.json` keeps the channel, the lab roster, each submission's answers, its link
  in Slack, and what it added. The added records live in the workspace like any other edit, and the token stays
  where you saved it.
- **Never stored:** the content of messages from people who are not on the roster, and the token.

## Limits

- Lab Network reads each message once. If a lab member edits or deletes a message after it was read, the change does
  not reach Lab Network. Undo the submission in the inbox and send it again.
- Lab members cannot browse or search the notebook from Slack; Lab Network itself stays on your computer.
- Lab Network fills in a new person's details. It does not change the details of someone already in the notebook.
  An institution or position from the form goes into the note, if the submission makes one.
- An answer longer than the notebook allows is shortened: 2,000 characters for Where and Follow-up, and about 4,800
  for Notes.
