# CourseMate — working notes

An AI tutor for Open edX. The repo documents *what* it is; this file covers what
a fresh session cannot infer: how to run things here, and where the traps are.

**Read `docs/LIMITATIONS.md` before claiming anything works.** It is deliberately
harsh and kept current. `git log` is the narrative record — commit messages carry
the reasoning, not just the diff.

## STATE — read this first, then verify it

Last updated 2026-08-20. **Treat every line as stale until checked** — the
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
> restart** — it is not persistent. If generation starts returning `unavailable`,
> check this forwarder before anything else.
>
> **The variable pointing at it is `OLLAMA_API_BASE`, NOT
> `COURSEMATE_MODEL_API_BASE` (corrected 2026-08-14).** This note said the latter
> and it was wrong, in the direction that breaks things. Verified against the
> running container:
>
>     COURSEMATE_MODEL_API_BASE=            <-- empty
>     OLLAMA_API_BASE=http://172.18.0.1:11435
>
> The line was true while both tiers were Ollama and stopped being true when
> ADR-0001 made `strong` hosted. Acting on it — setting `MODEL_API_BASE` to the
> forwarder — used to point the HOSTED primary at Ollama too, because `strong`
> and `cheap` shared one base URL. They no longer do: `cheap` has
> `COURSEMATE_CHEAP_API_BASE` / `COURSEMATE_CHEAP_API_KEY` of its own, falling
> back to the `MODEL_*` pair when unset. So the trap is closed at both ends, and
> for a local `cheap` tier beside a hosted `strong` one, `CHEAP_API_BASE` is now
> the variable to reach for.
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
> started it. **And check its parent, not just that it is listening** — on
> 2026-08-13 the forwarder was serving happily with `parent=540`, i.e. still
> attached to a shell despite having been started under `setsid`:
>
>     ps -o ppid= -p "$(pgrep -f 'TCP-LISTEN:11435')"   # want 1
>
> Listening now is not the same as surviving the next shell exit, and the failure
> arrives later and elsewhere as `APIConnectionError` on 172.18.0.1:11435.

| What | State | Check |
|---|---|---|
| Everything through the sweep | Done, verified live | `git log --oneline` |
| Plugin migrations | **0001–0004 applied**, incl. 0003 (mastery) + 0004 (difficulty_band), against the live DB with real data | `tools/ops/migrate.sh` |
| Service image | **`b85322f06759`, rebuilt 2026-08-20 from `8f85daf`** — carries the generator's source-candidate fallback, the semantic duplicate check (live) and seed rotation — **no longer dormant: `tutor.js` sends the mastery snapshot as of `2c948cd`**. Previously `ef8b08430de6` (2026-08-19, the fallback alone) and `8fb7f3fd` (2026-08-13): planner, tagger, generator, WAL, B1/B2 retrieval, the C1 ceiling, the C2 cache, plus the audit work: contract-version guard, real readiness, metrics. 18 API routes live | `docker exec tutor_local-coursemate-1 python -c "import urllib.request,json;print(len(json.load(urllib.request.urlopen('http://127.0.0.1:8000/openapi.json'))['paths']))"` |
| openedx image | **`ea5ae2e2fb06`, rebuilt 2026-08-20 from `2c948cd`**, all 5 containers adopted it (lms, cms, lms-worker, cms-worker, coursemate-beat — each checked individually, not lms alone). Adds the practice request's `mastery` snapshot, which is what finally activates the service-side seed rotation. Also carries the citation-chip URL handling, the mastery-badge repaint and the study-plan marks UI. Previously `30fb683978d8` (2026-08-19, `7afc011`) and `834436d9` (2026-08-13), which carried the study-plan UI, the D1 mark replay, the D2 self-assessment UI and the platform half of the contract lock | `tools/ops/adopt_new_image.sh` |
| Conversational retrieval (B1/B2) | **LIVE and browser-verified.** multi-turn r@3 0.333 → 0.917 | BENCHMARKS §3.8 |
| Daily spend ceiling (C1) | **LIVE.** 100k tokens/student/course/UTC day. Provider reports no usage here, so it charges an estimate | BENCHMARKS §3.9, LIMITATIONS §4.1 |
| First-turn response cache (C2) | **LIVE, and effectively inert.** The mechanism is browser-verified (74,973 ms → 133 ms, 0 charged) but `student_id` is in the key, so a hit needs the same student to re-ask as a *first* turn — and their history persists. Live counters after real traffic: **hits 0, misses 1** | `curl -H "Authorization: Bearer $CRED" .../coursemate/metrics` |
| `tutor.js` notices | **DEPLOYED 2026-08-13.** `unauthenticated` and `budget_exceeded` now render. Still missing: `disabled` has no entry, and the exam-prep status path falls back to `""` (silent) | `grep -c "unauthenticated:" .../static/js/src/tutor.js` |
| Contract version lock | **LIVE both directions.** Platform stamps `X-CourseMate-Contract-Version` and learns the peer's from `/health` on first contact; service refuses a mismatch with `CONTRACT_MISMATCH`/409. A **missing** header is allowed by design so rollout order stays free | `tools/verification/auth_probe.sh`, BENCHMARKS n/a — see `contracts/version.py` |
| `/health/ready` | **Real check now.** Returns 503 when the index cannot be opened; Redis reported, never gating. An empty index is still *ready* (that is `preparing`) | `curl .../coursemate/health/ready` |
| Metrics | **LIVE**, service-credential only, absent from the published spec. Six counters; verified moving under real traffic (`chat_requests_total` 0→1) | `curl -H "Authorization: Bearer $CRED" .../coursemate/metrics` |
| Feature B end to end | **VERIFIED IN A REAL BROWSER** — tab, 100-mark plan, generated question, abstention, as enrolled `cm_student` | BENCHMARKS §3.7 |
| OEX101 exam pack | **Loaded live** — 5 questions, 3 CLOs, 35 marks, **all 5 tagged**. CLO-3's text corrected 2026-08-19, and since 2026-08-20 **all three outcomes carry `confirmed_by: null`** — no instructor confirmed any of them, and §7.3 shows the student that. `/examprep/status` returns `confirmed: false` for all three | `/examprep/status` |
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

**Done 2026-08-13:** the D1/D2 work and the nine-item audit. Both images rebuilt
from `d0fb288`, adopted, and verified live; all 25 commits pushed. What the audit
changed is mostly *things that claimed to be true and were not* — a version lock
nothing called, a readiness probe that could not fail, a tie-break setting that
stated the opposite of the code, and three settings enforcing nothing.

**The recurring failure in this codebase is reachability, not correctness.**
B1/B2, C2 and the practice loop were each correct in isolation and unwired in
production; the audit found four more of the same shape. Before believing a
control works, check that something calls it. `test_error_contract.py` now
enforces exactly that for error codes, and it has caught two of my own changes.

The one thing left unhealthy is still the host, not CourseMate — see the warning
above.

**Done 2026-08-19:** three changes, deployed and verified server-side. Both
images rebuilt and adopted; exam data was **not** reloaded or mutated by either
deployment, and the rollback tags for both are preserved
(`coursemate/service:0.1.0-prev4` → `ef8b08430de6`,
`overhangio/openedx:21.0.8-indigo-prev5` → `30fb683978d8`).

1. **The generator no longer abstains on one weak seed** (`dc15689`, service
   image). `_find_source` returns up to ten candidates and the gate scores the
   *seed question's own text*, so an outcome could be refused because its first
   candidate was thin while usable ones sat unread. `stream` now gates candidates
   in order and takes the first that passes. Verified live: CLO-1 generated from
   a later candidate where it previously abstained; CLO-2 unchanged; **CLO-3 still
   abstains immediately** because it has zero candidates — that path is untouched.
   Behaviour is a strict superset: a passing first candidate short-circuits as
   before, and when nothing passes the first candidate's error code is what the
   student sees, so `PREPARING` and `ABSTAINED` stay distinct.
2. **A source chip is a link only when it has somewhere to go** (`7afc011`,
   openedx image). Papers deliberately carry no `url`, and every chip was still
   rendered as an `<a>`; `safeHref(undefined)` is `"#"`, which scrolls the unit
   page to the top and pushes a history entry. Chips without a usable URL now
   render as an inert `<span>`; lesson citations with real URLs are unchanged.
   **Browser-verified 2026-08-20** — see below.
3. **OEX101 CLO-3 was mis-specified, and is corrected.** It named Tutor
   configuration and troubleshooting, which this course does not teach — it points
   the reader at a different course for that. The corrected outcome is carried in
   `tools/packs/oex101_final_2024.pack.json`, which is now the reviewed source of
   truth for the offering's exam data. `confirmed_by` is **null**: the text was
   derived from the course material, not confirmed by an instructor, and §7.3
   shows the student that difference.

**Done 2026-08-20: the three UI fixes are browser-verified.** Real Chrome
against the deployed image `30fb683978d8`, as enrolled `cm_student`, with real
clicks — not harness doubles, and not inferred from unit tests. This closes a gap
that three documents had been carrying since 2026-08-19.

* **Citation chips.** The paper chip is `tagName: SPAN`, `hasAttribute("href")`
  false; the lesson chip is `A` with a real `jump_to` href. Clicked the paper
  chip from `scrollY 626`: scroll unchanged, no `#` appended, `history.length`
  unchanged at 3 — the exact defect, gone. Clicked the lesson chip: landed on
  vertical `48708246…`, and the modulestore confirms that vertical ("Named
  Releases") is the parent of the cited block. Both chips keep `cm-chip-link`,
  so the span is styled identically.
* **Mastery badge.** `5/12 self-marked` → `6/13` after one self-check, with a
  `window` sentinel surviving unchanged, one navigation entry, and `scrollY`
  static — so the repaint happened **without a reload**. CLO-2's badge stayed
  `9/19`, which also proves the `data-clo` exact-match fix.
* **Study-plan shortfall.** A 70-mark request rendered
  `Study plan — 35 of 70 marks` in `.cm-plan-heading`, with
  `35 marks could not be filled …` in `.cm-plan-unspent`. Read from the DOM, not
  from the API response.

**Two of my own checks were wrong before the fixes were.** A `sha1sum` of an
unexpanded variable returned `da39a3ee5e6b` — the hash of empty input — and read
as "feature absent". And looking for a child block id inside an iframe `src`
returned false, because that URL names the *vertical*, never its children. Both
looked like failures and were bad instruments. Check what the number is made of
before believing it.

**Known and still open** (evidence in the 2026-08-13 review):

* ~~**No cross-vendor failover.**~~ **Resolved 2026-08-14.** The live chain is
  `strong` → `openrouter/meta-llama/llama-3.3-70b-instruct` (hosted),
  `cheap` → `ollama_chat/qwen2.5:7b` (local floor). `DEGRADED` and the fallback
  chain have now fired against a real outage — `failover_probe.sh` disabled the
  hosted provider and the local model answered with citations intact. What is
  still missing is narrower: `fallback_model` remains empty, so there is **no
  second HOSTED vendor**, and practice generation therefore has no fallback at
  all (deliberate — ADR-0001). Evidence: BENCHMARKS §3.11.
* **`provider_failures_total` cannot see a silent degradation.** It increments
  only when an exception reaches `pipeline.py`, and the Router swallows the
  failure whenever a fallback succeeds — so the degraded step moved it by 0 and
  only a total outage moved it by 1. A primary degrading every request is
  invisible in metrics. Needs a separate `degraded_answers_total`; not added,
  because it is a behaviour change. BENCHMARKS §4.6, LIMITATIONS §2.
* **The C2 cache is inert**, not broken — see the table row above.
* **Coverage holes where it matters least comfortably**: `api/ingest.py` 36%,
  `boundary/authz.py` 71%, `api/invalidation.py` 67% — the write path and the
  authorization path.
* **`question` and `Turn.content` have no `max_length`**, so the C1 overshoot is
  not contract-bounded.
* **The Groq key is still unrotated.** Not present in the repo, history, tutor
  config or the running container — nothing to scrub, but disclosure is
  compromise. Revoke it at the provider.

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

## Keeping the WSL distro alive (2026-08-19)

**The distro terminates during unattended gaps and takes all 13 containers with
it.** The VM staying up is a different thing: `vmIdleTimeout=-1` in `.wslconfig`
governs the VM, not the distro. The symptom is every Tutor container restarting
mid-work, which reads as a Docker fault and is not one.

**The rule that governs it — learned the hard way, do not re-derive:**

> WSL decides the distro's lifetime by whether a **WSL session** is active, NOT
> by whether processes exist inside the distro.

A `systemd` unit running `sleep infinity` was built, enabled and proven active —
and the distro **still died**, with systemd, docker, containerd and all 13
containers running. Measured: `ps -o etime= -p 1` went `00:18` then `00:13`
across two consecutive commands, i.e. the age *decreased* — the distro was dying
after each `wsl.exe` invocation and rebooting on the next. A systemd service is
owned by PID 1 and lives outside any WSL session, so WSL does not count it.
**Do not try the systemd route again; it cannot work.**

**A holder's parent must be `/init`, and that is the point.** `/init` is WSL's
session shim. A keepalive whose parent is `/init` is inside a live WSL session,
which is exactly what holds the distro. This was once mistaken for a weakness
("it should be parented to PID 1") — that reading is backwards, and acting on it
removed the protection.

**What is deployed now.** A hidden logon script holding a real session:

    %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\CourseMate-WSL-Start.vbs
      -> sh.Run "wsl.exe -d Ubuntu-24.04 -e /usr/bin/sleep infinity", 0, False

`0` hides the window, `False` means do not wait, so the script exits and leaves
`wsl.exe` resident. Verified by running four separate `wsl.exe` invocations and
watching PID 1's age climb monotonically while the same held pid persisted.

**⚠ It is scoped to the interactive Windows logon.** It survives closing
terminals and overnight idling *while logged in*. It does **not** survive Windows
logoff, reboot, shutdown, or fast-user-switch. Closing that needs an elevated
scheduled task ("run whether user is logged on or not") or a Windows service;
both `Register-ScheduledTask` and `schtasks /Create` returned **Access is
denied** without administrator rights.

The blast radius of that gap is small: `docker.service` is `enabled` and all 13
containers are `unless-stopped`, so a logon restores the stack on its own in
~2–4 minutes. Nothing needs rebuilding.

**To restore protection by hand** (e.g. after a distro cycle, from inside WSL):

    setsid bash -c 'exec -a cm-keepalive-long sleep infinity' </dev/null >/dev/null 2>&1 &

Never a finite duration — an earlier `sleep 86400` silently expired after a day
and nothing restarted it. `setsid` is load-bearing; verify it outlived the shell
with a **separate** `wsl` call, and confirm the distro is actually held by
checking that PID 1's age *increases* across invocations. A process merely
existing is not evidence.

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

    make check        # 6 contracts + OpenAPI drift check (18 paths)
                      # + 1283 backend passed, 3 xfailed
                      # + 299 browser passed across 9 suites
                      # counts as of 2026-08-20; the target prints them
    make coverage     # gated at 80% for service+contracts (now 90.9%); platform ungated
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
- **The offline suite runs against a scripted router, and says so.** Every agent
  test in plain `make agent-eval` drives the real loop against a stub, so that
  target still reports tool-selection accuracy as NOT MEASURED. **Do not report a
  number from a stub** — that rule has not changed. The metric itself *has* since
  been measured against a real model; see the `--live` entry below.
  See LIMITATIONS §5.2.

- **The AGENT is what ships dark — Feature B does not.** As of 2026-08-12 Feature
  B is deployed and browser-verified end to end: real PDF → extractor → CLO tagger
  → `/packs/load` → study plan and generated practice question, seen by an
  enrolled student. `agent_enabled` is still `False`, and that flag governs only
  `/examprep/plan`'s prose path. Do not describe Feature B as unbuilt.

- **`--live` tool-selection accuracy is `0.78`, measured 2026-08-19.** Run inside
  the service container against the hosted `strong` deployment
  (`openrouter/meta-llama/llama-3.3-70b-instruct`), with all 10 regression gates
  passing. Reproduced twice. It is nine scored cases on one gold set against one
  model — **a measurement, not a rate.**

  **The earlier `0.44` was never a result and must still not be quoted.** On
  2026-08-12 the local `qwen2.5:7b` timed out on nine of ten planning calls and
  printed it; that figure measured timeouts, not tool choice. That note also said
  measuring it "needs a hosted provider" — ADR-0001 supplied one on 2026-08-14,
  and nobody re-ran the measurement until 2026-08-19. **The blocker was removed
  five days before anyone noticed**, which is the more useful lesson than either
  number.

  **Why `0.78` is believed where `0.44` is not:** the run was timed against the
  timeout threshold before the figure was read. `model_timeout_seconds` is 300,
  so nine timing-out calls would take ~45 minutes; the whole run took **99
  seconds**, so every call completed. Timeouts would also have *lowered* the
  score, not inflated it — an empty tool list cannot match the gold's first tool.
