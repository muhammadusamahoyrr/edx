# Installation and Deployment

*Everything here was executed against a real Ulmo instance. Where a step has a
known failure mode, it is documented with the symptom, because most of these cost
hours to diagnose the first time.*

---

## 1. Requirements

| | Minimum | Notes |
|---|---|---|
| Open edX | **Ulmo** (Tutor v21) | `OPENEDX_COMMON_VERSION: release/ulmo.3` |
| RAM | 8 GB free | Migrations peaked at 4.6 GB of 9.9 GB |
| Disk | 25 GB | Images ~8 GB, plus volumes |
| Model | Any LiteLLM provider, or local Ollama | |

**On Windows:** Tutor is not supported natively — it needs WSL2 with a real Linux
distro. Docker Desktop's internal `docker-desktop` distro will not do.

---

## 2. Install Open edX

```bash
pip install "tutor[full]"
tutor local launch          # ~30-60 min: pulls ~8 GB, runs migrations
tutor local do createuser --staff --superuser admin admin@example.com
tutor local do importdemocourse       # optional, for testing
```

> **The demo course key is `course-v1:OpenedX+DemoX+DemoCourse`**, not
> `course-v1:edX+DemoX+Demo_Course` as nearly all Open edX documentation states.
> Verified by enumerating courses on a running instance.

---

## 3. Install CourseMate

### 3.1 Plugin

```bash
cp deploy/tutor-plugin/coursemate.yml "$(tutor plugins printroot)/"
tutor plugins enable coursemate
tutor config save
```

This adds the service container, the `/coursemate/*` Caddy route, and the JWT
signing key. Two secrets are generated automatically.

> **If `tutor config save` fails with "Missing configuration value"** — the
> plugin's `config.unique` keys are created *during* save, and a template
> referencing one aborts before they exist. Break the cycle:
> `tutor config save --set COURSEMATE_JWT_SIGNING_KEY=$(openssl rand -hex 32)`

> **Run Tutor as your normal user, never `sudo`.** As root, Tutor uses
> `/root/.local/share/tutor` — config changes then appear not to apply, with no
> error.

### 3.2 Service image

Two stages, split because dependencies and source change at very different rates:

```bash
docker build -f deploy/Dockerfile.deps    -t coursemate/deps:1       .   # rare
docker build -f deploy/Dockerfile.service -t coursemate/service:0.1.0 .   # every change
```

The second is **offline and takes seconds**. Building them as one image meant
every one-line change re-downloaded ~200 MB of wheels — 10–15 minutes on a slow
link, and frequently failing outright.

### 3.3 Platform package

**Production: install it into the image.** The plugin's
`openedx-dockerfile-post-python-requirements` patch does this, and it is not
optional — it is what makes the asynchronous half of the system work at all:

```bash
rsync -a packages "$(tutor config printroot)/env/build/openedx/coursemate/"
tutor config save && tutor images build openedx
tutor local start -d
```

> **Why a file copy is not enough.** `docker cp` puts the code on `sys.path` but
> installs no dist-info, so pip's `cms.djangoapp` / `lms.djangoapp` entry points
> are absent — and those entry points are how Open edX discovers a plugin app.
> Without them the app never reaches `INSTALLED_APPS`, Django never loads it,
> Celery never autodiscovers `coursemate_platform.tasks`, and **every enqueued
> task is discarded** with `Received unregistered task`. Studio's Publish button
> still returns 200. The only symptom is in the worker log.

Then apply the plugin's migrations — `CourseIndexState` and `FailedIngestion`
live in MySQL:

```bash
tutor local run cms ./manage.py cms migrate coursemate_platform
```

For development iteration only, install into the running containers:

```bash
for C in lms cms; do
  docker cp packages/coursemate-contracts "tutor_local-$C-1:/tmp/c"
  docker cp packages/coursemate-platform  "tutor_local-$C-1:/tmp/p"
  docker exec -u root "tutor_local-$C-1" pip install --no-deps -q /tmp/c /tmp/p
done
docker restart tutor_local-lms-1 tutor_local-cms-1
```

> **The restart is required.** Python entry points load at process start, so
> running workers will not see a newly installed XBlock. Use `docker restart`,
> **not** `docker compose up --force-recreate` — the latter discards the
> container layer and your install with it.

> **`docker cp` into an existing directory nests it.** `docker cp pkg c:/tmp/p`
> when `/tmp/p` exists produces `/tmp/p/p`, and pip silently reinstalls the stale
> copy. Remove the target first.

This is a dev loop, not a deployment: it cannot help any container that starts
later, including `coursemate-beat`. Install into the image for anything real.

---

## 4. Configure a model

**Hosted provider:**

```bash
tutor config save \
  --set COURSEMATE_STRONG_MODEL="anthropic/claude-opus-5" \
  --set COURSEMATE_MODEL_API_KEY="sk-..." \
  --set COURSEMATE_FALLBACK_MODEL="openai/gpt-4o"
```

> Set `FALLBACK_MODEL` to a **different vendor**. A second model from the same
> vendor does not survive that vendor's outage.

**Local Ollama:**

```bash
tutor config save \
  --set COURSEMATE_STRONG_MODEL="ollama_chat/qwen2.5:7b" \
  --set COURSEMATE_OLLAMA_API_BASE="http://172.18.0.1:11434" \
  --set COURSEMATE_MODEL_TIMEOUT_SECONDS=300
```

> Ollama binds `127.0.0.1`, which containers cannot reach. Bridge it without
> reconfiguring Ollama:
> ```
> socat TCP-LISTEN:11434,bind=0.0.0.0,fork,reuseaddr TCP:127.0.0.1:11434
> ```
> A systemd unit for this is in the repository history.
>
> **Expect 20–50 s to first token on CPU** against a 2 s design budget. Local
> models are a development tool, not a demo path.

### Authorization

```bash
# create an OAuth2 client-credentials app owned by a staff user, then:
tutor config save \
  --set COURSEMATE_LMS_CLIENT_ID="..." \
  --set COURSEMATE_LMS_CLIENT_SECRET="..." \
  --set COURSEMATE_ENFORCE_ENROLLMENT=true
```

> The legacy `X-Edx-Api-Key` header **returns 401 on current Open edX**. OAuth2
> client credentials is the supported path.

---

## 5. Index a course

```bash
tutor local run cms ./manage.py cms coursemate_reindex \
  --course course-v1:OpenedX+DemoX+DemoCourse --inline
```

Expected: `{'leaves_found': 226, 'blocks_written': 226, 'chunks_written': 231}`.

Without this the tutor answers *"still being prepared"* for every question. There
is no event for content published before installation, and nobody re-publishes an
old course to wake up a plugin.

---

## 5.1 The reconciliation sweep

Open edX emits **no unpublish event**. Publish, delete, duplicate, import and
rerun all fire; unpublish does not. So nothing tells CourseMate when an
instructor unpublishes a unit, and without a sweep the tutor keeps answering
from — and citing — content students can no longer see.

The `coursemate-beat` container runs the sweep nightly at 03:30, and every
publish sweeps its own course. To run one by hand:

```bash
tutor local run cms ./manage.py cms coursemate_reconcile   --course course-v1:OpenedX+DemoX+DemoCourse --inline
```

```
course-v1:OpenedX+DemoX+DemoCourse: live=216 indexed=221 orphans=5 repaired=0 unrepaired=0
    removed (no longer published): block-v1:…+type@html+block@04be59e2…
```

`--all` sweeps every course CourseMate indexes (not every course on the
instance — a never-indexed course reads as "entirely missing", and a nightly job
must not silently enable the tutor for courses nobody opted into).

**The sweep refuses to remove more than half a course in one run.** A failed
course read yields zero live blocks, which is indistinguishable from a mass
unpublish; without the cap, one bad modulestore read wipes the index and logs
success. After checking the course by hand, `--force` lifts it.

---

## 6. Add the tutor to a course

1. Studio → **Settings → Advanced Settings → Advanced Module List** → add
   `coursemate_tutor`
2. Open a unit → **Add New Component → Advanced → AI Tutor**
3. **Publish**
4. Rebuild the block-structure cache, or the Learning MFE will not render it:

```bash
tutor local run lms ./manage.py lms shell -c "
from opaque_keys.edx.keys import CourseKey
from openedx.core.djangoapps.content.block_structure.api import get_block_structure_manager
m = get_block_structure_manager(CourseKey.from_string('course-v1:...'))
m.clear(); m.update_collected_if_needed()"
```

> `generate_course_blocks` alone is **not** sufficient — it reports success while
> changing nothing. The explicit `clear()` is what forces re-collection.

---

## 7. Verification

```bash
curl http://local.openedx.io/coursemate/health
# {"status":"ok","contract_version":1,...}

curl -X POST http://local.openedx.io/coursemate/api/chat \
     -H 'Content-Type: application/json' -d '{"question":"x"}'
# {"detail":"unauthenticated"}   ← correct
```

Then in a browser: open the unit, ask something the course covers (expect an
answer with citations) and something it does not (expect *"doesn't appear to be
covered"*).

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Block absent from the unit | Workers predate the install | `docker restart` LMS/CMS |
| Block absent from the MFE | Stale block-structure cache | `mgr.clear()` then re-collect |
| `Unexpected token '<' ... not valid JSON` | Missing CSRF on a handler POST | Send `X-CSRFToken` from the cookie |
| `/coursemate/*` returns the LMS 404 page | Caddy did not reload | Recreate the caddy container |
| Connection refused while everything looks healthy | Stale WSL port relay | `wsl --shutdown`, restart |
| Tutor abstains on everything | Course not indexed | Run `coursemate_reindex` |
| `no matching distribution for setuptools` | **A network read timeout**, not a version conflict | `--no-build-isolation`; raise pip timeout |
| Config changes have no effect | Tutor run as root | Run as your normal user |

---

## 9. Production notes

Not yet deployed to production. Before doing so:

- Replace the SQLite index with a shared store if running **more than one
  replica** — it is a local file, and the rate limiter is per-process
- Move the authz cache to Redis for the same reason
- Set `REQUIRE_GROUNDING=true` (default is `false` for development)
- Use a hosted provider; CPU inference cannot meet the latency budget
- Register the retirement endpoint in the tubular pipeline (§10.7)
