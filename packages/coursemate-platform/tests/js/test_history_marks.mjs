/* Browser-side test: unsupported-claim marks survive a reload.
 *
 * Same hand-rolled fake DOM as the other files here, and for the same reason.
 *
 * What this pins is the half no Python test can reach. `persist_turn` storing
 * the marks is worth nothing if `renderHistory` does not draw them, and the
 * whole defect was that the live stream drew them and the reload did not — so
 * the answer came back looking MORE trustworthy than the one the student was
 * shown. A test on the storage alone would have passed throughout.
 *
 * Run:  node packages/coursemate-platform/tests/js/test_history_marks.mjs
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
/* setTimeout/clearTimeout are here because `ask()` schedules the "still
   working" line on the waiting indicator, and tutor.js reaches them through
   `window.` — the same way it already reaches `window.location.origin`. The
   fake window was missing them, which is the harness being incomplete rather
   than the code being wrong. */
globalThis.window = {
  location: { origin: "https://lms.example" },
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (id) => clearTimeout(id),
};
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

/** Boot the block with a given persisted history — i.e. simulate a page load. */
function boot(root, initArgs = {}) {
  const src = readFileSync(JS, "utf8");
  const factory = vm.runInThisContext(`${src}\nCourseMateTutor;`, { filename: JS });
  factory(
    { handlerUrl: (_e, name) => `/handler/${name}` },
    { querySelector: (s) => find(root, s) || root },
    initArgs,
  );
}

const marks = (root) => findAll(root, ".cm-unsupported").map((n) => n.textContent);
const settle = async () => { for (let i = 0; i < 4; i++) { await new Promise((r) => setTimeout(r, 0)); } };

/* ------------------------------------------------------------------ tests */
const tests = {
  /* --- the reload path, which is the whole defect --------------------- */

  async "a reloaded answer keeps its unsupported marks"() {
    const root = buildPage();
    boot(root, {
      history: [
        { role: "student", content: "What is a cohort?" },
        {
          role: "tutor",
          content: "A cohort is a group. It also cures scurvy.",
          citations: [],
          unsupported: ["It also cures scurvy."],
        },
      ],
    });
    assert.deepEqual(marks(root), ["It also cures scurvy."]);
  },

  async "the flagged sentence and its mark both survive"() {
    // The failure mode was asymmetric: the sentence came back, the warning did
    // not. Assert both, or a test passes while the answer looks verified.
    const root = buildPage();
    boot(root, {
      history: [
        { role: "student", content: "q" },
        { role: "tutor", content: "Fact. Fiction.", citations: [], unsupported: ["Fiction."] },
      ],
    });
    const turn = findAll(root, ".cm-turn").find((n) => n.className.includes("tutor"));
    // The answer text lives in `.cm-answer` inside the bubble, not on the turn
    // itself. That separation is load-bearing: the token handler assigns
    // textContent on every frame, and assigning it to a node that also holds
    // citations and marks would delete them on the next token.
    assert.match(find(turn, ".cm-answer").textContent, /Fact\. Fiction\./);
    assert.deepEqual(marks(root), ["Fiction."]);
  },

  async "several marks all come back, in order"() {
    const root = buildPage();
    boot(root, {
      history: [
        { role: "student", content: "q" },
        { role: "tutor", content: "a", citations: [], unsupported: ["one", "two", "three"] },
      ],
    });
    assert.deepEqual(marks(root), ["one", "two", "three"]);
  },

  async "marks render alongside citations, not instead of them"() {
    const root = buildPage();
    boot(root, {
      history: [
        { role: "student", content: "q" },
        {
          role: "tutor", content: "a",
          citations: [{ usage_key: "block-v1:x", display_name: "Cohorts", url: "/j/x" }],
          unsupported: ["doubtful"],
        },
      ],
    });
    assert.deepEqual(marks(root), ["doubtful"]);
    assert.equal(findAll(root, ".cm-citation").length, 1);
  },

  /* --- backward compatibility ----------------------------------------- */

  async "a turn written before this change still renders"() {
    // No `unsupported` key at all — every turn in every existing student's
    // user_state looks like this.
    const root = buildPage();
    boot(root, {
      history: [
        { role: "student", content: "old q" },
        { role: "tutor", content: "old a", citations: [] },
      ],
    });
    assert.deepEqual(marks(root), []);
    const turn = findAll(root, ".cm-turn").find((n) => n.className.includes("tutor"));
    assert.equal(find(turn, ".cm-answer").textContent, "old a");
  },

  async "a turn with no citations and no marks renders cleanly"() {
    const root = buildPage();
    boot(root, { history: [{ role: "tutor", content: "bare" }] });
    assert.deepEqual(marks(root), []);
  },

  /* --- the live path still works and now feeds the reload -------------- */

  async "a live unsupported_claim frame is still drawn"() {
    const root = buildPage();
    globalThis.fetch = async (url) => {
      if (String(url).includes("/mint")) {
        return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
      }
      return sse([
        { type: "token", text: "Fact. Fiction." },
        { type: "unsupported_claim", text: "Fiction." },
        { type: "done" },
      ]);
    };
    boot(root, {});
    find(root, ".cm-input").value = "q";
    await find(root, ".cm-form")._listeners.submit({ preventDefault() {} });
    await settle();
    assert.deepEqual(marks(root), ["Fiction."]);
  },

  async "the marks are POSTed to persist_turn with the answer"() {
    // The storage half. Without this the reload has nothing to render, which is
    // exactly the state this change fixes.
    const root = buildPage();
    const posts = [];
    globalThis.fetch = async (url, opts = {}) => {
      const u = String(url);
      if (u.includes("/mint")) {
        return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
      }
      if (u.includes("/persist_turn")) { posts.push(JSON.parse(opts.body)); return { ok: true, json: async () => ({}) }; }
      return sse([
        { type: "token", text: "Fact. Fiction." },
        { type: "unsupported_claim", text: "Fiction." },
        { type: "citation", citation: { usage_key: "block-v1:x", display_name: "Cohorts" } },
        { type: "done" },
      ]);
    };
    boot(root, {});
    find(root, ".cm-input").value = "q";
    await find(root, ".cm-form")._listeners.submit({ preventDefault() {} });
    await settle();

    assert.equal(posts.length, 1, "persist_turn was not called");
    assert.deepEqual(posts[0].unsupported, ["Fiction."]);
    assert.equal(posts[0].citations.length, 1);
    assert.equal(posts[0].answer, "Fact. Fiction.");
  },

  async "an answer with no marks posts an empty list, not undefined"() {
    const root = buildPage();
    const posts = [];
    globalThis.fetch = async (url, opts = {}) => {
      const u = String(url);
      if (u.includes("/mint")) {
        return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
      }
      if (u.includes("/persist_turn")) { posts.push(JSON.parse(opts.body)); return { ok: true, json: async () => ({}) }; }
      return sse([{ type: "token", text: "clean" }, { type: "done" }]);
    };
    boot(root, {});
    find(root, ".cm-input").value = "q";
    await find(root, ".cm-form")._listeners.submit({ preventDefault() {} });
    await settle();
    assert.deepEqual(posts[0].unsupported, []);
  },

  /* --- the round trip, end to end -------------------------------------- */

  async "a streamed answer reloads identically to how it was shown"() {
    // Stream once, capture what was posted, boot a fresh page from it, and
    // compare. This is the assertion the defect would have failed: the live
    // page had a mark, the reloaded page did not.
    const live = buildPage();
    let posted = null;
    globalThis.fetch = async (url, opts = {}) => {
      const u = String(url);
      if (u.includes("/mint")) {
        return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
      }
      if (u.includes("/persist_turn")) { posted = JSON.parse(opts.body); return { ok: true, json: async () => ({}) }; }
      return sse([
        { type: "token", text: "Grounded. Ungrounded." },
        { type: "unsupported_claim", text: "Ungrounded." },
        { type: "done" },
      ]);
    };
    boot(live, {});
    find(live, ".cm-input").value = "q";
    await find(live, ".cm-form")._listeners.submit({ preventDefault() {} });
    await settle();
    const liveMarks = marks(live);

    const reloaded = buildPage();
    boot(reloaded, {
      history: [
        { role: "student", content: "q" },
        { role: "tutor", content: posted.answer, citations: posted.citations,
          unsupported: posted.unsupported },
      ],
    });

    assert.deepEqual(marks(reloaded), liveMarks);
    assert.deepEqual(liveMarks, ["Ungrounded."]);
  },

  async "marks are rendered as text, never as html"() {
    const root = buildPage();
    boot(root, {
      history: [{ role: "tutor", content: "a", unsupported: ["<img src=x onerror=1>"] }],
    });
    assert.deepEqual(marks(root), ["<img src=x onerror=1>"]);
    const src = readFileSync(JS, "utf8");
    assert.doesNotMatch(src, /cm-unsupported[\s\S]{0,120}innerHTML/);
  },
};

let pass = 0, fail = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { await fn(); console.log(`  ok   ${name}`); pass++; }
  catch (e) { console.log(`  FAIL ${name}\n       ${e.message}`); fail++; }
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
