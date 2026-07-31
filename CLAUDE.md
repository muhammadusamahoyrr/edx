# CourseMate — working notes

An AI tutor for Open edX. The repo documents *what* it is; this file covers what
a fresh session cannot infer: how to run things here, and where the traps are.

**Read `docs/LIMITATIONS.md` before claiming anything works.** It is deliberately
harsh and kept current. `git log` is the narrative record — commit messages carry
the reasoning, not just the diff.

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

    make check        # 98 tests + 6 import-linter contracts

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
image is a full rebuild measured in hours on this connection.

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

The six import-linter contracts in `.importlinter` enforce the structural half of
this and do fail when violated.
