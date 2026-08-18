/* Browser-side test for how error codes reach the student.
 *
 * Same hand-rolled fake DOM as the other two files here, and for the same
 * reason: `tutor.js` is plain ES5 with a small DOM surface, so a fake DOM is
 * cheaper and more honest than a jsdom dependency this repo does not otherwise
 * need.
 *
 * What this pins is the last mile of the ErrorCode contract. The Python side
 * (`test_error_contract.py`) checks that every producible code HAS a message;
 * only here can we check what the student actually reads, that the wrong
 * message is not shown, and that the generic fallback still exists for codes
 * nobody has written wording for.
 *
 * The case that prompted the file: `unauthenticated` is returned by five XBlock
 * handler paths and had no entry in NOTICES, so an expired session rendered as
 * "Something went wrong." — which invites a retry, and a retry is the one thing
 * that cannot work when the session is gone.
 *
 * Run:  node packages/coursemate-platform/tests/js/test_error_notices.mjs
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

function boot(root) {
  const src = readFileSync(JS, "utf8");
  const factory = vm.runInThisContext(`${src}\nCourseMateTutor;`, { filename: JS });
  factory({ handlerUrl: (_e, name) => `/handler/${name}` }, { querySelector: (s) => find(root, s) || root }, {});
}

const settle = async () => { for (let i = 0; i < 4; i++) { await new Promise((r) => setTimeout(r, 0)); } };

/** Ask a chat question where `mint` answers with `{error: code}`. */
async function askWithMintError(code) {
  const root = buildPage();
  globalThis.fetch = async (url) => {
    if (String(url).includes("/mint")) {
      return { ok: true, json: async () => ({ error: code }) };
    }
    throw new Error("no request should follow a failed mint");
  };
  boot(root);
  find(root, ".cm-input").value = "What is a cohort?";
  await find(root, ".cm-form")._listeners.submit({ preventDefault() {} });
  await settle();
  return root;
}

/** Ask a chat question that streams an `error` frame with `code`. */
async function askWithErrorFrame(code) {
  const root = buildPage();
  globalThis.fetch = async (url) => {
    if (String(url).includes("/mint")) {
      return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
    }
    return sse([{ type: "error", error_code: code }, { type: "done" }]);
  };
  boot(root);
  find(root, ".cm-input").value = "What is a cohort?";
  await find(root, ".cm-form")._listeners.submit({ preventDefault() {} });
  await settle();
  return root;
}

/** Generate a PRACTICE question whose stream returns an `error` frame. */
async function generateWithErrorFrame(code) {
  const root = buildPage();
  globalThis.fetch = async (url) => {
    if (String(url).includes("/mint")) {
      return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
    }
    return sse([{ type: "error", error_code: code }, { type: "done" }]);
  };
  boot(root);
  find(root, '.cm-panel[data-panel="prep"]').dataset.base = "/coursemate/api/examprep";
  find(root, ".cm-practice-clo").value = "CLO-3";
  await find(root, ".cm-practice-form")._listeners.submit({ preventDefault() {} });
  await settle();
  return root;
}

const noticeOf = (root) => find(root, ".cm-notice");
const GENERIC = "Something went wrong.";

/* ------------------------------------------------------------------ tests */
const tests = {
  /* --- the regression this file exists for ----------------------------- */

  async "an expired session is named, not reported as a generic fault"() {
    const notice = noticeOf(await askWithMintError("unauthenticated"));
    assert.notEqual(notice.textContent, GENERIC,
      "unauthenticated still falls through to the generic message");
    assert.match(notice.textContent, /expired/i);
    assert.equal(notice.hidden, false, "the notice was never shown");
  },

  async "the session message tells the student to sign in again"() {
    // Retrying is what a student does when told "Something went wrong", and it
    // cannot work — the session is gone. The message has to end that loop.
    const notice = noticeOf(await askWithMintError("unauthenticated"));
    assert.match(notice.textContent, /sign in again/i);
  },

  async "the notice carries the code as a class so it can be styled"() {
    const notice = noticeOf(await askWithMintError("unauthenticated"));
    assert.match(notice.className, /\bunauthenticated\b/);
  },

  async "the form is re-enabled after an expired session"() {
    // Otherwise the student is left with a disabled input and a message telling
    // them to act — the two contradict each other.
    const root = await askWithMintError("unauthenticated");
    assert.equal(find(root, ".cm-input").disabled, false);
    assert.equal(find(root, ".cm-send").disabled, false);
  },

  async "no question is sent after a failed mint"() {
    // askWithMintError throws on any non-mint request, so reaching here at all
    // proves the stream was never opened without a token.
    await askWithMintError("unauthenticated");
  },

  async "an unauthenticated error FRAME reads the same as a failed mint"() {
    // The service can also raise it mid-stream. Both ends must say one thing.
    const framed = noticeOf(await askWithErrorFrame("unauthenticated"));
    const minted = noticeOf(await askWithMintError("unauthenticated"));
    assert.equal(framed.textContent, minted.textContent);
    assert.match(framed.textContent, /expired/i);
  },

  /* --- the XBlock's own error vocabulary (2026-08-14) ------------------ */
  //
  // Everything above is an `ErrorCode` from the service. The block's handlers
  // return plain strings that reach the SAME showNotice() lookup and were in
  // neither the enum nor NOTICES, so four of them rendered as the generic
  // fallback. `disabled` is the one a real course hits.

  async "a tutor switched off in Studio says so, not 'something went wrong'"() {
    // `mint()` returns {"error": "disabled"} whenever an author unchecks
    // "enabled". That is a deliberate act, not a fault, and reporting it as one
    // sends students to support for a setting working exactly as intended.
    const notice = noticeOf(await askWithMintError("disabled"));
    assert.notEqual(notice.textContent, GENERIC,
      "disabled still falls through to the generic message");
    assert.match(notice.textContent, /off/i);
  },

  async "the switched-off message does not read as a breakage"() {
    const notice = noticeOf(await askWithMintError("disabled"));
    assert.doesNotMatch(notice.textContent, /wrong|error|failed|unavailable/i);
  },

  async "a learner refused an authoring handler is told why"() {
    // submit_studio_edits is reachable on the LMS route too, so a student can
    // provoke this. It must not look like the tutor broke.
    const notice = noticeOf(await askWithMintError("forbidden"));
    assert.notEqual(notice.textContent, GENERIC);
    assert.match(notice.textContent, /staff/i);
  },

  async "the remaining block errors are all named"() {
    for (const code of ["bad_request", "invalid_mode"]) {
      const notice = noticeOf(await askWithMintError(code));
      assert.notEqual(notice.textContent, GENERIC, `${code} has no wording`);
      assert.match(notice.className, new RegExp("\\b" + code + "\\b"));
    }
  },

  /* --- the daily spend ceiling (Phase C1) ------------------------------ */

  async "a spent daily budget is named, not reported as a generic fault"() {
    const notice = noticeOf(await askWithErrorFrame("budget_exceeded"));
    assert.notEqual(notice.textContent, GENERIC);
    assert.match(notice.textContent, /limit/i);
  },

  async "the budget message says when it comes back"() {
    // A limit with no stated end reads as being cut off for good.
    const notice = noticeOf(await askWithErrorFrame("budget_exceeded"));
    assert.match(notice.textContent, /resets|midnight/i);
  },

  async "the budget message exposes no cost or token detail"() {
    // The student cannot act on either number, and both are internal.
    const notice = noticeOf(await askWithErrorFrame("budget_exceeded"));
    assert.doesNotMatch(notice.textContent, /token|\$|cost|usd|cent/i);
  },

  async "the daily limit does not sound like the per-minute one"() {
    // "give it a moment" and "come back tomorrow" are different instructions.
    // Collapsing them sends a student refreshing for hours.
    const day = noticeOf(await askWithErrorFrame("budget_exceeded")).textContent;
    const minute = noticeOf(await askWithErrorFrame("rate_limited")).textContent;
    assert.notEqual(day, minute);
    assert.doesNotMatch(day, /give it a moment/i);
  },

  async "the form is re-enabled after the budget notice"() {
    const root = await askWithErrorFrame("budget_exceeded");
    assert.equal(find(root, ".cm-input").disabled, false);
    assert.equal(find(root, ".cm-send").disabled, false);
  },

  /* --- everything that already worked must still work ------------------ */

  async "abstained still says the course does not cover it"() {
    const notice = noticeOf(await askWithErrorFrame("abstained"));
    assert.match(notice.textContent, /doesn't appear to be covered/i);
  },

  async "preparing still says the course is being prepared"() {
    const notice = noticeOf(await askWithErrorFrame("preparing"));
    assert.match(notice.textContent, /still being prepared/i);
  },

  /* --- G1: an outcome with no tagged past-paper source ------------------ *
   *
   * CLO-3 on the live course has zero tagged questions, so the generator
   * abstains before it ever retrieves anything. The student saw either silence
   * or the planner's "not enough material" line, neither of which explains that
   * practice questions need a real question to model. */

  async "practice explains that a question needs a past-paper source"() {
    const notice = find(await generateWithErrorFrame("abstained"), ".cm-prep-notice");
    assert.match(notice.textContent, /modelled on a real past-paper question/i);
    assert.match(notice.textContent, /none is tagged to this outcome/i);
  },

  async "practice does not reuse the planner's wording"() {
    // "not enough material to plan" is about the PLANNER running short. The
    // practice generator abstains for a different reason entirely.
    const notice = find(await generateWithErrorFrame("abstained"), ".cm-prep-notice");
    assert.doesNotMatch(notice.textContent, /plan that reliably/i);
  },

  async "the chat abstention is untouched"() {
    // Same code, different surface: in chat it really does mean the lesson
    // material does not cover the question.
    const notice = noticeOf(await askWithErrorFrame("abstained"));
    assert.match(notice.textContent, /doesn't appear to be covered/i);
    assert.doesNotMatch(notice.textContent, /past-paper/i);
  },

  async "a non-abstain practice error keeps its own wording"() {
    const notice = find(await generateWithErrorFrame("preparing"), ".cm-prep-notice");
    assert.match(notice.textContent, /haven't been loaded/i);
    assert.doesNotMatch(notice.textContent, /past-paper question/i);
  },

  async "unavailable still says the tutor is unavailable"() {
    const notice = noticeOf(await askWithErrorFrame("unavailable"));
    assert.match(notice.textContent, /unavailable right now/i);
  },

  async "rate_limited still asks the student to wait"() {
    const notice = noticeOf(await askWithErrorFrame("rate_limited"));
    assert.match(notice.textContent, /give it a moment/i);
  },

  async "not_enrolled still says access, not outage"() {
    const notice = noticeOf(await askWithErrorFrame("not_enrolled"));
    assert.match(notice.textContent, /don't have access/i);
    assert.doesNotMatch(notice.textContent, /unavailable/i);
  },

  async "truncated still reports a cut-off answer"() {
    const root = buildPage();
    globalThis.fetch = async (url) => {
      if (String(url).includes("/mint")) {
        return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
      }
      return sse([{ type: "token", text: "A partial" }, { type: "done", truncated: true }]);
    };
    boot(root);
    find(root, ".cm-input").value = "q?";
    await find(root, ".cm-form")._listeners.submit({ preventDefault() {} });
    await settle();
    assert.match(noticeOf(root).textContent, /cut short/i);
  },

  /* --- the fallback must survive --------------------------------------- */

  async "a code with no wording still falls back to the generic message"() {
    // Deleting the fallback would turn an unknown code into a silent no-op,
    // which is strictly worse than a vague message.
    const notice = noticeOf(await askWithErrorFrame("some_future_code"));
    assert.equal(notice.textContent, GENERIC);
  },

  async "every notice is set as text, never as html"() {
    // The messages are ours today, but this is the assertion that keeps a
    // future service-supplied message from becoming an injection point.
    const src = readFileSync(JS, "utf8");
    assert.doesNotMatch(src, /notice\.innerHTML/);
    assert.match(src, /notice\.textContent = NOTICES\[code\]/);
  },
};

let pass = 0, fail = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { await fn(); console.log(`  ok   ${name}`); pass++; }
  catch (e) { console.log(`  FAIL ${name}\n       ${e.message}`); fail++; }
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
