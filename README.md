# LogSeeker

Self-service retrieval of order logs from the algo log hosts.

Only two people have shell access to `loghost.example.internal`. Today, everyone else asks
one of them to run a WinSCP + batch + remote-ksh chain by hand and copy the
result to a network share. LogSeeker turns that chain into a web app: one of
those two people hosts it, and everyone else requests logs through the browser.

**There is no sign-in.** The operator launches the app on their machine with a
**service account's** log-host username and password in `.env`; every visitor's
search runs on the server as that account. That is deliberate — it is what makes the tool usable by people
who have no server access at all. It also means the URL *is* the credential, so
[Step 7](#step-7--let-colleagues-in-safely) is not optional.

---

## Contents

- [Setup guide](#setup-guide) — start here
- [Using it](#using-it) — the form, search types, running several at once
- [Working with big logs](#working-with-big-logs)
- [Troubleshooting](#troubleshooting)
- [Configuration reference](#configuration-reference)
- [What changed from the legacy scripts](#what-changed-from-the-legacy-scripts)
- [How it works](#how-it-works)
- [Tests](#tests)

---

# Setup guide

Roughly 30 minutes end to end. Do the steps in order — step 2 lets you confirm
the app works before any credentials are involved, which makes everything after
it much easier to debug.

## Before you start

You need:

| | |
|---|---|
| **The right machine** | The Windows box belonging to someone who can already SSH to `loghost.example.internal`. The app inherits *his* access; it cannot grant access nobody has. |
| **A working login on the log host** | Just the username and password you already type. **No admin rights on the log host are required, and nothing on it is modified.** |
| **Python 3.10 or newer** | `python --version`. Anaconda is fine. |
| **An SSH client** | `ssh -V`. Built into Windows 10/11; otherwise install Git for Windows. |
| **Ability to leave it running** | Colleagues can only use the app while the machine is on and the process is up. |

---

## Step 1 — Install

```powershell
cd "path\to\LogSeeker"
pip install -r requirements.txt
```

That installs Streamlit (the web framework), paramiko (SSH), and pandas.

---

## Step 2 — Prove it works, before touching the real server

This is the most useful five minutes in the whole guide. It generates fake logs
on your own machine and runs the app against them, so if something breaks later
you already know the app itself is sound.

```powershell
python tools\make_demo.py
streamlit run streamlit_app.py
```

Open <http://localhost:8501>. Search for order id **`540000001`**, instance
`APIA`, today's date. You should get a few hundred matching lines within a
second, with a download button.

If that worked, the app is fine and everything from here is configuration.

Now clear the demo away so it cannot shadow your real settings:

```powershell
Remove-Item .streamlit\secrets.toml
Remove-Item -Recurse demo_logs
```

> `make_demo.py` writes `.streamlit\secrets.toml` pointing at the fake logs. If
> you leave it there and your `.env` is incomplete, the app will quietly keep
> searching fake data. Delete it.

---

## Step 3 — Connect to the log host once, by hand

Nothing has to be installed or configured on the server. LogSeeker signs in with
your ordinary login, exactly as you would at a prompt — **no admin rights on the
log host are needed, and nothing there changes.**

> **Two ways to sign in.** This step is common to both. At the end of it, step 3A
> (password) works immediately; step 3B (SSH key) is the upgrade to take when you
> can put a key on the log host. You can run both at once during the switch.

Open PowerShell and connect the way you normally would:

```powershell
ssh youruser@loghost.example.internal
```

The first time, `ssh` asks:

```
The authenticity of host 'loghost.example.internal' can't be established.
ED25519 key fingerprint is SHA256:...
Are you sure you want to continue connecting (yes/no)?
```

Type **`yes`**. That writes the host's key into `C:\Users\<you>\.ssh\known_hosts`
on *your* machine, which is how the app later tells the real server from an
impostor. It is a local file — it needs nothing from the server.

Then type your password, and once you are on the host:

```bash
ls /apps/k/algologs/ap
```

**Note down what that prints** — those are the real instance names, and they go
into `LOGSEEKER_INSTANCES` in step 5. Type `exit` to come back.

If that worked, you have everything the app needs: a host, a username, a
password, and a pinned host key.

---

## Step 3A — Sign in with your password *(start here)*

Nothing more to do. Put the password you just typed into `.env` at step 5:

```ini
LOGSEEKER_SSH_PASSWORD=your-log-host-password
```

That is the whole method. It needs no cooperation from anyone, nothing is
installed on the server, and it is closest to what the legacy WinSCP chain did —
that prompted for the same password.

Two costs, both manageable: the password sits in plain text in `.env` (so
restrict the file — step 5), and **when your password rotates the app stops
working** until you edit that line and restart.

---

## Step 3B — Sign in with an SSH key *(better, when you can)*

Take this route as soon as you are able to append a key to `~/.ssh/authorized_keys`
on the log host. It is worth the effort for three reasons:

- it survives password rotation, so the app stops breaking every 90 days;
- it can be revoked on its own, without touching your login;
- the server-side trail shows which connections came from LogSeeker rather than
  from you at a terminal.

**Installing it needs no admin rights** — `authorized_keys` lives in your own
home directory. It does need the host to permit key auth, which is the only
reason this is not the default.

Generate a key **for the app**, not your personal interactive one:

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\logseeker_ed25519" -C "logseeker"
```

Press Enter twice for an empty passphrase. A passphrase would have to be stored
in `.env` right next to the key for unattended start-up, which protects nothing —
use file permissions instead.

If the host is older AIX/Solaris and rejects ed25519, use RSA:

```powershell
ssh-keygen -t rsa -b 4096 -f "$env:USERPROFILE\.ssh\logseeker_rsa" -C "logseeker"
```

Install the public half (this prompts for your password one last time):

```powershell
Get-Content "$env:USERPROFILE\.ssh\logseeker_ed25519.pub" |
    ssh youruser@loghost.example.internal "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

Confirm it works **without** a password prompt:

```powershell
ssh -i "$env:USERPROFILE\.ssh\logseeker_ed25519" youruser@loghost.example.internal "echo OK"
```

Then add the key to `.env` at step 5:

```ini
LOGSEEKER_SSH_KEY_PATH=C:/Users/youruser/.ssh/logseeker_ed25519
```

### Switching from the password to a key later

You do not need a flag day. **paramiko tries keys before passwords**, so if both
`LOGSEEKER_SSH_KEY_PATH` and `LOGSEEKER_SSH_PASSWORD` are set, the key is used
and the password is only a fallback.

1. Leave `LOGSEEKER_SSH_PASSWORD` exactly where it is.
2. Add `LOGSEEKER_SSH_KEY_PATH` and restart.
3. Open **Admin → Health**. It states which credential is in use — you are
   looking for *"SSH key, with the password as fallback"*.
4. Once that reads correctly and a search works, delete the password line and
   restart. Admin should then say *"SSH key"*.

If the key is wrong or not installed, step 3 silently falls back to the password
and everything keeps working — which is why checking Admin matters rather than
assuming the key took effect.

---

## Step 4 — Confirm the host key was saved

```powershell
Select-String -Path "$env:USERPROFILE\.ssh\known_hosts" -Pattern "loghost"
```

One or more lines means you are set — leave `LOGSEEKER_SSH_KNOWN_HOSTS` blank in
step 5 and the app uses this file automatically.

Nothing printed means the fingerprint was never accepted. Repeat step 3 and
answer `yes` at the prompt. Do not reach for `ssh-keyscan` instead — it accepts
whatever key the network hands back, which is precisely the check being skipped.

---

## Step 5 — Write your `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

The settings that must be right — the same things you typed in step 3:

```ini
LOGSEEKER_BACKEND=ssh
LOGSEEKER_SSH_HOST=loghost.example.internal
LOGSEEKER_SSH_USER=youruser
LOGSEEKER_SSH_KNOWN_HOSTS=

# --- sign-in: step 3A, step 3B, or both during a switch ---
LOGSEEKER_SSH_PASSWORD=your-log-host-password
#LOGSEEKER_SSH_KEY_PATH=C:/Users/youruser/.ssh/logseeker_ed25519

LOGSEEKER_LOG_DIR_TEMPLATE=/apps/k/algologs/ap/{instance}/logs
LOGSEEKER_LOG_GLOB_TEMPLATE=APA*_pingOrderStream_{date}*.out
LOGSEEKER_INSTANCES=APIA,APIB,APIC,APID,APIE,APIF,APIG,APIH,APII,APIL,APIM
```

**Uncomment the key line and keep the password** while you switch to a key —
the key is tried first, so the password just sits there as a safety net until
you confirm the key works and delete it. **Admin → Health** tells you which one
is actually being used.

Notes that save time:

- **The password is plain text here, deliberately.** This file is the one place
  your login lives, so it is obvious where to change it and what to delete when
  the app is retired. It is also the only thing that has to change when your
  password expires — edit it and restart.
- **`.env` is gitignored.** Keep it that way. Anyone who can read this file can
  log in as you, so on a shared machine lock it down:
  `icacls .env /inheritance:r /grant:r "%USERNAME%:R"`
- **Leave `LOGSEEKER_SSH_KNOWN_HOSTS` blank.** The app then uses the
  `C:\Users\<you>\.ssh\known_hosts` that step 3 populated. Set a path only if you
  deliberately keep a separate file.
- **Use forward slashes** in any Windows path. Backslashes in a `.env` are a
  common cause of "file not found".
- `{instance}` and `{date}` are placeholders the app fills in. Leave them alone;
  change the text around them.
- `LOGSEEKER_INSTANCES` is an **allowlist** — an instance not in this list cannot
  be searched at all. Put the real names from step 3 here.

---

## Step 6 — Verify the configuration from the app

```powershell
streamlit run streamlit_app.py
```

Go to the **Admin** page. It exists for exactly this moment.

1. **Health tab → Test connection.** You should see the log host's `uname`
   output. If not, the error tells you which of steps 3–5 to revisit.
2. **Health tab → Log directory probe.** Pick an instance, type a recent trading
   date as `YYYYMMDD`, press Probe. You should get a list of real log files.
   *This is the check that catches a wrong `LOG_DIR_TEMPLATE` or
   `LOG_GLOB_TEMPLATE`* — the two settings most likely to be subtly wrong.
3. **Configuration tab.** Confirm each setting and, in the `source` column, that
   it came from where you think. If something says `secrets.toml` when you edited
   `.env`, you left the demo file behind — go back to step 2.

The Configuration tab also shows the exact commands the app will run on the log
host. Worth a glance if you want to know precisely what it does.

---

## Step 7 — Let colleagues in safely

Right now the app is only reachable from your own machine. Two things to do
before you share a link.

**First, restrict who can reach it.** There is no sign-in, so this is the only
access control the app has. Add your office ranges to `.env`:

```ini
LOGSEEKER_ALLOWED_NETWORKS=10.0.0.0/8,192.168.0.0/16
```

Requests from anywhere else get a refusal page. The check **fails closed**: if
the list is set and the client address cannot be determined, access is denied
rather than allowed. Your own browser on the host machine (`localhost`) is always
let in. If you put nginx or Caddy in front of the app *on the same machine*, set
`LOGSEEKER_ALLOW_LOCALHOST=false`, because every visitor would then look like
localhost. Until you set this, the app shows a standing warning on
every page — that warning is accurate, not decoration.

**Second, open the port** (once, as Administrator):

```powershell
New-NetFirewallRule -DisplayName "LogSeeker" -Direction Inbound `
    -LocalPort 8501 -Protocol TCP -Action Allow -Profile Domain,Private
```

Then start it for the network:

```powershell
.\run.ps1
```

`run.ps1` prints the link to share, the account searches will run as, and warns
if you have no network restriction. For a local-only test use `.\run.ps1 -LocalOnly`.

> Streamlit prints an `External URL` on startup. If that address is genuinely
> routable from outside your organisation, anyone who finds it can read
> production order logs as `LOGSEEKER_SSH_USER`. Check with your network team
> before sharing anything beyond the internal address. For real internet
> exposure, put nginx or Caddy in front to terminate TLS, and prefer a VPN.

---

## Step 8 — Keep it running

The app only works while the process is up. To restart it automatically at
logon:

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -File `"$PWD\run.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "LogSeeker" -Action $action -Trigger $trigger
```

For something more robust that survives logoff and restarts on crash, install
[NSSM](https://nssm.cc/) and register `streamlit` as a Windows service.

Housekeeping runs by itself: expired results are cleared at start-up and after
every search, and the results folder is held to `LOGSEEKER_MAX_DISK_BYTES`
(5 GB by default) by evicting the oldest first. You can also purge on demand
from **Admin → Storage**.

---

# Using it

## The search form

| Field | What it does |
|---|---|
| **Order id** | What to look for. Searched as literal text by default. |
| **Instance** | Which system's logs. **Leave it empty** (with kdb configured) and the app asks kdb which instance handled the order, then searches only that one. Pick one or more to override. |
| **Dates** | **Single date** by default, since that is almost always what you want. Switch to **Date range** above the form when you need several days. |
| **Reason** | Optional. Goes in the audit log next to your search. |

## Search type — "exact text" vs "pattern"

Under **Advanced**. This is the one option worth understanding.

**Exact text** (the default) looks for exactly what you typed. `540000001` matches
that string and nothing else. Punctuation is just punctuation. This is what you
want for an order id, and it is the faster of the two.

**Pattern (regex)** treats your input as a regular expression, so certain
characters become operators rather than literal text:

| Pattern | Finds |
|---|---|
| `5400000(01\|02)` | either of two order ids in one search |
| `orderId=54000.*FILL` | lines where a matching id is followed by `FILL` |
| `orderId=5400000[0-9]{2}` | a whole block of ids |
| `ERROR\|REJECT` | either word |

The trade-off: in pattern mode `.` means "any character", `*` means "repeat",
and `|` means "or", so a search containing those characters will not do what you
expect unless you meant them as operators. If you are pasting an order id,
leave it on **Exact text**.

The other Advanced option is ordinary grep behaviour: **Ignore upper/lower
case** (`grep -i`).

## Checking the id in kdb

When `LOGSEEKER_KDB_RDB` and/or `LOGSEEKER_KDB_HDB` are set, every exact-text
search first asks the OMS `target` table whether the id exists and whose it is:

| Date searched | Asked of | Query |
|---|---|---|
| today | RDB | `select trader from target where id_target=<id>` |
| any earlier date | HDB | `select date, trader from target where date in <dates>, id_target=<id>` |

**The `trader` column names the log folder** (`APIA`, `APIE`, …). Leave
**Instance** empty and the app uses it: kdb first, then a grep of that instance
only. The trader must be one of `LOGSEEKER_INSTANCES` (any case). A value from kdb
cannot point the search anywhere else. If kdb has no answer (id not found, kdb
down, or a pattern search), an automatic search stops before grepping and asks you
to pick the instance.

The card then says **"id_target exists in kdb — trader: …"**, or warns that kdb
has no such target for those dates, which usually means a mistyped id or the
wrong date. When you picked the instance yourself, **the log search runs either way**: kdb
being down or missing an id never blocks fetching the logs.

`id_target` is a 32-bit int in kdb (`meta target` shows type `i`), so the id is
sent as an int. An id that is not all digits, or is over 2,147,483,647, cannot
exist there and is not looked up; the card says why instead of a bare "not
found". Pattern (regex) searches are not sent to kdb.

pykx connects over IPC only, so no q licence is needed. Each lookup runs in its
own short-lived Python process (`logseeker/kdbworker/lookup.py`), so a pykx
failure cannot take the app down. **Admin → Health → kdb** has a connection
test and a one-off id lookup.

## Running several searches at once

You do not have to wait. Submit a search, and while it is scanning you can
submit another — each one gets its own status card showing queued, scanning with
a progress bar, or finished with a download button. Cards stay until you dismiss
them.

Three searches run at a time by default (`LOGSEEKER_WORKERS`); beyond that they
queue, and the card says so. Two people asking the identical question share a
single grep rather than running it twice, and the **Results** page shows what
everyone has run — if a colleague already pulled the logs you need, take their
file.

---

# Working with big logs

Order-stream shards run to about a gigabyte each, so a single day across three
shards is ~3 GB and a fortnight can be 45 GB. That is the size this was built
for. Measured against **three 1.07 GB logs, 3.21 GB and 16.5 million lines
total**:

| | |
|---|---|
| Single order id across all 3.21 GB | 3,300 matches, **~0 MB** Python memory growth |
| Pattern matching all 16.5 M lines | capped at 200,000 lines, **~0 MB** growth |
| 65 MB result, gzipped for download | **3.3 MB** (95% smaller) |
| Progress reporting | real, per gigabyte: 0% → 33% → 67% → 100% |
| Refusing an oversized search | before reading a single byte |
| Running out of time mid-scan | partial results kept, *"2 GB of 3 GB"* reported |

> **On timings.** Those runs completed in seconds on a local NVMe with a warm
> page cache, which is not a number to plan with. On a real NFS- or SAN-backed
> log host, grep is disk-bound: expect roughly 50–200 MB/s, so 3 GB is more like
> **15–60 seconds** and a 45 GB fortnight is **4–15 minutes**. The app is not the
> bottleneck at any of these sizes — the log host's disk is. What the app
> guarantees is that its own memory does not grow with the data.

## Why memory stays flat

Nothing ever holds a whole log, or a whole result, in memory:

- `grep` runs **on the log host**. Only matching lines cross the network — a
  gigabyte log with 3,000 matches sends about a megabyte.
- Matches are streamed to disk in 64 KB chunks as they arrive.
- The on-screen preview reads the first 200 lines only.
- Line counts for cached results come from a small `.meta.json` sidecar, so a
  cache hit never re-reads the payload.
- gzip is produced by streaming 1 MB blocks and is built once, then reused.

## Knowing what you are about to scan

The file listing uses `du -k`, so before any reading starts the app knows how
many files there are **and how big they are**. That one change is what makes
gigabyte logs manageable:

- **A real progress bar.** Fraction of *log bytes read*, not time elapsed against
  the timeout. On a ten-minute scan the latter tells you nothing.
- **An ETA**, from measured throughput once enough has been read to be honest.
- **A pre-flight refusal.** A search over more than `MAX_SCAN_BYTES` (200 GB) is
  rejected with the actual figure — *"would scan 340 GB across 312 files"* —
  rather than being discovered by timing out ten minutes later.
- **Batched greps.** Files are grepped in ~512 MB batches over one SSH
  connection, so progress advances as each batch lands, and a search that runs
  out of time **keeps the batches that finished**.

The **Admin → Log directory probe** shows the same figures for one instance and
date, so you can check what a search would cost before running it.

## The caps, and how they tell you

Six limits stop one careless search from hurting everyone. **Each announces
itself** — a truncated result is never presented as a complete one:

| Cap | Default | What you see |
|---|---|---|
| `MAX_SCAN_BYTES` | 200 GB | Refused up front: *"would scan 340 GB across 312 files, over the 200 GB limit."* |
| `MAX_FILES_PER_SEARCH` | 500 | *"More than 500 log files matched; only the first 500 were scanned."* |
| `COMMAND_TIMEOUT` | 900 s | *"Hit the 900s time limit after 12 GB of 45 GB; the output below is partial."* |
| `MAX_RESULT_LINES` | 200,000 | *"Output reached the 200,000-line cap. There are almost certainly more matches…"* |
| `MAX_RESULT_BYTES` | 64 MB | *"Output reached the 64 MB size cap and was cut off."* |
| `MAX_DAYS_PER_SEARCH` | 14 | Rejected at the form. |

Two of these are worth explaining.

**The line cap.** `head -n` closes the pipe and `grep` dies of SIGPIPE, so a
capped result is byte-for-byte indistinguishable from a complete one. Returning
200,000 of 16,500,000 matches silently is how somebody concludes an order
stopped trading at 09:03. Hence the explicit warning, and a test that fails if
it ever stops appearing. The cap is also **global, not per batch** — the budget
shrinks as batches complete, so the documented limit is the real one.

**The scan cap, not the file cap.** With gigabyte shards, "500 files" could be
half a terabyte. Counting files was the wrong guard; counting bytes is the right
one.

## Downloading large results

Results over 2 MB need one click to prepare, then offer **gzip first** (typically
ten-to-one on log text) with the raw `.txt` beside it.

This is not politeness — it is required. `st.download_button` needs its payload
in memory, so attaching a 60 MB file directly would re-read it on every script
rerun: once a second inside the live progress panel, and once per row on the
Results page. The two-step keeps that to a single read of a single file the user
actually asked for.

## If you are hitting the caps

Reach for these in order — the first is usually enough:

1. **Narrow the search.** One instance, one date, Exact text rather than Pattern.
   Exact (`grep -F`) is both faster and what you want for an order id.
2. **Raise the timeout** for wide date ranges: `LOGSEEKER_COMMAND_TIMEOUT=3600`.
   At 50–200 MB/s, 45 GB needs 4–15 minutes; the 900 s default is not enough for
   a full fortnight across several instances.
3. **Raise the line cap** if you genuinely need more:
   `LOGSEEKER_MAX_RESULT_LINES=500000`.
4. **Raise the scan cap** only deliberately: `LOGSEEKER_MAX_SCAN_BYTES`. It
   exists to stop someone accidentally asking for a terabyte.
5. **Lower `LOGSEEKER_WORKERS` to 1 or 2** if the log host feels loaded. Three
   concurrent greps over gigabytes is real I/O on a production box, and it is
   shared with whatever else runs there.
6. **Lower `LOGSEEKER_SCAN_BATCH_BYTES`** for finer progress on very large
   scans, at the cost of one extra SSH command per batch.

---

# Troubleshooting

| Symptom | Cause and fix |
|---|---|
| **"the log host's key is not pinned…"** on every page | Step 3 was never completed. `ssh youruser@loghost.example.internal` once and answer `yes` at the fingerprint prompt. |
| **"SSH connection failed: Server ... not found in known_hosts"** | Same cause: the host key is not pinned. Connect once by hand, accept the fingerprint, retry. |
| **"SSH connection failed: Authentication failed"** | Wrong `LOGSEEKER_SSH_PASSWORD` or `LOGSEEKER_SSH_USER`. Check by running `ssh youruser@loghost.example.internal` by hand — if that prompts you again, the password is wrong. Also check the password has not expired, and that it contains no stray quotes in `.env`. |
| **Password ends in a space, or starts *and* ends with a quote** | Wrap the value in double quotes: `LOGSEEKER_SSH_PASSWORD="p@ss "`. Unquoted values are trimmed, and a value that both starts and ends with the same quote character has them stripped. `#`, `$`, `=`, `\` and interior spaces all need no quoting. |
| **Worked yesterday, "Authentication failed" today** | On step 3A, the account's password expired or was rotated. Update `LOGSEEKER_SSH_PASSWORD` and restart. This is the failure step 3B removes. |
| **Added a key but it still uses the password** | The key was never accepted by the host, so paramiko fell through to the password. Check **Admin → Health → Signing in with**; if it says *"SSH key, with the password as fallback"* the key is being *offered*, not necessarily accepted. Prove it outside the app: `ssh -i <key> youruser@loghost "echo OK"` must not prompt. |
| **"ssh_key_path does not exist"** | Backslashes in `.env`. Use forward slashes: `C:/Users/...`. |
| **Key works by hand, fails in the app** | Usually a passphrase on the key. Either regenerate it without one (step 3B) or set `LOGSEEKER_SSH_KEY_PASSPHRASE`. Note paramiko falls back to using `SSH_PASSWORD` as the passphrase when none is set, which can mask the real cause. |
| **"No log files exist for that instance/date"** | Non-trading day, wrong instance, or a wrong path template. Use **Admin → Log directory probe** to see what is really there. |
| **Searches return nothing but the probe finds files** | The order id is not in those logs, or you are on the wrong instance. Try regex mode with a fragment of the id. |
| **App searches fake data** | A leftover `.streamlit\secrets.toml` from the demo. Delete it. Check **Admin → Configuration → source**. |
| **"Access denied" on your own machine** | Fixed: `localhost` is always allowed now. If you still see it, check `LOGSEEKER_ALLOW_LOCALHOST` is not `false`. |
| **Colleagues get "Access denied"** | Their IP is outside `LOGSEEKER_ALLOWED_NETWORKS`. Add their range. |
| **Colleagues cannot reach it at all** | Firewall rule missing (step 7), or started without `run.ps1` so it bound to localhost. |
| **"Search hit the time limit"** | Expected on very wide ranges — partial results are kept and the warning says how far it got. Narrow the range, or raise `LOGSEEKER_COMMAND_TIMEOUT`. |
| **"That search would scan N GB… over the limit"** | Working as intended. Narrow the range or pick one instance; raise `LOGSEEKER_MAX_SCAN_BYTES` only deliberately. |
| **Progress bar sits at 0% for a long time** | The first batch is still running. Batches are 512 MB by default; lower `LOGSEEKER_SCAN_BATCH_BYTES` for finer updates. |
| **Everything is slow with several people using it** | Lower `LOGSEEKER_WORKERS`. Identical searches already share one grep and are cached. |
| **Disk filling up** | It is bounded by `LOGSEEKER_MAX_DISK_BYTES`. Lower it, or lower `CACHE_TTL_HOURS`. |

---

# Configuration reference

Settings resolve in this order, most specific first:

**environment variable → `.env` → `.streamlit/secrets.toml` → default**

Environment beats `secrets.toml` on purpose: the launch environment is what the
operator controls at start-up, and a stale secrets file from a demo must not
silently override it. **Admin → Configuration** shows which layer won for each
setting.

| Setting (`LOGSEEKER_…`) | Default | Meaning |
|---|---|---|
| `BACKEND` | `ssh` | `ssh`, or `local` if the app runs on the log host itself |
| `SSH_HOST` / `SSH_PORT` | — / `22` | The log host |
| `SSH_USER` | — | **The service account's login.** Every visitor searches as this account. |
| `SSH_PASSWORD` | — | **Step 3A.** The service account's log-host password, plain text by design. |
| `SSH_KEY_PATH` | — | **Step 3B.** Private key for the app. **Set with `SSH_PASSWORD`, this wins** — the password becomes a fallback. |
| `SSH_KEY_PASSPHRASE` | — | Leave empty. Stored next to the key, it protects nothing. |
| `SSH_KNOWN_HOSTS` | *(empty)* | Host key verification. Blank uses `~/.ssh/known_hosts`. |
| `SSH_AUTO_ADD_HOST_KEY` | `false` | Disables host verification. First connection only. |
| `KDB_RDB` | *(empty)* | `host:port` of the OMS RDB; checks today's ids |
| `KDB_HDB` | *(empty)* | `host:port` of the OMS HDB; checks earlier dates |
| `KDB_USER` / `KDB_PASSWORD` | *(empty)* | Only if the kdb processes require a login |
| `KDB_TIMEOUT` | `30` | Seconds per kdb query |
| `LOG_DIR_TEMPLATE` | `/apps/k/algologs/ap/{instance}/logs` | Where logs live |
| `LOG_GLOB_TEMPLATE` | `APA*_pingOrderStream_{date}*.out` | Which files to scan |
| `INSTANCES` | `APIA,APIB,APIC,APID,APIE,APIF,APIG,APIH,APII,APIL,APIM` | Allowlist; nothing outside it can be searched |
| `ALLOWED_NETWORKS` | *(empty)* | CIDR allowlist. The app's only access control. |
| `ALLOW_LOCALHOST` | `true` | Always admit the host machine's own browser. `false` behind a same-machine reverse proxy. |
| `SHOW_ADMIN` | `true` | `false` hides the Admin page |
| `WORKERS` | `3` | Concurrent greps against the log host |
| `COMMAND_TIMEOUT` | `900` | Seconds before a search is killed |
| `MAX_RESULT_LINES` | `200000` | Server-side line cap, global across batches |
| `MAX_RESULT_BYTES` | `67108864` | Client-side byte cap (64 MB) |
| `MAX_SCAN_BYTES` | `214748364800` | Most log a search may read (200 GB) |
| `SCAN_BATCH_BYTES` | `536870912` | Log grepped per round trip (512 MB) |
| `MAX_FILES_PER_SEARCH` | `500` | Files scanned per search |
| `MAX_DAYS_PER_SEARCH` | `14` | Date-range ceiling |
| `CACHE_TTL_HOURS` | `12` | How long an identical search is reused |
| `MAX_DISK_BYTES` | `5368709120` | Results budget (5 GB); oldest evicted first |
| `INLINE_DOWNLOAD_BYTES` | `2097152` | Above this, downloads are two-step + gzip |
| `DATA_DIR` | `./data` | Results and audit log |

---

# What changed from the legacy scripts

The legacy scripts are kept verbatim in a `legacy/` folder, with their quirks and
defects catalogued in `legacy/NOTES.md`. That folder is deliberately not
published — it carries internal host names and share paths — so it lives only in
the working copy. Source comments that point at `../legacy/` refer to it.

| Legacy | LogSeeker | Why |
|---|---|---|
| WinSCP → `.bat` → `run_ftp_get.txt` → remote `.ksh` → SMB share | One SSH connection from the app | Four of the five parts existed only to pass three strings to a grep. |
| grep writes `$ORDER_ID.txt` on the server, a second SFTP hop fetches it | grep's stdout streams straight into the app | No temp file, no cleanup, no write permission needed on the log host, no dependency on `\\fileserver.example.internal\share\...`. |
| Every user hand-edits `youruser` out of 6 places (that is what `Readme.txt` is for) | The operator's login lives in one `.env` | This is the single reason the legacy flow could not be shared. |
| No match ⇒ deletes output, exits 5, WinSCP's `get` fails, operator sees `ERROR: WinSCP failed` | "No line matched" is its own reported outcome | "Nothing found" and "the transfer broke" are different answers to a client query. |
| Output is `<ORDER_ID>.txt` in one shared directory | `<id>_<instances>_<dates>_<hash>.txt` | Two people searching the same order for different dates silently overwrote each other. |
| Order id goes from a `.bat` variable straight into `ksh -c "…"` | Allowlist validation + `shlex.quote` everywhere | Fine for a trusted operator at a keyboard. **Remote code execution** once a web form drives it. |
| One order id, one instance, one date | Multiple instances, date ranges, exact or regex | The follow-up question was always "and the day before?". |
| Blocks the operator's terminal for minutes | Bounded worker pool, live progress, cancellable | Ten simultaneous requests must not become ten greps on a production host. |
| No record of who pulled what | Append-only JSONL audit log | Production order logs on a shared app. |
| Identical request re-greps from scratch | Result cache keyed on the search itself | Two people asking the same question share one grep. |

## The security change is the important one

The legacy chain interpolated `%ORDER_ID%` into a remote `ksh -c` command line.
An order id of `540000001; rm -rf ~` would have run as typed. That was
theoretical while the only caller was a colleague at a keyboard; it stops being
theoretical the moment the command is built from an HTTP request.

So in `logseeker/search.py`:

- order ids must match `^[A-Za-z0-9._:\-]{1,64}$`;
- instance names must match `^[A-Za-z0-9_\-]{1,32}$` **and** be in `INSTANCES`,
  so nobody can aim the glob at an arbitrary directory;
- dates must be exactly 8 digits;
- the pattern and every resolved filename go through `shlex.quote`;
- directories are quoted while glob metacharacters stay live (`quote_glob`), so
  a path containing a space cannot split into two arguments.

A visitor can search the logs. They cannot run commands on the host.

---

# How it works

```
run.ps1                launcher: prints the share link and the account in use
streamlit_app.py       network guard, navigation, shared sidebar
app_pages/
  search.py            request form + live progress panel
  results.py           every search this server has run
  admin.py             connection test, directory probe, audit log, storage
logseeker/
  config.py            env/.env/secrets resolution, config health check
  search.py            validation, request identity, command construction
  remote.py            SSH (paramiko) and local backends; streaming, cancellable
  jobs.py              worker pool, job records, cache short-circuit, housekeeping
  store.py             result files, cache, gzip, disk budget
  audit.py             append-only JSONL trail, tail-read from the end
  access.py            CIDR allowlist and audit labelling (no sign-in)
  kdb.py               id_target check against the OMS RDB/HDB
  kdbworker/lookup.py  the pykx side, run as a child process
  ui.py                formatting and download controls
```

A search runs in two steps against the log host, over a single SSH connection:

1. `du -k <globs>` resolves which log files exist **and how big they are**. This
   gives an accurate "no logs for that date" message, the scan size to check
   against `MAX_SCAN_BYTES`, a denominator for the progress bar, and lets step 2
   avoid shell globbing entirely.
2. `grep -F -- 'pattern' <explicit quoted file list> /dev/null | head -n N`, run
   once per ~512 MB batch with `N` set to the *remaining* line budget. The
   trailing `/dev/null` forces grep to prefix matches with the filename even when
   only one file matched — `grep -H` is not portable to the older greps on these
   hosts.

Searches run on a process-wide worker pool, never inside a Streamlit rerun. The
UI polls job records from a fragment that **stops ticking once the job
finishes**, so a completed 60 MB result is not re-rendered every second.

---

# Tests

```powershell
python -m pytest tests -q
```

104 tests covering: input validation including a dozen shell-injection attempts;
command construction and glob quoting; request identity and filename collisions;
the CIDR allowlist and its fail-closed behaviour; config resolution order; the
line, byte and disk caps *and their warnings*; gzip reuse; cache-hit metadata;
audit tail-reads; kdb RDB/HDB routing, failure reporting and a real
worker against a dead port; an end-to-end search through the local backend; and headless
renders of all three pages via `streamlit.testing.v1.AppTest`.

---

# Before this goes live

1. **Restrict the network** (step 7). With no sign-in it is the only thing
   between the URL and production order logs.
2. **Confirm `APA*` in the glob.** The legacy script really does use
   `APA*_pingOrderStream_…` while the instances are `APIA`/`APIE`, so the glob is
   not simply `{instance}*`. If that is a typo in the original,
   `LOG_GLOB_TEMPLATE` is where to fix it.
3. **Confirm the instance list** against what step 3's `ls` showed.
4. **Know what the password in `.env` gives away.** The app signs in with the
   operator's own account, so that file grants everything that account can do on
   the log host — not just reading logs. Restrict the file
   (`icacls .env /inheritance:r /grant:r "%USERNAME%:R"`), and if the desk ever
   *can* get a read-only account named for the app, moving to it costs one line
   in `.env` and makes the server-side trail honest — right now it shows one
   person doing everything.
5. **Have a plan for password expiry.** On step 3A, when the operator's password
   rotates the app stops working, with `Authentication failed` on every search.
   One line in `.env` and a restart fixes it; knowing that in advance saves an
   outage. **Step 3B removes this failure mode entirely**, which is the main
   practical argument for moving to a key as soon as you can.
6. **Ask whether a key is possible yet.** Appending to `~/.ssh/authorized_keys`
   needs no admin rights on the log host — only that the host permits key auth.
   It is worth asking early: it is a ten-minute change (step 3B), it can be done
   alongside the password with no downtime, and it fixes items 4 and 5 at once.
