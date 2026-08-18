/* Browser-side test for the budgeted study-plan UI (Phase 4C).
 *
 * Same hand-rolled fake DOM as test_practice_ui.mjs, and for the same reason:
 * `tutor.js` is plain ES5 with a small DOM surface, so a fake DOM is cheaper and
 * more honest than a jsdom dependency and a package.json this repo does not
 * otherwise need.
 *
 * What this pins is the half no Python test can reach. The service returns a
 * StudyPlan; whether the browser sends the right shape to get one, and whether
 * what it renders is TRUE — the marks add up, a short bank says it is short, an
 * empty plan does not look like an error — lives only here.
 *
 * Run:  node packages/coursemate-platform/tests/js/test_study_plan_ui.mjs
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import assert from "node:assert/strict";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const JS = resolve(here, "../../coursemate_platform/xblock/static/js/src/tutor.js");

/* ---------------------------------------------------------------- fake DOM */
let nodes = [];

function makeNode(tag, cls) {
  const node = {
    tagName: tag, className: cls || "", textContent: "", hidden: false,
    disabled: false, value: "", href: "", dataset: {}, children: [],
    parentNode: null, _listeners: {},
    /* `childNodes` alias. The renderer asks "has anything been written here",
     * which in a real DOM must count text nodes — see tutor.js `closePara`.
     * This double keeps its original single-list model; only
     * test_answer_formatting.mjs separates elements from text nodes, because
     * that is the file asserting the distinction. */
    get childNodes() { return node.children; },
    appendChild(c) { c.parentNode = node; node.children.push(c); return c; },
    removeChild(c) {
      const i = node.children.indexOf(c);
      if (i < 0) { throw new Error("NotFoundError: node is not a child"); }
      node.children.splice(i, 1); c.parentNode = null;
    },
    addEventListener(ev, fn) { node._listeners[ev] = fn; },
    querySelector(sel) { return find(node, sel); },
    querySelectorAll(sel) { return findAll(node, sel); },
    setAttribute(k, v) { node._attrs = node._attrs || {}; node._attrs[k] = v; },
    /* data-* reads from dataset, like a browser. The stub returned null, so the
     * tab wiring — `tab.getAttribute("data-panel")` — could never work and
     * `loadPrepStatus` never ran in this harness. */
    getAttribute(k) {
      if (k && k.indexOf("data-") === 0) { return node.dataset[k.slice(5)] ?? null; }
      return (node._attrs || {})[k] ?? null;
    },
    classList: { toggle() {}, add() {}, remove() {} },
    matches(sel) {
      const attr = sel.match(/\[([\w-]+)="([^"]*)"\]/);
      const base = sel.replace(/\[[^\]]*\]/g, "");
      if (attr) {
        const key = attr[1].replace(/^data-/, "");
        if (node.dataset[key] !== attr[2]) { return false; }
      }
      if (!base) { return true; }
      if (base.startsWith(".")) {
        return (" " + node.className + " ").includes(" " + base.slice(1) + " ");
      }
      return node.tagName === base;
    },
    get text() {
      return [node.textContent, ...node.children.map((c) => c.text)].join(" ").trim();
    },
  };
  nodes.push(node);
  return node;
}

function walk(root, fn) { fn(root); root.children.forEach((c) => walk(c, fn)); }
function find(root, sel) { let hit = null; walk(root, (n) => { if (!hit && n !== root && n.matches(sel)) hit = n; }); return hit; }
function findAll(root, sel) { const out = []; walk(root, (n) => { if (n !== root && n.matches(sel)) out.push(n); }); return out; }

globalThis.document = {
  createElement: (t) => makeNode(t, ""),
  createTextNode: (txt) => Object.assign(makeNode("#text", ""), { textContent: txt }),
  cookie: "csrftoken=abc",
};
globalThis.window = { location: { origin: "https://lms.example" } };
globalThis.URL = URL;
globalThis.TextDecoder = TextDecoder;

/* --------------------------------------------------------------- fixtures */
function buildPage() {
  nodes = [];
  const root = makeNode("div", "coursemate-tutor");
  const mk = (cls, parent, extra = {}) => {
    const n = makeNode("div", cls);
    Object.assign(n, extra);
    (parent || root).appendChild(n);
    return n;
  };
  /* The tab strip. Absent here until 2026-08-15, which meant `loadPrepStatus`
   * never ran and the panel's own initialisation was never exercised — the same
   * shape of harness gap as the missing slots below. */
  const tabs = mk("cm-tabs", root);
  const tabChat = mk("cm-tab", tabs); tabChat.dataset.panel = "chat";
  const tabPrep = mk("cm-tab", tabs); tabPrep.dataset.panel = "prep";

  const chat = mk("cm-panel", root); chat.dataset.panel = "chat";
  mk("cm-log", chat); mk("cm-notice", chat);
  const form = mk("cm-form", chat); mk("cm-input", form); mk("cm-send", form);

  const prep = mk("cm-panel", root); prep.dataset.panel = "prep";
  mk("cm-prep-status", prep); mk("cm-prep-notice", prep);
  /* The three replacement slots, in template order. They were absent here, so
   * this harness had been exercising `slotTarget`'s legacy fallback — the
   * shared log — rather than what a real page renders. That is the shape the
   * prose-plan duplicate lived in, and a harness that cannot reproduce a bug
   * cannot pin its fix. */
  const prac = mk("cm-practice-form", prep);
  mk("cm-practice-clo", prac); mk("cm-practice-band", prac); mk("cm-practice-send", prac);
  mk("cm-practice-slot", prep);
  const bf = mk("cm-budget-form", prep);
  mk("cm-budget-input", bf); mk("cm-budget-send", bf);
  mk("cm-plan-slot", prep);
  const pf = mk("cm-prep-form", prep); mk("cm-prep-input", pf); mk("cm-prep-send", pf);
  mk("cm-prose-plan-slot", prep);
  mk("cm-prep-log", prep);
  return root;
}

/** A page rendered BEFORE the prose slot existed — `slotTarget`'s fallback. */
function buildLegacyPage() {
  const root = buildPage();
  const slot = find(root, ".cm-prose-plan-slot");
  slot.parentNode.removeChild(slot);
  return root;
}

const PLAN_TWO_CLOS = {
  offering_id: "course-v1:OpenedX+OEX101+2024",
  items: [
    {
      clo_id: "CLO-2", marks_budget: 15,
      question_ids: ["oex101_final_2024.pdf#2", "oex101_final_2024.pdf#2(b)"],
      rationale: "not practised yet; 15 of 15 marks allocated",
    },
    {
      clo_id: "CLO-1", marks_budget: 5,
      question_ids: ["oex101_final_2024.pdf#4"],
      rationale: "3/8 self-marked; 5 of 20 marks allocated (no more past-paper questions are tagged to this outcome)",
    },
  ],
};

/* Boot the page, set the budget, submit. `plan` is the JSON the fake service
 * returns; `status` overrides the /study-plan response status. */
async function drive({ plan = PLAN_TWO_CLOS, status = 200, budget = "20", mastery = null } = {}) {
  const root = buildPage();
  const calls = [];
  globalThis.fetch = async (url, opts = {}) => {
    calls.push({ url, opts });
    if (String(url).includes("/mint")) {
      return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
    }
    if (String(url).includes("/status")) {
      return { ok: true, json: async () => ({ pack_loaded: true, questions: 5, clos: 3, clo_options: [] }) };
    }
    if (String(url).includes("/study-plan")) {
      return { ok: status >= 200 && status < 300, status, json: async () => plan };
    }
    return { ok: false, status: 500 };
  };

  const src = readFileSync(JS, "utf8");
  const factory = vm.runInThisContext(`${src}
CourseMateTutor;`, { filename: JS });
  factory({ handlerUrl: (_e, name) => `/handler/${name}` },
          { querySelector: (s) => find(root, s) || root },
          mastery ? { mastery } : {});

  const prepPanel = find(root, '.cm-panel[data-panel="prep"]');
  assert.ok(prepPanel, "prep panel not found — the harness selector is wrong");
  prepPanel.dataset.base = "/coursemate/api/examprep";

  find(root, ".cm-budget-input").value = budget;
  const submit = find(root, ".cm-budget-form")._listeners.submit;
  assert.ok(submit, "budget form has no submit handler");
  await submit({ preventDefault() {} });
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  return { root, calls };
}

const post = (calls) => calls.find((c) => String(c.url).includes("/study-plan"));

/* --- the PROSE planner, which streams -------------------------------------
 *
 * `drive()` above answers `/plan` with a 500, because it exists to exercise the
 * budgeted plan. The prose plan is a different route with a different shape, so
 * it needs its own driver. */
function sseBody(frames) {
  const bytes = new TextEncoder().encode(
    frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join("")
  );
  let sent = false;
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: async () => (sent ? { done: true } : ((sent = true), { done: false, value: bytes })),
      }),
    },
  };
}

const settleAsync = async () => {
  for (let i = 0; i < 6; i++) { await new Promise((r) => setTimeout(r, 0)); }
};

/** Boot a page whose `/plan` route streams `text`. Returns helpers to re-ask. */
async function drivePlanner(root, text = "Revise CLO-1, then CLO-2.") {
  globalThis.fetch = async (url) => {
    if (String(url).includes("/mint")) {
      return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
    }
    if (String(url).includes("/status")) {
      return { ok: true, json: async () => ({ pack_loaded: true, questions: 5, clos: 3, clo_options: [] }) };
    }
    if (String(url).includes("/study-plan")) {
      return { ok: true, json: async () => PLAN_TWO_CLOS };
    }
    if (String(url).includes("/plan")) {
      return sseBody([{ type: "token", text }, { type: "done" }]);
    }
    return { ok: true, json: async () => ({}) };
  };

  const src = readFileSync(JS, "utf8");
  const factory = vm.runInThisContext(`${src}\nCourseMateTutor;`, { filename: JS });
  factory({ handlerUrl: (_e, name) => `/handler/${name}` },
          { querySelector: (s) => find(root, s) || root }, {});
  const prepPanel = find(root, '.cm-panel[data-panel="prep"]');
  prepPanel.dataset.base = "/coursemate/api/examprep";
  await settleAsync();

  return async function askForAPlan() {
    find(root, ".cm-prep-input").value = "what should I revise?";
    await find(root, ".cm-prep-form")._listeners.submit({ preventDefault() {} });
    await settleAsync();
  };
}

/** Every rendered prose-plan turn, wherever it landed. */
const proseTurns = (root) => {
  const slot = find(root, ".cm-prose-plan-slot");
  const log = find(root, ".cm-prep-log");
  return findAll(slot || log, ".cm-turn");
};


/* --- the STRUCTURED revision plan (Phase 2, 2026-08-15) -------------------
 *
 * The deterministic plan used to arrive as markdown inside text tokens and be
 * parsed back into structure by the browser. It now arrives as a `RevisionPlan`.
 *
 * The parsing was not merely redundant, it was unsound: `_Source:
 * oex101_final_2024.pdf, p.2_` cannot be told from its own italic markers,
 * because the filename contains underscores. Structure removes the collision
 * instead of working around it, which is why the underscore case below is a
 * regression test rather than a curiosity.
 *
 * `/plan` still streams when the agent is on — it genuinely narrates then.
 */

const PLAN_JSON = {
  offering_id: "course-v1:OpenedX+OEX101+2023",
  outcomes: [
    {
      clo_id: "CLO-1",
      clo_text: "Identify the organisations and roles",
      attempts: 0,
      correct: 0,
      questions: [
        {
          question_id: "q1", text: "Name two major members of the community",
          marks: 3, year: 2024, exam_type: "final",
          source_doc_id: "oex101_final_2024.pdf", page: 2,
          low_confidence_flag: false,
        },
        {
          question_id: "q2", text: "State what the community is",
          marks: 2, year: 2024, exam_type: "final",
          source_doc_id: "oex101_final_2024.pdf", page: 1,
          low_confidence_flag: true,
        },
      ],
    },
    {
      clo_id: "CLO-3", clo_text: "Configure a Tutor deployment",
      attempts: 3, correct: 2, questions: [],
    },
  ],
};

/** Boot with the agent OFF, so the client takes the structured route. */
async function drivePlannerStructured(root, plan = PLAN_JSON, status = 200) {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url: String(url), opts });
    if (String(url).includes("/mint")) {
      return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
    }
    if (String(url).includes("/status")) {
      return { ok: true, json: async () => ({
        pack_loaded: true, questions: 5, clos: 3, clo_options: [],
        agent_available: false,
      }) };
    }
    if (String(url).includes("/revision-plan")) {
      return { ok: status === 200, status, json: async () => plan };
    }
    if (String(url).includes("/plan")) {
      throw new Error("the streaming route must not be used when the agent is off");
    }
    return { ok: true, json: async () => ({}) };
  };

  const src = readFileSync(JS, "utf8");
  const factory = vm.runInThisContext(`${src}\nCourseMateTutor;`, { filename: JS });
  factory({ handlerUrl: (_e, name) => `/handler/${name}` },
          { querySelector: (s) => find(root, s) || root }, {});
  /* Select the prep tab, which is what runs `loadPrepStatus` — the function
   * that learns whether the agent is on. Driving the real path rather than
   * setting `dataset.agent` by hand: a test that stubs the wiring it is
   * checking proves nothing about the wiring. */
  const prepTab = findAll(root, ".cm-tab").find((t) => t.dataset.panel === "prep");
  await prepTab._listeners.click();
  await settleAsync();

  const prepPanel = find(root, '.cm-panel[data-panel="prep"]');
  prepPanel.dataset.base = "/coursemate/api/examprep";
  await settleAsync();

  find(root, ".cm-prep-input").value = "what should I revise?";
  await find(root, ".cm-prep-form")._listeners.submit({ preventDefault() {} });
  await settleAsync();
  return calls;
}

const planNode = (root) => {
  const slot = find(root, ".cm-prose-plan-slot") || find(root, ".cm-prep-log");
  return find(slot, ".cm-answer");
};

/* ------------------------------------------------------------------ tests */
const tests = {
  /* --- the request ------------------------------------------------- */

  async "posts to the study-plan route"() {
    const { calls } = await drive();
    const req = post(calls);
    assert.ok(req, "no POST to /study-plan");
    assert.equal(String(req.url), "/coursemate/api/examprep/study-plan");
    assert.equal(req.opts.method, "POST");
    assert.match(req.opts.headers.Authorization, /^Bearer /);
  },

  async "sends exactly the StudyPlanRequest shape"() {
    const { calls } = await drive({ budget: "20" });
    const body = JSON.parse(post(calls).opts.body);
    assert.deepEqual(Object.keys(body).sort(), ["marks_budget", "mastery"]);
    assert.equal(body.marks_budget, 20);
    assert.equal(typeof body.marks_budget, "number", "the budget must not be a string");
  },

  async "carries the mastery snapshot when the platform supplied one"() {
    const snapshot = { offering_id: "course-v1:OpenedX+OEX101+2024", clos: [] };
    const { calls } = await drive({ mastery: snapshot });
    assert.deepEqual(JSON.parse(post(calls).opts.body).mastery, snapshot);
  },

  async "sends null, not an empty object, when there is no mastery"() {
    const { calls } = await drive();
    assert.equal(JSON.parse(post(calls).opts.body).mastery, null);
  },

  /* The contract has no field for either, so the browser cannot express a
   * cross-offering request — it can only be refused. This pins that the UI does
   * not helpfully add one back. */
  async "no student or offering identity is sent"() {
    const { calls } = await drive();
    const body = JSON.parse(post(calls).opts.body);
    ["student_id", "offering_id", "user_id", "sub", "course_id", "tenant"]
      .forEach((f) => assert.ok(!(f in body), `${f} leaked into the request body`));
  },

  /* --- budget validation ------------------------------------------- */

  async "a valid budget is submitted"() {
    const { calls } = await drive({ budget: "100" });
    assert.equal(JSON.parse(post(calls).opts.body).marks_budget, 100);
  },

  async "an empty budget is refused before any request"() {
    const { root, calls } = await drive({ budget: "" });
    assert.equal(post(calls), undefined, "an empty budget reached the service");
    const notice = find(root, ".cm-prep-notice");
    assert.equal(notice.hidden, false);
    assert.match(notice.textContent, /between 1 and 500/);
  },

  async "zero, negative and non-numeric budgets are refused"() {
    for (const bad of ["0", "-5", "abc", "  "]) {
      const { calls } = await drive({ budget: bad });
      assert.equal(post(calls), undefined, `budget ${JSON.stringify(bad)} was sent`);
    }
  },

  async "a budget over the contract ceiling is refused"() {
    const { calls } = await drive({ budget: "501" });
    assert.equal(post(calls), undefined, "an out-of-range budget reached the service");
  },

  async "the boundary values are accepted"() {
    for (const ok of ["1", "500"]) {
      const { calls } = await drive({ budget: ok });
      assert.ok(post(calls), `budget ${ok} should be allowed`);
    }
  },

  /* --- rendering ---------------------------------------------------- */

  async "renders a plan with one card"() {
    const { root } = await drive();
    assert.ok(find(root, ".cm-plan-card"), "no plan card rendered");
  },

  async "renders every CLO item"() {
    const { root } = await drive();
    const items = findAll(find(root, ".cm-plan-card"), ".cm-plan-item");
    assert.equal(items.length, 2, "expected one item per outcome");
    assert.match(items[0].text, /CLO-2/);
    assert.match(items[1].text, /CLO-1/);
  },

  async "renders allocated marks per outcome"() {
    const { root } = await drive();
    const clos = findAll(find(root, ".cm-plan-card"), ".cm-plan-clo");
    assert.match(clos[0].textContent, /CLO-2 · 15 marks/);
    assert.match(clos[1].textContent, /CLO-1 · 5 marks/);
  },

  async "renders the question ids"() {
    const { root } = await drive();
    const qs = findAll(find(root, ".cm-plan-card"), ".cm-plan-questions");
    assert.match(qs[0].textContent, /oex101_final_2024\.pdf#2/);
    assert.match(qs[0].textContent, /oex101_final_2024\.pdf#2\(b\)/);
    assert.match(qs[1].textContent, /oex101_final_2024\.pdf#4/);
  },

  /* The rationale opens with the same mastery clause the badge now shows, so
   * the clause is lifted into the badge and the remainder stays as the
   * sentence. Both halves are still on screen — this asserts the information
   * survived the split, not just that something rendered. */
  async "renders the rationale, with mastery lifted into a badge"() {
    const { root } = await drive();
    const card = find(root, ".cm-plan-card");
    assert.match(find(card, ".cm-plan-mastery").textContent, /not practised yet/);
    assert.match(card.text, /15 of 15 marks allocated/);
    assert.match(card.text, /no more past-paper questions are tagged to this outcome/);
    // Said once, not twice.
    assert.equal((card.text.match(/not practised yet/g) || []).length, 2,
      "one badge per outcome, and no leftover copy in the rationale");
  },

  /* C1: the counter is a self-report, and the wording now says so. */
  async "the mastery badge says self-marked, never correct"() {
    const { root } = await drive({
      mastery: { clos: [
        { clo_id: "CLO-1", difficulty_band: null, attempts: 4, correct: 2 },
      ] },
    });
    const badge = findAll(root, ".cm-plan-mastery")
      .find((b) => /\d+\/\d+/.test(b.textContent));
    assert.match(badge.textContent, /2\/4 self-marked/);
    assert.doesNotMatch(badge.textContent, /correct/,
      "a self-report is being shown as a graded result");
  },

  /* A plan rendered from a turn persisted BEFORE the wording changed still has
   * its clause lifted, rather than printed twice beside the badge. */
  async "a legacy rationale saying correct is still lifted into the badge"() {
    const legacy = {
      items: [{ clo_id: "CLO-1", marks_budget: 5, question_ids: ["p#1"],
                rationale: "2/4 correct; 5 of 20 marks allocated" }],
    };
    const { root } = await drive({ plan: legacy });
    const card = find(root, ".cm-plan-card");
    assert.doesNotMatch(card.text, /2\/4 correct;/,
      "the legacy clause was printed instead of being lifted");
    assert.match(card.text, /5 of 20 marks allocated/);
  },

  async "reports the total planned marks against what was asked for"() {
    const { root } = await drive({ budget: "20" });
    assert.match(find(root, ".cm-plan-heading").textContent, /20 of 20 marks/);
  },

  /* The service's PlanReport is deliberately not in the StudyPlan contract, so
   * the shortfall is derived from what IS: requested minus the item total.
   * Padding the plan, or saying nothing, would both misrepresent a short bank. */
  async "states unspent budget honestly"() {
    const { root } = await drive({ budget: "100" });
    const card = find(root, ".cm-plan-card");
    assert.match(find(card, ".cm-plan-heading").textContent, /20 of 100 marks/);
    const unspent = find(card, ".cm-plan-unspent");
    assert.ok(unspent, "a short plan did not say it was short");
    assert.match(unspent.textContent, /80 marks could not be filled/);
  },

  async "a fully spent budget claims no shortfall"() {
    const { root } = await drive({ budget: "20" });
    assert.equal(find(find(root, ".cm-plan-card"), ".cm-plan-unspent"), null);
  },

  async "says nothing in the plan is AI-generated"() {
    const { root } = await drive();
    const card = find(root, ".cm-plan-card");
    assert.match(find(card, ".cm-plan-footnote").textContent, /Nothing here is AI-generated/);
    assert.equal(find(card, ".cm-ai-badge"), null,
      "a past-paper plan must not carry the generated-content badge");
  },

  /* --- empty is not an error --------------------------------------- */

  async "an empty plan renders as an answer, not a fault"() {
    const { root } = await drive({ plan: { offering_id: "x", items: [] } });
    const card = find(root, ".cm-plan-card");
    assert.ok(card, "an empty plan rendered nothing at all");
    assert.ok(find(card, ".cm-plan-empty"), "no explanation for the empty plan");
    assert.match(find(card, ".cm-plan-empty").textContent, /nothing to plan from/);
    assert.equal(find(root, ".cm-prep-notice").hidden, true,
      "an empty plan must not raise the error notice");
  },

  async "an empty plan still reports the budget it was asked for"() {
    const { root } = await drive({ plan: { offering_id: "x", items: [] }, budget: "60" });
    assert.match(find(root, ".cm-plan-heading").textContent, /0 of 60 marks/);
  },

  /* --- errors are faults, and render as such ----------------------- */

  async "a server error renders the unavailable notice"() {
    const { root } = await drive({ status: 500 });
    const notice = find(root, ".cm-prep-notice");
    assert.equal(notice.hidden, false);
    assert.match(notice.textContent, /unavailable/i);
    assert.equal(find(root, ".cm-plan-card"), null, "an error rendered a plan card");
  },

  async "a 429 says to wait rather than that the tutor is broken"() {
    const { root } = await drive({ status: 429 });
    assert.match(find(root, ".cm-prep-notice").textContent, /Too many/i);
  },

  async "a 403 says access, not outage"() {
    const { root } = await drive({ status: 403 });
    assert.match(find(root, ".cm-prep-notice").textContent, /don't have access/i);
  },

  async "a network failure is reported, not swallowed"() {
    const root = buildPage();
    globalThis.fetch = async (url) => {
      if (String(url).includes("/mint")) {
        return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
      }
      throw new Error("network down");
    };
    const src = readFileSync(JS, "utf8");
    const factory = vm.runInThisContext(`${src}
CourseMateTutor;`, { filename: JS });
    factory({ handlerUrl: (_e, n) => `/handler/${n}` },
            { querySelector: (s) => find(root, s) || root }, {});
    find(root, '.cm-panel[data-panel="prep"]').dataset.base = "/coursemate/api/examprep";
    find(root, ".cm-budget-input").value = "20";
    await find(root, ".cm-budget-form")._listeners.submit({ preventDefault() {} });
    await new Promise((r) => setTimeout(r, 0));

    assert.equal(find(root, ".cm-prep-notice").hidden, false);
  },

  /* --- controls come back ------------------------------------------ */

  async "controls are re-enabled after success"() {
    const { root } = await drive();
    assert.equal(find(root, ".cm-budget-input").disabled, false);
    assert.equal(find(root, ".cm-budget-send").disabled, false);
  },

  async "controls are re-enabled after an error"() {
    const { root } = await drive({ status: 500 });
    assert.equal(find(root, ".cm-budget-input").disabled, false,
      "the student cannot retry — the form stayed disabled");
    assert.equal(find(root, ".cm-budget-send").disabled, false);
  },

  async "a second plan can be requested after the first"() {
    const { root, calls } = await drive();
    find(root, ".cm-budget-input").value = "50";
    await find(root, ".cm-budget-form")._listeners.submit({ preventDefault() {} });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    const posts = calls.filter((c) => String(c.url).includes("/study-plan"));
    assert.equal(posts.length, 2);
    assert.equal(JSON.parse(posts[1].opts.body).marks_budget, 50);
  },

  /* --- injection ---------------------------------------------------- */

  /* Plan text is built from extracted PDF content, which §10.6 treats as
   * semi-trusted: a paper containing markup must render as characters. */
  async "plan content cannot inject html"() {
    const nasty = "<img src=x onerror=alert(1)>";
    const { root } = await drive({
      plan: {
        offering_id: "x",
        items: [{
          clo_id: nasty, marks_budget: 5,
          question_ids: ["<script>alert(1)</script>"],
          rationale: "<b>bold</b> and " + nasty,
        }],
      },
    });
    const card = find(root, ".cm-plan-card");
    /* Everything landed as text on a node, and no element was created from it. */
    assert.match(find(card, ".cm-plan-clo").textContent, /<img src=x onerror=alert\(1\)>/);
    assert.match(find(card, ".cm-plan-rationale").textContent, /<b>bold<\/b>/);
    ["img", "script", "b"].forEach((tag) => {
      assert.equal(find(card, tag), null, `a <${tag}> element was created from plan text`);
    });
  },

  async "the renderer never uses innerHTML"() {
    /* Source-level, because a single innerHTML added later would undo every
     * assertion above and nothing else would notice.
     *
     * Matches an ASSIGNMENT, not the word: the first version of this test
     * matched `/innerHTML/` and failed on the comment two lines above the
     * renderer explaining why innerHTML is not used. A guard that fires on its
     * own documentation is a guard someone deletes. */
    const src = readFileSync(JS, "utf8");
    assert.equal(/\.innerHTML\s*=/.test(src), false,
      "tutor.js now assigns innerHTML somewhere");
    assert.equal(/insertAdjacentHTML|outerHTML/.test(src), false,
      "tutor.js now writes markup by another route");
  },

  /* --- the neighbouring UI is untouched ---------------------------- */

  async "the prose plan form still posts to /plan"() {
    const { root, calls } = await drive();
    find(root, ".cm-prep-input").value = "help me revise";
    await find(root, ".cm-prep-form")._listeners.submit({ preventDefault() {} });
    await new Promise((r) => setTimeout(r, 0));
    const prose = calls.find((c) => /\/examprep\/plan$/.test(String(c.url)));
    assert.ok(prose, "the free-text plan route is no longer called");
    assert.deepEqual(Object.keys(JSON.parse(prose.opts.body)).sort(), ["mastery", "request"]);
  },

  /* --- the prose plan replaces rather than stacks ------------------------
   *
   * The regression. `requestPlan` appended straight into `cm-prep-log`, which
   * is never cleared, so a second request left the first plan on screen beneath
   * it — reading as one response rendered twice. Every other output in this
   * panel already went through `slotTarget`, which clears first.
   */

  async "one plan request renders exactly one prose plan"() {
    const root = buildPage();
    const ask = await drivePlanner(root);
    await ask();
    assert.equal(proseTurns(root).length, 1);
  },

  async "two consecutive plan requests leave exactly ONE prose plan"() {
    const root = buildPage();
    const ask = await drivePlanner(root);
    await ask();
    await ask();

    const turns = proseTurns(root);
    assert.equal(turns.length, 1,
      `${turns.length} prose plans on screen — the second request stacked ` +
      `beneath the first instead of replacing it`);
  },

  async "the surviving plan is the NEWEST one, not the first"() {
    // Replacing with the stale copy would be the same bug wearing a
    // different symptom, and node-counting alone cannot tell them apart.
    const root = buildPage();
    let ask = await drivePlanner(root, "FIRST plan text.");
    await ask();
    ask = await drivePlanner(root, "SECOND plan text.");
    await ask();

    const text = proseTurns(root).map((t) => t.text).join(" ");
    assert.match(text, /SECOND plan text/);
    assert.doesNotMatch(text, /FIRST plan text/, "the stale plan is still on screen");
  },

  async "the prose plan does not leak into the shared prep log"() {
    // `cm-prep-log` must stay empty AND stay present: it is `slotTarget`'s
    // fallback, and clearing it globally is what its own comment warns against.
    const root = buildPage();
    const ask = await drivePlanner(root);
    await ask();

    const log = find(root, ".cm-prep-log");
    assert.ok(log, "the shared log was removed, breaking the legacy fallback");
    assert.equal(findAll(log, ".cm-turn").length, 0);
  },

  async "a page without the slot still renders, using the shared log"() {
    // Backward compatibility, which is the whole reason slotTarget has a
    // fallback. An older rendered page must not break — it keeps the old
    // stacking behaviour, which is worse than replacing but far better than
    // rendering nothing.
    const root = buildLegacyPage();
    const ask = await drivePlanner(root);
    await ask();

    assert.equal(find(root, ".cm-prose-plan-slot"), null, "the harness still has a slot");
    assert.equal(findAll(find(root, ".cm-prep-log"), ".cm-turn").length, 1,
      "with no slot the plan should fall back to the shared log");
  },

  /* --- the neighbouring outputs are untouched --------------------------- */

  async "asking for a prose plan does not disturb the budgeted plan"() {
    const root = buildPage();
    const ask = await drivePlanner(root);

    find(root, ".cm-budget-input").value = "20";
    await find(root, ".cm-budget-form")._listeners.submit({ preventDefault() {} });
    await settleAsync();
    const cardsBefore = findAll(find(root, ".cm-plan-slot"), ".cm-plan-card").length;

    await ask();

    assert.equal(cardsBefore, 1, "the budgeted plan did not render");
    assert.equal(findAll(find(root, ".cm-plan-slot"), ".cm-plan-card").length, 1,
      "the prose plan disturbed the budgeted plan card next to it");
  },

  async "a second budgeted plan still replaces, as it always did"() {
    const root = buildPage();
    await drivePlanner(root);
    for (const marks of ["20", "30"]) {
      find(root, ".cm-budget-input").value = marks;
      await find(root, ".cm-budget-form")._listeners.submit({ preventDefault() {} });
      await settleAsync();
    }
    assert.equal(findAll(find(root, ".cm-plan-slot"), ".cm-plan-card").length, 1);
  },

  async "the practice slot is still the practice card's own home"() {
    const root = buildPage();
    await drivePlanner(root);
    const practice = find(root, ".cm-practice-slot");
    assert.ok(practice, "the practice slot is missing from the harness");
    assert.equal(findAll(practice, ".cm-turn").length, 0,
      "a prose plan landed in the practice slot");
  },

  async "each prep output writes to its own container"() {
    // The rule this fix restores, stated once. Three outputs, three homes.
    //
    // Comments are stripped first. The fix's own comment QUOTES the line it
    // replaced — `prepLog.appendChild(planNode)` — so scanning raw text failed
    // on the explanation rather than on any code. Same trap, and same answer,
    // as test_studio_settings.py stripping a docstring before scanning.
    const code = readFileSync(JS, "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/^\s*\/\/.*$/gm, "");

    assert.match(code, /slotTarget\(prosePlanSlot\)/,
      "the prose plan no longer goes through slotTarget");
    assert.doesNotMatch(code, /prepLog\.appendChild/,
      "something appends to the shared log directly again");
  },

  async "the html template carries the prose-plan slot"() {
    const html = readFileSync(
      resolve(here, "../../coursemate_platform/xblock/static/html/student_view.html"), "utf8");
    assert.match(html, /class="cm-prose-plan-slot"/,
      "the slot is not in the template, so production falls back to stacking");
  },


  /* --- Phase 2: the plan arrives as data, not markdown ------------------ */

  async "with the agent off the client asks for the plan as data"() {
    const root = buildPage();
    const calls = await drivePlannerStructured(root);
    const urls = calls.map((c) => c.url);
    assert.ok(urls.some((u) => u.includes("/revision-plan")),
      "the structured route was not called");
    assert.equal(urls.filter((u) => /\/plan$/.test(u)).length, 0,
      "the prose stream was used even though the plan is deterministic");
  },

  async "the request carries no identity, only the free text and mastery"() {
    const root = buildPage();
    const calls = await drivePlannerStructured(root);
    const req = calls.find((c) => c.url.includes("/revision-plan"));
    assert.deepEqual(Object.keys(JSON.parse(req.opts.body)).sort(), ["mastery", "request"]);
  },

  async "each outcome renders as a heading with its own record line"() {
    const root = buildPage();
    await drivePlannerStructured(root);
    const a = planNode(root);
    const heads = findAll(a, "h4").map((h) => h.text);
    assert.deepEqual(heads, [
      "CLO-1 — Identify the organisations and roles",
      "CLO-3 — Configure a Tutor deployment",
    ]);
    const notes = findAll(a, ".cm-answer-note").map((n) => n.text);
    assert.deepEqual(notes, ["Your record: not practised yet", "Your record: 2/3 self-marked"]);
  },

  async "question text carries its marks, year and exam metadata"() {
    const root = buildPage();
    await drivePlannerStructured(root);
    const items = findAll(planNode(root), "li");
    assert.equal(items.length, 2);
    assert.match(items[0].text, /Name two major members of the community \(3 marks, 2024, final\)/);
  },

  async "a filename containing underscores survives intact"() {
    // The reason this plan is structured at all. As markdown,
    // `_Source: oex101_final_2024.pdf, p.2_` cannot be told from its own
    // italics — the filename's underscores collide with the markup. Carrying
    // the value means there is nothing to parse and nothing to collide.
    const root = buildPage();
    await drivePlannerStructured(root);
    const text = planNode(root).text;
    assert.match(text, /oex101_final_2024\.pdf/,
      "the source filename was mangled");
    assert.equal((text.match(/oex101_final_2024\.pdf/g) || []).length, 2,
      "both questions should name their source paper");
  },

  async "an unusual filename with many underscores is still verbatim"() {
    const plan = JSON.parse(JSON.stringify(PLAN_JSON));
    plan.outcomes[0].questions[0].source_doc_id = "a_b_c_2024_final_v2.pdf";
    const root = buildPage();
    await drivePlannerStructured(root, plan);
    assert.match(planNode(root).text, /a_b_c_2024_final_v2\.pdf/,
      "an inline-italic parser would have eaten the interior underscores");
  },

  async "the source line stays inside its own question"() {
    const root = buildPage();
    await drivePlannerStructured(root);
    const items = findAll(planNode(root), "li");
    for (const li of items) {
      const subs = findAll(li, ".cm-answer-subline");
      assert.ok(subs.length >= 1, "a question has no source line");
      assert.match(subs[0].text, /^Source: /);
    }
  },

  async "the page number is shown when present"() {
    const root = buildPage();
    await drivePlannerStructured(root);
    assert.match(planNode(root).text, /Source: oex101_final_2024\.pdf, p\.2/);
  },

  async "a low-confidence extraction is flagged, not hidden"() {
    const root = buildPage();
    await drivePlannerStructured(root);
    const items = findAll(planNode(root), "li");
    assert.equal(findAll(items[0], ".cm-answer-subline").length, 1, "q1 is not flagged");
    assert.equal(findAll(items[1], ".cm-answer-subline").length, 2, "q2's flag is missing");
    assert.match(items[1].text, /Extraction confidence was low/);
  },

  async "an outcome with no questions says so, and is not an error"() {
    const root = buildPage();
    await drivePlannerStructured(root);
    assert.match(planNode(root).text, /No past-paper question is tagged to this outcome yet/);
    assert.equal(find(root, ".cm-prep-notice").hidden, true,
      "an empty outcome was reported as a fault");
  },

  async "outcome order is the planner's advice and is rendered as given"() {
    // Weakest first. A client that re-sorted would be overriding the
    // recommendation, which is the whole content of the plan.
    const root = buildPage();
    await drivePlannerStructured(root);
    const heads = findAll(planNode(root), "h4").map((h) => h.text);
    assert.ok(heads[0].startsWith("CLO-1"), `order changed: ${heads}`);
    assert.ok(heads[1].startsWith("CLO-3"), `order changed: ${heads}`);
  },

  async "the source papers appear as citation chips"() {
    const root = buildPage();
    await drivePlannerStructured(root);
    // `.cm-citation`, which is what `citationNode` builds — the same chip the
    // chat answer and the streamed plan already use, so provenance looks the
    // same wherever it appears.
    const slot = find(root, ".cm-prose-plan-slot") || find(root, ".cm-prep-log");
    const chips = findAll(slot, ".cm-citation");
    assert.equal(chips.length, 1, "one distinct paper should yield one chip");
    assert.equal(chips[0].text, "oex101_final_2024.pdf");
  },

  async "no markdown is parsed on this path"() {
    // Nothing should reach the student as raw markup, and equally nothing
    // should have needed a parser to get here.
    const root = buildPage();
    await drivePlannerStructured(root);
    const text = planNode(root).text;
    assert.doesNotMatch(text, /##/, "raw heading markup reached the student");
    assert.doesNotMatch(text, /_Your record/, "raw italic markup reached the student");
    assert.doesNotMatch(text, /_Source:/, "raw italic markup reached the student");
  },

  async "a still-preparing course is reported as a state, not a fault"() {
    const root = buildPage();
    await drivePlannerStructured(root, PLAN_JSON, 409);
    const notice = find(root, ".cm-prep-notice");
    assert.equal(notice.hidden, false);
    assert.match(notice.textContent, /haven't been loaded/i);
  },

  async "a withdrawn entitlement says access, not outage"() {
    const root = buildPage();
    await drivePlannerStructured(root, PLAN_JSON, 403);
    assert.match(find(root, ".cm-prep-notice").textContent, /access/i);
  },

  async "a failed structured plan leaves no empty bubble behind"() {
    const root = buildPage();
    await drivePlannerStructured(root, PLAN_JSON, 409);
    const slot = find(root, ".cm-prose-plan-slot") || find(root, ".cm-prep-log");
    assert.equal(findAll(slot, ".cm-turn").length, 0,
      "an empty plan bubble was orphaned after a refusal");
  },

  async "with the agent ON the prose stream is still used"() {
    // The kill switch works both ways: when the agent narrates, prose does
    // arrive a token at a time and the stream is the right shape.
    const root = buildPage();
    const calls = [];
    globalThis.fetch = async (url) => {
      calls.push(String(url));
      if (String(url).includes("/mint")) {
        return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
      }
      if (String(url).includes("/status")) {
        return { ok: true, json: async () => ({
          pack_loaded: true, questions: 5, clos: 3, clo_options: [],
          agent_available: true,
        }) };
      }
      if (String(url).includes("/revision-plan")) {
        throw new Error("the structured route must not be used when the agent is on");
      }
      if (String(url).includes("/plan")) {
        return sseBody([{ type: "token", text: "Revise CLO-1 first." }, { type: "done" }]);
      }
      return { ok: true, json: async () => ({}) };
    };
    const src = readFileSync(JS, "utf8");
    const factory = vm.runInThisContext(`${src}\nCourseMateTutor;`, { filename: JS });
    factory({ handlerUrl: (_e, name) => `/handler/${name}` },
            { querySelector: (s) => find(root, s) || root }, {});
    find(root, '.cm-panel[data-panel="prep"]').dataset.base = "/coursemate/api/examprep";
    await settleAsync();
    find(root, ".cm-prep-input").value = "plan me a session";
    await find(root, ".cm-prep-form")._listeners.submit({ preventDefault() {} });
    await settleAsync();

    assert.ok(calls.some((u) => /\/plan$/.test(u)), "the prose stream was not used");
    assert.match(planNode(root).text, /Revise CLO-1 first/);
  },

  async "the budget form is a separate form from the practice form"() {
    const root = buildPage();
    assert.ok(find(root, ".cm-budget-form"));
    assert.ok(find(root, ".cm-practice-form"));
    assert.notEqual(find(root, ".cm-budget-form"), find(root, ".cm-practice-form"));
  },

  async "the html template carries the budget control with the contract bounds"() {
    const html = readFileSync(
      resolve(here, "../../coursemate_platform/xblock/static/html/student_view.html"), "utf8");
    // Class-token match, not the whole attribute: the control also carries the
    // shared `cm-field` styling class, and pinning the exact attribute string
    // made this fail for a purely visual change.
    assert.match(html, /class="[^"]*\bcm-budget-input\b[^"]*"/);
    assert.match(html, /min="1"/);
    assert.match(html, /max="500"/);
  },

  /* --- F1: the examiner's reference answer --------------------------- */

  async "no reveal control when the paper printed no answer"() {
    // The live bank is entirely in this state: OEX101 is a question paper with
    // no marking scheme. A control that opens an empty panel teaches a student
    // the feature is broken.
    const root = buildPage();
    await drivePlannerStructured(root);
    assert.equal(findAll(planNode(root), ".cm-refanswer-toggle").length, 0);
  },

  async "a question with an answer offers to reveal it"() {
    const plan = JSON.parse(JSON.stringify(PLAN_JSON));
    plan.outcomes[0].questions[0].reference_answer = "edX and Axim Collaborative.";
    plan.outcomes[0].questions[0].reference_answer_source_doc_id = "oex101_marking_scheme_2024.pdf";
    plan.outcomes[0].questions[0].reference_answer_page = 11;
    const root = buildPage();
    await drivePlannerStructured(root, plan);
    assert.equal(findAll(planNode(root), ".cm-refanswer-toggle").length, 1);
  },

  async "the answer is hidden until the student asks"() {
    const plan = JSON.parse(JSON.stringify(PLAN_JSON));
    plan.outcomes[0].questions[0].reference_answer = "edX and Axim Collaborative.";
    const root = buildPage();
    await drivePlannerStructured(root, plan);
    const body = find(planNode(root), ".cm-refanswer-body");
    assert.equal(body.hidden, true, "the answer was shown without being asked for");
    assert.equal(find(planNode(root), ".cm-refanswer-toggle").getAttribute("aria-expanded"), "false");
  },

  async "revealing shows the examiner's words verbatim"() {
    const plan = JSON.parse(JSON.stringify(PLAN_JSON));
    plan.outcomes[0].questions[0].reference_answer = "edX and Axim Collaborative.";
    const root = buildPage();
    await drivePlannerStructured(root, plan);
    const toggle = find(planNode(root), ".cm-refanswer-toggle");
    toggle._listeners.click();
    const body = find(planNode(root), ".cm-refanswer-body");
    assert.equal(body.hidden, false);
    assert.match(body.text, /edX and Axim Collaborative\./);
    assert.match(toggle.textContent, /Hide/);
    assert.equal(toggle.getAttribute("aria-expanded"), "true");
  },

  async "the reference answer carries its OWN citation"() {
    // A marking scheme is frequently a different document from the paper.
    // Reusing the question's citation would point at a page without this text.
    const plan = JSON.parse(JSON.stringify(PLAN_JSON));
    const q = plan.outcomes[0].questions[0];
    q.reference_answer = "edX and Axim Collaborative.";
    q.reference_answer_source_doc_id = "oex101_marking_scheme_2024.pdf";
    q.reference_answer_page = 11;
    const root = buildPage();
    await drivePlannerStructured(root, plan);
    find(planNode(root), ".cm-refanswer-toggle")._listeners.click();
    const body = find(planNode(root), ".cm-refanswer-body");
    assert.match(body.text, /Reference answer from: oex101_marking_scheme_2024\.pdf, p\.11/);
  },

  async "the question keeps its own source citation unchanged"() {
    const plan = JSON.parse(JSON.stringify(PLAN_JSON));
    plan.outcomes[0].questions[0].reference_answer = "edX and Axim.";
    plan.outcomes[0].questions[0].reference_answer_source_doc_id = "marking_scheme.pdf";
    const root = buildPage();
    await drivePlannerStructured(root, plan);
    const items = findAll(planNode(root), "li");
    assert.match(items[0].text, /Source: oex101_final_2024\.pdf, p\.2/,
      "the question's own provenance was replaced by the answer's");
  },

  async "an underscored answer filename survives intact"() {
    const plan = JSON.parse(JSON.stringify(PLAN_JSON));
    plan.outcomes[0].questions[0].reference_answer = "edX and Axim.";
    plan.outcomes[0].questions[0].reference_answer_source_doc_id = "a_b_c_2024_scheme_v2.pdf";
    const root = buildPage();
    await drivePlannerStructured(root, plan);
    find(planNode(root), ".cm-refanswer-toggle")._listeners.click();
    assert.match(find(planNode(root), ".cm-refanswer-body").text, /a_b_c_2024_scheme_v2\.pdf/);
  },

  async "an answer with no source shows no citation line"() {
    // Better than citing a document we cannot name.
    const plan = JSON.parse(JSON.stringify(PLAN_JSON));
    plan.outcomes[0].questions[0].reference_answer = "edX and Axim.";
    const root = buildPage();
    await drivePlannerStructured(root, plan);
    find(planNode(root), ".cm-refanswer-toggle")._listeners.click();
    assert.doesNotMatch(find(planNode(root), ".cm-refanswer-body").text, /Reference answer from:/);
  },

  async "revealing one answer does not reveal another"() {
    const plan = JSON.parse(JSON.stringify(PLAN_JSON));
    plan.outcomes[0].questions[0].reference_answer = "First answer.";
    plan.outcomes[0].questions[1].reference_answer = "Second answer.";
    const root = buildPage();
    await drivePlannerStructured(root, plan);
    const toggles = findAll(planNode(root), ".cm-refanswer-toggle");
    assert.equal(toggles.length, 2);
    toggles[0]._listeners.click();
    const bodies = findAll(planNode(root), ".cm-refanswer-body");
    assert.equal(bodies[0].hidden, false);
    assert.equal(bodies[1].hidden, true, "revealing one answer opened another");
  },

  async "the answer never appears in the plan text until revealed"() {
    const plan = JSON.parse(JSON.stringify(PLAN_JSON));
    plan.outcomes[0].questions[0].reference_answer = "SECRETMODELANSWER";
    const root = buildPage();
    await drivePlannerStructured(root, plan);
    const body = find(planNode(root), ".cm-refanswer-body");
    assert.equal(body.hidden, true);
    // It is in the DOM but hidden — the control is a disclosure, not a fetch.
    assert.match(body.text, /SECRETMODELANSWER/);
  },
};

let pass = 0, fail = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { await fn(); console.log(`  ok   ${name}`); pass++; }
  catch (e) { console.log(`  FAIL ${name}\n       ${e.message}`); fail++; }
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);