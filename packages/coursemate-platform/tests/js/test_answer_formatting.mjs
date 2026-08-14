/* How a tutor answer is rendered.
 *
 * The whole answer used to land in one text node. `white-space: pre-wrap` kept
 * line breaks, so it was never chaos — but the model emits markdown and the
 * student read the punctuation raw. A real captured answer, as it appeared:
 *
 *     - **Automatic Cohorts**: Learners are automatically assigned to a cohort…
 *
 * **The scope is measured and deliberately short.** Across the 8 answers in
 * eval/reports/ from the configured models: blank-line paragraphs 6/8, bullets
 * 5/8, bold 2/8, ordered lists 2/8 — and inline code, fenced code, headings,
 * tables and links all 0/8.
 *
 * So half this file asserts what is rendered, and half asserts what is NOT.
 * The second half is the point. A formatter grows by one small reasonable
 * addition at a time, and every addition is more surface for output derived
 * from an untrusted question and semi-trusted documents. If a future model
 * emits one of these, the honest move is to measure it and then add it — which
 * means deleting a test here on purpose, not discovering it already passes.
 *
 * Run:  node packages/coursemate-platform/tests/js/test_answer_formatting.mjs
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
    /* `raw` recurses without trimming; `text` trims once, at the top.
     *
     * The first version trimmed at every level, which silently ate the single
     * space the renderer inserts between soft-wrapped lines — so a real,
     * correct behaviour looked like a bug. Trimming inside a recursion destroys
     * exactly the whitespace that carries meaning between inline nodes. */
    get raw() {
      return [node.textContent, ...node.children.map((c) => c.raw)].join("");
    },
    get text() { return node.raw.trim(); },
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
const liveTimers = new Set();
globalThis.window = {
  location: { origin: "https://lms.example" },
  setTimeout: (fn, ms) => { const id = setTimeout(fn, ms); liveTimers.add(id); return id; },
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

function sse(frames, { hangAfter = false } = {}) {
  const body = frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join("");
  const bytes = new TextEncoder().encode(body);
  let sent = false;
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: async () => {
          if (sent) { return hangAfter ? new Promise(() => {}) : { done: true }; }
          sent = true;
          return { done: false, value: bytes };
        },
      }),
    },
  };
}

function boot(root, initArgs = {}) {
  const src = readFileSync(JS, "utf8");
  const factory = vm.runInThisContext(`${src}\nCourseMateTutor;`, { filename: JS });
  factory({ handlerUrl: (_e, name) => `/handler/${name}` },
          { querySelector: (s) => find(root, s) || root }, initArgs);
}

const settle = async () => { for (let i = 0; i < 6; i++) { await new Promise((r) => setTimeout(r, 0)); } };

const tutorTurns = (root) => findAll(find(root, ".cm-log"), ".cm-turn")
  .filter((n) => (" " + n.className + " ").includes(" tutor "));
const tutorAnswer = (root) => {
  const t = tutorTurns(root)[0];
  return t ? find(t, ".cm-answer") : null;
};

/** Stream one answer, in `chunks`, and return its rendered .cm-answer node. */
async function render(chunks) {
  const root = buildPage();
  const frames = [].concat(chunks).map((t) => ({ type: "token", text: t }));
  globalThis.fetch = async (url) => {
    if (String(url).includes("/mint")) {
      return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
    }
    if (String(url).includes("persist_turn")) { return { ok: true, json: async () => ({}) }; }
    return sse(frames.concat([{ type: "done" }]));
  };
  boot(root);
  find(root, ".cm-input").value = "q";
  await find(root, ".cm-form")._listeners.submit({ preventDefault() {} });
  await settle();
  return tutorAnswer(root);
}

/** Render the same text through the HISTORY path instead of the live stream. */
function renderFromHistory(text) {
  const root = buildPage();
  globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });
  boot(root, { history: [{ role: "tutor", content: text }] });
  return tutorAnswer(root);
}

/* Text as a READER sees it, not as `textContent` concatenates it.
 *
 * DOM textContent runs block elements together — two <li> holding "first item"
 * and "Two: second" flatten to "…first itemTwo: second…", and a word check over
 * that reports a phantom "itemTwo". The browser does the same thing; it is the
 * rendering that separates them. So block tags contribute a line break, which
 * is what makes a word-for-word comparison against the source meaningful. */
const BLOCK_TAGS = new Set(["p", "ul", "ol", "li", "div"]);

function readable(node) {
  if (node.tagName === "#text") { return node.textContent; }
  const inner = node.textContent + node.children.map(readable).join("");
  return BLOCK_TAGS.has(node.tagName) ? `\n${inner}\n` : inner;
}

const wordsOf = (node) => readable(node).match(/\w+/g) || [];

/** A structural fingerprint: tag/class tree plus text, ignoring node identity. */
function shape(node) {
  if (!node) { return "null"; }
  const kids = node.children.map(shape).join(",");
  return `${node.tagName}.${node.className}[${node.textContent}](${kids})`;
}

/* ------------------------------------------------------------------ tests */
const tests = {
  /* --- the four constructs that were measured -------------------------- */

  async "a blank line starts a new paragraph"() {
    const a = await render("First point.\n\nSecond point.");
    const paras = findAll(a, ".cm-answer-p");
    assert.equal(paras.length, 2, "blank-line separated prose is not two paragraphs");
    assert.match(paras[0].text, /First point/);
    assert.match(paras[1].text, /Second point/);
  },

  async "a single newline inside prose is a soft wrap, not a break"() {
    // The model wraps its prose. Treating those as breaks made wrapped lines
    // look like deliberate short lines.
    const a = await render("This sentence continues\non the next line.");
    assert.equal(findAll(a, ".cm-answer-p").length, 1);
    assert.match(a.text, /continues on the next line/);
  },

  async "a run of hyphens becomes one list, not three paragraphs"() {
    const a = await render("Modes:\n- Automatic\n- Manual\n- Custom");
    const lists = findAll(a, ".cm-answer-list");
    assert.equal(lists.length, 1, `expected one list, got ${lists.length}`);
    assert.equal(lists[0].tagName, "ul");
    assert.equal(findAll(lists[0], "li").length, 3);
  },

  async "a numbered run becomes an ordered list"() {
    const a = await render("1. Open the dashboard\n2. Choose Cohorts\n3. Save");
    const list = find(a, ".cm-answer-list");
    assert.equal(list.tagName, "ol", "a numbered run did not become <ol>");
    assert.equal(findAll(list, "li").length, 3);
  },

  async "double asterisks become bold, and the asterisks disappear"() {
    const a = await render("The **Automatic** mode assigns learners.");
    const strong = find(a, "strong");
    assert.ok(strong, "**bold** was not rendered");
    assert.equal(strong.text, "Automatic");
    assert.doesNotMatch(a.text, /\*/, "the asterisks are still visible to the student");
  },

  async "the real captured answer renders as a list of bold labels"() {
    // Taken verbatim from eval/reports/ — the exact text a student read with
    // the punctuation showing.
    const a = await render(
      "Cohorts can be managed in two modes: automatic or manual.\n" +
      "- **Automatic Cohorts**: Learners are automatically assigned.\n" +
      "- **Manual Cohorts**: You assign learners manually."
    );
    assert.equal(findAll(a, ".cm-answer-p").length, 1);
    const items = findAll(find(a, ".cm-answer-list"), "li");
    assert.equal(items.length, 2);
    assert.equal(find(items[0], "strong").text, "Automatic Cohorts");
    assert.doesNotMatch(a.text, /\*\*/);
  },

  /* --- what must NOT be rendered. the anti-drift half ------------------- */
  //
  // Each of these is 0/8 in the captured answers. They stay literal until a
  // model is measured emitting one — which means deleting a test here on
  // purpose rather than finding the support already crept in.

  async "a heading stays literal text"() {
    const a = await render("## Cohorts\nSome prose.");
    assert.equal(find(a, "h1"), null);
    assert.equal(find(a, "h2"), null);
    assert.equal(find(a, "h3"), null);
    assert.match(a.text, /## Cohorts/, "heading markup was consumed but not rendered");
  },

  async "backticks stay literal text"() {
    const a = await render("Run `tutor local start` to begin.");
    assert.equal(find(a, "code"), null);
    assert.equal(find(a, "pre"), null);
    assert.match(a.text, /`tutor local start`/);
  },

  async "a fenced block stays literal text"() {
    const a = await render("```\nnot code here\n```");
    assert.equal(find(a, "pre"), null);
    assert.equal(find(a, "code"), null);
    assert.match(a.text, /```/);
  },

  async "a markdown link is never turned into an anchor"() {
    // The one that matters most. A model-authored javascript: URL is the
    // injection this file exists to prevent, and the answer already carries its
    // sources as citation chips the service verified.
    const a = await render("See [the docs](javascript:alert(1)) for more.");
    assert.equal(find(a, "a"), null, "a model-authored link became an anchor");
    assert.match(a.text, /\[the docs\]/);
  },

  async "a table stays literal text"() {
    const a = await render("| mode | who |\n| --- | --- |\n| auto | system |");
    assert.equal(find(a, "table"), null);
    assert.equal(find(a, "tr"), null);
  },

  async "single asterisks are not italics"() {
    // Only ** was measured. Supporting * as well would make a stray asterisk in
    // prose swallow the rest of the sentence.
    const a = await render("Use the *Cohorts* panel and the * key.");
    assert.equal(find(a, "em"), null);
    assert.equal(find(a, "i"), null);
    assert.match(a.text, /\*Cohorts\*/);
  },

  /* --- safety ----------------------------------------------------------- */

  async "markup in an answer is shown, never interpreted"() {
    const a = await render("Try <script>alert(1)</script> and <b>bold</b>.");
    assert.equal(find(a, "script"), null, "a script element was created from answer text");
    assert.equal(find(a, "b"), null);
    assert.match(a.text, /<script>alert\(1\)<\/script>/);
  },

  async "the renderer never touches innerHTML"() {
    const src = readFileSync(JS, "utf8");
    const block = src.split("function renderAnswer")[1].split("\n  function ")[0];
    assert.doesNotMatch(block, /innerHTML|insertAdjacentHTML|outerHTML/);
    const inline = src.split("function appendInline")[1].split("\n  var BULLET")[0];
    assert.doesNotMatch(inline, /innerHTML|insertAdjacentHTML|outerHTML/);
  },

  /* --- streaming -------------------------------------------------------- */

  async "a half-arrived bold marker never renders as bold"() {
    // Tokens arrive mid-construct. Re-rendering the accumulated string, rather
    // than appending, is what keeps `**bo` from flashing bold and reflowing.
    const a = await render(["The **Auto", "matic** mode."]);
    assert.equal(find(a, "strong").text, "Automatic");
    assert.doesNotMatch(a.text, /\*/);
  },

  async "a list built across several tokens is still one list"() {
    const a = await render(["Modes:\n- Auto", "matic\n- Man", "ual"]);
    const lists = findAll(a, ".cm-answer-list");
    assert.equal(lists.length, 1, "the list fragmented across token boundaries");
    assert.equal(findAll(lists[0], "li").length, 2);
  },

  async "re-rendering leaves no duplicated content"() {
    const a = await render(["One. ", "Two. ", "Three."]);
    assert.equal(findAll(a, ".cm-answer-p").length, 1);
    assert.equal(a.text, "One. Two. Three.");
  },

  /* --- the reload trap -------------------------------------------------- */

  async "a reloaded answer looks identical to the live one"() {
    // This file has been bitten twice by the live and replayed paths drifting —
    // citations and unsupported-claim marks both used to vanish on refresh,
    // which made a reloaded answer read as MORE trustworthy than the live one.
    const text = "Intro line.\n\n- **One**: first\n- **Two**: second\n\nClosing.";
    const live = await render(text);
    const replayed = renderFromHistory(text);
    assert.equal(shape(replayed), shape(live),
      "the same answer renders differently after a page reload");
  },

  async "the student's own question is never reformatted"() {
    // A student who types ** meant **. Formatting their words would be
    // rewriting what someone wrote.
    const root = buildPage();
    globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });
    boot(root, { history: [{ role: "student", content: "What does **this** mean?" }] });
    const turn = findAll(find(root, ".cm-log"), ".cm-turn")
      .filter((n) => (" " + n.className + " ").includes(" student "))[0];
    const node = find(turn, ".cm-answer");
    assert.equal(find(node, "strong"), null, "the student's question was reformatted");
    assert.match(node.text, /\*\*this\*\*/);
  },

  async "an empty answer renders nothing rather than an empty paragraph"() {
    const node = renderFromHistory("");
    assert.equal(node.children.length, 0);
    assert.equal(node.text, "");
  },

  /* --- the property that matters most ---------------------------------- */

  async "not one word of the answer is lost"() {
    // A formatter that quietly drops a line is far worse than one that renders
    // a construct plainly: the student reads a confident answer with a piece
    // missing and has no way to tell. Checked word-for-word, in order, against
    // a REAL captured answer rather than a fixture — 176 words, four
    // paragraphs, a two-item list and two bold labels.
    const source =
      "To set up cohorts in your course, you need to use the Instructor " +
      "Dashboard and navigate to the Cohorts panel.\n\n" +
      "Cohorts can be managed in two modes: automatic or manual.\n" +
      "- **Automatic Cohorts**: Learners are automatically assigned to a cohort " +
      "when they join the course.\n" +
      "- **Manual Cohorts**: You assign learners manually through email " +
      "addresses or usernames.\n\n" +
      "We recommend setting up at least one automatic cohort.";

    // The only things the renderer is allowed to consume are the markers it
    // interprets: `**` and a leading bullet.
    const expected = source
      .replace(/\*\*/g, "")
      .replace(/^\s*[-*]\s+/gm, "")
      .match(/\w+/g);

    const rendered = wordsOf(await render(source));

    assert.deepEqual(rendered, expected,
      "the rendered answer is not word-for-word the source");
  },

  async "no word is lost when the answer arrives in fragments"() {
    // Token boundaries fall mid-word and mid-construct. Re-rendering the
    // accumulated string is what keeps that from dropping or doubling text.
    const source = "Intro.\n\n- **One**: first item\n- **Two**: second item\n\nEnd.";
    const chunks = [];
    for (let i = 0; i < source.length; i += 7) { chunks.push(source.slice(i, i + 7)); }

    const expected = source.replace(/\*\*/g, "").replace(/^\s*[-*]\s+/gm, "").match(/\w+/g);
    const rendered = wordsOf(await render(chunks));

    assert.deepEqual(rendered, expected,
      `split into ${chunks.length} tokens, the answer no longer matches its source`);
  },
};

let pass = 0, fail = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { await fn(); console.log(`  ok   ${name}`); pass++; }
  catch (e) { console.log(`  FAIL ${name}\n       ${e.message}`); fail++; }
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
