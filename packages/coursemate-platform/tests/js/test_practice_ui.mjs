/* Browser-side test for the exam-prep practice UI.
 *
 * Node is available and `tutor.js` is plain ES5 with a small DOM surface, so the
 * cheapest honest harness is a hand-rolled fake DOM rather than a jsdom
 * dependency and a package.json this repo does not otherwise need. It covers the
 * surface the script actually touches: querySelector, createElement, append /
 * removeChild, addEventListener, textContent, className, hidden, disabled,
 * dataset.
 *
 * What this pins is the half no Python test can reach: that the browser sends
 * the PracticeRequest shape the service validates, and that the AI-generated
 * badge and provenance line are rendered from the frames the service emits.
 *
 * Run:  node packages/coursemate-platform/tests/js/test_practice_ui.mjs
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
      // Faithful to the real DOM: this is the throw the guard exists for.
      if (i < 0) { throw new Error("NotFoundError: node is not a child"); }
      node.children.splice(i, 1); c.parentNode = null;
    },
    addEventListener(ev, fn) { node._listeners[ev] = fn; },
    querySelector(sel) { return find(node, sel); },
    querySelectorAll(sel) { return findAll(node, sel); },
    setAttribute() {}, getAttribute() { return null; },
    classList: { toggle() {}, add() {}, remove() {} },
    matches(sel) {
      // Compound selectors matter: tutor.js asks for
      // `.cm-panel[data-panel="prep"]`, and a class-only match silently returns
      // null for it — which is how the first run of this harness failed nine
      // tests on a null `prepPanel`.
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
    // rendered text of the whole subtree, for assertions
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
  // The provenance line joins citations with ", ". Omitting this silently broke
  // one test and exposed the over-broad catch in readStream.
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

const CITATION_PAPER = { usage_key: "final-2024.pdf", display_name: "final-2024.pdf" };
const CITATION_LESSON = { usage_key: "block-v1:d", display_name: "Deadlock avoidance", url: "/courses/x/jump_to/block-v1:d" };

async function drive(frames, { statusClos = [{ clo_id: "CLO-1", text: "Deadlock", confirmed: true }] } = {}) {
  const root = buildPage();
  const calls = [];
  globalThis.fetch = async (url, opts = {}) => {
    calls.push({ url, opts });
    if (String(url).includes("/mint")) {
      return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
    }
    if (String(url).includes("/status")) {
      return { ok: true, json: async () => ({ pack_loaded: true, questions: 3, clos: 1, clo_options: statusClos }) };
    }
    return sse(frames);
  };

  // `vm.runInThisContext` rather than `new Function` with an interpolated body:
  // the file is our own, but the interpolation pattern is the code-injection
  // shape and there is no reason to write it.
  const src = readFileSync(JS, "utf8");
  const factory = vm.runInThisContext(`${src}
CourseMateTutor;`, { filename: JS });
  factory({ handlerUrl: (_e, name) => `/handler/${name}` }, { querySelector: (s) => find(root, s) || root }, {});

  // open the prep tab -> loads status -> enables the practice form
  const prepPanel = find(root, '.cm-panel[data-panel="prep"]');
  assert.ok(prepPanel, "prep panel not found — the harness selector is wrong");
  // The fixture has no tabs, so the status load is not triggered by a click.
  // Set the base directly; /status population is covered by the Python tests.
  prepPanel.dataset.base = "/coursemate/api/examprep";
  const clo = find(root, ".cm-practice-clo");
  clo.value = "CLO-1";
  find(root, ".cm-practice-band").value = "medium";

  const submit = find(root, ".cm-practice-form")._listeners.submit;
  assert.ok(submit, "practice form has no submit handler");
  await submit({ preventDefault() {} });
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  return { root, calls };
}

/* ------------------------------------------------------------------ tests */
const tests = {
  async "sends the PracticeRequest shape the service validates"() {
    const { calls } = await drive([{ type: "token", text: "Q?" }, { type: "done" }]);
    const post = calls.find((c) => String(c.url).includes("/practice/stream"));
    assert.ok(post, "no POST to /practice/stream");
    const body = JSON.parse(post.opts.body);
    assert.deepEqual(Object.keys(body).sort(), ["clo_id", "difficulty_band"]);
    assert.equal(body.clo_id, "CLO-1");
    assert.equal(body.difficulty_band, "medium");
    assert.match(post.opts.headers.Authorization, /^Bearer /);
  },

  async "sends null, not an empty string, when no level is chosen"() {
    const { root, calls } = await drive([{ type: "token", text: "Q?" }, { type: "done" }]);
    // re-submit with the band cleared
    find(root, ".cm-practice-band").value = "";
    await find(root, ".cm-practice-form")._listeners.submit({ preventDefault() {} });
    await new Promise((r) => setTimeout(r, 0));
    const posts = calls.filter((c) => String(c.url).includes("/practice/stream"));
    assert.equal(JSON.parse(posts[posts.length - 1].opts.body).difficulty_band, null);
  },

  async "renders the AI-generated badge and the streamed question"() {
    const { root } = await drive([
      { type: "token", text: "Explain why neither can proceed." },
      { type: "citation", citation: CITATION_PAPER },
      { type: "done" },
    ]);
    const card = find(root, ".cm-practice-card");
    assert.ok(card, "no practice card rendered");
    assert.ok(find(card, ".cm-ai-badge"), "AI-generated badge missing");
    assert.match(find(card, ".cm-ai-badge").textContent, /AI-generated/i);
    assert.match(find(card, ".cm-practice-text").textContent, /neither can proceed/);
  },

  async "renders the provenance line from citation frames"() {
    const { root } = await drive([
      { type: "token", text: "Q?" },
      { type: "citation", citation: CITATION_PAPER },
      { type: "citation", citation: CITATION_LESSON },
      { type: "done" },
    ]);
    const prov = find(find(root, ".cm-practice-card"), ".cm-provenance");
    assert.ok(prov, "no provenance line");
    // A label followed by one chip per source, rather than a comma-joined
    // sentence: the sources are separate facts and each is its own link.
    assert.match(find(prov, ".cm-sources-label").textContent, /Derived from/);
    assert.match(prov.text, /final-2024\.pdf/);
    assert.match(prov.text, /Deadlock avoidance/);
    assert.equal(findAll(prov, ".cm-chip-link").length, 2,
      "each source should be its own chip");
  },

  async "says so when a question arrives with no citation"() {
    const { root } = await drive([{ type: "token", text: "Q?" }, { type: "done" }]);
    const prov = find(find(root, ".cm-practice-card"), ".cm-provenance");
    assert.match(prov.textContent, /Source unavailable/);
  },

  async "an abstention removes the card so no empty badge is left claiming a question"() {
    const { root } = await drive([{ type: "error", error_code: "abstained" }]);
    assert.equal(find(root, ".cm-practice-card"), null, "empty AI-generated card left on screen");
    assert.equal(find(root, ".cm-prep-notice").hidden, false);
    /* Practice has its own abstention wording as of G1: the generator models
       every question on a real past-paper one, so an outcome with none tagged
       can never produce anything. The planner's "not enough material" line was
       about a different failure. */
    assert.match(find(root, ".cm-prep-notice").textContent,
                 /modelled on a real past-paper question/i);
  },

  async "preparing and abstained render different messages"() {
    const a = await drive([{ type: "error", error_code: "abstained" }]);
    const b = await drive([{ type: "error", error_code: "preparing" }]);
    const ta = find(a.root, ".cm-prep-notice").textContent;
    const tb = find(b.root, ".cm-prep-notice").textContent;
    assert.notEqual(ta, tb, "two distinct states rendered identically");
    assert.match(tb, /haven't been loaded|prepared/i);
  },

  async "controls are re-enabled after a failure so the student can retry"() {
    const { root } = await drive([{ type: "error", error_code: "unavailable" }]);
    assert.equal(find(root, ".cm-practice-send").disabled, false, "form left disabled after an error");
    assert.equal(find(root, ".cm-practice-clo").disabled, false);
  },

  async "an unconfirmed outcome is marked, not hidden"() {
    // §7.3: usable, but never presented as the instructor's.
    const src = readFileSync(JS, "utf8");
    assert.match(src, /unconfirmed/, "unconfirmed outcomes are not labelled in the selector");
  },

  async "a rendering error is not swallowed by the stream reader"() {
    // The reader must ignore an unparseable LINE and nothing else. If it
    // swallowed handler errors too, a broken frame branch would leave a
    // half-drawn card and no signal at all.
    const src = readFileSync(JS, "utf8");
    assert.match(src, /try \{ frame = JSON\.parse\(payload\); \} catch \(e\) \{ return; \}/,
      "readStream still wraps the frame handler in the parse catch");
  },

  async "a javascript: citation url is neutralised"() {
    const { root } = await drive([
      { type: "token", text: "Q?" },
      { type: "citation", citation: { usage_key: "x", display_name: "x", url: "javascript:alert(1)" } },
      { type: "done" },
    ]);
    const prov = find(find(root, ".cm-practice-card"), ".cm-provenance");
    const link = prov.children.find((c) => c.tagName === "a");
    assert.equal(link.href, "#", "javascript: url reached an href");
  },
};

let pass = 0, fail = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { await fn(); console.log(`  ok   ${name}`); pass++; }
  catch (e) { console.log(`  FAIL ${name}\n       ${e.message}`); fail++; }
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
