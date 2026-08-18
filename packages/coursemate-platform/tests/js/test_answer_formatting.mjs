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
    disabled: false, value: "", href: "", dataset: {}, childNodes: [],
    parentNode: null, _listeners: {}, _attrs: {},
    /* **`children` is ELEMENTS ONLY, as in a real DOM.** This double used to
     * push text nodes into `children`, which is the reason a real rendering
     * bug passed this suite: `closePara` read `children.length` to decide
     * whether a paragraph had content, and in the browser a plain-prose
     * paragraph has zero ELEMENT children and was discarded. Here it had one,
     * so the test agreed with code that did not work. A double that is wrong
     * about the property under test cannot fail. */
    get children() { return node.childNodes.filter((c) => c.tagName !== "#text"); },
    appendChild(c) { c.parentNode = node; node.childNodes.push(c); return c; },
    removeChild(c) {
      const i = node.childNodes.indexOf(c);
      if (i < 0) { throw new Error("NotFoundError: node is not a child"); }
      node.childNodes.splice(i, 1); c.parentNode = null;
    },
    addEventListener(ev, fn) { node._listeners[ev] = fn; },
    querySelector(sel) { return find(node, sel); },
    querySelectorAll(sel) { return findAll(node, sel); },
    setAttribute(k, v) { node._attrs[k] = v; },
    getAttribute(k) { return node._attrs[k] ?? null; },
    removeAttribute(k) { delete node._attrs[k]; },
    /* Real DOM constants, because `sanitizeMath` walks `childNodes` and must
     * skip text nodes the same way it does in a browser. A double that reports
     * every node as an element would let a bug through on the one distinction
     * the walk is built around. */
    nodeType: tag === "#text" ? 3 : 1,
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
      return [node.textContent, ...node.childNodes.map((c) => c.raw)].join("");
    },
    get text() { return node.raw.trim(); },
  };
  nodes.push(node);
  return node;
}

function walk(root, fn) { fn(root); root.childNodes.forEach((c) => walk(c, fn)); }
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

/* --- MathJax double ------------------------------------------------------
 *
 * The host page loads MathJax 2.7.5 from a CDN; this suite has neither, which
 * is the point. Absent, `typesetMath` must no-op and the answer must still
 * read. Present, it must be handed the FINISHED node exactly once.
 *
 * `Queue(args, done)` mirrors the real v2 signature. Real typesetting is
 * asynchronous, so `sync: false` keeps the callback for the caller to fire —
 * that is what makes the sanitiser testable without a browser. */
function installMathJax({ sync = true } = {}) {
  const calls = [];
  globalThis.window.MathJax = {
    Hub: {
      Queue(args, done) {
        calls.push({
          verb: args[0],
          node: args[2],
          /* Snapshotted AT CALL TIME. Reading it afterwards would prove
           * nothing about when the call happened. */
          textAtCall: args[2] ? args[2].text : null,
          attached: !!(args[2] && args[2].parentNode),
          done,
        });
        if (sync && typeof done === "function") { done(); }
      },
    },
  };
  return calls;
}

function removeMathJax() { delete globalThis.window.MathJax; }

/** Stream arbitrary frames (not just tokens) and return the whole page root. */
async function renderRaw(frames) {
  const root = buildPage();
  globalThis.fetch = async (url) => {
    if (String(url).includes("/mint")) {
      return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
    }
    if (String(url).includes("persist_turn")) { return { ok: true, json: async () => ({}) }; }
    return sse(frames);
  };
  boot(root);
  find(root, ".cm-input").value = "q";
  await find(root, ".cm-form")._listeners.submit({ preventDefault() {} });
  await settle();
  return root;
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
  const inner = node.textContent + node.childNodes.map(readable).join("");
  return BLOCK_TAGS.has(node.tagName) ? `\n${inner}\n` : inner;
}

const wordsOf = (node) => readable(node).match(/\w+/g) || [];

/** A structural fingerprint: tag/class tree plus text, ignoring node identity. */
function shape(node) {
  if (!node) { return "null"; }
  const kids = node.childNodes.map(shape).join(",");
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

  /* `## heading` WAS excluded here and is now supported — deleted on purpose
   * on 2026-08-15, which is the workflow this file describes rather than a
   * hole in it.
   *
   * The original exclusion was correct: headings were 0/8 across captured MODEL
   * answers, so supporting them would have been a feature for output nobody
   * produced. What changed is that a second producer appeared. `api/plan.py`
   * writes the revision plan itself and emits `## CLO-1 — …` on every run, and
   * its markup is enumerated from source rather than sampled — there is no
   * model to be unpredictable about it.
   *
   * The remaining exclusions below are untouched and still measured at 0/8. */

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


  /* --- the deterministic revision plan (2026-08-15) ---------------------
   *
   * `api/plan.py` writes the plan itself — headings, whole-line italics,
   * bullets with an indented source line. It reached the student as raw text:
   * `## CLO-1 — …` and `_Your record: not practised yet._`, 14 of its 18
   * markup constructs shown literally.
   *
   * These constructs were added WITHOUT re-running the chat measurement, and
   * that is defensible only because the producer is our own function: its
   * markup is enumerated from source, not sampled from a model. The four
   * model-facing constructs above stay locked to what was measured.
   */

  async "a plan heading becomes a heading, not literal hashes"() {
    const a = await render("## CLO-1 — Identify the organisations\nSome prose.");
    const h = find(a, "h4");
    assert.ok(h, "## was not rendered as a heading");
    assert.equal(h.text, "CLO-1 — Identify the organisations");
    assert.doesNotMatch(a.text, /##/, "the hashes are still visible to the student");
  },

  async "the heading is h4, not h1"() {
    // The component renders inside an LMS unit that owns the page outline.
    // Emitting a top-level heading would corrupt the structure a screen reader
    // navigates by.
    const a = await render("## CLO-2 — Explain the release process");
    for (const tag of ["h1", "h2", "h3"]) {
      assert.equal(find(a, tag), null, `rendered ${tag}, which hijacks the page outline`);
    }
    assert.ok(find(a, "h4"));
  },

  async "a whole-line italic becomes emphasis"() {
    const a = await render("_Your record: not practised yet._");
    assert.ok(find(a, "em"), "the record line was not emphasised");
    assert.equal(find(a, "em").text, "Your record: not practised yet.");
    assert.doesNotMatch(a.text, /_/, "the underscores are still visible");
  },

  async "a source line stays attached to its own question"() {
    // Provenance that floats free of the question it describes is worse than
    // none (§7.6). The indented line must render INSIDE the <li>.
    const a = await render(
      "- Name two major members (3 marks, 2024, final)\n" +
      "  _Source: oex101_final_2024.pdf, p.2_\n" +
      "- State what the community is (2 marks)\n" +
      "  _Source: oex101_final_2024.pdf, p.1_"
    );
    const items = findAll(find(a, ".cm-answer-list"), "li");
    assert.equal(items.length, 2);
    for (const li of items) {
      assert.equal(findAll(li, ".cm-answer-subline").length, 1,
        "the source line is not inside its question's list item");
    }
  },

  async "a filename's own underscores survive"() {
    // The reason italics are whole-line and not inline. An inline rule either
    // mangles `oex101_final_2024.pdf` or refuses to match the line at all.
    const a = await render("  _Source: oex101_final_2024.pdf, p.2_");
    assert.match(a.text, /oex101_final_2024\.pdf/,
      "the filename was mangled by the italic rule");
  },

  async "snake_case in ordinary prose is never italicised"() {
    // The failure an inline `_..._` rule would cause: the tutor mangling its
    // own configuration advice. Measured against real identifiers from this
    // repository.
    for (const line of [
      "Set COURSEMATE_MODEL_API_BASE to the forwarder.",
      "Use rerank_top_k and student_daily_token_budget.",
      "The derived_from field carries source ids.",
    ]) {
      const a = await render(line);
      assert.equal(find(a, "em"), null, `italicised part of an identifier: ${line}`);
      assert.equal(a.text, line, `text was altered: ${a.text}`);
    }
  },

  async "the real captured plan renders with no markup left over"() {
    // Taken from a live deterministic_plan() run against OEX101.
    const plan = [
      "Here is a revision plan for this course, weakest outcome first.",
      "",
      "## CLO-1 — Identify the organisations and roles",
      "_Your record: not practised yet._",
      "",
      "- Name two major members of the Open edX community (3 marks, 2024, final)",
      "  _Source: oex101_final_2024.pdf, p.2_",
      "",
      "## CLO-3 — Configure and troubleshoot a Tutor-based deployment.",
      "_Your record: 2/3 self-marked._",
      "",
      "No past-paper question is tagged to this outcome yet.",
    ].join("\n");

    const a = await render(plan);
    assert.equal(findAll(a, "h4").length, 2);
    assert.equal(findAll(a, "li").length, 1);
    assert.equal(findAll(a, ".cm-answer-subline").length, 1);
    assert.doesNotMatch(a.text, /##/, "raw hashes reached the student");
    assert.match(a.text, /oex101_final_2024\.pdf/, "the source filename was lost");
  },

  async "the plan renders through the SAME renderer as chat"() {
    // One renderer, so a plan and an answer cannot look like two products.
    const src = readFileSync(JS, "utf8");
    const block = src.split("function requestPlan")[1].split("\n  function ")[0];
    assert.match(block, /renderAnswer\(answerNode, answer\)/,
      "the prose plan no longer goes through renderAnswer");
    assert.doesNotMatch(block, /answerNode\.textContent = answer/,
      "the plan assigns raw text again");
  },

  async "the new constructs are still built as nodes, never markup"() {
    const src = readFileSync(JS, "utf8");
    const block = src.split("function renderAnswer")[1].split("\n  function ")[0];
    assert.doesNotMatch(block, /innerHTML|insertAdjacentHTML|outerHTML/);
  },

  async "a plan reloaded from history looks identical to the streamed one"() {
    const plan = "## CLO-1 — Roles\n_Your record: 1/2 self-marked._\n\n- A question\n  _Source: p.pdf, p.1_";
    assert.equal(shape(renderFromHistory(plan)), shape(await render(plan)),
      "the plan renders differently after a page reload");
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

  /* --- the outline answer, and the class of bug it exposed --------------- */

  async "a paragraph of plain prose survives, with no inline markup at all"() {
    /* **The regression.** `closePara` gated on `para.children.length`, which
     * counts ELEMENT children. `appendInline` emits a text node for anything
     * that is not `**bold**`, so a paragraph with no markup had zero element
     * children and was thrown away.
     *
     * It reached production. The deterministic outline answer quotes the course
     * author verbatim and contains no markup, so a real browser rendered four
     * bare headings and dropped every word of the body — including the caveat
     * saying the overview may be incomplete. */
    const out = await render("Named releases are cut twice a year.");
    assert.equal(find(out, ".cm-answer-p") !== null, true,
      "a plain-prose paragraph was dropped entirely");
    assert.match(out.text, /Named releases are cut twice a year\./);
  },

  async "the outline answer keeps its headings AND its prose"() {
    /* The exact shape `_outline_frames` emits: a lead-in line, `##` headings
     * with unmarked prose beneath each, and a closing caveat. Before the fix
     * only the headings survived. */
    const answer = [
      "This course's author-provided overview covers:",
      "",
      "## Learning Objectives",
      "After finishing this course you'll learn about the project's history.",
      "",
      "## Module Summary",
      "In this module, we learned how the community operates.",
      "",
      "This is the overview written by the course author. It may not name every page in the course.",
    ].join("\n");

    const out = await render(answer);
    const heads = findAll(out, ".cm-answer-h").map((h) => h.text);
    assert.deepEqual(heads, ["Learning Objectives", "Module Summary"]);

    assert.equal(findAll(out, ".cm-answer-p").length, 4,
      "lead-in, two body paragraphs and the caveat must all render");
    assert.match(out.text, /author-provided overview covers/);
    assert.match(out.text, /project's history/);
    assert.match(out.text, /how the community operates/);
    assert.match(out.text, /may not name every page/,
      "the honesty caveat was dropped — the one line the answer must never lose");
  },

  async "the double itself distinguishes elements from text nodes"() {
    /* Guards the harness, not the renderer. This double used to push text nodes
     * into `children`, so it disagreed with the browser about the exact
     * property `closePara` reads — and a suite that models the DOM wrongly
     * cannot fail on a DOM bug. */
    const out = await render("plain words only");
    const para = find(out, ".cm-answer-p");
    assert.equal(para.children.length, 0, "text nodes must NOT count as children");
    assert.equal(para.childNodes.length > 0, true, "but they must be childNodes");
  },

  /* --- math: the hand-off to the host page's MathJax --------------------
   *
   * Course content carries TeX in Open edX's own delimiters — measured: 6
   * active chunks, three of them in `Design a Logic Gate` — and the model
   * quotes it back. `renderAnswer` builds text nodes, so the student read
   * `\(Z = \lnot{(C(A+B))}\)` as characters. The page already loads MathJax
   * 2.7.5 configured for exactly `\(…\)` and `\[…\]`; only the call was
   * missing.
   *
   * These tests cannot check glyphs — there is no MathJax here. They check the
   * CONTRACT, which is the part that broke: called once, after the text stops
   * changing, on a node still in the document, and never on a destroyed one. */

  async "with no MathJax the LaTeX stays readable text, and nothing throws"() {
    removeMathJax();
    const a = await render("The energy is \\(x^2 + y^2\\) as shown.");
    assert.equal(a.text, "The energy is \\(x^2 + y^2\\) as shown.",
      "absent MathJax must leave the answer exactly as it reads today");
    const display = await render("Identity:\n\n\\[E = mc^2\\]\n\nfollows.");
    assert.match(display.text, /\\\[E = mc\^2\\\]/);
  },

  async "a stream of many tokens typesets exactly ONCE"() {
    /* The regression this exists for. `renderAnswer` runs on EVERY token and
     * opens with `clearNode`; MathJax's queue is asynchronous, so a per-token
     * typeset lands on nodes the next token has already destroyed. */
    const calls = installMathJax();
    const tokens = ["The ", "gate ", "\\(Z = ", "\\lnot{(C(A+B))}", "\\) ", "is ", "shown."];
    await render(tokens);
    removeMathJax();
    assert.equal(calls.length, 1, `typeset ran ${calls.length} times for ${tokens.length} tokens`);
    assert.equal(calls[0].verb, "Typeset");
  },

  async "the typeset receives the FINISHED answer, not a partial one"() {
    const calls = installMathJax();
    await render(["Half of \\(x^2", " + y^2\\) done."]);
    removeMathJax();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].textAtCall, "Half of \\(x^2 + y^2\\) done.",
      "typeset saw a partial answer — it ran before the last render");
  },

  async "the typeset target is still in the document when it runs"() {
    const calls = installMathJax();
    await render("\\(R_{ON}\\) matters.");
    removeMathJax();
    assert.equal(calls[0].attached, true, "typeset was handed a detached node");
    assert.equal((calls[0].node.className || "").includes("cm-answer"), true);
  },

  async "the reloaded history path typesets too"() {
    /* A page reload must not change how an answer looks. This file has been
     * bitten three times by exactly that shape — citations, unsupported marks,
     * and the prose that Phase B restored. */
    const calls = installMathJax();
    const a = renderFromHistory("Recall \\(E = mc^2\\) from earlier.");
    removeMathJax();
    assert.equal(calls.length, 1, "a reloaded answer was never typeset");
    assert.equal(calls[0].node, a, "typeset was handed the wrong node");
    assert.equal(calls[0].attached, true);
  },

  async "an abstention typesets nothing, because its turn is already gone"() {
    /* `settle()` removes the turn when no tokens arrived. Calling typeset from
     * the frame switch would race that removal and hand MathJax an orphan. */
    const calls = installMathJax();
    const root = await renderRaw([{ type: "error", error_code: "abstained" }, { type: "done" }]);
    removeMathJax();
    assert.equal(calls.length, 0, "typeset ran on an answer that was never produced");
    assert.equal(tutorTurns(root).length, 0, "the empty turn should have been removed");
  },

  async "a stream that drops without `done` still typesets what arrived"() {
    const calls = installMathJax();
    await renderRaw([{ type: "token", text: "Partial \\(x^2\\)" }]);
    removeMathJax();
    assert.equal(calls.length, 1, "a truncated answer was left as raw LaTeX");
  },

  /* --- math: the trust boundary ----------------------------------------
   *
   * Measured on the live page, NOT assumed: `MathJax.Extension.Safe` is
   * undefined and `Safe.js` is absent from all 33 files the hub loaded, so
   * `\href{javascript:…}` reaches the DOM as a working anchor. Answer text is
   * model output shaped by uploaded documents and by the student's question —
   * semi-trusted and untrusted under §10.6 — so the output is whitelisted after
   * typesetting. Fired manually here, which is what `sync: false` is for. */

  async "a javascript: link produced by TeX is stripped after typesetting"() {
    const calls = installMathJax({ sync: false });
    const a = await render("click \\(\\href{javascript:alert(1)}{HERE}\\)");

    const bad = a.appendChild(makeNode("a", ""));
    bad.setAttribute("href", "javascript:alert(1)");
    const badXlink = a.appendChild(makeNode("a", ""));
    badXlink.setAttribute("xlink:href", "javascript:alert(1)");
    const beacon = a.appendChild(makeNode("g", ""));
    beacon.setAttribute("style", "color:red;background:url(http://evil.example/p)");

    calls[0].done();
    removeMathJax();

    assert.equal(bad.getAttribute("href"), null, "a javascript: href survived");
    assert.equal(badXlink.getAttribute("xlink:href"), null, "a javascript: xlink:href survived");
    assert.equal(beacon.getAttribute("style"), null, "a url() beacon survived");
  },

  async "ordinary links and styles produced by TeX are left alone"() {
    /* The control arm. A sanitiser that strips everything is not a sanitiser,
     * and would quietly break the citation chips that share `safeHref`. */
    const calls = installMathJax({ sync: false });
    const a = await render("see \\(\\href{https://example.org/x}{ref}\\)");

    const ok = a.appendChild(makeNode("a", ""));
    ok.setAttribute("href", "https://example.org/x");
    const rel = a.appendChild(makeNode("a", ""));
    rel.setAttribute("href", "/courses/x");
    const styled = a.appendChild(makeNode("g", ""));
    styled.setAttribute("style", "color:red");

    calls[0].done();
    removeMathJax();

    assert.equal(ok.getAttribute("href"), "https://example.org/x");
    assert.equal(rel.getAttribute("href"), "/courses/x");
    assert.equal(styled.getAttribute("style"), "color:red");
  },

  async "the sanitiser reaches nested nodes and ignores text nodes"() {
    const calls = installMathJax({ sync: false });
    const a = await render("nested \\(x\\)");
    const outer = a.appendChild(makeNode("g", ""));
    const inner = outer.appendChild(makeNode("a", ""));
    inner.setAttribute("href", "javascript:alert(1)");
    outer.appendChild(Object.assign(makeNode("#text", ""), { textContent: "plain" }));

    calls[0].done();
    removeMathJax();
    assert.equal(inner.getAttribute("href"), null, "a nested javascript: href survived");
  },

  /* --- the Phase-B prose fix must not regress -------------------------- */

  async "prose still survives when MathJax IS present"() {
    /* Phase B restored plain-prose paragraphs by reading `childNodes` rather
     * than `children`. Adding the typeset hand-off must not undo that, and the
     * answer must be complete BEFORE MathJax is ever handed it. */
    const calls = installMathJax();
    const a = await render(
      "## Heading\n\nA paragraph of plain prose with no markup at all.\n\n" +
      "Another one, mentioning \\(x^2\\) in passing.");
    removeMathJax();

    const paras = findAll(a, ".cm-answer-p");
    assert.equal(paras.length, 2, "a plain-prose paragraph was dropped again");
    assert.equal(findAll(a, ".cm-answer-h").length, 1);
    assert.match(a.text, /A paragraph of plain prose with no markup at all\./);
    assert.match(calls[0].textAtCall, /no markup at all/,
      "MathJax was handed the answer before the prose was rendered into it");
  },
};

let pass = 0, fail = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { await fn(); console.log(`  ok   ${name}`); pass++; }
  catch (e) { console.log(`  FAIL ${name}\n       ${e.message}`); fail++; }
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
