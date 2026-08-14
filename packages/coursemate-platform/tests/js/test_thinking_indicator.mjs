/* The waiting state: does the student know the tutor is working, and does the
 * indicator ALWAYS stop?
 *
 * Before this, `busy()` disabled the input and the send button and nothing else,
 * so between pressing Ask and the first token the student watched an empty grey
 * bubble. Measured time to first token in this repo: 3,512 ms on the hosted
 * primary (ADR-0001) and 9.7 s / 24 s / 106.3 s on the local model (BENCHMARKS
 * §132, §266). A minute and a half of blank bubble reads as broken, and the only
 * move it leaves a student is a reload that throws away the generation.
 *
 * **The removal cases are the point of this file, not the appearance case.** An
 * indicator that shows correctly and never stops is worse than none: it says the
 * tutor is still working after it has given up. There are five ways out of
 * `ask()` and each gets a test, because four of them are failure paths that no
 * happy-path test would ever reach.
 *
 * Run:  node packages/coursemate-platform/tests/js/test_thinking_indicator.mjs
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import assert from "node:assert/strict";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const JS = resolve(here, "../../coursemate_platform/xblock/static/js/src/tutor.js");
const CSS = resolve(here, "../../coursemate_platform/xblock/static/css/tutor.css");
const HTML = resolve(here, "../../coursemate_platform/xblock/static/html/student_view.html");

/* ---------------------------------------------------------------- fake DOM */
let nodes = [];

function makeNode(tag, cls) {
  const node = {
    tagName: tag, className: cls || "", textContent: "", hidden: false,
    disabled: false, value: "", href: "", dataset: {}, children: [],
    parentNode: null, _listeners: {}, _attrs: {},
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
      const base = sel.replace(/\[[^\]]*\]/g, "");
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

/* The "still working" line is scheduled through `window.setTimeout`, exactly as
 * tutor.js already reaches `window.location.origin`.
 *
 * `liveTimers` exists because the DOM alone cannot see the whole bug. On a
 * failed ask the answer node is removed, so the indicator vanishes from the
 * tree whether or not its timer was cancelled — and a mutation deleting
 * `thinking.stop()` from `settle()` passed all the DOM tests. The timer then
 * survives its node and fires into a detached element. This is what catches it. */
const liveTimers = new Set();

globalThis.window = {
  location: { origin: "https://lms.example" },
  setTimeout: (fn, ms) => {
    const id = setTimeout(fn, ms);
    liveTimers.add(id);
    return id;
  },
  clearTimeout: (id) => { liveTimers.delete(id); clearTimeout(id); },
};

function buildPage() {
  nodes = [];
  const root = makeNode("div", "coursemate-tutor");
  const mk = (cls, parent = root, tag = "div") => parent.appendChild(makeNode(tag, cls));
  mk("cm-log");
  mk("cm-notice").hidden = true;
  const form = mk("cm-form", root, "form");
  mk("cm-input", form, "input");
  mk("cm-send", form, "button");
  return root;
}

/* `hang`      : the stream opens and never delivers   (the 106-second wait)
 * `hangAfter` : the frames arrive and the stream STAYS OPEN.
 *
 * `hangAfter` is what makes the removal tests mean anything. With a `done`
 * frame the stream closes, `settle()` runs, and the indicator is cleared no
 * matter what the frame handler did — an earlier version of this file passed
 * with `thinking.stop()` DELETED from the token and error branches, which I
 * confirmed by deleting it. Holding the stream open is the only way to observe
 * whether the handler stopped it, and that is the case that matters: a real
 * answer keeps streaming for up to 49 s after its first token (ADR-0001). */
function sse(frames, { hang = false, hangAfter = false } = {}) {
  const body = frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join("");
  const bytes = new TextEncoder().encode(body);
  let sent = false;
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: async () => {
          if (hang) { return new Promise(() => {}); }   // never resolves
          if (sent) { return hangAfter ? new Promise(() => {}) : { done: true }; }
          sent = true;
          return { done: false, value: bytes };
        },
      }),
    },
  };
}

function boot(root) {
  const src = readFileSync(JS, "utf8");
  const factory = vm.runInThisContext(`${src}\nCourseMateTutor;`, { filename: JS });
  factory({ handlerUrl: (_e, name) => `/handler/${name}` },
          { querySelector: (s) => find(root, s) || root }, {});
}

const settle = async () => { for (let i = 0; i < 6; i++) { await new Promise((r) => setTimeout(r, 0)); } };

/** Drive one ask. `respond` decides what the stream fetch returns. */
async function ask(respond, { mintError = null } = {}) {
  const root = buildPage();
  liveTimers.forEach((id) => clearTimeout(id));
  liveTimers.clear();
  globalThis.fetch = async (url) => {
    if (String(url).includes("/mint")) {
      return { ok: true, json: async () => (mintError
        ? { error: mintError }
        : { token: "t", stream_path: "/coursemate/api/chat" }) };
    }
    if (String(url).includes("persist_turn")) { return { ok: true, json: async () => ({}) }; }
    return respond(url);
  };
  boot(root);
  find(root, ".cm-input").value = "What is a cohort?";
  await find(root, ".cm-form")._listeners.submit({ preventDefault() {} });
  await settle();
  return root;
}

const thinkingIn = (root) => find(root, ".cm-thinking");

/* `turnNode` builds a .cm-answer for BOTH roles, so a bare find(root,
 * ".cm-answer") returns the STUDENT's turn — which made an earlier version of
 * the token test pass while asserting on the question rather than the answer.
 * Select the tutor turn first. */
const tutorTurns = (root) => findAll(find(root, ".cm-log"), ".cm-turn")
  .filter((n) => (" " + n.className + " ").includes(" tutor "));
const tutorAnswer = (root) => {
  const turn = tutorTurns(root)[0];
  return turn ? find(turn, ".cm-answer") : null;
};

/* ------------------------------------------------------------------ tests */
const tests = {
  /* --- it appears, and says something true ---------------------------- */

  async "an indicator appears while the answer is still being generated"() {
    // The stream never delivers, which is the 106-second case.
    const root = await ask(() => sse([], { hang: true }));
    const node = thinkingIn(root);
    assert.ok(node, "no waiting indicator while a generation is in flight");
    assert.notEqual(node.text, "", "the indicator shows nothing readable");
  },

  async "the indicator says what is happening, not how long it will take"() {
    // A predicted duration would be a guess presented as a status; nothing on
    // this side can know it.
    const root = await ask(() => sse([], { hang: true }));
    const text = thinkingIn(root).text;
    assert.match(text, /searching|connecting/i);
    assert.doesNotMatch(text, /\d+\s*(second|minute|sec|min)s?\b/i,
      "the indicator promises a duration it cannot know");
  },

  async "the label moves on once the stream is open"() {
    // Two states, and only the two the client can actually observe: the LMS
    // hop, then the service. Before the stream opens it cannot claim to be
    // searching anything.
    const root = await ask(() => sse([], { hang: true }));
    assert.match(thinkingIn(root).text, /searching this course/i);
  },

  async "the dots are hidden from assistive tech, the label is not"() {
    const root = await ask(() => sse([], { hang: true }));
    const dots = find(thinkingIn(root), ".cm-thinking-dots");
    assert.ok(dots, "no dots element");
    assert.equal(dots.getAttribute("aria-hidden"), "true",
      "decorative dots would be announced");
  },

  /* --- the five ways out, which is what this file is really for -------- */

  async "it stops on the first token, while the stream is still open"() {
    // The stream stays open, so ONLY the token handler can have stopped it.
    // Written with a `done` frame first, this passed with the handler's
    // `thinking.stop()` deleted — `settle()` was doing the work at stream end,
    // by which time a real answer has been streaming for up to 49 s.
    const root = await ask(() => sse([{ type: "token", text: "Cohorts are" }],
                                     { hangAfter: true }));
    assert.equal(thinkingIn(root), null,
      "still spinning while the answer is streaming");
    assert.match(tutorAnswer(root).text, /Cohorts are/);
  },

  async "it stops on an abstention, which carries no tokens at all"() {
    // ABSTAINED and PREPARING are the two most common non-answers and neither
    // sends a token, so the token handler can never stop the indicator for them.
    const root = await ask(() => sse([{ type: "error", error_code: "abstained" }],
                                     { hangAfter: true }));
    assert.equal(thinkingIn(root), null, "left spinning on an abstention");
    assert.equal(find(root, ".cm-notice").hidden, false);
  },

  async "it stops when the service reports an outage"() {
    const root = await ask(() => sse([{ type: "error", error_code: "unavailable" }],
                                     { hangAfter: true }));
    assert.equal(thinkingIn(root), null, "left spinning on an error frame");
  },

  async "it stops when the token could not be minted"() {
    const root = await ask(() => { throw new Error("no stream after a failed mint"); },
                           { mintError: "disabled" });
    assert.equal(thinkingIn(root), null, "left spinning after a failed mint");
  },

  async "it stops when the stream request itself fails"() {
    const root = await ask(() => ({ ok: false, body: null }));
    assert.equal(thinkingIn(root), null, "left spinning on a rejected stream");
  },

  async "it stops when the connection drops mid-request"() {
    const root = await ask(() => { throw new TypeError("network down"); });
    assert.equal(thinkingIn(root), null, "left spinning after a dropped connection");
  },

  /* --- what is left behind afterwards --------------------------------- */

  async "a turn that produced nothing leaves no empty bubble"() {
    // The indicator lives in the answer bubble. Removing only the dots would
    // leave a blank grey bubble in the log — which was already happening on
    // these paths, and taking the dots out would have made it more obvious
    // rather than less.
    const root = await ask(() => sse([
      { type: "error", error_code: "abstained" }, { type: "done" },
    ]));
    assert.equal(tutorTurns(root).length, 0,
      "an empty tutor bubble was orphaned in the log");
  },

  async "an answered turn keeps its bubble"() {
    const root = await ask(() => sse([
      { type: "token", text: "Yes." }, { type: "done" },
    ]));
    assert.ok(tutorAnswer(root), "the answer bubble was removed with the indicator");
    assert.match(tutorAnswer(root).text, /Yes\./);
  },

  async "the form is usable again on every exit"() {
    for (const [name, respond, opts] of [
      ["answered", () => sse([{ type: "token", text: "Yes." }, { type: "done" }]), {}],
      ["abstained", () => sse([{ type: "error", error_code: "abstained" }, { type: "done" }]), {}],
      ["mint failed", () => { throw new Error("x"); }, { mintError: "disabled" }],
      ["stream refused", () => ({ ok: false, body: null }), {}],
      ["connection dropped", () => { throw new TypeError("down"); }, {}],
    ]) {
      const root = await ask(respond, opts);
      assert.equal(find(root, ".cm-input").disabled, false, `input left disabled: ${name}`);
      assert.equal(find(root, ".cm-send").disabled, false, `send left disabled: ${name}`);
    }
  },

  async "no exit path leaves a timer running"() {
    // The DOM check cannot see this. On a failed ask the answer node is
    // removed, so the indicator disappears from the tree whether or not its
    // timer was cancelled — deleting `thinking.stop()` from `settle()` passed
    // every DOM test in this file. A surviving timer then fires into a detached
    // node, which is a leak per abandoned question and, in a browser, work done
    // for a turn nobody is waiting on.
    for (const [name, respond, opts] of [
      ["answered", () => sse([{ type: "token", text: "Yes." }, { type: "done" }]), {}],
      ["abstained", () => sse([{ type: "error", error_code: "abstained" }, { type: "done" }]), {}],
      ["mint failed", () => { throw new Error("x"); }, { mintError: "disabled" }],
      ["stream refused", () => ({ ok: false, body: null }), {}],
      ["connection dropped", () => { throw new TypeError("down"); }, {}],
    ]) {
      await ask(respond, opts);
      assert.equal(liveTimers.size, 0, `timer left running after: ${name}`);
    }
  },

  /* --- the long wait --------------------------------------------------- */

  async "a long wait eventually says it is still working"() {
    const src = readFileSync(JS, "utf8");
    // Pinned as source rather than by waiting 10 s in a test suite that runs in
    // milliseconds. What matters is that the timer exists, is cleared, and says
    // something a student can act on.
    assert.match(src, /THINKING_SLOW_MS\s*=\s*\d+/);
    assert.match(src, /still working/i);
    assert.match(src, /clearTimeout\(slowTimer\)/,
      "the slow-line timer is never cleared, so it can fire into a removed node");
  },

  /* --- accessibility and motion ---------------------------------------- */

  async "the indicator does not nest a live region inside the log"() {
    // .cm-log already carries role=log aria-live=polite. A nested live region
    // is not additive — implementations disagree on which wins, and the usual
    // result is a double or dropped announcement.
    const src = readFileSync(JS, "utf8");
    const block = src.split("function thinkingNode")[1].split("\n  }")[0];
    assert.doesNotMatch(block, /aria-live/,
      "the indicator declares its own live region inside one that already exists");
  },

  async "the notice is announced, since it sits outside the log"() {
    const html = readFileSync(HTML, "utf8");
    assert.match(html, /class="cm-notice"[^>]*role="status"/,
      "cm-notice is outside .cm-log and announces nothing");
  },

  async "the animation is opt-out"() {
    const css = readFileSync(CSS, "utf8");
    assert.match(css, /@keyframes cm-thinking-pulse/);
    assert.match(css, /@media \(prefers-reduced-motion: reduce\)/,
      "looping motion with no reduced-motion fallback");
    const reduced = css.split("prefers-reduced-motion")[1];
    assert.match(reduced, /animation:\s*none/,
      "the reduced-motion block does not actually stop the animation");
  },

  async "the indicator is built as nodes, never as html"() {
    const src = readFileSync(JS, "utf8");
    const block = src.split("function thinkingNode")[1].split("\n  }")[0];
    assert.doesNotMatch(block, /innerHTML|insertAdjacentHTML/);
  },
};

let pass = 0, fail = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { await fn(); console.log(`  ok   ${name}`); pass++; }
  catch (e) { console.log(`  FAIL ${name}\n       ${e.message}`); fail++; }
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
