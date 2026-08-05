# CourseMate — working notes

An AI tutor for Open edX. The repo documents *what* it is; this file covers what
a fresh session cannot infer: how to run things here, and where the traps are.

**Read `docs/LIMITATIONS.md` before claiming anything works.** It is deliberately
harsh and kept current. `git log` is the narrative record — commit messages carry
the reasoning, not just the diff.

## STATE — read this first, then verify it

Last updated 2026-08-05. **Treat every line as stale until checked** — the
commands to check are given. A note that disagrees with the running system is
wrong; the system wins.

**The stack is up and working. Do not disturb it casually.**

> ⚠️ **The Docker daemon in `Ubuntu-24.04` is restart-looping** (2026-08-05).
> `dockerd` uptime resets every ~2–3 minutes while the WSL distro itself has been
> up for hours and memory is idle (9.2 GB free, PSI 0.00). Every container cycles
> with it, so `docker ps` uptimes are always seconds and `celery inspect` never
> gets a reply.
>
> **What it does NOT invalidate:** work observed completing is still real —
> tasks executed, beat dispatched, the sweep ran, the access filter passed,
> the reindex wrote 227 chunks. Restarts happen *between* operations, they do not
> corrupt results.
>
> **What it does invalidate:** any claim that an unattended nightly job is
> dependable here. Beat re-reads its persistent schedule on each restart so 03:30
> would still fire, but this is not a stable host.
>
> `systemctl`/`journalctl` are unusable — the systemd user session fails to start,
> which is the warning printed on every `wsl` call and is probably related.
> First thing to try is `wsl --shutdown` then restart; that is now **safe**,
> because the package lives in the image rather than in container layers.

| What | State | Check |
|---|---|---|
| Everything through the sweep | Done, verified live | `git log --oneline` |
| Plugin migrations | Applied | `tools/ops/migrate.sh` |
| Package in all 4 containers | From the IMAGE now, not container layers | `tools/ops/check_install.sh` |
| Celery tasks registered | Yes, in both workers | `tools/ops/check_tasks.sh` |
| Beat dispatches the sweep | **VERIFIED** — both from a derived image and the real container | `tools/verification/beat_container_probe.sh` |
| `coursemate-beat` container | **RUNNING**, production schedule `crontab 30 3 * * *`, dispatched live | `docker ps \| grep beat` |
| openedx image carries the package | **YES** — rebuilt 2026-08-05 in 29 min, all 4 containers adopted it | `tools/ops/adopt_new_image.sh` |
| `--force-recreate` on openedx containers | **NOW SAFE** — the install comes from the image, not the container layer | `tools/ops/check_install.sh` after |
| Video transcripts | **VERIFIED end to end** on 1 video (583 chars, retrieved at score 1.000). The other 9 DemoX videos have edx-val rows with missing files | `tools/verification/add_test_transcript.sh` |
| Block-level access filter | **VERIFIED live end to end** — 2 restricted chunks hidden from a caller without the group, served to one with it | `tools/verification/access_filter_live.sh` |
| DemoX index | **227 chunks active, 222 blocks** (incl. 1 video), 2 carrying group tokens | `tools/ops/store_dump.sh` |
| Opt-in (`--all`) | **VERIFIED** — `course_has_tutor()` True on DemoX | `access_probe.sh` §E |
| Docker daemon stability | **RESTART-LOOPING** every ~2-3 min — see the warning above | `ps -eo etime,cmd \| grep dockerd` |

**Done 2026-08-05:** the openedx image was rebuilt with the plugin baked in, all
four containers adopted it, and `coursemate-beat` runs for the first time. The
one thing left unhealthy is the host, not CourseMate — see the warning above.

### Do not do these without asking

1. ~~**`tutor local restart` / `--force-recreate`**~~ — **no longer dangerous**
   as of the 2026-08-05 image rebuild. The four containers used to hold pip
   installs in the *container layer*, so recreating one silently reverted
   CourseMate to absent while the LMS kept working. The package now comes from
   the image, so a recreate keeps it. `tools/ops/adopt_new_image.sh` does this
   and verifies the install survived. Still check `check_install.sh` afterwards
   rather than assuming.
2. **Rebuild the openedx image "just to be safe".** Measured 2026-08-05:
   **29 minutes** with a warm buildx cache, not the hours this note used to
   claim — the July estimate was made on a ~0.6 MB/s link and a cold cache.
   Still not free (it compiles Python, runs npm and webpack), so ask; but it is
   no longer a reason to avoid fixing something.
3. **Re-run `coursemate_reindex` on a whim.** The index is good. A reindex is
   safe by design (write→verify→swap) but it is not free.

Rebuilding the *service* image is fine — ~20s, offline, no dependencies.

## Verify, don't assume

Claims get labelled **VERIFIED** (observed), **INFERRED** (from source), or
**UNVERIFIED** (docs only). Several things in this project passed review while
being broken, because the failure path returned success:

- a swap boundary that indexed 226 blocks and served 26,
- a confidence gate that could never fire (`score = raw / best` makes the top hit
  always 1.0),
- Celery discarding every task while Studio's Publish button returned 200,
- models whose tables never existed, because nothing read them back.

A green response is not evidence. Check the thing the user would check.

## Environment

Open edX runs under Tutor 21.0.8 (Ulmo) inside the WSL distro **`Ubuntu-24.04`**
— not `ubuntu`, not Docker Desktop's distro. Containers are `tutor_local-*`.
The DemoX course key here is **`course-v1:OpenedX+DemoX+DemoCourse`** (not the
upstream `edX+DemoX+Demo_Course`).

## Tests

    make check        # 105 tests + 6 import-linter contracts

Runs on Windows against `.venv/`. No Open edX, no network, no containers.

## Running anything in WSL

**Put it in a script file under `tools/ops/` and call it with
`MSYS_NO_PATHCONV=1`.** Inline `wsl -d Ubuntu-24.04 -- bash -lc '...'` from Git
Bash silently drops shell variable expansion — `SRC="/mnt/c/..."` arrives empty.
That is not cosmetic: an early inline `cp -r "$SRC"/*` became `cp -r /*` and
copied 291 MB of the host filesystem. Every script here uses `set -eu` and tests
that its paths exist before writing.

    tools/ops/sync.sh              # working tree  -> ~/cm-build in WSL
    tools/ops/deploy_platform.sh   # dev loop: copy package into running containers
    tools/ops/install_workers.sh   # real pip install into the two worker containers
    tools/ops/deploy_image.sh      # production path: build it into the openedx image
    tools/ops/migrate.sh           # apply the plugin's migrations
    tools/ops/adopt_new_image.sh   # move the 4 containers onto a rebuilt image
    tools/verification/*.sh        # evidence-producing probes

## Deploying the platform package

`docker cp` puts code on `sys.path` but installs **no dist-info**, so pip's
`cms.djangoapp` / `lms.djangoapp` entry points are missing — and those entry
points are how Open edX discovers a plugin app. Without them the app never
reaches `INSTALLED_APPS`, Celery autodiscovers nothing, and every enqueued task
dies with `Received unregistered task` while the request path returns 200.

So: `deploy_platform.sh` is for iterating on code already installed. Anything
that must work — workers, or any container that starts later — needs a real
install (`install_workers.sh`) or the image (`deploy_image.sh`).

Rebuilding the service image is fast (~20s, offline). Rebuilding the **openedx**
image took 29 minutes on 2026-08-05 with a warm cache; budget longer if buildx
has to re-pull.

## Architecture invariants

Do not break these without saying so explicitly:

1. The browser streams **directly** from the FastAPI service via the same-origin
   `/coursemate/` Caddy path. The XBlock only mints a short-lived JWT. **No
   Gunicorn worker is ever held open during generation** — worker pools exhaust
   by occupancy, not computation.
2. Only `content_adapter` touches the modulestore, and only the published branch.
3. Ingestion is published-only. `XBLOCK_CREATED` / `XBLOCK_UPDATED` fire on
   drafts and are deliberately **not** subscribed.
4. Nothing in `settings/common.py` may raise or read `ENV_TOKENS` — it runs
   inside LMS/CMS startup for every course on the instance.
5. `tasks/__init__.py` must import every task module. An empty one registers
   nothing.
6. Staff-only content is dropped at ingest; cohort/track restrictions are
   **carried and filtered at query time**. Filtering the second kind at ingest
   would hide paid content from the students who paid for it.
7. Indexing is opt-in: a course qualifies by containing the tutor block.
   `--force-all` exists and says what it is doing.

The six import-linter contracts in `.importlinter` enforce the structural half of
this and do fail when violated.
