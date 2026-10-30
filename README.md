# KHL League

A KHL fantasy league website. Every manager buys 4 KHL clubs with 20 coins. Clubs score
3 points for a regulation win, 2 for an overtime or shootout win, 1 for an overtime or
shootout loss, and 0 for a regulation loss. The league table updates live.

It runs without Claude:

| Part | Service | Cost |
|---|---|---|
| Website | GitHub Pages | free |
| Database and sign-in | Supabase | free plan |
| Hourly results update | GitHub Actions | free |

```
index.html                     the website (one page)
config.js                      your Supabase address and public key
supabase/schema.sql            database tables, prices, security rules
updater/update.py              reads khl.ru and saves results to Supabase
updater/requirements.txt
.github/workflows/update.yml   runs the updater every hour
```

---

## Setup (about 20 minutes)

### 1. Create the database (Supabase)

1. Sign up at <https://supabase.com> and click **New project**. Pick a region close to
   you (for example Frankfurt), set a database password, and wait for it to start.
2. Open **SQL Editor** → **New query**, paste the whole of `supabase/schema.sql`, and
   click **Run**. You should see "Success". This creates the clubs with your prices,
   the tables, the security rules and the save-team function.
3. Open **Project Settings → API** and copy:
   - **Project URL** (looks like `https://abcd1234.supabase.co`)
   - **anon public** key
   - **service_role** key (secret: only for GitHub, never put it in the website)

### 2. Put the code on GitHub

1. Create a new repository on <https://github.com> (for example `khl-league`).
   It can be public.
2. Upload all the files in this folder, keeping the folders as they are
   (`.github/workflows/update.yml` must stay in that path).
3. Edit `config.js` in the repository and paste your **Project URL** and **anon public** key.

### 3. Turn on the hourly update

1. In the repository: **Settings → Secrets and variables → Actions → New repository secret**.
   Add two secrets:
   - `SUPABASE_URL` = your Project URL
   - `SUPABASE_SERVICE_KEY` = your service_role key
2. Open the **Actions** tab, enable workflows if asked, choose **Update KHL results**,
   and click **Run workflow**. After a minute or two it should finish green, with a line like
   `Saved standings for 22 clubs; 748 of 748 matches new or changed`.
   After that it runs by itself every hour.

### 4. Publish the website

1. In the repository: **Settings → Pages**. Under **Build and deployment**, choose
   **Deploy from a branch**, branch `main`, folder `/ (root)`, and save.
2. After a minute your site is live at `https://YOUR-USERNAME.github.io/khl-league/`.

### 5. Let people sign in

1. In Supabase: **Authentication → URL Configuration**. Set **Site URL** to your GitHub
   Pages address, and add the same address under **Redirect URLs**.
2. Send the link to your friends. They type their email, open the sign-in link they
   receive, and build their team.

> Supabase's built-in email sender only sends a few sign-in emails per hour. That is
> fine for a group of friends. For a bigger league, connect your own email service under
> **Authentication → Emails → SMTP Settings**.

---

## Running the league

- **Change a club's price:** Supabase → **Table Editor → clubs** → edit `price`.
  The website picks it up straight away. Teams already saved keep their clubs.
- **Change the budget:** **Table Editor → settings** → `budget`.
- **Count points from the start of the season** instead of from when a team was saved:
  **Table Editor → settings** → set `count_from` to `season` (or back to `save`).
- **Remove someone's team:** **Table Editor → squads** → delete the row.
  Managers can also delete their own team from the **My team** tab.
- **Update results right now:** GitHub → **Actions → Update KHL results → Run workflow**.

## How it works

- `save_squad()` in the database checks the budget and records each club's points at
  the moment it was bought, so league points can't be faked from the browser.
- League points = points kept from sold clubs + (each current club's points now − its
  points when bought).
- The updater opens khl.ru in a headless Chromium browser (the site builds its pages with
  JavaScript and its data API needs a session token), asks the site's own API for the
  standings and the full season schedule, checks that the numbers add up, and saves only
  what changed. If a check fails it saves nothing and the GitHub run shows the reason.

## If the update stops working

Open the failed run in the **Actions** tab and read the `ERROR:` line.

- **"Could not open khl.ru" / "refused the request"**: khl.ru may be down or blocking
  GitHub's servers. Run the updater from your own computer instead:
  ```
  pip install -r updater/requirements.txt
  python -m playwright install chromium
  SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python updater/update.py
  ```
  (On Windows PowerShell, set the two values with `$env:SUPABASE_URL="..."` first.)
- **"format has changed"**: khl.ru changed its data. `parse_standings` and
  `parse_matches` in `updater/update.py` are the places to adjust.
- **"Unknown KHL club id"**: a club joined or was renamed. Add it to `CLUB_IDS` in
  `update.py` and to the `clubs` table.

Reading khl.ru automatically is not an official KHL service. Keep it to the hourly
schedule.
