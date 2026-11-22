# 🎁 Cadeaux Noël 2026 — family Christmas gift ideas

A small Streamlit app (UI in French) where about ten family members share gift
ideas for each other. Nobody ever sees their own page, so surprises are kept.
Entry is protected by one shared family password; after that, identity is
trust-based (tap your name). Data lives in a private
Google Sheet accessed through a Google Cloud service account.

```
app.py                         the app
init_sheet.py                  creates the tabs/headers and adds the family names
requirements.txt
.streamlit/config.toml         theme (light only), hides chrome
static/motifs/                 illustrations (used with permission), served by Streamlit
.streamlit/secrets.toml.example
```

## How it works

- The shared password (`app_password` in the secrets) is asked once. After
  that, a key `k=…` is added to the URL, so a bookmark or home-screen icon
  opens straight in. Anyone with such a link gets in without the password, so
  only send those links to the family. Changing the password invalidates all
  old links.
- `?user=Name` in the URL remembers who you are; bookmark it or add it to the
  iPhone home screen (Safari → Share → *Add to Home Screen*).
- The home page starts with a bar of names that stays at the top while you
  scroll; tap a name to jump to that person.
- "Mes souhaits": your own wishlist, which everyone else sees in your tile.
  Others can copy a wish into their private idea list ("+ idée") to track the
  purchase without you knowing.
- Then one tile per other family member (two per row on a computer, one per
  row on a phone), people with the fewest ideas first (order fixed when you
  open the app). Each tile shows their wishes, the ideas list, an
  "Ajouter une idée" link and a shared notepad that saves when you leave the field.
- An idea can be for several people at once (a joint gift, e.g. a couple): it
  shows in each of their tiles and stays hidden from all of them. In the sheet,
  `destinataire` then reads `Bruno + Victoire`.
- An idea can be marked secret: only the person who added it sees it, as a
  green-tinted line (`prive` = `oui` in the sheet).
- Deleting an idea or a wish is immediate; an "Annuler" bar appears for about
  12 seconds to bring it back.
- Reads: one API call fetches all five tabs, cached for 25 s and shared by all
  visitors. Writes: the change is applied to your screen immediately, then sent
  to the sheet in the background (one queue, so order is kept), and the cache is
  cleared. Only the affected row is appended, updated or deleted.
- If a write fails, a short French message appears on the next interaction and
  the screen reloads from the sheet.

## Setup

### 1. Google Cloud project and Sheets API
1. Go to <https://console.cloud.google.com/>, sign in, click the project picker
   at the top → **New project**, name it (e.g. `cadeaux-noel`) → **Create**.
2. With that project selected: **APIs & Services → Library**, search
   **Google Sheets API** → **Enable**.

### 2. Service account and JSON key
1. **APIs & Services → Credentials → Create credentials → Service account**.
2. Name it (e.g. `cadeaux`), click **Create and continue**, skip the optional
   role and user steps, **Done**.
3. Open the new service account → **Keys** tab → **Add key → Create new key →
   JSON → Create**. A `.json` file downloads. Keep it private and never commit
   it (the `.gitignore` excludes common names, but keep it outside the repo
   anyway).
4. Note the service account email, something like
   `cadeaux@cadeaux-noel.iam.gserviceaccount.com`.

> If your organisation blocks key creation (`iam.disableServiceAccountKeyCreation`),
> use a personal Google account for this project.

### 3. The Google Sheet
1. Create a new blank sheet at <https://sheets.new> and name it (e.g. *Cadeaux Noël*).
2. **Share** → paste the service account email → role **Editor** → untick
   *Notify people* → **Share**. The sheet itself stays private.
3. Copy the sheet ID from the URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`.

### 4. Local secrets and sheet initialisation
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```
Edit `.streamlit/secrets.toml`: set `sheet_id` and `app_password`, and copy each field of the JSON
key into `[gcp_service_account]`. For `private_key`, keep it on one line with
the `\n` sequences exactly as in the JSON file.

Then create the tabs and add the family:
```bash
python init_sheet.py --names "Alice, Bob, Camille, David"
```
This creates `personnes` (`nom`), `idees` (`id, destinataire, nom, prix,
statut, suggere_par, cree_le, lien, prive`), `commentaires` (`id, idee_id, auteur, texte,
cree_le`), `notes` (`destinataire, texte, modifie_par, modifie_le`: one
shared notepad per person) and `souhaits` (`id, personne, nom, prix, lien,
cree_le`: each person's own wishes), writes the headers, and adds any names not
already present. It is safe to run again. You can also do it by hand: create the five tabs with
exactly those headers in row 1 and type the names under `nom`.

To rename someone or add a person later, edit the `personnes` tab directly
(the app picks it up within ~25 s). Renaming does not rename existing ideas:
also update `destinataire`/`suggere_par`/`auteur` if needed.

Run it locally:
```bash
streamlit run app.py
```

### 5. Private GitHub repository
1. On GitHub: **New repository**, choose **Private**, no README.
2. In this folder:
   ```bash
   git init && git add . && git status   # check secrets.toml is NOT listed
   git commit -m "Gift ideas app"
   git branch -M main
   git remote add origin git@github.com:<you>/<repo>.git
   git push -u origin main
   ```

### 6. Deploy on Streamlit Community Cloud
1. Go to <https://share.streamlit.io>, sign in with GitHub, and authorise
   access to private repositories when asked.
2. **Create app → Deploy a public app from GitHub** (wording may vary): pick the
   repo, branch `main`, main file `app.py`. Optionally choose a custom subdomain
   (e.g. `cadeaux-famille`).
3. Open **Advanced settings → Secrets** and paste the whole content of your
   local `.streamlit/secrets.toml`. Choose Python 3.12 or 3.13. **Deploy**.
4. Secrets can be edited later: app menu **⋮ → Settings → Secrets**.

### 7. Who can open the app
Apps deployed from a private repository are **private by default**: only
viewers you invite by email (who must sign in) can open them. Choose one:
- **Invite viewers**: *Share* → add each family member's email. Each person
  signs in once with Google/GitHub/email link.
- **Make it public** (recommended with `app_password` set): *Share* → *Who can
  view this app* → public. Visitors must type the family password first.

Then send everyone the plain link (e.g. `https://cadeaux-famille.streamlit.app`).
Each person types the password once, taps their name, and adds the page to
their home screen from there, so the icon keeps both the key and their name.

## Testing without Google

A local JSON backend is built in:
```bash
python init_sheet.py --local --names "Alice, Bob, Chloé" --demo
PERENOEL_BACKEND=local streamlit run app.py
```
Add `PERENOEL_PASSWORD=test` in front to try the password screen locally.
`PERENOEL_LOCAL_DELAY=1` adds one second of fake latency to every read/write.

## Notes and limits
- Community Cloud apps go to sleep after some days without visitors; the first
  visitor then clicks a button to wake it (about a minute).
- On Community Cloud the small hosting badge/“Manage app” controls are drawn by
  the host page outside the app, so CSS cannot hide them.
- Sheets API quota is ~60 read requests per minute per user; with the shared
  25 s cache, normal family use stays far below that.
- Concurrency: last write wins. Changes by others appear within ~25 s.
