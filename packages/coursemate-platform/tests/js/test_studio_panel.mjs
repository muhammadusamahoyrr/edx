/* Browser-side tests for the Studio settings panel.
 *
 * Written after finding that `CourseMateTutorStudio` had never run at all.
 * **Studio passes `element` as a jQuery object; the LMS passes a DOM node.**
 * The first line of the function called `element.querySelector`, which throws
 * on the jQuery wrapper, so init aborted every time — the index button bound
 * nothing, and the only evidence was a console error on a page nobody opens
 * DevTools on.
 *
 * That is why the first test here boots the factory with a jQuery-SHAPED
 * object. Booting it with a plain DOM node — the obvious thing to write — passes
 * against the broken code and proves nothing.
 *
 * Run:  node packages/coursemate-platform/tests/js/test_studio_panel.mjs
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import assert from "node:assert/strict";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const JS = resolve(here, "../../coursemate_platform/xblock/static/js/src/studio.js");

/* ---------------------------------------------------------------- fake DOM */
let nodes = [];

function makeNode(tag, cls) {
  const node = {
    tagName: tag, className: cls || "", textContent: "", hidden: false,
    disabled: false, value: "", checked: false, children: [], dataset: {},
    parentNode: null, _listeners: {},
    appendChild(c) { c.parentNode = node; node.children.push(c); return c; },
    addEventListener(ev, fn) { node._listeners[ev] = fn; },
    querySelector(sel) { return find(node, sel); },
    setAttribute() {}, getAttribute() { return null; },
    matches(sel) {
      if (!sel.startsWith(".")) { return node.tagName === sel; }
      return (" " + node.className + " ").includes(" " + sel.slice(1) + " ");
    },
  };
  nodes.push(node);
  return node;
}

function walk(root, fn) { fn(root); root.children.forEach((c) => walk(c, fn)); }
function find(root, sel) {
  let hit = null;
  walk(root, (n) => { if (!hit && n !== root && n.matches(sel)) hit = n; });
  return hit;
}

globalThis.document = {
  createElement: (t) => makeNode(t, ""),
  cookie: "csrftoken=tok123",
};

function buildPanel() {
  nodes = [];
  const root = makeNode("div", "coursemate-studio");
  ["cm-cfg-name", "cm-cfg-enabled", "cm-cfg-exam-prep", "cm-cfg-mode",
   "cm-cfg-save", "cm-cfg-status", "cm-index", "cm-last-indexed",
   "cm-block-count"].forEach((c) => root.appendChild(makeNode("div", c)));
  return root;
}

/** Studio's runtime hands the fragment over wrapped in jQuery. */
function asJQuery(node) {
  return { jquery: "3.6.0", 0: node, length: 1 };
}

const src = readFileSync(JS, "utf8");
const factory = () => vm.runInThisContext(`${src}\nCourseMateTutorStudio;`, { filename: JS });

function boot(element, { fetchImpl } = {}) {
  const calls = [];
  globalThis.fetch = fetchImpl || (async (url, opts) => {
    calls.push({ url, opts });
    return { ok: true, json: async () => ({ saved: true }) };
  });
  const notifications = [];
  const runtime = {
    handlerUrl: (_el, name) => `/handler/${name}`,
    notify: (kind, payload) => notifications.push({ kind, payload }),
  };
  factory()(runtime, element);
  return { calls, notifications };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

/* ------------------------------------------------------------------ tests */
const tests = {
  /* The regression. Before the fix this threw and every later test was
     unreachable in the runtime that actually matters. */
  async "it initialises when Studio passes a jQuery object"() {
    const root = buildPanel();
    assert.doesNotThrow(() => boot(asJQuery(root)),
      "init threw on a jQuery element — the whole panel is dead in Studio");
    assert.ok(root.querySelector(".cm-cfg-save")._listeners.click,
      "save button bound no handler");
    assert.ok(root.querySelector(".cm-index")._listeners.click,
      "index button bound no handler");
  },

  /* The LMS and the workbench pass a raw node; both shapes must work. */
  async "it still initialises with a plain DOM element"() {
    const root = buildPanel();
    assert.doesNotThrow(() => boot(root));
    assert.ok(root.querySelector(".cm-cfg-save")._listeners.click);
  },

  /* --- saving --------------------------------------------------------- */

  async "saving posts every settings field"() {
    const root = buildPanel();
    const { calls } = boot(asJQuery(root));
    root.querySelector(".cm-cfg-name").value = "Revision helper";
    root.querySelector(".cm-cfg-enabled").checked = true;
    root.querySelector(".cm-cfg-exam-prep").checked = true;
    root.querySelector(".cm-cfg-mode").value = "socratic";

    root.querySelector(".cm-cfg-save")._listeners.click();
    await tick(); await tick();

    const post = calls.find((c) => String(c.url).includes("submit_studio_edits"));
    assert.ok(post, "no POST to submit_studio_edits");
    assert.deepEqual(JSON.parse(post.opts.body), {
      display_name: "Revision helper",
      enabled: true,
      exam_prep_enabled: true,
      mode: "socratic",
    });
  },

  /* Django answers a token-less POST with an HTML error page, which then fails
     to parse as JSON — surfacing as "Unexpected token '<'". */
  async "the save carries the CSRF token and the session"() {
    const root = buildPanel();
    const { calls } = boot(asJQuery(root));
    root.querySelector(".cm-cfg-save")._listeners.click();
    await tick(); await tick();
    const post = calls.find((c) => String(c.url).includes("submit_studio_edits"));
    assert.equal(post.opts.headers["X-CSRFToken"], "tok123");
    assert.equal(post.opts.credentials, "same-origin");
  },

  /* The index button never ran before, so its missing token was never noticed. */
  async "the index request carries the CSRF token too"() {
    const root = buildPanel();
    const { calls } = boot(asJQuery(root));
    root.querySelector(".cm-index")._listeners.click();
    await tick(); await tick();
    const post = calls.find((c) => String(c.url).includes("index_course"));
    assert.ok(post, "no POST to index_course");
    assert.equal(post.opts.headers["X-CSRFToken"], "tok123");
  },

  async "a refusal is reported in the author's own terms"() {
    const root = buildPanel();
    boot(asJQuery(root), {
      fetchImpl: async () => ({ ok: true, json: async () => ({ error: "forbidden" }) }),
    });
    root.querySelector(".cm-cfg-save")._listeners.click();
    await tick(); await tick();
    assert.match(root.querySelector(".cm-cfg-status").textContent, /course staff access/);
    assert.equal(root.querySelector(".cm-cfg-save").disabled, false,
      "the button must be usable again after a refusal");
  },

  async "an invalid mode is named rather than collapsed into a generic failure"() {
    const root = buildPanel();
    boot(asJQuery(root), {
      fetchImpl: async () => ({ ok: true, json: async () => ({ error: "invalid_mode" }) }),
    });
    root.querySelector(".cm-cfg-save")._listeners.click();
    await tick(); await tick();
    assert.match(root.querySelector(".cm-cfg-status").textContent, /mode/i);
  },

  async "a successful save reports it and tells Studio to refresh"() {
    const root = buildPanel();
    const { notifications } = boot(asJQuery(root));
    root.querySelector(".cm-cfg-save")._listeners.click();
    await tick(); await tick();
    // "Publish", not "reload": Studio writes the draft branch and the LMS
    // serves the published one, so the setting is invisible to learners until
    // the unit is published. Verified on the live stack — draft True,
    // published False, learner saw no tab.
    assert.match(root.querySelector(".cm-cfg-status").textContent, /Saved/);
    assert.match(root.querySelector(".cm-cfg-status").textContent, /[Pp]ublish/,
      "an author told to 'reload' checks the one place that cannot have changed");
    assert.ok(notifications.some((n) => n.kind === "save"),
      "Studio was not told the block changed, so the preview keeps the old fragment");
  },

  /* Found on the live stack: the settings were written, the block was
     re-indexed and the POST returned 200, but Studio's `notify` threw — and
     because `.catch()` is attached after the success handler, it rewrote
     "Saved" into "That didn't save". The author would click again and save
     twice. A cosmetic refresh must not be able to invalidate the outcome of a
     write that already succeeded. */
  async "a throwing Studio refresh does not report a successful save as failed"() {
    const root = buildPanel();
    const calls = [];
    globalThis.fetch = async (url, opts) => {
      calls.push({ url, opts });
      return { ok: true, json: async () => ({ saved: true }) };
    };
    const runtime = {
      handlerUrl: (_el, name) => `/handler/${name}`,
      notify: () => { throw new Error("Studio notify blew up"); },
    };
    factory()(runtime, asJQuery(root));

    root.querySelector(".cm-cfg-save")._listeners.click();
    await tick(); await tick();

    assert.match(root.querySelector(".cm-cfg-status").textContent, /Saved/,
      "a successful save was reported as a failure");
    assert.ok(calls.some((c) => String(c.url).includes("submit_studio_edits")));
  },

  async "a network failure re-enables the button"() {
    const root = buildPanel();
    boot(asJQuery(root), { fetchImpl: async () => { throw new Error("offline"); } });
    root.querySelector(".cm-cfg-save")._listeners.click();
    await tick(); await tick();
    assert.match(root.querySelector(".cm-cfg-status").textContent, /didn't save/);
    assert.equal(root.querySelector(".cm-cfg-save").disabled, false);
  },
};

let pass = 0, fail = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { await fn(); console.log(`  ok   ${name}`); pass++; }
  catch (e) { console.log(`  FAIL ${name}\n       ${e.message}`); fail++; }
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
