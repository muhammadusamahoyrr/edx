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
    appendChild(c) { c.parentNode = node; node.children.push(c); return c; },
    removeChild(c) {
      const i = node.children.indexOf(c);
      if (i < 0) { throw new Error("NotFoundError: node is not a child"); }
      node.children.splice(i, 1); c.parentNode = null;
    },
    addEventListener(ev, fn) { node._listeners[ev] = fn; },
    querySelector(sel) { return find(node, sel); },
    querySelectorAll(sel) { return findAll(node, sel); },
    setAttribute() {}, getAttribute() { return null; },
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
  const chat = mk("cm-panel", root); chat.dataset.panel = "chat";
  mk("cm-log", chat); mk("cm-notice", chat);
  const form = mk("cm-form", chat); mk("cm-input", form); mk("cm-send", form);

  const prep = mk("cm-panel", root); prep.dataset.panel = "prep";
  mk("cm-prep-status", prep); mk("cm-prep-log", prep); mk("cm-prep-notice", prep);
  const pf = mk("cm-prep-form", prep); mk("cm-prep-input", pf); mk("cm-prep-send", pf);
  const bf = mk("cm-budget-form", prep);
  mk("cm-budget-input", bf); mk("cm-budget-send", bf);
  const prac = mk("cm-practice-form", prep);
  mk("cm-practice-clo", prac); mk("cm-practice-band", prac); mk("cm-practice-send", prac);
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
      rationale: "3/8 correct; 5 of 20 marks allocated (bank had nothing smaller that fit)",
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
    assert.match(clos[0].textContent, /CLO-2 — 15 marks/);
    assert.match(clos[1].textContent, /CLO-1 — 5 marks/);
  },

  async "renders the question ids"() {
    const { root } = await drive();
    const qs = findAll(find(root, ".cm-plan-card"), ".cm-plan-questions");
    assert.match(qs[0].textContent, /oex101_final_2024\.pdf#2/);
    assert.match(qs[0].textContent, /oex101_final_2024\.pdf#2\(b\)/);
    assert.match(qs[1].textContent, /oex101_final_2024\.pdf#4/);
  },

  async "renders the rationale"() {
    const { root } = await drive();
    const card = find(root, ".cm-plan-card");
    assert.match(card.text, /not practised yet; 15 of 15 marks allocated/);
    assert.match(card.text, /bank had nothing smaller that fit/);
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

  async "the budget form is a separate form from the practice form"() {
    const root = buildPage();
    assert.ok(find(root, ".cm-budget-form"));
    assert.ok(find(root, ".cm-practice-form"));
    assert.notEqual(find(root, ".cm-budget-form"), find(root, ".cm-practice-form"));
  },

  async "the html template carries the budget control with the contract bounds"() {
    const html = readFileSync(
      resolve(here, "../../coursemate_platform/xblock/static/html/student_view.html"), "utf8");
    assert.match(html, /class="cm-budget-input"/);
    assert.match(html, /min="1"/);
    assert.match(html, /max="500"/);
  },
};

let pass = 0, fail = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { await fn(); console.log(`  ok   ${name}`); pass++; }
  catch (e) { console.log(`  FAIL ${name}\n       ${e.message}`); fail++; }
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
