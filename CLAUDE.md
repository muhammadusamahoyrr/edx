# CourseMate — working notes

An AI tutor for Open edX. The repo documents *what* it is; this file covers what
a fresh session cannot infer: how to run things here, and where the traps are.

**Read `docs/LIMITATIONS.md` before claiming anything works.** It is deliberately
harsh and kept current. `git log` is the narrative record — commit messages carry
the reasoning, not just the diff.

## STATE — read this first, then verify it

Last updated 2026-08-12. **Treat every line as stale until checked** — the
commands to check are given. A note that disagrees with the running system is
wrong; the system wins.

**The stack is up and working. Do not disturb it casually.**

> ⚠️ **WSL networking and the Docker daemon — updated 2026-08-12.**
>
> **`dockerd` still dies unprompted, and the interval is not predictable.**
> Observed on 2026-08-11/12: one clean run of about 1 h 45 min, and separately
> several restarts minutes apart (uptimes of 15 s, 23 s and 47 s were sampled
> during one session). Do not plan around a stable window. Symptoms: `docker ps`
> returns nothing, `systemctl is-active docker` says `inactive`.
>
> Recovery is `wsl --shutdown` then reopen the distro. **Everything deployed
> survives** — images, migrations and the SQLite stores are durable; that was
> re-checked after each recovery rather than assumed. Budget **~2 minutes** after
> any WSL boot before docker is `active` (systemd reaches the unit late; measured
> twice at 110–120 s), plus 1–2 more for the LMS to answer.
>
> **`systemctl is-active docker` is unreliable here** — it reported `inactive`
> while 13 containers were serving. Probe functionally (`docker ps`), not by unit
> state. The systemd *user* session still fails to start; that warning on every
> `wsl` call is cosmetic.
>
> **WSL now uses mirrored networking** (`%USERPROFILE%\.wslconfig`, backup at
> `.wslconfig.pre-phase4e.bak`). Before this, Windows could not reach the stack at
> all: the Hyper-V firewall for the WSL VM has `DefaultInboundAction: Block` with
> only an ICMP allow rule, so **ping succeeded and every TCP connection failed** —
> which reads as a routing problem and is not one. A narrow inbound rule needs
> admin; mirrored mode is the user-scoped equivalent. Two consequences:
> * Docker's iptables DNAT publishes with **no listening socket**, which mirrored
>   mode cannot project. It works because `docker-proxy` provides a real socket —
>   if `ss -ltn | grep :80` is empty, Windows will not reach the LMS.
> * In mirrored mode the host's ports are **already occupied from WSL's view**, so
>   binding 11434 inside WSL fails with `Address already in use`.
>
> **Adopting a new openedx image drops the `:80` socket (2026-08-12).** After
> `adopt_new_image.sh`, `ss -ltn | grep :80` came back EMPTY and Windows could not
> reach the LMS at all, while everything inside WSL answered normally. Recreating
> the containers takes the old `docker-proxy` with it and the replacement is not
> always projected. Recovery is one line:
>
>     docker restart tutor_local-caddy-1
>
> Check `ss -ltn | grep :80` **before** blaming the browser — the same symptom
> reads as "the page will not load" and sends you looking in the wrong place.
>
> **Windows→WSL reachability also flaps on its own.** Measured on 2026-08-12:
> after each socket recreate Windows got roughly 15 seconds of working requests
> and then `000` again, while WSL-internal curl held a steady 200 throughout. The
> Hyper-V firewall is still `DefaultInboundAction: Block`, so the durable fix is a
> narrow inbound allow rule, which needs admin. Until then, budget for browser
> work happening in short windows and re-check reachability between steps rather
> than assuming a failure is the application.
>
> **Ollama is bound to `127.0.0.1:11434` on Windows** and is not being changed.
> Containers reach it through a `socat` forwarder bound **only** to the tutor
> network gateway:
>
>     setsid socat TCP-LISTEN:11435,fork,reuseaddr,bind=172.18.0.1 \
>            TCP:127.0.0.1:11434 </dev/null >/tmp/socat.log 2>&1 &
>
> Run it as root (`wsl -u root`, no password needed) and **re-run it after any WSL
> restart** — it is not persistent. `COURSEMATE_MODEL_API_BASE` points at
> `http://172.18.0.1:11435`. If generation starts returning `unavailable`, check
> this forwarder before anything else.
>
> **`setsid` is load-bearing; `nohup … &` is not enough (2026-08-12).** Started
> with plain `nohup` from a `wsl -- bash script.sh` invocation, the forwarder dies
> the moment that invocation exits — the whole session is torn down and the child
> goes with it. It looks like it worked: the script prints a listening socket and
> a successful `/api/tags` probe, and the forwarder is gone by the next command.
> The symptom arrives later and elsewhere, as
> `APIConnectionError: Cannot connect to host 172.18.0.1:11435` and an
> `unavailable` frame. `setsid` detaches it into its own session, which survives.
> Verify it outlived the shell with a SEPARATE `wsl` call, not the one that
> started it.

| What | State | Check |
|---|---|---|
| Everything through the sweep | Done, verified live | `git log --oneline` |
| Plugin migrations | **0001–0004 applied**, incl. 0003 (mastery) + 0004 (difficulty_band), against the live DB with real data | `tools/ops/migrate.sh` |
| Service image | **`542e73c1`, rebuilt 2026-08-12** — planner, tagger, generator, WAL, plus B1/B2 conversational retrieval, the C1 spend ceiling and the C2 first-turn cache. 16 API routes live | `docker exec tutor_local-coursemate-1 python -c "import urllib.request,json;print(len(json.load(urllib.request.urlopen('http://127.0.0.1:8000/openapi.json'))['paths']))"` |
| Conversational retrieval (B1/B2) | **LIVE and browser-verified.** multi-turn r@3 0.333 → 0.917 | BENCHMARKS §3.8 |
| Daily spend ceiling (C1) | **LIVE.** 100k tokens/student/course/UTC day. Provider reports no usage here, so it charges an estimate | BENCHMARKS §3.9, LIMITATIONS §4.1 |
| First-turn response cache (C2) | **LIVE and browser-verified.** 74,973 ms → 133 ms, 0 charged on the hit | BENCHMARKS §3.10 |
| **`tutor.js` notices NOT deployed** | The `unauthenticated` and `budget_exceeded` messages are committed but live in the **openedx** image, which has not been rebuilt since. Students still see "Something went wrong." for an expired session | `docker exec tutor_local-lms-1 grep -c unauthenticated ...static/js/src/tutor.js` |
| openedx image carries the package | **YES — rebuilt 2026-08-12**, all 4 containers adopted it; carries the Phase 4C study-plan UI. (First baked in 2026-08-05, ~29 min) | `tools/ops/adopt_new_image.sh` |
| Feature B end to end | **VERIFIED IN A REAL BROWSER** — tab, 100-mark plan, generated question, abstention, as enrolled `cm_student` | BENCHMARKS §3.7 |
| OEX101 exam pack | **Loaded live** — 5 questions, 3 CLOs, 35 marks, 4 tagged | `/examprep/status` |
| Package in all 4 containers | From the IMAGE now, not container layers | `tools/ops/check_install.sh` |
| Celery tasks registered | Yes, in both workers | `tools/ops/check_tasks.sh` |
| Beat dispatches the sweep | **VERIFIED** — both from a derived image and the real container | `tools/verification/beat_container_probe.sh` |
| `coursemate-beat` container | **RUNNING**, `crontab 30 3 * * *`, dispatched live. Deliberately left on the OLD openedx image: it only dispatches `reconcile_all`, whose code is byte-identical | `docker ps \| grep beat` |
| `--force-recreate` on openedx containers | **NOW SAFE** — the install comes from the image, not the container layer | `tools/ops/check_install.sh` after |
| Video transcripts | **VERIFIED end to end** on 1 video (583 chars, retrieved at score 1.000). The other 9 DemoX videos have edx-val rows with missing files | `tools/verification/add_test_transcript.sh` |
| Block-level access filter | **VERIFIED live end to end** — 2 restricted chunks hidden from a caller without the group, served to one with it | `tools/verification/access_filter_live.sh` |
| Index | **2 courses**: DemoX 227 chunks (1 video, 2 restricted), OEX101 55 chunks | `tools/ops/store_dump.sh` |
| Course isolation | **VERIFIED with two courses present** — no cross-course leakage | `tools/verification/import_second_course.sh` |
| Enqueued bootstrap swaps | **FIXED 2026-08-05** — it never did; wrote inactive and reported success | `tools/verification/bootstrap_swap_probe.sh` |
| Opt-in (`--all`) | **VERIFIED** — `course_has_tutor()` True on DemoX | `access_probe.sh` §E |
| Shared state (rate limit, authz, LiteLLM cooldowns) | **Redis db1, VERIFIED live** — 2 limiters, one budget | `tools/ops/deploy_shared_state.sh` |
| `require_grounding` | **True by default now** (was False — the gate was opt-in) | `docker exec ...coursemate-1 python -c` |
| Claim verification | **VERIFIED live** — ungrounded sentence marked, grounded control clean | `tools/verification/claim_verify_live.sh` |
| Docker daemon stability | **STILL DIES UNPROMPTED**, interval unpredictable (minutes to ~1h45m observed). Recovers via `wsl --shutdown`; deployed state survives — see the warning above | `docker ps` (NOT `systemctl is-active`) |

**Done 2026-08-12:** both images rebuilt and adopted, migrations 0003/0004
applied to the live database, the OEX101 pack loaded, and Feature B verified end
to end in a real browser. Then Feature A: conversational retrieval (B1/B2), the
daily spend ceiling (C1) and the first-turn response cache (C2) — all three built,
deployed to the service image and verified in a real browser.

**Two of those shipped broken and passed every test.** B1/B2 and C2 both assumed
`request.history` holds prior turns; `tutor.js` pushes the current question into
it first, so the browser sends a shape no fixture in this repo had. Only a real
browser found either. If you add anything that reads `history`, capture a payload
off the wire before writing the test — see BENCHMARKS §4.5.

The one thing left unhealthy is still the host, not CourseMate — see the warning
above.

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

## Ollama (host-side, 2026-08-11)

Both models live on **`D:\ollama\models`** — moved off C, which was down to 45 GB.

    nomic-embed-text  0.26 GB   embeddings, 768-dim, ~1.4 s
    qwen2.5:7b        4.36 GB   chat, 25 s cold / 2.3 s warm on CPU

Two traps, both of which cost time on 2026-08-11:

1. **`OLLAMA_MODELS` is NOT authoritative on Windows.** Ollama 0.32.7 stores the
   path in its own settings DB — `%LOCALAPPDATA%\Ollama\db.sqlite`, table
   `settings`, column `models` — and that value **overrides the environment
   variable**. Setting the env var and restarting looks like it works and changes
   nothing; the server logs `OLLAMA_MODELS:<the db value>` regardless. Check the
   `server config` line in `%LOCALAPPDATA%\Ollama\server.log` to see what the
   server actually opened. The installer also resets the env var.
2. **An interrupted auto-update leaves inference broken but downloads working.**
   The 0.32.6→0.32.7 updater was killed mid-run, which emptied
   `AppData\Local\Programs\Ollama\lib\` of every runner binary. `ollama pull` and
   `ollama list` kept working — they need no runner — so the break was invisible
   until a generation failed with `llama-server binary not found`. Fixed by
   re-running the full installer (1.49 GB). If inference dies after an update,
   check `lib\ollama\llama-server.exe` exists before debugging anything else.

## A cold LMS trips the 5-second authz timeout (2026-08-12)

`authz_timeout_seconds = 5.0`. Measured against a freshly restarted LMS, three
consecutive enrollment checks from the same process:

    1st   TIMED OUT at 5.58s
    2nd   OK          3.46s
    3rd   OK          0.00s   (token cached in-process)

The first OAuth token exchange after the LMS comes up is slow enough to exceed
the timeout. The boundary then fails **closed** — which is correct, and is the
behaviour §10.1 wants — but the visible result is confusing: retrieval returns
nothing, and the exam-prep generator reports `abstained`. Two hours of Phase D2
went into chasing "why is it abstaining" before the log line
`enrollment unverifiable, denying: token exchange failed: timed out` explained
it.

Two practical consequences:

* **A restart of the service process re-cools it.** The token cache is
  per-process, so anything that restarts uvicorn puts the next request back at
  the 5.58s path.
* **Warm it before verifying anything.** Two throwaway calls are enough; the
  third is cached and free.

**Open decision, deliberately not taken:** whether `authz_timeout_seconds` should
be raised. Arguments both ways, and they are not close to obvious — 5s is a
sensible ceiling for a synchronous check on a student's request path, and raising
it makes a genuinely unreachable platform hold the student longer before failing
closed. But a value that a healthy-but-cold platform routinely exceeds turns a
timeout into a flapping outage. If it is raised, raise it for the *cold* case
only if the retry/caching behaviour cannot absorb it instead.

---

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

    make check        # 6 contracts + OpenAPI drift check + 736 backend
                      # + 63 browser tests
    make coverage     # gated at 80% for service+contracts (now 90.4%); platform ungated
    make agent-eval   # the 4 agent regression gates — needs no provider
    make openapi      # regenerate docs/openapi.json from the routes

Runs on Windows against `.venv/`. No Open edX, no network, no containers.

`make install` now also pulls **django, XBlock and web_fragments**. They are not
the Open edX runtime — they are the two libraries the plugin's models and block
are built on, and without them the mastery idempotency guarantee and the
exam-prep handler are untestable outside a container, which in practice means
untested. **Do not add `pytest-django`**: it calls `setup_test_environment()` at
session start and collides with the fixture in
`packages/coursemate-platform/tests/unit/conftest.py`, which has to own that
lifecycle to keep the in-memory database isolated per test.

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
8. **The agent's tool surface is read-only.** §10.6's claim — *"there is no prompt
   that makes CourseMate change what students see"* — is structural, not
   aspirational. The only write in Feature B is `record_attempt`, and it lives on
   the XBlock, platform-side, deliberately off the tool surface. Adding a write
   tool ends the claim on the day it is added.

   **It had no caller until 2026-08-12**, which made the claim true for the wrong
   reason: the practice card showed a question and offered no way to answer it,
   so `StudentMastery` was read by four components and written by none. The loop
   is now closed through the student's own self-assessment — still platform-side,
   still off the tool surface, so the invariant holds for the right reason. If
   you are checking that "the only write is `record_attempt`", also check that
   something calls it.
9. **Identity is injected, never accepted.** The tool registry refuses
   model-supplied `offering_id`/`student_id` rather than overriding them —
   overriding hides the attempt. No tool schema declares an identity field, so a
   cross-offering request cannot be expressed, only refused.
10. **One confidence gate, one implementation** (`ai/gate.py`). Chat and every
    retrieval tool call it. A second copy would compare against the wrong scale —
    the gate reads the *blended* rerank score, not raw coverage — and both paths
    would look correct in isolation.

The six import-linter contracts in `.importlinter` enforce the structural half of
this and do fail when violated. Contracts 3 and 4 were widened on 2026-08-10 to
cover `coursemate_service.agents` and `coursemate_service.mcp`; verified by
deliberate violation.

## The agent layer (2026-08-10)

Built, tested offline, **shipping dark**. `agent_enabled` defaults to `False` and
is read at the API layer, so a default install routes `/examprep/plan` to the
deterministic path in `api/plan.py` and no agent code is even imported.

What a fresh session most needs to know:

- **`api/plan.py` is not a stub.** It is the exam-prep feature with the flag off,
  and it has to keep working — a kill switch that routes to something broken is a
  switch nobody dares use. It is also the baseline the agent must beat.
- **Mastery is platform-owned and browser-carried**, the same courier §3.1
  settled for chat history. It is **not in Redis**: this deployment's Redis is
  `maxmemory-policy allkeys-lru`, which evicts keys with no TTL, AOF persists the
  eviction, and nothing logs it. Measured, not assumed.
- **Nothing has run against a real model.** Every agent test drives the real loop
  against a scripted router. `make agent-eval` measures the four regression gates
  and reports tool-selection accuracy as NOT MEASURED. Do not report a number
  from a stub. See LIMITATIONS §5.2.

- **The AGENT is what ships dark — Feature B does not.** As of 2026-08-12 Feature
  B is deployed and browser-verified end to end: real PDF → extractor → CLO tagger
  → `/packs/load` → study plan and generated practice question, seen by an
  enrolled student. `agent_enabled` is still `False`, and that flag governs only
  `/examprep/plan`'s prose path. Do not describe Feature B as unbuilt.

- **`--live` has been tried and it is still NOT MEASURED.** On 2026-08-12 the
  local `qwen2.5:7b` timed out on nine of ten planning calls and printed `0.44`.
  That figure measures timeouts, not tool choice, and must not be quoted. This
  model cannot drive the loop; measuring it needs a hosted provider.
