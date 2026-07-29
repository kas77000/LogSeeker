# LogSeeker

Self-service retrieval of order logs from the algo log hosts.

Only two people have shell access to `loghost.example.internal`. Today, everyone else asks
one of them to run a WinSCP + batch + remote-ksh chain by hand and copy the
result to a network share. LogSeeker turns that chain into a web app: one of
those two people hosts it, and everyone else requests logs through the browser.

**There is no sign-in.** The operator launches the app on his machine with his
own log-host credentials in the environment; every visitor's search runs on the
server as him. That is deliberate — it is what makes the tool usable by people
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

## Step 3 — Give the app its own SSH key

Do not reuse your personal interactive key. A dedicated key can be revoked on
its own if the app is ever retired or compromised, and it makes the server-side
logs show which connections came from LogSeeker.

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\logseeker_ed25519" -C "logseeker"
```

Press Enter twice to leave the passphrase empty. (A passphrase would have to be
stored in `.env` next to the key to allow unattended start-up, which buys you
nothing — protect the key with file permissions instead.)

If the log host is older AIX/Solaris and rejects ed25519, use RSA instead:

```powershell
ssh-keygen -t rsa -b 4096 -f "$env:USERPROFILE\.ssh\logseeker_rsa" -C "logseeker"
```

Install the public half on the log host:

```powershell
Get-Content "$env:USERPROFILE\.ssh\logseeker_ed25519.pub" |
    ssh youruser@loghost.example.internal "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

Check it works without a password:

```powershell
ssh -i "$env:USERPROFILE\.ssh\logseeker_ed25519" youruser@loghost.example.internal "echo OK; ls /apps/k/algologs/ap"
```

You should see `OK` and a list of instance directories, with no password prompt.
**That command also tells you the real instance names** — note them down for
step 5.

---

## Step 4 — Pin the log host's key

Skip this and the app will refuse to connect (by design — an unverified host key
means you cannot tell the real server from an impostor).

The `ssh` command in step 3 already added the host to your `known_hosts` on
first connection, when it asked *"Are you sure you want to continue connecting?"*.
So just point LogSeeker at that file — it is at:

```
C:\Users\<you>\.ssh\known_hosts
```

Confirm the entry exists:

```powershell
Select-String -Path "$env:USERPROFILE\.ssh\known_hosts" -Pattern "loghost.example.internal"
```

If it is missing, connect once with `ssh` and accept the fingerprint, then check
again. Prefer this over `ssh-keyscan`, which fetches whatever key the network
hands it and verifies nothing.

---

## Step 5 — Write your `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

The settings that must be right:

```ini
LOGSEEKER_BACKEND=ssh
LOGSEEKER_SSH_HOST=loghost.example.internal
LOGSEEKER_SSH_USER=youruser
LOGSEEKER_SSH_KEY_PATH=C:/Users/youruser/.ssh/logseeker_ed25519
LOGSEEKER_SSH_KNOWN_HOSTS=C:/Users/youruser/.ssh/known_hosts

LOGSEEKER_LOG_DIR_TEMPLATE=/apps/k/algologs/ap/{instance}/logs
LOGSEEKER_LOG_GLOB_TEMPLATE=APA*_pingOrderStream_{date}*.out
LOGSEEKER_INSTANCES=APIA,APIE
```

Notes that save time:

- **Use forward slashes** in Windows paths. Backslashes in a `.env` are a common
  cause of "key not found".
- `{instance}` and `{date}` are placeholders the app fills in. Leave them alone;
  change the text around them.
- `LOGSEEKER_INSTANCES` is an **allowlist** — an instance not in this list cannot
  be searched at all. Put the real names from step 3 here.
- `.env` is gitignored. It holds your log-host access; treat it like a password.

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
rather than allowed. Until you set this, the app shows a standing warning on
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
| **Instance** | Which system's logs. Pick more than one to search both at once. |
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

The other two Advanced options are ordinary grep behaviour: **Ignore
upper/lower case** (`grep -i`), and **Lines of context** (`grep -C`), which
includes the surrounding lines so you can see what happened either side of a
match.

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
| **"no ssh_known_hosts file configured…"** on every page | Step 4. Set `LOGSEEKER_SSH_KNOWN_HOSTS`. |
| **"SSH connection failed: Server ... not found in known_hosts"** | The host key is not pinned. `ssh youruser@loghost.example.internal` once, accept the fingerprint, retry. |
| **"SSH connection failed: Authentication failed"** | Public key not installed, or wrong `SSH_KEY_PATH`. Re-run the `ssh -i ...` check at the end of step 3. |
| **"ssh_key_path does not exist"** | Backslashes in `.env`. Use forward slashes: `C:/Users/...`. |
| **"No log files exist for that instance/date"** | Non-trading day, wrong instance, or a wrong path template. Use **Admin → Log directory probe** to see what is really there. |
| **Searches return nothing but the probe finds files** | The order id is not in those logs, or you are on the wrong instance. Try regex mode with a fragment of the id. |
| **App searches fake data** | A leftover `.streamlit\secrets.toml` from the demo. Delete it. Check **Admin → Configuration → source**. |
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
| `SSH_USER` | — | **The operator's login.** Every visitor searches as this account. |
| `SSH_KEY_PATH` | — | Private key for the app |
| `SSH_PASSWORD` | — | Alternative to a key; prefer the key |
| `SSH_KNOWN_HOSTS` | — | Host key verification |
| `SSH_AUTO_ADD_HOST_KEY` | `false` | Disables host verification. First connection only. |
| `LOG_DIR_TEMPLATE` | `/apps/k/algologs/ap/{instance}/logs` | Where logs live |
| `LOG_GLOB_TEMPLATE` | `APA*_pingOrderStream_{date}*.out` | Which files to scan |
| `INSTANCES` | `APIA,APIE` | Allowlist; nothing outside it can be searched |
| `ALLOWED_NETWORKS` | *(empty)* | CIDR allowlist. The app's only access control. |
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
| One order id, one instance, one date | Multiple instances, date ranges, exact or regex, context lines | The follow-up question was always "and the day before?". |
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

67 tests covering: input validation including a dozen shell-injection attempts;
command construction and glob quoting; request identity and filename collisions;
the CIDR allowlist and its fail-closed behaviour; config resolution order; the
line, byte and disk caps *and their warnings*; gzip reuse; cache-hit metadata;
audit tail-reads; an end-to-end search through the local backend; and headless
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
4. **Consider a dedicated service account.** Every search reaches the log host as
   `SSH_USER`, so the server-side trail will show one person doing everything. A
   read-only account named for the app keeps that trail honest and can be revoked
   without touching anyone's personal login.
