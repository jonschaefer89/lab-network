# Sharing a read-only copy with your lab

Lab Network runs on one computer. To let lab members browse the network from their own browsers, build a read-only
copy and put it online behind a sign-in that admits only the people you list. This guide uses Cloudflare, which is
free for a lab: Cloudflare Pages holds the files and Cloudflare Access lets in only the people you list.

## About the copy

- **The whole app, read-only.** The copy is a folder of static files: the web app, Lab Network's Python code and a
  snapshot of your workspace. Each visitor's browser runs the Python with [Pyodide](https://pyodide.org), so the map,
  search, paths, profiles and conference pages work as they do on your computer. The first visit downloads about
  20 MB; later visits use the browser's cache.
- **Nothing can be changed in it.** The buttons that would add or edit something say where changes go instead (you set
  that sentence with `--how-to-add`). Lab members can still add people and notes from Slack
  ([slack-intake.md](slack-intake.md)); their additions reach the copy the next time you rebuild it.
- **Included:** the notebook as it is now: people, relationships and their evidence, meeting notes, the
  research library, conference programs and update digests.
- **Left out:** the change history, archived records and earlier values of edited fields; Slack
  submissions, imported spreadsheets, exports and logs; and the text files that came with the packet. A profile does
  not check OpenAlex and NIH for newer papers; the weekly update does that on your computer.

The copy contains your current meeting notes, so everyone you admit can read all of them.

## 1. Build the copy and look at it

```bash
python3 labnetwork.py site ~/LabNetwork/my-lab/workspace --out ~/LabNetwork/my-lab-site \
  --how-to-add "To add someone or a meeting note, post it in #lab-network on Slack."
python3 labnetwork.py site-preview ~/LabNetwork/my-lab-site --open
```

Build the copy in a folder of its own, outside the Lab Network folder; `site` refuses to write inside it, inside the
workspace or inside the packet. The first build downloads Pyodide (about 6 MB) from npm and checks it against a pinned checksum. `site-preview`
serves the copy on this computer only, with the same security headers it will have online.

## 2. Set up Cloudflare

The menu names in this guide match Cloudflare's dashboard in October 2026.

1. Create a Cloudflare account at [dash.cloudflare.com](https://dash.cloudflare.com/sign-up), or sign in to yours.
2. In the dashboard, open **Zero Trust**, pick a team name, and choose the **Zero Trust Free** plan. The checkout asks
   for a card or PayPal account, a billing address and agreement to the terms. The Free plan covers up to 50 people,
   and the amount due is $0.
3. Sign in to Cloudflare's command-line tool, which `npx` downloads and runs without installing anything:

   ```bash
   npx wrangler login
   ```

## 3. Create the site with a placeholder, then lock it

Put a placeholder online first, never your data, and add the sign-in before anything else:

```bash
mkdir -p ~/LabNetwork/placeholder && echo "Lab Network" > ~/LabNetwork/placeholder/index.html
npx wrangler pages project create my-lab-network --production-branch main --force
npx wrangler pages deploy ~/LabNetwork/placeholder --project-name my-lab-network --branch main
```

Without `--force`, recent versions of wrangler try to create a Workers project instead and stop with an error. Use
`--force` only for `project create`.

The site has two kinds of address, and each needs its own sign-in rule: the main address
(`my-lab-network.pages.dev`), which you send to the lab, and the address each deployment gets
(`<id>.my-lab-network.pages.dev`).

1. **Deployment addresses.** Open **Workers & Pages**, select the project, go to **Settings**, and under **Preview
   access** choose **Restrict previews**. Cloudflare creates an Access application for `*.my-lab-network.pages.dev`
   that admits only the members of your Cloudflare account. Leave it that way; lab members use the main address.
2. **Main address.** Open **Zero Trust → Access controls → Applications**, choose **Create new application**, then
   **Self-hosted and private** and **Continue**.
3. Under **Destinations**, choose **Switch to custom input** below the **Subdomain** field and type
   `my-lab-network.pages.dev`.
4. Under **Access policies**, choose **Create new policy**. Name it (for example, Lab members), keep the action
   **Allow**, choose **Emails** as the **Include** rule, and type each lab member's address, pressing Enter after
   each one. Choose **Save policy**.
5. Under **Details**, name the application (for example, Lab Network) and choose **Create**.

Lab members sign in with a code that Cloudflare emails them (the One-time PIN login method, which a new Zero Trust
account has by default), so nobody needs a Cloudflare account.

## 4. Check that it is locked

Open both addresses in a private browser window. Each must show Cloudflare's sign-in page, which asks for an email and
offers **Send login code**, not the placeholder. From a terminal:

```bash
curl -sI https://my-lab-network.pages.dev | head -3
```

The answer must be a redirect (`302`) to `<your team>.cloudflareaccess.com`. Check one deployment address the same
way; `npx wrangler pages deployment list --project-name my-lab-network` lists them.

## 5. Put the copy online

```bash
npx wrangler pages deploy ~/LabNetwork/my-lab-site --project-name my-lab-network --branch main
```

Send the lab the address `https://my-lab-network.pages.dev`. Each person enters their email, types the code they
receive, and the app opens.

## Keep it current

Rebuild and deploy whenever you want the lab to see what changed, for example after the weekly library update:

```bash
python3 labnetwork.py site ~/LabNetwork/my-lab/workspace --out ~/LabNetwork/my-lab-site --force \
  --how-to-add "To add someone or a meeting note, post it in #lab-network on Slack."
npx wrangler pages deploy ~/LabNetwork/my-lab-site --project-name my-lab-network --branch main
```

`--force` replaces an earlier copy, and only after the new one is complete; it never removes a folder that holds
anything else.

## Change who can sign in

Open **Zero Trust → Access controls → Applications**, select the application for the main address, and edit the email
list in its policy. Someone you remove cannot sign in again; a session they already have lasts until it expires
(24 hours by default; change it under **Session Duration** in the application's **Details**).

## Sign in with Google instead of a code

Emailed codes sometimes land in spam, and each one stops working 10 minutes after it is sent. If you add Google as a
second sign-in method, people who are signed in to Google get in with a click, and the emailed code stays available.
Cloudflare checks the address of the Google account each person chooses, so that address must be on the policy's
list. An institutional address works only if the person has a Google account at that address. Otherwise people sign
in with a personal Google account, and that address must be on the list.

1. In Cloudflare, open **Zero Trust → Integrations → Identity providers**, choose **Add an identity provider**, then
   **Google**. Keep this page open: Google needs its **Callback URL**, and the client ID and secret from Google go
   into it.
2. In another tab, open the [Google Cloud console](https://console.cloud.google.com/projectcreate) and create a
   project, for example "Lab Network sign-in". It needs no billing account.
3. Open [Google Auth Platform](https://console.cloud.google.com/auth/overview) for that project and choose **Get
   started**. Enter an app name and your email, choose **External**, agree to Google's user data policy, then choose
   **Continue** and **Create**. Leave the app in **Testing**: Cloudflare asks Google only for a person's name, email
   address and profile, and Google lets any Google account sign in to a test-mode app that asks for no more than that.
4. Open **Clients**, choose **Create client**, and pick **Web application**. Under **Authorized JavaScript origins**,
   add the Callback URL up to and including `.cloudflareaccess.com`. Under **Authorized redirect URIs**, add the whole
   Callback URL. Choose **Create** and copy the **Client ID** and **Client secret** from the box that appears. Google
   shows the secret only once.
5. In the Cloudflare tab, paste them into **Client ID** and **Client secret** and choose **Save**. The **Name** you
   give it appears on the sign-in button after "Google", so a name such as "Personal Google account" tells people which
   account to choose.
6. Open the main application's **Login methods**. If **Accept all available identity providers** is on, Google is
   offered already; if it is off, select Google in the list and save.
7. In a private window, open the main address, choose the Google button and sign in with an address on the list.
   Then repeat the check in [section 4](#4-check-that-it-is-locked).

Some institutions block sign-in to outside apps. Google then says the app is blocked, and those people use the
emailed code or a personal Google account on the list.

## Notes on safety

- Never deploy the copy before both applications exist and step 4 shows the sign-in page.
- The copy's files tell search engines not to index them, but only the sign-in keeps them private.
- People you admit can save what they see, as with any shared document.
- The fictional demo lab is safe to publish without a sign-in, for example on GitHub Pages:
  `python3 labnetwork.py site --demo --out ~/LabNetwork/demo-site`. It is built from a fresh copy of the demo, so
  nothing typed into your own demo app goes with it.
