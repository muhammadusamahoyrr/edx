/* Browser-side test: the practice loop actually closes.
 *
 * Before this, Feature B generated a question, displayed it, and stopped. There
 * was no answer field, no submit, and nothing called `record_attempt` — so
 * `StudentMastery` was a table nothing wrote, while the model, the migration,
 * the contract, the planner's weakness ranking and the agent's plan-context tool
 * were all built on top of it. Every student looked like a new student forever.
 *
 * These tests exist because that gap was invisible from every direction except
 * runtime: each component worked, and the chain between them did not exist.
 *
 * Run:  node packages/coursemate-platform/tests/js/test_practice_loop.mjs
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
    parentNode: null, _listeners: {}, _attrs: {},
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
    setAttribute(k, v) { node._attrs[k] = v; },
    getAttribute(k) { return node._attrs[k] ?? null; },
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
  const prac = mk("cm-practice-form", prep);
  mk("cm-practice-clo", prac); mk("cm-practice-band", prac); mk("cm-practice-send", prac);
  /* The real template's practice slot. Without it the script falls back to the
     shared prep log, and none of the run behaviour below would be exercised. */
  mk("cm-practice-slot", prep);
  return root;
}

function sse(frames) {
  const body = frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join("");
  const bytes = new TextEncoder().encode(body);
  let sent = false;
  return {
    ok: true,
    body: { getReader: () => ({ read: async () => (sent ? { done: true } : ((sent = true), { done: false, value: bytes })) }) },
  };
}

const settle = async () => { for (let i = 0; i < 6; i++) { await new Promise((r) => setTimeout(r, 0)); } };

/** Generate one practice question and return the page plus every request made. */
async function generate(frames, { recordFails = false, recordError = null } = {}) {
  const root = buildPage();
  const calls = [];
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url);
    calls.push({ url: u, body: opts.body ? JSON.parse(opts.body) : null });
    if (u.includes("/mint")) {
      return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
    }
    if (u.includes("/record_attempt")) {
      if (recordFails) { throw new Error("network down"); }
      if (recordError) { return { ok: true, json: async () => ({ error: recordError }) }; }
      return { ok: true, json: async () => ({ recorded: true }) };
    }
    if (u.includes("/persist_practice")) {
      return { ok: true, json: async () => ({ saved: true }) };
    }
    return sse(frames);
  };

  const src = readFileSync(JS, "utf8");
  const factory = vm.runInThisContext(`${src}\nCourseMateTutor;`, { filename: JS });
  factory({ handlerUrl: (_e, name) => `/handler/${name}` },
          { querySelector: (s) => find(root, s) || root }, {});

  const prepPanel = find(root, '.cm-panel[data-panel="prep"]');
  prepPanel.dataset.base = "/coursemate/api/examprep";
  find(root, ".cm-practice-clo").value = "CLO-1";
  find(root, ".cm-practice-band").value = "medium";

  await find(root, ".cm-practice-form")._listeners.submit({ preventDefault() {} });
  await settle();
  return { root, calls };
}

const QUESTION = [
  { type: "token", text: "Explain why neither process can proceed." },
  { type: "citation", citation: { usage_key: "final-2024.pdf", display_name: "final-2024.pdf" } },
  { type: "done", question_id: "Q-42", difficulty_band: "hard" },
];

const recordCalls = (calls) => calls.filter((c) => c.url.includes("/record_attempt"));

/** Generate several questions on ONE page, the way a student actually would. */
async function generateRun(framesList) {
  const root = buildPage();
  const calls = [];
  let next = 0;
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url);
    calls.push({ url: u, body: opts.body ? JSON.parse(opts.body) : null });
    if (u.includes("/mint")) {
      return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
    }
    if (u.includes("/record_attempt")) {
      return { ok: true, json: async () => ({ recorded: true }) };
    }
    /* E2: the client now persists each card. Without this the persist call
       falls through and eats the NEXT question's mocked frames. */
    if (u.includes("/persist_practice")) {
      return { ok: true, json: async () => ({ saved: true }) };
    }
    return sse(framesList[next++]);
  };

  const src = readFileSync(JS, "utf8");
  const factory = vm.runInThisContext(`${src}\nCourseMateTutor;`, { filename: JS });
  factory({ handlerUrl: (_e, name) => `/handler/${name}` },
          { querySelector: (s) => find(root, s) || root }, {});

  find(root, '.cm-panel[data-panel="prep"]').dataset.base = "/coursemate/api/examprep";
  find(root, ".cm-practice-clo").value = "CLO-1";
  find(root, ".cm-practice-band").value = "medium";

  for (let i = 0; i < framesList.length; i++) {
    await find(root, ".cm-practice-form")._listeners.submit({ preventDefault() {} });
    await settle();
  }
  return { root, calls };
}

const qFrames = (n) => ([
  { type: "token", text: `Question number ${n}?` },
  { type: "citation", citation: { usage_key: "final-2024.pdf", display_name: "final-2024.pdf" } },
  { type: "done", question_id: `Q-${n}`, difficulty_band: "hard" },
]);

/* ------------------------------------------------------------------ tests */
const tests = {
  /* --- the controls exist at all --------------------------------------- */

  async "a generated question offers a place to answer"() {
    const { root } = await generate(QUESTION);
    assert.ok(find(root, ".cm-practice-answer"), "no answer field on the practice card");
  },

  async "a generated question offers a self-assessment"() {
    const { root } = await generate(QUESTION);
    assert.ok(find(root, ".cm-selfcheck-got"), "no 'I got this' control");
    assert.ok(find(root, ".cm-selfcheck-not"), "no 'Not yet' control");
  },

  /* --- the write actually happens -------------------------------------- */

  async "marking it correct writes an attempt"() {
    const { root, calls } = await generate(QUESTION);
    find(root, ".cm-selfcheck-got")._listeners.click();
    await settle();

    const posts = recordCalls(calls);
    assert.equal(posts.length, 1, "record_attempt was not called");
    assert.equal(posts[0].body.correct, true);
  },

  async "marking it wrong writes an attempt too"() {
    // Both directions must record. Only counting successes would make the
    // planner's weakness ranking read every student as uniformly strong.
    const { root, calls } = await generate(QUESTION);
    find(root, ".cm-selfcheck-not")._listeners.click();
    await settle();

    const posts = recordCalls(calls);
    assert.equal(posts.length, 1);
    assert.equal(posts[0].body.correct, false);
  },

  async "the payload carries every field record_attempt requires"() {
    const { root, calls } = await generate(QUESTION);
    find(root, ".cm-selfcheck-got")._listeners.click();
    await settle();

    const body = recordCalls(calls)[0].body;
    // clo_id, question_id and attempt_id are all required by the handler; it
    // returns an error rather than recording if any is missing.
    assert.equal(body.clo_id, "CLO-1");
    assert.equal(body.question_id, "Q-42");
    assert.ok(body.attempt_id, "no attempt_id — the handler refuses without one");
    assert.equal(typeof body.correct, "boolean");
  },

  async "the band recorded is the one the service actually used"() {
    // The student asked for "medium"; the service fell back to "hard" and said
    // so on the DONE frame. Recording "medium" would bucket the counter under a
    // difficulty they never practised.
    const { root, calls } = await generate(QUESTION);
    assert.equal(find(root, ".cm-practice-band").value, "medium");
    find(root, ".cm-selfcheck-got")._listeners.click();
    await settle();

    assert.equal(recordCalls(calls)[0].body.difficulty_band, "hard");
  },

  async "an unscored question records an empty band, not a guess"() {
    const { root, calls } = await generate([
      { type: "token", text: "q" },
      { type: "done", question_id: "Q-1" },   // no difficulty_band
    ]);
    find(root, ".cm-selfcheck-got")._listeners.click();
    await settle();

    assert.equal(recordCalls(calls)[0].body.difficulty_band, "");
  },

  /* --- idempotency ------------------------------------------------------ */

  async "a double click records once"() {
    // The handler is idempotent on attempt_id, but the UI must not fire twice
    // either — and the buttons disable on the first press.
    const { root, calls } = await generate(QUESTION);
    const got = find(root, ".cm-selfcheck-got");
    got._listeners.click();
    got._listeners.click();
    await settle();

    assert.equal(recordCalls(calls).length, 1);
  },

  async "both buttons disable after one is used"() {
    const { root } = await generate(QUESTION);
    find(root, ".cm-selfcheck-got")._listeners.click();
    await settle();

    assert.equal(find(root, ".cm-selfcheck-got").disabled, true);
    assert.equal(find(root, ".cm-selfcheck-not").disabled, true,
      "the student could still contradict their own answer");
  },

  async "changing your mind cannot double count"() {
    const { root, calls } = await generate(QUESTION);
    find(root, ".cm-selfcheck-got")._listeners.click();
    await settle();
    find(root, ".cm-selfcheck-not")._listeners.click();
    await settle();

    assert.equal(recordCalls(calls).length, 1);
  },

  async "two questions get two different attempt ids"() {
    // A fixed id would make every attempt after the first a replay, freezing a
    // student's record at whatever they answered once.
    const a = await generate(QUESTION);
    find(a.root, ".cm-selfcheck-got")._listeners.click();
    await settle();
    const b = await generate(QUESTION);
    find(b.root, ".cm-selfcheck-got")._listeners.click();
    await settle();

    assert.notEqual(
      recordCalls(a.calls)[0].body.attempt_id,
      recordCalls(b.calls)[0].body.attempt_id,
    );
  },

  /* --- when it must NOT offer the controls ------------------------------ */

  async "an abstention offers no self-assessment"() {
    const { root, calls } = await generate([
      { type: "error", error_code: "abstained" },
      { type: "done" },
    ]);
    assert.equal(find(root, ".cm-selfcheck-got"), null,
      "offered a mastery write for a question that was never shown");
    assert.equal(recordCalls(calls).length, 0);
  },

  async "a DONE with no question_id offers no self-assessment"() {
    // A control that silently does nothing is worse than no control: the
    // handler refuses without a question_id, and the student would believe
    // their practice had been counted.
    const { root } = await generate([
      { type: "token", text: "a question" },
      { type: "done" },
    ]);
    assert.equal(find(root, ".cm-selfcheck-got"), null);
  },

  /* --- failure is visible ----------------------------------------------- */

  async "a failed write says so and lets the student retry"() {
    const { root } = await generate(QUESTION, { recordFails: true });
    find(root, ".cm-selfcheck-got")._listeners.click();
    await settle();

    assert.match(find(root, ".cm-selfcheck-status").textContent, /didn't save/i);
    assert.equal(find(root, ".cm-selfcheck-got").disabled, false,
      "a failed write left the student unable to retry");
  },

  async "a handler error is reported, not swallowed"() {
    // The handler returns {"error": ...} with HTTP 200 for a refused write.
    const { root } = await generate(QUESTION, { recordError: "unauthenticated" });
    find(root, ".cm-selfcheck-got")._listeners.click();
    await settle();

    assert.match(find(root, ".cm-selfcheck-status").textContent, /didn't save/i);
    assert.equal(find(root, ".cm-selfcheck-not").disabled, false);
  },

  async "a successful write confirms it and stays confirmed"() {
    const { root } = await generate(QUESTION);
    find(root, ".cm-selfcheck-got")._listeners.click();
    await settle();

    assert.match(find(root, ".cm-selfcheck-status").textContent, /recorded/i);
  },

  /* --- privacy ---------------------------------------------------------- */

  async "the written answer never leaves the page"() {
    // Nothing can mark it, so sending it would create a store of student prose
    // with no purpose, inside the retirement boundary, for no gain.
    const { root, calls } = await generate(QUESTION);
    find(root, ".cm-practice-answer").value = "my private attempt at the answer";
    find(root, ".cm-selfcheck-got")._listeners.click();
    await settle();

    const sent = JSON.stringify(calls.map((c) => c.body));
    assert.doesNotMatch(sent, /my private attempt/,
      "the student's written answer was transmitted");
  },

  /* --- the loop is wired, structurally ---------------------------------- */

  async "record_attempt is reached through the platform handler"() {
    // Not the service. The only write in Feature B lives platform-side and off
    // the agent's tool surface, which is what keeps §10.6's read-only claim true.
    const { root, calls } = await generate(QUESTION);
    find(root, ".cm-selfcheck-got")._listeners.click();
    await settle();

    assert.match(recordCalls(calls)[0].url, /\/handler\/record_attempt$/);
  },

  async "the write is same-origin and CSRF-protected"() {
    const src = readFileSync(JS, "utf8");
    const fn = src.slice(src.indexOf("function selfAssessment"));
    assert.match(fn, /credentials: "same-origin"/);
    assert.match(fn, /platformHeaders\(\)/);
  },

  /* --- Item D: a practice RUN, not one card at a time ------------------ */

  async "three questions leave three cards"() {
    const { root } = await generateRun([qFrames(1), qFrames(2), qFrames(3)]);
    const cards = findAll(root, ".cm-practice-card");
    assert.equal(cards.length, 3, "earlier questions were destroyed");
    assert.match(cards[0].text, /Question number 1\?/);
    assert.match(cards[1].text, /Question number 2\?/);
    assert.match(cards[2].text, /Question number 3\?/);
  },

  async "each card keeps its own citations"() {
    const { root } = await generateRun([qFrames(1), qFrames(2)]);
    findAll(root, ".cm-practice-card").forEach((card, i) => {
      assert.ok(findAll(card, ".cm-chip-link").length >= 1,
        `card ${i} lost its citations when the next question arrived`);
    });
  },

  async "each card carries a unique attempt_id"() {
    // The idempotency key is built from attempt_id. Two cards sharing one would
    // make the second answer a replay of the first and discard it silently.
    const { root, calls } = await generateRun([qFrames(1), qFrames(2), qFrames(3)]);
    const cards = findAll(root, ".cm-practice-card");
    cards.forEach((card) => find(card, ".cm-selfcheck-got")._listeners.click());
    await settle();
    const ids = recordCalls(calls).map((c) => c.body.attempt_id);
    assert.equal(ids.length, 3, `expected 3 attempts, saw ${ids.length}`);
    assert.equal(new Set(ids).size, 3, `attempt_ids collided: ${JSON.stringify(ids)}`);
  },

  async "each attempt names its own question"() {
    const { root, calls } = await generateRun([qFrames(1), qFrames(2)]);
    const cards = findAll(root, ".cm-practice-card");
    cards.forEach((card) => find(card, ".cm-selfcheck-got")._listeners.click());
    await settle();
    assert.deepEqual(recordCalls(calls).map((c) => c.body.question_id), ["Q-1", "Q-2"]);
  },

  async "answering one card does not touch another"() {
    const { root, calls } = await generateRun([qFrames(1), qFrames(2)]);
    const cards = findAll(root, ".cm-practice-card");
    find(cards[0], ".cm-selfcheck-got")._listeners.click();
    await settle();

    assert.equal(recordCalls(calls).length, 1, "answering one card wrote more than one attempt");
    assert.equal(recordCalls(calls)[0].body.question_id, "Q-1");
    // The untouched card is still answerable.
    assert.equal(find(cards[1], ".cm-selfcheck-got").disabled, false,
      "answering the first card disabled the second");
  },

  async "the two cards can be marked differently"() {
    const { root, calls } = await generateRun([qFrames(1), qFrames(2)]);
    const cards = findAll(root, ".cm-practice-card");
    find(cards[0], ".cm-selfcheck-got")._listeners.click();
    find(cards[1], ".cm-selfcheck-not")._listeners.click();
    await settle();
    const posts = recordCalls(calls);
    assert.deepEqual(posts.map((c) => c.body.correct), [true, false]);
  },

  async "a double click on one card still records once"() {
    // The per-card guard must survive the card no longer being the only one.
    const { root, calls } = await generateRun([qFrames(1), qFrames(2)]);
    const got = find(findAll(root, ".cm-practice-card")[1], ".cm-selfcheck-got");
    got._listeners.click();
    got._listeners.click();
    await settle();
    assert.equal(recordCalls(calls).length, 1);
  },

  async "the button invites another question once one exists"() {
    const { root } = await generateRun([qFrames(1)]);
    assert.match(find(root, ".cm-practice-send").textContent, /another/i,
      "nothing on screen suggested asking again was possible");
  },
};

let pass = 0, fail = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { await fn(); console.log(`  ok   ${name}`); pass++; }
  catch (e) { console.log(`  FAIL ${name}\n       ${e.message}`); fail++; }
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
