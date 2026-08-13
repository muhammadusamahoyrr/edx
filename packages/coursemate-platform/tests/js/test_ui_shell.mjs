/* Browser-side tests for the redesigned tutor shell (2026-08-13).
 *
 * The other four JS suites use a minimal fixture that predates the redesign —
 * no slots, no app bar — and they still pass, which is deliberate: the script
 * falls back to the shared log when a page has no slots. That fallback means
 * none of them exercise the new behaviour at all.
 *
 * So this fixture mirrors the REAL template, and pins what the redesign is for:
 *
 *   - one question and one plan at a time, replaced rather than stacked
 *   - the empty states a first-run panel shows instead of dead controls
 *   - the allocation bar, including the hatched shortfall
 *   - mastery read from the snapshot, not parsed out of the rationale
 *   - citations gathered into one row rather than a stack of "Source:" lines
 *
 * One of these is a content test rather than a layout test: the empty-state
 * copy must not promise a verbatim past-paper question, because
 * `/practice/stream` cannot serve one — every question it returns is
 * `ai_generated=True`. That check exists because the design mock this shell was
 * built from DID promise it.
 *
 * Run:  node packages/coursemate-platform/tests/js/test_ui_shell.mjs
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import assert from "node:assert/strict";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const JS = resolve(here, "../../coursemate_platform/xblock/static/js/src/tutor.js");
const HTML = resolve(here, "../../coursemate_platform/xblock/static/html/student_view.html");
const CSS = resolve(here, "../../coursemate_platform/xblock/static/css/tutor.css");

/* ---------------------------------------------------------------- fake DOM */
let nodes = [];

function makeNode(tag, cls) {
  const node = {
    tagName: tag, className: cls || "", textContent: "", hidden: false,
    disabled: false, value: "", href: "", dataset: {}, children: [],
    // The allocation bar sizes its segments through `style.width`; without this
    // the whole plan render throws on the first segment.
    style: {}, attrs: {},
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
    setAttribute(k, v) { node.attrs[k] = v; },
    getAttribute(k) { return k in node.attrs ? node.attrs[k] : (k === "data-panel" ? node.dataset.panel ?? null : null); },
    classList: {
      toggle(cls, on) {
        const has = (" " + node.className + " ").includes(" " + cls + " ");
        if (on && !has) { node.className = (node.className + " " + cls).trim(); }
        if (!on && has) {
          node.className = node.className.split(/\s+/).filter((c) => c !== cls).join(" ");
        }
      },
      add() {}, remove() {},
    },
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
/* Mirrors the real template: app bar with both sub-lines and both tabs, and
   the two slots. The other suites deliberately omit these. */
function buildPage() {
  nodes = [];
  const root = makeNode("div", "coursemate-tutor");
  const mk = (cls, parent, tag = "div") => {
    const n = makeNode(tag, cls);
    (parent || root).appendChild(n);
    return n;
  };

  const bar = mk("cm-appbar", root);
  const top = mk("cm-appbar-top", bar);
  mk("cm-header", top, "span");
  const tabs = mk("cm-tabs", top);
  const tabChat = mk("cm-tab is-active", tabs, "button");
  tabChat.dataset.panel = "chat";
  tabChat.setAttribute("data-panel", "chat");
  const tabPrep = mk("cm-tab", tabs, "button");
  tabPrep.dataset.panel = "prep";
  tabPrep.setAttribute("data-panel", "prep");
  mk("cm-subline cm-chat-subline", bar, "p");
  const status = mk("cm-subline cm-prep-status", bar, "p");
  status.hidden = true;

  const chat = mk("cm-panel", root); chat.dataset.panel = "chat";
  mk("cm-log", chat); mk("cm-notice", chat);
  const form = mk("cm-form", chat); mk("cm-input", form); mk("cm-send", form);

  const prep = mk("cm-panel", root); prep.dataset.panel = "prep";
  prep.hidden = true;
  mk("cm-prep-notice", prep);
  const pracCard = mk("cm-card", prep);
  const prac = mk("cm-practice-form", pracCard);
  mk("cm-practice-clo", prac); mk("cm-practice-band", prac); mk("cm-practice-send", prac);
  mk("cm-practice-slot", prep);
  const planCard = mk("cm-card", prep);
  const bf = mk("cm-budget-form", planCard);
  mk("cm-budget-input", bf); mk("cm-budget-send", bf);
  mk("cm-plan-slot", planCard);
  const pf = mk("cm-prep-form", planCard);
  mk("cm-prep-input", pf); mk("cm-prep-send", pf);
  mk("cm-prep-log", planCard);
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

const src = readFileSync(JS, "utf8");

function boot(root, initArgs = {}, handler = null) {
  globalThis.fetch = handler || (async () => ({ ok: true, json: async () => ({}) }));
  const factory = vm.runInThisContext(`${src}\nCourseMateTutor;`, { filename: JS });
  factory({ handlerUrl: (_e, name) => `/handler/${name}` },
          { querySelector: (s) => find(root, s) || root }, initArgs);
  const prep = find(root, '.cm-panel[data-panel="prep"]');
  prep.dataset.base = "/coursemate/api/examprep";
  return root;
}

const tick = () => new Promise((r) => setTimeout(r, 0));

async function generate(root, frames) {
  globalThis.fetch = async (url) => {
    if (String(url).includes("/mint")) {
      return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
    }
    return sse(frames);
  };
  find(root, ".cm-practice-clo").value = "CLO-1";
  await find(root, ".cm-practice-form")._listeners.submit({ preventDefault() {} });
  await tick(); await tick();
}

const PLAN = {
  items: [
    { clo_id: "CLO-2", marks_budget: 15, question_ids: ["p#2"],
      rationale: "not practised yet; 15 of 67 marks allocated (bank had nothing smaller that fit)" },
    { clo_id: "CLO-1", marks_budget: 5, question_ids: ["p#4"],
      rationale: "2/4 correct; 5 of 85 marks allocated (bank had nothing smaller that fit)" },
  ],
};

async function buildPlan(root, budget = "100", plan = PLAN) {
  globalThis.fetch = async (url) => {
    if (String(url).includes("/mint")) {
      return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
    }
    return { ok: true, json: async () => plan };
  };
  find(root, ".cm-budget-input").value = budget;
  await find(root, ".cm-budget-form")._listeners.submit({ preventDefault() {} });
  await tick(); await tick();
}

/* ------------------------------------------------------------------ tests */
const tests = {
  /* --- empty states --------------------------------------------------- */

  async "a first-run panel shows both empty states, not bare controls"() {
    const root = boot(buildPage());
    assert.equal(findAll(find(root, ".cm-practice-slot"), ".cm-empty").length, 1);
    assert.equal(findAll(find(root, ".cm-plan-slot"), ".cm-empty").length, 1);
    assert.match(find(root, ".cm-practice-slot").text, /No question yet/);
    assert.match(find(root, ".cm-plan-slot").text, /No study plan yet/);
  },

  /* The mock this shell copies said the student would get "a real past-paper
   * question when the bank has one". The generator sets ai_generated=True on
   * every question it returns, so that sentence would be a promise the service
   * cannot keep. */
  async "the empty state does not promise a verbatim past-paper question"() {
    const root = boot(buildPage());
    const copy = find(root, ".cm-practice-slot").text;
    assert.match(copy, /AI-generated/);
    assert.doesNotMatch(copy, /get a real past-paper question/i);
    assert.doesNotMatch(copy, /You'll get a real past/i);
  },

  async "generating a question clears the empty state"() {
    const root = boot(buildPage());
    await generate(root, [{ type: "token", text: "Q?" }, { type: "done" }]);
    assert.equal(find(find(root, ".cm-practice-slot"), ".cm-empty"), null);
    assert.ok(find(root, ".cm-practice-card"), "no question card rendered");
  },

  /* --- one at a time --------------------------------------------------- */

  async "a second question replaces the first"() {
    const root = boot(buildPage());
    await generate(root, [{ type: "token", text: "First?" }, { type: "done" }]);
    await generate(root, [{ type: "token", text: "Second?" }, { type: "done" }]);
    const cards = findAll(root, ".cm-practice-card");
    assert.equal(cards.length, 1, "questions stacked instead of replacing");
    assert.match(cards[0].text, /Second\?/);
    assert.doesNotMatch(cards[0].text, /First\?/);
  },

  async "a second plan replaces the first"() {
    const root = boot(buildPage());
    await buildPlan(root);
    await buildPlan(root, "20");
    assert.equal(findAll(root, ".cm-plan-card").length, 1, "plans stacked");
  },

  async "the question card lands in the practice slot, not the shared log"() {
    const root = boot(buildPage());
    await generate(root, [{ type: "token", text: "Q?" }, { type: "done" }]);
    assert.ok(find(find(root, ".cm-practice-slot"), ".cm-practice-card"));
    assert.equal(find(find(root, ".cm-prep-log"), ".cm-practice-card"), null);
  },

  async "the plan lands in the plan slot, not the shared log"() {
    const root = boot(buildPage());
    await buildPlan(root);
    assert.ok(find(find(root, ".cm-plan-slot"), ".cm-plan-card"));
    assert.equal(find(find(root, ".cm-prep-log"), ".cm-plan-card"), null);
  },

  /* --- the allocation bar ---------------------------------------------- */

  async "the bar draws one segment per allocated outcome"() {
    const root = boot(buildPage());
    await buildPlan(root);
    const segs = findAll(find(root, ".cm-plan-bar"), ".cm-plan-seg");
    assert.equal(segs.length, 2);
    assert.equal(segs[0].textContent, "15");
    assert.equal(segs[1].textContent, "5");
  },

  async "segment widths are proportional to the budget"() {
    const root = boot(buildPage());
    await buildPlan(root, "100");
    const segs = findAll(find(root, ".cm-plan-bar"), ".cm-plan-seg");
    assert.equal(segs[0].style.width, "15%");
    assert.equal(segs[1].style.width, "5%");
  },

  /* The shortfall is the reason the bar exists: a sentence gets skipped, 80%
   * of a bar in hatching does not. */
  async "the shortfall is drawn, sized, and labelled"() {
    const root = boot(buildPage());
    await buildPlan(root, "100");
    const gap = find(find(root, ".cm-plan-bar"), ".cm-plan-gap");
    assert.ok(gap, "a short plan drew no gap");
    assert.equal(gap.style.width, "80%");
    assert.match(gap.textContent, /80 unfilled/);
  },

  async "a fully spent budget draws no gap"() {
    const root = boot(buildPage());
    await buildPlan(root, "20");
    assert.equal(find(find(root, ".cm-plan-bar"), ".cm-plan-gap"), null);
  },

  async "the bar is labelled for a screen reader"() {
    const root = boot(buildPage());
    await buildPlan(root, "100");
    const bar = find(root, ".cm-plan-bar");
    assert.equal(bar.getAttribute("role"), "img");
    assert.match(bar.getAttribute("aria-label"), /20 of 100 marks allocated/);
  },

  async "the legend names one outcome per segment"() {
    const root = boot(buildPage());
    await buildPlan(root);
    const keys = findAll(find(root, ".cm-plan-legend"), ".cm-plan-key");
    assert.equal(keys.length, 2);
    assert.match(keys[0].text, /CLO-2/);
    assert.match(keys[1].text, /CLO-1/);
    assert.match(find(root, ".cm-plan-total").textContent, /20 of 100 marks allocated/);
  },

  /* --- mastery --------------------------------------------------------- */

  async "the mastery badge is read from the snapshot"() {
    const root = boot(buildPage(), {
      mastery: { clos: [{ clo_id: "CLO-1", difficulty_band: null, attempts: 4, correct: 2 }] },
    });
    await buildPlan(root);
    const badges = findAll(root, ".cm-plan-mastery");
    assert.match(badges[0].textContent, /not practised yet/);   // CLO-2
    assert.match(badges[1].textContent, /2\/4 correct/);        // CLO-1
    assert.match(badges[1].className, /practised/);
  },

  /* A student can be solid on the easy items and lost on the hard ones. The
   * badge is about the outcome, so the bands are summed. */
  async "mastery sums across difficulty bands"() {
    const root = boot(buildPage(), {
      mastery: { clos: [
        { clo_id: "CLO-1", difficulty_band: "easy", attempts: 3, correct: 3 },
        { clo_id: "CLO-1", difficulty_band: "hard", attempts: 2, correct: 0 },
      ] },
    });
    await buildPlan(root);
    assert.match(findAll(root, ".cm-plan-mastery")[1].textContent, /3\/5 correct/);
  },

  async "an outcome absent from the snapshot reads as unpractised"() {
    const root = boot(buildPage(), { mastery: { clos: [] } });
    await buildPlan(root);
    findAll(root, ".cm-plan-mastery").forEach((b) => {
      assert.match(b.textContent, /not practised yet/);
      assert.match(b.className, /unpractised/);
    });
  },

  async "no mastery snapshot at all still renders a plan"() {
    const root = boot(buildPage());
    await buildPlan(root);
    assert.ok(find(root, ".cm-plan-card"));
    assert.equal(findAll(root, ".cm-plan-mastery").length, 2);
  },

  /* --- the rationale split --------------------------------------------- */

  async "the mastery clause is not printed twice"() {
    const root = boot(buildPage(), {
      mastery: { clos: [{ clo_id: "CLO-1", difficulty_band: null, attempts: 4, correct: 2 }] },
    });
    await buildPlan(root);
    const card = find(root, ".cm-plan-card");
    assert.equal((card.text.match(/2\/4 correct/g) || []).length, 1);
    // ...and the rest of the sentence survived.
    assert.match(card.text, /5 of 85 marks allocated \(bank had nothing smaller that fit\)/);
  },

  /* Narrow on purpose. A rationale the service rewords must pass through
     whole rather than be silently truncated by a loose pattern. */
  async "a rationale with no mastery clause is left alone"() {
    const root = boot(buildPage());
    await buildPlan(root, "20", {
      items: [{ clo_id: "CLO-9", marks_budget: 20, question_ids: ["p#1"],
                rationale: "chosen because it is the only tagged question" }],
    });
    assert.match(find(root, ".cm-plan-rationale").textContent,
      /^Chosen because it is the only tagged question$/);
  },

  /* --- citations -------------------------------------------------------- */

  async "citations gather into one labelled row"() {
    const root = boot(buildPage(), {
      history: [
        { role: "student", content: "q" },
        { role: "tutor", content: "a", citations: [
          { usage_key: "b1", display_name: "Lesson one", url: "/j/1" },
          { usage_key: "b2", display_name: "Lesson two", url: "/j/2" },
        ] },
      ],
    });
    const rows = findAll(root, ".cm-sources");
    assert.equal(rows.length, 1, "one row per answer, not one per citation");
    assert.equal(findAll(rows[0], ".cm-citation").length, 2);
    assert.match(find(rows[0], ".cm-sources-label").textContent, /Sources/);
  },

  async "an answer with no citations grows no sources row"() {
    const root = boot(buildPage(), {
      history: [{ role: "tutor", content: "a", citations: [] }],
    });
    assert.equal(find(root, ".cm-sources"), null);
  },

  async "the student turn carries no avatar and the tutor turn does"() {
    const root = boot(buildPage(), {
      history: [
        { role: "student", content: "q" },
        { role: "tutor", content: "a" },
      ],
    });
    const turns = findAll(root, ".cm-turn");
    assert.equal(find(turns[0], ".cm-avatar-sm"), null);
    assert.ok(find(turns[1], ".cm-avatar-sm"));
  },

  /* --- the app bar ------------------------------------------------------ */

  async "switching tabs swaps exactly one sub-line"() {
    const root = boot(buildPage());
    const chatLine = find(root, ".cm-chat-subline");
    const prepLine = find(root, ".cm-prep-status");
    assert.equal(chatLine.hidden, false);
    assert.equal(prepLine.hidden, true);

    findAll(root, ".cm-tab")[1]._listeners.click();
    assert.equal(chatLine.hidden, true, "chat sub-line stayed on the prep tab");
    assert.equal(prepLine.hidden, false);

    findAll(root, ".cm-tab")[0]._listeners.click();
    assert.equal(chatLine.hidden, false);
    assert.equal(prepLine.hidden, true);
  },

  async "the status line emphasises the figures, not the nouns"() {
    const root = buildPage();
    globalThis.fetch = async (url) => {
      if (String(url).includes("/mint")) {
        return { ok: true, json: async () => ({ token: "t", stream_path: "/coursemate/api/chat" }) };
      }
      return { ok: true, json: async () => ({
        pack_loaded: true, questions: 5, clos: 3,
        earliest_year: 2024, latest_year: 2024, clo_options: [],
      }) };
    };
    const factory = vm.runInThisContext(`${src}\nCourseMateTutor;`, { filename: JS });
    factory({ handlerUrl: (_e, name) => `/handler/${name}` },
            { querySelector: (s) => find(root, s) || root }, {});
    findAll(root, ".cm-tab")[1]._listeners.click();
    await tick(); await tick(); await tick();

    const line = find(root, ".cm-prep-status");
    const stats = findAll(line, ".cm-stat").map((n) => n.textContent);
    assert.deepEqual(stats, ["5", "3", "2024–2024"]);
    assert.match(line.text, /past-paper questions/);
    assert.match(line.text, /learning outcomes/);
  },

  /* --- the shipped assets ---------------------------------------------- */

  async "the template keeps both tabs, both sub-lines and both slots"() {
    const html = readFileSync(HTML, "utf8");
    ["cm-chat-subline", "cm-prep-status", "cm-practice-slot", "cm-plan-slot",
     "cm-appbar", "cm-avatar"].forEach((cls) => {
      assert.match(html, new RegExp(`class="[^"]*\\b${cls}\\b`), `${cls} missing`);
    });
  },

  /* Provenance must survive a colour-blind reader and a greyscale print, so it
     is encoded three times: hue, glyph, and words. This pins the glyph. */
  async "the two provenance badges differ by glyph, not only by colour"() {
    const css = readFileSync(CSS, "utf8");
    assert.match(css, /\.cm-ai-badge::before\s*\{\s*content:\s*"\+"/);
    assert.match(css, /\.cm-ai-badge\.is-source::before\s*\{\s*content:\s*"\\25A0"/);
  },

  /* Reported as "the text in the buttons is blurry" (2026-08-13). The cause was
     fractional font sizes: at 12.5px the glyph stems land between device
     pixels, and the LMS sets `-webkit-font-smoothing: antialiased` globally, so
     light-on-dark text over half-pixel stems smears. Fifteen declarations had
     it. Whole numbers only. */
  async "no font size is fractional"() {
    const css = readFileSync(CSS, "utf8");
    const bad = css.match(/font-size:\s*[0-9]+\.[0-9]+px/g) || [];
    assert.deepEqual(bad, [], `fractional font sizes render blurry: ${bad.join(", ")}`);
  },

  /* Reported in the same breath. White on the first teal measured 3.74:1, under
     the 4.5:1 AA needs for 13px text — 13px bold is NOT "large text"
     (18.66px bold / 24px normal). Every white-on-teal control takes this one
     token, so this single check covers the Ask button, Generate, I got this,
     the active tab, the avatar and the filled bar segments. */
  async "white text on the action colour meets WCAG AA"() {
    const css = readFileSync(CSS, "utf8");
    const hex = css.match(/--cm-teal:\s*#([0-9a-f]{6})/i)[1];
    const chan = (i) => parseInt(hex.slice(i * 2, i * 2 + 2), 16) / 255;
    const lin = (c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
    const L = 0.2126 * lin(chan(0)) + 0.7152 * lin(chan(1)) + 0.0722 * lin(chan(2));
    const ratio = 1.05 / (L + 0.05);
    assert.ok(ratio >= 4.5,
      `white on #${hex} is ${ratio.toFixed(2)}:1, below the 4.5:1 AA floor`);
  },

  /* The real cause of "blurry buttons", found only after the first fix did not
     help. The LMS loads Inter at weight 400 only — every other weight reports
     `unloaded` — so a control asking for 600 got Inter 400 smeared into a
     synthetic semibold. Naming Inter in our stack invites that back. */
  async "the font stack excludes the platform's partially-loaded Inter"() {
    // Comments stripped first. The file EXPLAINS the platform's own
    // `font-family: Inter … !important` rule in prose, and scanning the raw
    // text failed on that sentence rather than on any declaration — a check
    // that reads documentation as code.
    const css = readFileSync(CSS, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    const stacks = css.match(/font-family:[^;]+;/g) || [];
    stacks.forEach((s) => {
      assert.doesNotMatch(s, /\bInter\b/,
        `Inter is loaded at 400 only; 600 would be synthesised: ${s.trim()}`);
    });
    assert.ok(stacks.length > 0, "no font stack declared at all");
  },

  /* `lms-main-v1.css` sets font-family on form controls with `!important`, so a
     normal declaration loses no matter how specific. Without this override the
     buttons silently fall back to the platform face. */
  async "form controls override the platform's important font-family"() {
    const css = readFileSync(CSS, "utf8");
    const rule = css.match(
      /\.coursemate-tutor button,[\s\S]{0,200}?\{[^}]*\}/);
    assert.ok(rule, "no control font-family override rule");
    assert.match(rule[0], /font-family:[^;]*!important/,
      "the override must be !important — it is beating one");
    ["button", "input", "select", "textarea"].forEach((el) => {
      assert.match(rule[0], new RegExp(`\\.coursemate-tutor ${el}\\b`),
        `${el} is not covered by the override`);
    });
  },

  /* A unitless 1.2 on 13px is a 15.6px box, which lands the baseline off the
     device-pixel grid on a fractionally-scaled display (1.5× here) and reads as
     soft. Interactive controls state whole pixels. */
  async "interactive controls use whole-pixel line heights"() {
    const css = readFileSync(CSS, "utf8");
    [".cm-tab", ".cm-btn"].forEach((sel) => {
      const rule = css.match(new RegExp(`\\${sel}\\s*\\{[^}]*\\}`))[0];
      const lh = rule.match(/line-height:\s*([^;]+);/);
      assert.ok(lh, `${sel} declares no line-height`);
      assert.match(lh[1].trim(), /^\d+px$/,
        `${sel} line-height "${lh[1].trim()}" is not a whole pixel value`);
    });
  },

  /* The two pills read as one smudged block at 2px. */
  async "the tab pills are separated"() {
    const css = readFileSync(CSS, "utf8");
    const rule = css.match(/\.cm-tabs\s*\{[^}]*\}/)[0];
    const gap = Number(rule.match(/gap:\s*([0-9]+)px/)[1]);
    assert.ok(gap >= 4, `tab gap is ${gap}px — too tight to read as two controls`);
  },

  /* Green means "course-owned / real". A green primary button would read as a
     provenance claim about the student's own answer. */
  async "the self-check button does not borrow the provenance green"() {
    const css = readFileSync(CSS, "utf8");
    const rule = css.match(/\.cm-selfcheck-got\s*\{[^}]*\}/)[0];
    assert.match(rule, /var\(--cm-teal\)/);
    assert.doesNotMatch(rule, /--cm-real/);
  },
};

let pass = 0, fail = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { await fn(); console.log(`  ok   ${name}`); pass++; }
  catch (e) { console.log(`  FAIL ${name}\n       ${e.message}`); fail++; }
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
