/* CourseMate tutor — browser client.
 *
 * Design §3.4 rule 3 (v8): the XBlock mints a token and gets out of the way; this
 * script streams the answer from the CourseMate service directly. No LMS worker is
 * held open for the duration of a generation.
 *
 * EventSource cannot send an Authorization header, so we read the SSE stream with
 * fetch + ReadableStream and parse frames ourselves.
 */
function CourseMateTutor(runtime, element, initArgs) {
  "use strict";

  var root = element.querySelector(".coursemate-tutor") || element;
  var log = root.querySelector(".cm-log");
  var notice = root.querySelector(".cm-notice");
  var form = root.querySelector(".cm-form");
  var input = root.querySelector(".cm-input");
  var sendButton = root.querySelector(".cm-send");

  var newChatButton = root.querySelector(".cm-new-chat");
  var deleteChatButton = root.querySelector(".cm-delete-chat");
  var conversationPicker = root.querySelector(".cm-conversations");
  var clearPracticeButton = root.querySelector(".cm-clear-practice");

  var mintUrl = runtime.handlerUrl(element, "mint");
  var persistUrl = runtime.handlerUrl(element, "persist_turn");
  var recordUrl = runtime.handlerUrl(element, "record_attempt");
  var clearUrl = runtime.handlerUrl(element, "clear_history");
  var newConversationUrl = runtime.handlerUrl(element, "new_conversation");
  var switchConversationUrl = runtime.handlerUrl(element, "switch_conversation");
  var persistPracticeUrl = runtime.handlerUrl(element, "persist_practice");
  var clearPracticeUrl = runtime.handlerUrl(element, "clear_practice");

  var history = (initArgs && initArgs.history) || [];
  var mode = (initArgs && initArgs.mode) || "direct";
  /* E3. `|| []` and `|| ""` throughout: a page rendered by the previous build
   * sends neither key, and must keep working rather than throwing on load. */
  var conversations = (initArgs && initArgs.conversations) || [];
  var activeConversation = (initArgs && initArgs.active_conversation) || "";
  /* E2. The practice run as the server last saw it. */
  var savedPractice = (initArgs && initArgs.practice) || [];

  // Three states that must never be rendered as one generic error, because the
  // difference between "looks broken" and "tells you what is happening" is the
  // difference between a dead demo and a live one (design §5.1).
  var NOTICES = {
    abstained: "That doesn't appear to be covered in this course.",
    preparing: "This course is still being prepared — please try again shortly.",
    unavailable: "The tutor is unavailable right now.",
    rate_limited: "Too many questions just now — give it a moment.",
    /* Says when it comes back, and says nothing about tokens or cost. A student
     * cannot act on either number and a limit with no stated end reads as being
     * cut off for good. "rate_limited" above is minutes; this one is a day, so
     * the two must not sound alike. */
    budget_exceeded: "You've reached today's question limit for this course. It resets at midnight UTC.",
    not_enrolled: "You don't have access to this course's tutor.",
    /* The only notice whose fix is not "wait and retry". Retrying is exactly
     * what a student does when told "Something went wrong", and it cannot work
     * — the session is gone, so every retry mints nothing and fails the same
     * way. Say the thing that ends the loop. */
    unauthenticated: "Your session has expired — reload the page and sign in again.",
    truncated: "That answer was cut short. Try asking for a smaller piece of it.",

    /* --- states the XBLOCK returns, not the service -------------------------
     * The four below come from `tutor_block.py`'s handlers, which have their own
     * error vocabulary and are NOT in the `ErrorCode` enum. That is why they
     * were missing: `test_error_contract.py` checked the enum against this
     * object and had no reason to look at the block's handlers at all. The
     * enum-side wiring was complete and this half was invisible.
     *
     * `disabled` is the one that actually gets seen. `mint()` returns it
     * whenever an author unchecks "enabled" in Studio, which is a normal,
     * deliberate action — and until 2026-08-14 the student was told the tutor
     * was broken rather than switched off. */
    disabled: "The tutor is switched off for this unit.",
    /* The handler is reachable on the LMS route as well as in Studio, so a
     * learner CAN reach it. Says what is true without hinting at what the
     * control does. */
    forbidden: "Only course staff can change these settings.",
    /* Both are "the page sent something the handler could not use", which a
     * student can neither cause nor fix. Reloading is the only useful advice. */
    bad_request: "That request could not be understood — try reloading the page.",
    invalid_mode: "That tutor mode is not one this block supports."
  };

  /* Exam prep reuses every notice above, and overrides the two whose wording
   * would be wrong here — "not covered in this course" is about a lesson, and a
   * revision planner's version of the same state is about the question bank. */
  var PREP_NOTICES = {
    abstained: "There isn't enough in this course's material to plan that reliably.",
    preparing: "Past papers for this course haven't been loaded yet."
  };

  /* Practice needs its own version again. `PREP_NOTICES.abstained` is about the
   * PLANNER running short of material; the practice generator abstains for a
   * different and much more specific reason — it models every question on a real
   * past-paper one, so an outcome with none tagged to it can never produce
   * anything. Silence there reads as a broken button.
   *
   * Hedged on purpose. The service sends `abstained` for two distinct causes —
   * no source question, and two failed generation attempts — and the frame
   * cannot tell them apart. Naming the mechanism is true in both cases; naming
   * the cause outright would be wrong in the second. A distinct error code would
   * let this be exact, and that is a contract change, not a wording one. */
  var PRACTICE_NOTICES = {
    abstained: "No practice question could be built for this outcome. Every practice "
             + "question is modelled on a real past-paper question — most often this "
             + "means none is tagged to this outcome yet."
  };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text) { node.textContent = text; }
    return node;
  }

  /* Empty a container without innerHTML (§10.6 forbids it for anything derived
   * from document text, and using two mechanisms invites the wrong one). Reads
   * `children` from the end so the live collection cannot shift underneath. */
  function clearNode(node) {
    while (node.children.length) {
      node.removeChild(node.children[node.children.length - 1]);
    }
  }

  /* --- answer formatting ---------------------------------------------------
   *
   * The whole answer used to land in one text node. `white-space: pre-wrap`
   * meant line breaks survived, so it was never chaos — but the model emits
   * markdown, and the student read the punctuation raw:
   *
   *     - **Automatic Cohorts**: Learners are automatically assigned to a…
   *
   * **The construct list is measured, not guessed, and it is deliberately
   * short.** Across the 8 answers captured in `eval/reports/` from the
   * configured models:
   *
   *     blank-line paragraph   6/8      IN
   *     - bullet               5/8      IN
   *     **bold**               2/8      IN
   *     1. ordered list        2/8      IN
   *     `inline code`          0/8      out
   *     ``` fenced code        0/8      out
   *     # heading              0/8      out
   *     | table |              0/8      out
   *     [link](url)            0/8      out
   *
   * Anything at 0/8 is a feature for a model nobody here runs, and every one of
   * them is surface that has to be got right for output derived from an
   * untrusted question and semi-trusted documents. If a future model emits one,
   * measure it and then add it — `test_answer_formatting.mjs` has a case per
   * excluded construct asserting it stays literal text, so this list cannot
   * drift open quietly.
   *
   * **Links stay out on their own merits, even if a model starts emitting them.**
   * A model-authored `javascript:` URL is the injection this whole file is
   * careful about, and the answer already carries its sources as citation chips
   * that the service verified. A second, unverified link mechanism beside the
   * verified one is worse than none.
   *
   * **Built as DOM, never as markup.** No `innerHTML`, no string of HTML at any
   * point — §10.6 makes this structural rather than a matter of escaping well,
   * and three other test files already hold the same line. A `<script>` in an
   * answer becomes visible text, because it only ever passes through
   * `textContent`.
   */

  //: The one place the inline rule is written down. `**bold**`, nothing else.
  var BOLD = /\*\*([^*]+)\*\*/g;

  /* Inline text into `node`, with `**bold**` lifted into <strong>.
   *
   * Everything not matched by BOLD is appended as a text node, so an unmatched
   * `**` or a stray `<` is shown, not interpreted. */
  function appendInline(node, text) {
    var last = 0;
    var m;
    BOLD.lastIndex = 0;
    while ((m = BOLD.exec(text)) !== null) {
      if (m.index > last) {
        node.appendChild(document.createTextNode(text.slice(last, m.index)));
      }
      node.appendChild(el("strong", "", m[1]));
      last = m.index + m[0].length;
    }
    if (last < text.length) {
      node.appendChild(document.createTextNode(text.slice(last)));
    }
  }

  var BULLET = /^\s*[-*]\s+(.*)$/;
  var ORDERED = /^\s*\d+\.\s+(.*)$/;

  /* --- constructs the DETERMINISTIC planner emits (2026-08-15) -------------
   *
   * The four rules above were measured over model ANSWERS. These three come
   * from a different producer with a different guarantee: `api/plan.py` writes
   * the revision plan itself, so its markup is not sampled, it is enumerated
   * from the source. That is why they can be added without re-running the
   * chat measurement — there is no model to be unpredictable.
   *
   * Until now the plan reached the student as raw text: `## CLO-1 — …`,
   * `_Your record: not practised yet._`. 14 of its 18 markup constructs were
   * shown literally.
   */
  var HEADING = /^(#{1,3})\s+(.*)$/;

  /* **Whole-line italics, deliberately not inline.**
   *
   * An inline `_…_` rule is unsafe here in both directions, which is why it was
   * measured before choosing:
   *
   *   `COURSEMATE_MODEL_API_BASE`  a naive rule italicises `_MODEL_` and eats
   *                               the underscores — the tutor mangles its own
   *                               configuration advice
   *   `_Source: oex101_final_2024.pdf, p.2_`
   *                               a word-boundary rule refuses to match at all,
   *                               because the FILENAME contains underscores, so
   *                               the plan's own markup stays visible
   *
   * A line that both opens and closes with `_` has neither problem: the
   * filename's underscores are interior and never considered. It also matches
   * how the planner actually writes — every italic it emits is a whole line.
   *
   * (That the plan's markup collides with its own data is the strongest
   * argument for carrying it as structure rather than as a string. Noted here
   * because this is where the collision is visible.) */
  var LINE_ITALIC = /^_(.+)_$/;

  //: A `  _Source: …_` line belongs to the bullet above it, not to a new block.
  var INDENTED = /^\s{2,}\S/;

  /* Render `text` into `container`, replacing whatever was there.
   *
   * Called on EVERY token rather than appending, so a partial `**bo` is never
   * rendered as bold and then reflowed when the closing `**` arrives. The cost
   * is a re-parse per token over an answer of 255–811 characters (ADR-0001),
   * which is not worth optimising ahead of a measurement saying it is.
   *
   * Used by BOTH the live stream and `renderHistory`. Formatting only the live
   * path would mean a page reload changed how an answer looks — and this file
   * has been bitten twice by exactly that shape, with citations and with
   * unsupported-claim marks. */
  function renderAnswer(container, text) {
    clearNode(container);
    container.textContent = "";
    if (!text) { return; }

    var lines = String(text).split("\n");
    var i = 0;
    var para = null;

    /* `childNodes`, NOT `children`. `children` counts ELEMENT children only,
     * and `appendInline` emits a text node for anything that is not `**bold**`
     * — so a paragraph of plain prose had zero element children and was
     * silently dropped. Every answer whose prose happened to contain bold
     * survived, which is why this went unseen: the deterministic outline
     * answer quotes the course author verbatim, contains no markup at all, and
     * rendered as bare headings with the body missing.
     *
     * The test double hid it too. Its `appendChild` pushed text nodes into
     * `children`, so the fake DOM disagreed with the real one about the one
     * property this line reads. */
    function closePara() {
      if (para && para.childNodes.length) { container.appendChild(para); }
      para = null;
    }

    while (i < lines.length) {
      var line = lines[i];

      if (!line.trim()) { closePara(); i++; continue; }

      /* Headings. `h4`, not `h1`: this renders inside a chat bubble in an LMS
       * unit that already owns the page outline, and emitting a top-level
       * heading from a component would corrupt the document structure a screen
       * reader navigates by. */
      var heading = line.match(HEADING);
      if (heading) {
        closePara();
        container.appendChild(el("h4", "cm-answer-h", heading[2].trim()));
        i++;
        continue;
      }

      /* A whole line wrapped in underscores — the planner's `_Your record: …_`.
       * Its own block rather than an inline run, because that is how it is
       * written and because the inline form is unsafe (see LINE_ITALIC). */
      var lineItalic = line.trim().match(LINE_ITALIC);
      if (lineItalic) {
        closePara();
        var note = el("p", "cm-answer-note");
        note.appendChild(el("em", "", lineItalic[1].trim()));
        container.appendChild(note);
        i++;
        continue;
      }

      /* A run of adjacent bullets is ONE list. Checked before the paragraph
       * case so a list interrupting a paragraph starts a list rather than
       * becoming a line inside it. */
      var isBullet = BULLET.test(line);
      var isOrdered = !isBullet && ORDERED.test(line);
      if (isBullet || isOrdered) {
        closePara();
        var list = el(isBullet ? "ul" : "ol", "cm-answer-list");
        var rule = isBullet ? BULLET : ORDERED;
        while (i < lines.length && rule.test(lines[i])) {
          var item = el("li");
          appendInline(item, lines[i].match(rule)[1]);
          i++;
          /* Indented lines after a bullet are that bullet's own — the plan puts
           * `_Source: …_` and the low-confidence warning there. Rendered inside
           * the <li> so provenance stays attached to the question it belongs
           * to; as a sibling block it would read as applying to the whole list. */
          while (i < lines.length && INDENTED.test(lines[i]) && !rule.test(lines[i])) {
            var subText = lines[i].trim();
            var subItalic = subText.match(LINE_ITALIC);
            var sub = el("div", "cm-answer-subline");
            if (subItalic) {
              sub.appendChild(el("em", "", subItalic[1].trim()));
            } else {
              appendInline(sub, subText);
            }
            item.appendChild(sub);
            i++;
          }
          list.appendChild(item);
        }
        container.appendChild(list);
        continue;
      }

      if (!para) { para = el("p", "cm-answer-p"); }
      /* A single newline inside a paragraph is a soft wrap, not a break: the
       * model wraps prose and `pre-wrap` used to make those look deliberate.
       *
       * `childNodes` for the same reason as `closePara` above — this asks "has
       * anything been written yet", and with `children` a first line of plain
       * prose counted as nothing, so the separating space was skipped and
       * "continues
on" rendered as "continueson". Same one-word mistake, same
       * hiding place in the test double. */
      if (para.childNodes.length) {
        para.appendChild(document.createTextNode(" "));
      }
      appendInline(para, line.trim());
      i++;
    }
    closePara();
  }

  function showNotice(code) {
    notice.textContent = NOTICES[code] || "Something went wrong.";
    notice.className = "cm-notice " + code;
    notice.hidden = false;
  }

  function clearNotice() { notice.hidden = true; }

  /* Citation URLs arrive from the service, which builds them from retrieved
   * content. §10.6 treats document text as semi-trusted data, so a URL derived
   * from it is not trusted to be safe: a "javascript:" href would execute in the
   * student's session. Only same-origin relative paths and http(s) are allowed. */
  function safeHref(url) {
    if (typeof url !== "string" || !url) { return "#"; }
    if (url.charAt(0) === "/" && url.charAt(1) !== "/") { return url; }
    try {
      var parsed = new URL(url, window.location.origin);
      return (parsed.protocol === "http:" || parsed.protocol === "https:") ? parsed.href : "#";
    } catch (e) {
      return "#";
    }
  }

  /* --- math -----------------------------------------------------------------
   *
   * Course content carries TeX in Open edX's own delimiters. Measured in the
   * live index: 6 active chunks hold `\(…\)` or `\[…\]`, and `Design a Logic
   * Gate` alone contributes three — `\(Z = \lnot{(C(A+B))}\)`, `\(R_{ON}\)`,
   * `\[\frac{V_{DD}\cdot R_{EQ}}{…}\]`. The model quotes that notation back
   * because SYSTEM_GROUNDED tells it to prefer the course's own terminology, so
   * TeX in an answer is faithful, not a formatting mistake.
   *
   * `renderAnswer` builds text nodes and nothing else, so until now the student
   * read the backslashes. Nothing needed to be parsed to fix that: the host page
   * already loads MathJax 2.7.5, configured with `inlineMath [["\\(","\\)"]]`
   * and `displayMath [["\\[","\\]"]]` — exactly what arrives. It simply typesets
   * the document once at load, and this answer is injected long afterwards. The
   * missing piece was the hand-off, not a renderer.
   *
   * Absent MathJax (it comes from a CDN) the answer stays as it reads today.
   * A detached node is skipped too: `settle()` removes the turn when nothing was
   * produced, so an abstention has nothing left to typeset. */
  function typesetMath(node) {
    var MJ = window.MathJax;
    if (!node || !node.parentNode) { return; }
    if (!MJ || !MJ.Hub || typeof MJ.Hub.Queue !== "function") { return; }
    MJ.Hub.Queue(["Typeset", MJ.Hub, node], function () { sanitizeMath(node); });
  }

  /* **Why typesetting alone is not safe here, measured on this deployment.**
   *
   * MathJax's own `Safe` extension is NOT loaded — `MathJax.Extension.Safe` is
   * undefined and `Safe.js` is absent from all 33 files the hub fetched. Only
   * `noUndefined.js` is present, and that is cosmetic. Probed against the live
   * page, TeX therefore reaches the DOM with its attributes intact:
   *
   *     \href{javascript:alert(1)}{X}   ->  <a href="javascript:alert(1)"> x3
   *     \style{background:url(http://…)} ->  style="background: url("http://…")"
   *     \cssId{pwn}{x}                   ->  id="pwn"
   *
   * Answer text is model output shaped by uploaded documents and by the
   * student's own question — semi-trusted and untrusted under §10.6 — so that is
   * the same script-injection path the no-innerHTML rule exists to close, and
   * handing it to a TeX interpreter unguarded would reopen it.
   *
   * The fix is scoped to OUR node rather than to MathJax's configuration.
   * `MathJax.Hub.Config({extensions:["Safe.js"]})` is one line, but it is global:
   * it would also silently disarm `\href` inside every capa problem on the page,
   * in courses this block does not own. So the output is whitelisted instead —
   * a whitelist on produced attributes, not a blacklist of macros, which is what
   * makes it complete rather than a list to keep extending.
   *
   * `safeHref` is reused deliberately: citation chips and math links must not
   * disagree about which URLs are allowed.
   *
   * Residual and accepted: `\cssId`/`\class` can still inject an id or a class.
   * Neither executes anything nor loads anything; the worst case is a duplicate
   * id inside an answer bubble. Named here so it is a decision, not an oversight. */
  var MATH_HREF_ATTRS = ["href", "xlink:href"];

  function sanitizeMath(node) {
    if (!node || node.nodeType !== 1) { return; }
    var i;
    for (i = 0; i < MATH_HREF_ATTRS.length; i++) {
      var name = MATH_HREF_ATTRS[i];
      var url = node.getAttribute(name);
      if (url !== null && url !== undefined && safeHref(url) === "#") {
        node.removeAttribute(name);
      }
    }
    /* `url(...)` is the only thing in a style attribute that reaches the network,
     * and a background beacon in an answer is a disclosure, not a nuisance. */
    var style = node.getAttribute("style");
    if (style && style.toLowerCase().indexOf("url(") !== -1) {
      node.removeAttribute("style");
    }
    var kids = node.childNodes || [];
    for (i = 0; i < kids.length; i++) { sanitizeMath(kids[i]); }
  }

  /* One place that builds a citation chip, used by both the live stream and the
   * reloaded history — so a persisted answer looks identical to a fresh one. */
  function citationNode(citation) {
    var wrap = el("span", "cm-citation");
    var link = el("a", null, citation.display_name || citation.usage_key);
    link.href = safeHref(citation.url);
    wrap.appendChild(link);
    return wrap;
  }

  /* Citations collect into ONE labelled row rather than a stack of "Source:"
   * lines. Created on first use, so an answer with no citations shows no empty
   * row — an answer that cited nothing must not look like one that cited. */
  function sourcesRow(container) {
    var row = container.querySelector(".cm-sources");
    if (!row) {
      row = el("div", "cm-sources");
      row.appendChild(el("span", "cm-sources-label", "Sources"));
      container.appendChild(row);
    }
    return row;
  }

  /* A turn is an avatar plus a bubble, and the streamed text goes in a child of
   * the bubble rather than the bubble itself. That is load-bearing: the token
   * handler assigns `textContent` on every frame, and assigning it to a node
   * that also holds citations would delete them on the next token. */
  function turnNode(role, content) {
    var node = el("div", "cm-turn " + role);
    if (role === "tutor") { node.appendChild(el("span", "cm-avatar-sm", "CT")); }
    var bubble = el("div", "cm-bubble");
    var answer = el("div", "cm-answer");
    /* The TUTOR's turn is model output and gets formatted; the student's is the
     * student's own words and is left exactly as typed. Formatting a question
     * would rewrite what someone wrote — a student who types `**` meant `**`. */
    if (role === "tutor") {
      renderAnswer(answer, content || "");
    } else {
      answer.textContent = content || "";
    }
    bubble.appendChild(answer);
    node.appendChild(bubble);
    return node;
  }

  function bubbleOf(node) { return node.querySelector(".cm-bubble") || node; }
  function answerOf(node) { return node.querySelector(".cm-answer") || node; }

  function renderHistory() {
    clearNode(log);
    history.forEach(function (turn) {
      var node = turnNode(turn.role, turn.content);
      var bubble = bubbleOf(node);
      /* Marks first, so a doubtful sentence is flagged above its sources rather
       * than below them. Persisted with the turn since 2026-08-12: before that a
       * refresh dropped the warning and kept the sentence, so a reloaded answer
       * read as MORE trustworthy than the live one. `|| []` is the
       * compatibility path — turns written earlier have no such key. */
      (turn.unsupported || []).forEach(function (text) {
        bubble.appendChild(el("div", "cm-unsupported", text));
      });
      /* Citations are persisted with the turn, so a reloaded answer keeps its
       * sources. Before this they existed only during the live stream, and a
       * refresh silently stripped them — which undercuts the whole point of a
       * tutor that cites. */
      (turn.citations || []).forEach(function (c) {
        sourcesRow(bubble).appendChild(citationNode(c));
      });
      log.appendChild(node);
      /* Typeset only once the turn is IN the document. MathJax measures layout
       * as it typesets and a detached subtree measures against nothing, so this
       * cannot move up into `turnNode`. Tutor turns only: a student's question
       * is their own words, and is not formatted for the same reason. */
      if (turn.role === "tutor") { typesetMath(answerOf(node)); }
    });
    log.scrollTop = log.scrollHeight;
    /* Owned here rather than at each call site: this function already runs at
     * every moment `history.length` can change — on init, after a question is
     * pushed, and after a clear. */
    if (newChatButton) { newChatButton.hidden = history.length === 0; }
    /* Same rule, same reason: nothing to delete on an empty conversation. */
    if (deleteChatButton) { deleteChatButton.hidden = history.length === 0; }
  }

  /* E3. One entry per conversation, newest last, the active one selected.
   * Hidden below two, because a picker with a single option is a control that
   * cannot do anything. */
  function renderConversations() {
    if (!conversationPicker) { return; }
    clearNode(conversationPicker);
    conversations.forEach(function (c) {
      var opt = el("option", "", c.title + (c.turns ? " (" + c.turns + ")" : ""));
      opt.value = c.id;
      if (c.id === activeConversation) { opt.selected = true; }
      conversationPicker.appendChild(opt);
    });
    conversationPicker.value = activeConversation;
    conversationPicker.hidden = conversations.length < 2;
  }

  /* Starts a fresh conversation, KEEPING the previous one.
   *
   * The handler existed from the beginning and nothing ever called it. E1 wired
   * this button to `clear_history`, which destroyed the turns; with E3 the older
   * conversation stays and becomes resumable from the picker, so nothing is
   * lost. `clear_history` remains for deleting one deliberately, and is scoped
   * to the active conversation.
   *
   * Chat only, in every version of this. Mastery is a different lifetime in a
   * different table — a student starting a new chat has not un-practised
   * anything, and wiping their recorded attempts would be a loss they never
   * asked for. */
  if (newChatButton) {
    newChatButton.addEventListener("click", function () {
      newChatButton.disabled = true;
      fetch(newConversationUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: platformHeaders(),
        body: JSON.stringify({})
      }).then(function (r) {
        return r.ok ? r.json() : {};
      }).then(function (result) {
        /* Only switch once the platform confirms. Clearing the page while the
         * server still held the old conversation would show an empty chat that
         * the next reload undid. */
        if (result && result.conversation_id) {
          activeConversation = result.conversation_id;
          conversations = result.conversations || conversations;
          history.length = 0;
          clearNotice();
          renderHistory();
          renderConversations();
        }
        newChatButton.disabled = false;
      }).catch(function () {
        newChatButton.disabled = false;
      });
    });
  }

  /* Delete the conversation in front of you.
   *
   * This is what `clear_history` is for, and wiring it is not optional: the
   * handler existed unreachable from the block's creation until E1, E3 moved the
   * New Chat button off it, and it went dead again in the same file for the same
   * reason. A handler nothing can call is a feature that does not exist.
   *
   * The ACTIVE conversation only, and never mastery — both asserted server-side
   * and pinned by tests, because "delete" is the one word here a student cannot
   * undo. */
  if (deleteChatButton) {
    deleteChatButton.addEventListener("click", function () {
      deleteChatButton.disabled = true;
      fetch(clearUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: platformHeaders(),
        body: JSON.stringify({})
      }).then(function (r) {
        return r.ok ? r.json() : { cleared: false };
      }).then(function (result) {
        /* Only drop the local copy once the platform confirms its own is gone.
         * Clearing optimistically would show an empty page while the server
         * still held the turns, and the next reload would bring them back. */
        if (result && result.cleared) {
          history.length = 0;
          clearNotice();
          renderHistory();
        }
        deleteChatButton.disabled = false;
      }).catch(function () {
        deleteChatButton.disabled = false;
      });
    });
  }

  /* Resume a conversation. The server returns its turns, so the client never
   * has to hold every conversation in memory to switch between them. */
  if (conversationPicker) {
    conversationPicker.addEventListener("change", function () {
      var wanted = conversationPicker.value;
      /* Compared against null/undefined, NOT falsiness. The legacy conversation
       * — every turn written before E3 — has the id `""`, so `!wanted` would
       * treat selecting it as "nothing selected" and a student could never get
       * back to the history they already had. */
      if (wanted === null || wanted === undefined || wanted === activeConversation) {
        return;
      }
      conversationPicker.disabled = true;
      fetch(switchConversationUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: platformHeaders(),
        body: JSON.stringify({ conversation_id: wanted })
      }).then(function (r) {
        return r.ok ? r.json() : {};
      }).then(function (result) {
        /* Presence, not truthiness — the legacy conversation's id is `""`, so a
         * truthy check would silently refuse to switch to the one conversation
         * every existing student already has. */
        if (result && typeof result.conversation_id === "string") {
          activeConversation = result.conversation_id;
          conversations = result.conversations || conversations;
          history.length = 0;
          (result.history || []).forEach(function (t) { history.push(t); });
          clearNotice();
          renderHistory();
          renderConversations();
        } else {
          /* Put the control back where it was rather than leaving it showing a
           * conversation that is not open. */
          renderConversations();
        }
        conversationPicker.disabled = false;
      }).catch(function () {
        renderConversations();
        conversationPicker.disabled = false;
      });
    });
  }

  /* The model needs role and content; it has no use for citations, and Turn in
   * the shared contract carries only those two fields. Stripping here keeps the
   * request payload matching the contract rather than relying on the server to
   * ignore extra keys. */
  function historyForRequest() {
    return history.slice(-10).map(function (t) {
      return { role: t.role, content: t.content };
    });
  }

  function busy(state) {
    input.disabled = state;
    sendButton.disabled = state;
  }

  /* --- the waiting state ---------------------------------------------------
   *
   * `busy()` above disabled the input and the button and did nothing else, so
   * between pressing Ask and the first token the student watched an EMPTY grey
   * bubble with no sign the tutor was working. Time to first token, measured in
   * this repo: 3,512 ms on the hosted primary (ADR-0001), and 9.7 s / 24 s /
   * 106.3 s on the local model (BENCHMARKS §132, §266). A minute and a half of
   * blank bubble reads as broken, and the student's only move is to reload —
   * which throws away the generation they were waiting for.
   *
   * **Two states, because two are all the client can actually observe.** The
   * server does retrieval, the gate and the provider call between the request
   * and the first frame, and none of that is visible from here. Inventing a
   * third label ("generating…") would be a guess presented as a status, which
   * is the failure this project keeps naming. So:
   *
   *     CONNECTING  ask() -> mint returns          (an LMS handler round trip)
   *     SEARCHING   stream opened -> first frame   (retrieval + gate + model)
   *
   * **No aria-live here, deliberately.** The indicator is appended inside
   * `.cm-log`, which already carries `role="log" aria-live="polite"`. A nested
   * live region inside another one is not additive — implementations differ on
   * which wins, and the usual result is double or dropped announcements.
   */
  var THINKING_SLOW_MS = 10000;

  function thinkingNode() {
    var node = el("div", "cm-thinking");
    /* aria-hidden: the dots are decoration. The label beside them is the part
     * worth announcing, and the log's live region carries it. */
    var dots = el("span", "cm-thinking-dots");
    dots.setAttribute("aria-hidden", "true");
    dots.appendChild(el("i"));
    dots.appendChild(el("i"));
    dots.appendChild(el("i"));
    node.appendChild(dots);
    node.appendChild(el("span", "cm-thinking-label", "Connecting…"));
    return node;
  }

  /* A live generation, or null. Holds the timer so it can be cleared on every
   * exit path — a timer that outlives its node is how an indicator ends up
   * writing into a detached element. */
  function startThinking(bubble) {
    var node = thinkingNode();
    var label = node.querySelector(".cm-thinking-label");
    bubble.appendChild(node);

    /* A static label held for 106 seconds still reads as hung. This says the
     * wait is expected without promising a time nothing here can predict. */
    var slowTimer = window.setTimeout(function () {
      node.appendChild(
        el("span", "cm-thinking-slow", "still working — this can take a minute")
      );
    }, THINKING_SLOW_MS);

    return {
      searching: function () { label.textContent = "Searching this course…"; },
      stop: function () {
        window.clearTimeout(slowTimer);
        if (node.parentNode) { node.parentNode.removeChild(node); }
      }
    };
  }

  /* Django rejects a POST without a CSRF token and returns an HTML error page,
   * which then fails to parse as JSON — surfacing as the misleading
   * "Unexpected token '<'". Read the token the platform already set. */
  function csrfToken() {
    var match = document.cookie.match(/(^|;\s*)csrftoken=([^;]*)/);
    return match ? decodeURIComponent(match[2]) : "";
  }

  function platformHeaders() {
    return {
      "Content-Type": "application/json",
      "X-CSRFToken": csrfToken()
    };
  }

  function mintToken() {
    return fetch(mintUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: platformHeaders(),
      body: JSON.stringify({})
    }).then(function (r) {
      if (!r.ok) { throw new Error("mint failed: HTTP " + r.status); }
      return r.json();
    });
  }

  /* Parse an SSE byte stream into frames, invoking onFrame per event. */
  function readStream(response, onFrame) {
    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";

    function pump() {
      return reader.read().then(function (result) {
        if (result.done) { return; }
        buffer += decoder.decode(result.value, { stream: true });
        var parts = buffer.split("\n\n");
        buffer = parts.pop();
        parts.forEach(function (block) {
          block.split("\n").forEach(function (line) {
            if (line.indexOf("data:") !== 0) { return; }
            var payload = line.slice(5).trim();
            if (!payload) { return; }
            /* Only the PARSE may fail silently — a keep-alive or a partial
             * line is not an error. Wrapping the HANDLER in the same catch
             * swallowed rendering exceptions, so a bug in a frame branch left a
             * half-drawn card on screen with nothing logged anywhere. Found by
             * the JS harness, where a missing DOM method vanished without trace. */
            var frame;
            try { frame = JSON.parse(payload); } catch (e) { return; }
            onFrame(frame);
          });
        });
        return pump();
      });
    }
    return pump();
  }

  function ask(question) {
    clearNotice();
    busy(true);

    history.push({ role: "student", content: question });
    renderHistory();

    var answerNode = turnNode("tutor", "");
    var answerBubble = bubbleOf(answerNode);
    var answerText = answerOf(answerNode);
    log.appendChild(answerNode);
    var answer = "";
    var citations = [];
    /* Collected so the marks can be persisted with the turn, not just drawn
     * once. See renderHistory. */
    var unsupported = [];

    var thinking = startThinking(answerBubble);

    /* Every exit runs through here, and that is the whole point. An indicator
     * removed on the happy path and left spinning on a failure is worse than
     * none: it says the tutor is still working when it has already given up.
     *
     * It also drops the turn's node when nothing was produced. The empty bubble
     * was already being orphaned on these paths before the indicator existed —
     * a failed ask left a blank grey bubble in the log forever — and removing
     * only the dots would have made that more visible, not less. */
    function settle() {
      thinking.stop();
      if (!answer && answerNode.parentNode) {
        answerNode.parentNode.removeChild(answerNode);
      }
      busy(false);
    }

    mintToken().then(function (token) {
      if (token.error) { showNotice(token.error); settle(); return; }

      return fetch(token.stream_path, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + token.token
        },
        body: JSON.stringify({
          question: question,
          history: historyForRequest(),
          mode: mode
        })
      }).then(function (response) {
        if (!response.ok || !response.body) {
          showNotice("unavailable");
          settle();
          return;
        }
        /* The stream is open, so the LMS hop is done and the wait is now the
         * service: retrieval, the gate, then the model. */
        thinking.searching();
        return readStream(response, function (frame) {
          switch (frame.type) {
            case "token":
              /* First text: the wait is over even if more is coming. */
              thinking.stop();
              answer += frame.text || "";
              renderAnswer(answerText, answer);
              log.scrollTop = log.scrollHeight;
              break;
            case "citation":
              citations.push(frame.citation);
              sourcesRow(answerBubble).appendChild(citationNode(frame.citation));
              break;
            case "unsupported_claim":
              // Mark it; never silently rewrite text the student already read.
              unsupported.push(frame.text || "");
              answerBubble.appendChild(el("div", "cm-unsupported", frame.text || ""));
              break;
            case "degraded":
              // An outage must not read as "the tutor got worse this week".
              answerBubble.appendChild(
                el("div", "cm-degraded", "Answered by a fallback model (" + (frame.provider || "") + ")")
              );
              break;
            case "incomplete":
              // Distinct from "degraded", which says a different MODEL answered.
              // This says the EVIDENCE was incomplete, which is the more serious
              // of the two and the one a student should weigh.
              answerBubble.appendChild(
                el("div", "cm-incomplete",
                   "Some information could not be checked (" + (frame.text || "") + ").")
              );
              break;
            case "error":
              // Includes ABSTAINED and PREPARING, which arrive with no tokens
              // at all — so this is the ONLY thing that stops the indicator on
              // the two most common non-answers.
              thinking.stop();
              showNotice(frame.error_code || "unavailable");
              break;
            case "done":
              // A cut-off answer looks identical to a complete one — it just
              // stops, and the student reads that as the tutor not knowing the
              // rest. Say which it was.
              thinking.stop();
              if (frame.truncated) { showNotice("truncated"); }
              break;
          }
        }).then(function () {
          settle();
          if (!answer) { return; }
          /* Here rather than in `case "done"`, for two reasons that are not
           * stylistic. `settle()` has just removed the turn if nothing was
           * produced, so an abstention — which arrives as an `error` frame with
           * no tokens — cannot reach a typeset at all; putting the call in the
           * frame switch would race that removal. And a stream that drops
           * without a closing `done` still leaves a partial answer on screen,
           * which should read the same as any other.
           *
           * `renderAnswer` runs on EVERY token and opens with `clearNode`, so
           * this must never be per-token: MathJax's queue is asynchronous, and a
           * job queued for token N lands after token N+1 has already destroyed
           * the nodes it was given. Once, when the text stops changing. */
          typesetMath(answerText);
          history.push({
            role: "tutor", content: answer,
            citations: citations, unsupported: unsupported
          });
          // Persist through the platform, which owns conversation state (§3.1).
          fetch(persistUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: platformHeaders(),
            body: JSON.stringify({
              question: question, answer: answer,
              citations: citations, unsupported: unsupported
            })
          });
        });
      });
    }).catch(function () {
      // The last exit path: a rejected mint, a dropped connection, a read that
      // threw mid-stream. Without this the indicator outlives the request that
      // owns it and spins until the page is reloaded.
      showNotice("unavailable");
      settle();
    });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var question = input.value.trim();
    if (!question) { return; }
    input.value = "";
    ask(question);
  });

  renderHistory();
  renderConversations();

  /* ------------------------------------------------------------------ *
   * Exam prep (Feature B). Present only when the instructor enabled the
   * tab, so everything below is guarded on the panel existing.
   * ------------------------------------------------------------------ */

  var prepPanel = root.querySelector('.cm-panel[data-panel="prep"]');
  if (!prepPanel) { return; }

  /* Queried from the root, not the panel: the status line lives in the app bar
   * so the panel below can start with content instead of a status message. */
  var prepStatus = root.querySelector(".cm-prep-status");
  var chatSubline = root.querySelector(".cm-chat-subline");
  var prepLog = prepPanel.querySelector(".cm-prep-log");
  /* Dedicated slots, so a new question REPLACES the last one instead of piling
   * up in a shared log. Falling back to the log keeps the script working
   * against a page rendered before these existed. */
  var practiceSlot = prepPanel.querySelector(".cm-practice-slot");
  var planSlot = prepPanel.querySelector(".cm-plan-slot");
  /* The prose planner's slot. It was the one output in this panel writing
   * straight into the shared log, so it was the one that piled up: two requests
   * left two identical plans, which reads as a single response rendered twice.
   * Null on a page rendered before this slot existed, and `slotTarget` falls
   * back to the log for exactly that case. */
  var prosePlanSlot = prepPanel.querySelector(".cm-prose-plan-slot");

  /* Only a real slot is cleared. The fallback is the shared log, and emptying
   * that would delete a plan because a question was generated next to it. */
  function slotTarget(slot) {
    if (!slot) { return prepLog; }
    clearNode(slot);
    return slot;
  }

  /* The practice slot is a RUN, not a single value, so it appends where
   * `slotTarget` replaces. The empty state is the one thing that must go, and
   * only once: it is a placeholder for "no question yet", not a card.
   *
   * Deliberately NOT applied to the plan slots. A plan is a value — asking for
   * a second one means the first is superseded — and two plans on screen was a
   * real defect this file already fixed once. */
  function practiceTarget() {
    if (!practiceSlot) { return prepLog; }
    if (practiceSlot.querySelector(".cm-empty")) { clearNode(practiceSlot); }
    return practiceSlot;
  }

  /* Offered only while there is a run to end. Called wherever the number of
   * cards can change — after a generation, after a restore, after a clear. */
  function syncPracticeTools() {
    if (!clearPracticeButton) { return; }
    var has = !!(practiceSlot && practiceSlot.querySelector(".cm-practice-card"));
    clearPracticeButton.hidden = !has;
  }

  function emptyState(slot, icon, title, text, extra) {
    if (!slot) { return; }
    clearNode(slot);
    var box = el("div", "cm-empty");
    if (icon) { box.appendChild(el("div", "cm-empty-icon", icon)); }
    if (extra) { box.appendChild(el("div", extra)); }
    box.appendChild(el("div", "cm-empty-title", title));
    box.appendChild(el("div", "cm-empty-text", text));
    slot.appendChild(box);
  }

  /* First run. A panel of disabled controls with no output reads as broken;
   * these say what each control will do before it has done it.
   *
   * The practice copy promises a labelled generated question and nothing more,
   * because that is all `/practice/stream` can produce — every question it
   * returns is `ai_generated=True`, modelled on a real past-paper question
   * rather than quoted from one. Promising a verbatim past paper here would be
   * a UI claim the service cannot keep. */
  emptyState(practiceSlot, "?", "No question yet",
    "Pick an outcome and generate a question. Every one is modelled on a real "
    + "past-paper question and clearly labelled as AI-generated.");
  emptyState(planSlot, null, "No study plan yet",
    "Set a session length in marks and build one. It allocates real past-paper "
    + "questions across the outcomes you still need.", "cm-empty-bar");
  var prepNotice = prepPanel.querySelector(".cm-prep-notice");
  var prepForm = prepPanel.querySelector(".cm-prep-form");
  var practiceForm = prepPanel.querySelector(".cm-practice-form");
  var practiceClo = prepPanel.querySelector(".cm-practice-clo");
  var practiceBand = prepPanel.querySelector(".cm-practice-band");
  var practiceSend = prepPanel.querySelector(".cm-practice-send");
  var prepInput = prepPanel.querySelector(".cm-prep-input");
  var prepSend = prepPanel.querySelector(".cm-prep-send");
  var budgetForm = prepPanel.querySelector(".cm-budget-form");
  var budgetInput = prepPanel.querySelector(".cm-budget-input");
  var budgetSend = prepPanel.querySelector(".cm-budget-send");

  /* The memory layer, carried rather than stored (§3.1). The platform owns it;
   * this script is the courier, exactly as it is for chat history. A student can
   * edit it in their own browser — what that buys them is worse study
   * recommendations for themselves, and the service re-checks the offering
   * against the token before it shapes anything. */
  var mastery = (initArgs && initArgs.mastery) || null;

  root.querySelectorAll(".cm-tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      var wanted = tab.getAttribute("data-panel");
      root.querySelectorAll(".cm-tab").forEach(function (t) {
        var on = t === tab;
        t.classList.toggle("is-active", on);
        t.setAttribute("aria-selected", on ? "true" : "false");
      });
      root.querySelectorAll(".cm-panel").forEach(function (p) {
        p.hidden = p.getAttribute("data-panel") !== wanted;
      });
      /* One sub-line per tab. Both live in the app bar, so exactly one is
       * visible at a time rather than both stacking. */
      if (chatSubline) { chatSubline.hidden = wanted !== "chat"; }
      if (prepStatus) { prepStatus.hidden = wanted !== "prep"; }
      if (wanted === "prep") { loadPrepStatus(); }
    });
  });

  function showPrepNotice(code, context) {
    var specific = context === "practice" ? PRACTICE_NOTICES[code] : null;
    prepNotice.textContent =
      specific || PREP_NOTICES[code] || NOTICES[code] || "Something went wrong.";
    prepNotice.className = "cm-prep-notice " + code;
    prepNotice.hidden = false;
  }

  /* Ask the service what it can actually offer BEFORE enabling the form. A tab
   * that renders an input which turns out to do nothing is the failure §5.1
   * describes: it looks broken rather than telling you what is happening. */
  var statusLoaded = false;
  function loadPrepStatus() {
    if (statusLoaded) { return; }
    statusLoaded = true;

    mintToken().then(function (token) {
      if (token.error) { prepStatus.textContent = NOTICES[token.error] || ""; return; }
      var base = token.stream_path.replace(/\/chat$/, "/examprep");
      prepPanel.dataset.base = base;

      return fetch(base + "/status", {
        headers: { Authorization: "Bearer " + token.token }
      }).then(function (r) { return r.ok ? r.json() : null; }).then(function (status) {
        if (!status) { prepStatus.textContent = NOTICES.unavailable; return; }
        /* The panel already fetches this; it just ignored the flag. With the
         * agent off the plan is deterministic, so it can be asked for as DATA
         * rather than as prose — see requestPlan. */
        prepPanel.dataset.agent = status.agent_available ? "1" : "0";
        if (!status.pack_loaded) {
          prepStatus.textContent = PREP_NOTICES.preparing;
          return;
        }
        var parts = [[status.questions, "past-paper questions"],
                     [status.clos, "learning outcomes"]];
        if (status.earliest_year && status.latest_year) {
          parts.push([status.earliest_year + "–" + status.latest_year, ""]);
        }
        /* Soft spots are shown, not hidden. A student who knows some items were
         * hard to extract can discount them; one who does not will read every
         * one as exact. */
        if (status.low_confidence) {
          parts.push([status.low_confidence, "flagged for low extraction confidence"]);
        }
        /* The figures carry the information and the nouns are scaffolding, so
         * the figures are the part that is emphasised. Built as nodes rather
         * than one string for that reason — and with textContent throughout,
         * because `status` is derived from extracted PDF text (§10.6). */
        prepStatus.textContent = "";
        clearNode(prepStatus);
        parts.forEach(function (part, i) {
          if (i) { prepStatus.appendChild(el("span", "cm-stat-sep", "·")); }
          prepStatus.appendChild(el("b", "cm-stat", String(part[0])));
          if (part[1]) {
            prepStatus.appendChild(document.createTextNode(" " + part[1]));
          }
        });
        prepForm.hidden = false;

        /* The outcome selector. Only enabled when the course actually declares
         * outcomes — an empty dropdown is a control that cannot work, which is
         * the failure §5.1 is about. */
        /* The budget form needs a pack, not outcomes: the planner reports an
         * empty plan honestly when nothing is tagged, which is more use to a
         * student than a hidden control that never explains itself. */
        if (budgetForm) { budgetForm.hidden = false; }

        var options = status.clo_options || [];
        if (options.length && practiceForm) {
          options.forEach(function (c) {
            var opt = document.createElement("option");
            opt.value = c.clo_id;
            /* §7.3: an unconfirmed outcome is usable but must not be presented
             * as the instructor's. Marked, not hidden. */
            opt.textContent = c.clo_id + " — " + c.text + (c.confirmed ? "" : "  (unconfirmed)");
            practiceClo.appendChild(opt);
          });
          practiceForm.hidden = false;
        }
      });
    }).catch(function () { prepStatus.textContent = NOTICES.unavailable; });
  }

  /* --- the deterministic plan, rendered from DATA -------------------------
   *
   * The same plan used to arrive as markdown inside text tokens and be parsed
   * back into structure here. It now arrives as a `RevisionPlan`, so there is
   * no markup to parse and no chance of the markup colliding with the data —
   * which it did: `_Source: oex101_final_2024.pdf, p.2_` cannot be told from
   * its own italics, because the filename contains underscores.
   *
   * Every element the prose version carried is carried here: the outcome
   * heading, the record line, question text, the marks/year/exam metadata, the
   * source filename and page, the low-confidence warning, the empty-outcome
   * case, and the order — which is the planner's advice, so it is rendered as
   * given and never re-sorted.
   */
  function planQuestionNode(q) {
    var item = el("li");
    var bits = [];
    if (typeof q.marks === "number") { bits.push(q.marks + " marks"); }
    if (q.year) { bits.push(String(q.year)); }
    if (q.exam_type) { bits.push(q.exam_type); }
    item.appendChild(document.createTextNode(
      q.text + (bits.length ? " (" + bits.join(", ") + ")" : "")
    ));

    /* Provenance on every item (§7.6). A question a student cannot trace back
     * to a real paper is indistinguishable from one we invented. Built as text,
     * so a filename is shown exactly as stored — underscores and all. */
    var src = el("div", "cm-answer-subline");
    src.appendChild(el("em", "", "Source: " + q.source_doc_id
      + (typeof q.page === "number" ? ", p." + q.page : "")));
    item.appendChild(src);

    if (q.low_confidence_flag) {
      /* Shown, not hidden. A student who knows an item was hard to extract can
       * discount it; one who does not will assume it is exact. */
      var warn = el("div", "cm-answer-subline");
      warn.appendChild(el("em", "", "Extraction confidence was low — check the original."));
      item.appendChild(warn);
    }

    /* The examiner's own answer, where the paper printed one.
     *
     * **Behind a deliberate action, and absent entirely when there is nothing
     * to reveal.** A control that opens to an empty panel teaches a student the
     * feature is broken; showing the answer unasked destroys the exercise the
     * question exists for.
     *
     * Cited SEPARATELY from the question. A marking scheme is frequently a
     * different document from the paper, and reusing the question's citation
     * would point a student at a page that does not contain this text. */
    if (q.reference_answer) {
      var ra = el("div", "cm-refanswer");
      var toggle = el("button", "cm-refanswer-toggle cm-btn cm-btn-ghost",
                      "Reveal reference answer");
      toggle.setAttribute("type", "button");
      toggle.setAttribute("aria-expanded", "false");

      var body = el("div", "cm-refanswer-body");
      body.hidden = true;
      /* textContent throughout — this is examiner prose lifted out of a PDF and
       * is semi-trusted under §10.6, exactly like the question text above. */
      body.appendChild(el("div", "cm-refanswer-text", q.reference_answer));

      var from = q.reference_answer_source_doc_id;
      if (from) {
        var cite = el("div", "cm-answer-subline");
        cite.appendChild(el("em", "", "Reference answer from: " + from
          + (typeof q.reference_answer_page === "number"
             ? ", p." + q.reference_answer_page : "")));
        body.appendChild(cite);
      }

      toggle.addEventListener("click", function () {
        body.hidden = !body.hidden;
        toggle.setAttribute("aria-expanded", body.hidden ? "false" : "true");
        toggle.textContent = body.hidden
          ? "Reveal reference answer" : "Hide reference answer";
      });

      ra.appendChild(toggle);
      ra.appendChild(body);
      item.appendChild(ra);
    }
    return item;
  }

  function renderRevisionPlan(container, plan) {
    clearNode(container);
    container.textContent = "";

    var intro = el("p", "cm-answer-p",
      "Here is a revision plan for this course, weakest outcome first. Every "
      + "question below is a real past-paper question, quoted as printed. "
      + "Nothing here is AI-generated.");
    container.appendChild(intro);

    var outcomes = (plan && plan.outcomes) || [];
    outcomes.forEach(function (o) {
      container.appendChild(el("h4", "cm-answer-h", o.clo_id + " — " + o.clo_text));

      var note = el("p", "cm-answer-note");
      /* "self-marked", not "correct". These counters come from the student
       * pressing "I got this"; nothing verified them, because no answer key
       * exists anywhere in the system. See the self-assessment block below. */
      note.appendChild(el("em", "", "Your record: " + (
        o.attempts ? o.correct + "/" + o.attempts + " self-marked" : "not practised yet"
      )));
      container.appendChild(note);

      var questions = o.questions || [];
      if (!questions.length) {
        /* Not an error, and must not render as one: the request was fine, the
         * course simply has nothing tagged to this outcome yet. */
        container.appendChild(el("p", "cm-answer-p",
          "No past-paper question is tagged to this outcome yet."));
        return;
      }
      var list = el("ul", "cm-answer-list");
      questions.forEach(function (q) { list.appendChild(planQuestionNode(q)); });
      container.appendChild(list);
    });
  }

  /** The distinct papers a plan draws on, in first-seen order. */
  function planSources(plan) {
    var seen = {};
    var out = [];
    ((plan && plan.outcomes) || []).forEach(function (o) {
      (o.questions || []).forEach(function (q) {
        if (q.source_doc_id && !seen[q.source_doc_id]) {
          seen[q.source_doc_id] = true;
          out.push({ usage_key: q.source_doc_id, display_name: q.source_doc_id });
        }
      });
    });
    return out;
  }

  function requestPlan(text) {
    prepNotice.hidden = true;
    prepInput.disabled = true;
    prepSend.disabled = true;

    /* Into the prose slot, which `slotTarget` clears first — so a second plan
     * replaces the first instead of stacking beneath it. This was
     * `prepLog.appendChild(planNode)`, and `prepLog` is never cleared, so the
     * previous plan stayed on screen under the new one.
     *
     * `slotTarget` returns the shared log when the slot is absent, which keeps
     * a page rendered before this change working exactly as it did. */
    var planNode = turnNode("tutor", "");
    /* Held, because the streaming handler below has to scroll the container the
     * plan is actually in. It used to scroll `prepLog` unconditionally, which
     * after this change would scroll an empty element and leave a long plan
     * stuck at the top of its own slot. */
    var planTarget = slotTarget(prosePlanSlot);
    planTarget.appendChild(planNode);
    var bubble = bubbleOf(planNode);
    var answerNode = answerOf(planNode);
    var answer = "";

    mintToken().then(function (token) {
      if (token.error) { showPrepNotice(token.error); return; }
      var base = prepPanel.dataset.base || token.stream_path.replace(/\/chat$/, "/examprep");

      /* With the agent OFF the plan is deterministic — arithmetic over data the
       * service already has — so ask for it as a value. `/plan` still streams
       * when the agent is on, because then it genuinely narrates and prose does
       * arrive a token at a time. The flag comes from `/status`, which this
       * panel already fetched. */
      if (prepPanel.dataset.agent === "0") {
        return fetch(base + "/revision-plan", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: "Bearer " + token.token
          },
          body: JSON.stringify({ request: text, mastery: mastery })
        }).then(function (response) {
          if (!response.ok) {
            /* 409 is PREPARING — a state, not a fault (§5.1) — and 403 is a
             * withdrawn entitlement. Both have their own wording already. */
            showPrepNotice(response.status === 409 ? "preparing"
                         : response.status === 403 ? "not_enrolled"
                         : "unavailable");
            planTarget.removeChild(planNode);
            return;
          }
          return response.json().then(function (plan) {
            renderRevisionPlan(answerNode, plan);
            planSources(plan).forEach(function (c) {
              sourcesRow(bubble).appendChild(citationNode(c));
            });
            planTarget.scrollTop = planTarget.scrollHeight;
          });
        });
      }

      return fetch(base + "/plan", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + token.token
        },
        /* No student id and no offering id in this payload, deliberately: the
         * request contract has no field for either, and scope comes from the
         * token the service verifies. */
        body: JSON.stringify({ request: text, mastery: mastery })
      }).then(function (response) {
        if (!response.ok || !response.body) { showPrepNotice("unavailable"); return; }
        return readStream(response, function (frame) {
          switch (frame.type) {
            case "token":
              answer += frame.text || "";
              /* Formatted, not raw. `api/plan.py` writes headings, whole-line
               * italics and bullets, and this assigned the lot to textContent —
               * so the student read `## CLO-1 — …` and `_Your record: …_`
               * literally. Same renderer as chat, so one answer cannot look
               * like two different products. */
              renderAnswer(answerNode, answer);
              planTarget.scrollTop = planTarget.scrollHeight;
              break;
            case "citation":
              sourcesRow(bubble).appendChild(citationNode(frame.citation));
              break;
            case "incomplete":
              bubble.appendChild(
                el("div", "cm-incomplete",
                   "Some information could not be checked (" + (frame.text || "") + ").")
              );
              break;
            case "degraded":
              bubble.appendChild(
                el("div", "cm-degraded", "Answered by a fallback model (" + (frame.provider || "") + ")")
              );
              break;
            case "error":
              showPrepNotice(frame.error_code || "unavailable");
              break;
            case "done":
              if (frame.truncated) { showPrepNotice("truncated"); }
              /* Same renderer as chat, so the same hand-off — once, at the end.
               * There is no `settle()` on this path, so the empty-answer case is
               * guarded here instead: an error frame leaves `answer` at "" and
               * there is nothing to typeset. */
              if (answer) { typesetMath(answerNode); }
              break;
          }
        });
      });
    }).catch(function () {
      showPrepNotice("unavailable");
    }).then(function () {
      prepInput.disabled = false;
      prepSend.disabled = false;
    });
  }

  /* --- budgeted study plan (§7.4) -------------------------------------- *
   * A different request from the prose plan above, so a different renderer: the
   * service returns a StudyPlan as JSON, not a stream, because a plan is a value
   * rather than a narration. Nothing here is AI-generated — every question named
   * is a real past-paper question — so no AI badge is attached, deliberately. */

  var MIN_BUDGET = 1;
  var MAX_BUDGET = 500;

  /* All plan text is written with textContent, never innerHTML. `rationale` and
   * `clo_id` come from the service, and the service builds them from extracted
   * PDF text — semi-trusted under §10.6, so a question whose paper contained
   * markup must render as characters, not as elements. */
  /* The planner opens its rationale with the same mastery clause the badge now
   * shows ("2/4 self-marked; 5 of 85 marks allocated (…)"). Printing both would
   * say it twice, so the clause is lifted out for the badge and the remainder
   * stays as the sentence. Anchored and narrow on purpose: a rationale that does
   * not begin with this exact shape is left completely untouched.
   *
   * `correct` is still accepted so a plan rendered from a turn persisted before
   * the wording changed still has its clause lifted rather than printed twice. */
  var MASTERY_CLAUSE = /^(?:not practised yet|\d+\/\d+ (?:self-marked|correct))\s*;\s*/;

  function rationaleRemainder(text) {
    var rest = String(text).replace(MASTERY_CLAUSE, "");
    if (!rest) { return ""; }
    return rest.charAt(0).toUpperCase() + rest.slice(1);
  }

  /* Read from the mastery snapshot the page already carries, not parsed out of
   * the rationale prose: the snapshot is structured and authoritative, and the
   * prose is a sentence the service is free to reword. Summed across difficulty
   * bands, because the badge is about the outcome, not one band of it. */
  function masteryBadge(cloId) {
    var rows = (mastery && mastery.clos) || [];
    var attempts = 0;
    var correct = 0;
    rows.forEach(function (row) {
      if (row.clo_id !== cloId) { return; }
      attempts += row.attempts || 0;
      correct += row.correct || 0;
    });
    if (!attempts) {
      return el("span", "cm-plan-mastery unpractised", "not practised yet");
    }
    /* Self-marked, not correct: these came from the student's own "I got this",
     * and no answer key exists to check them against. */
    return el("span", "cm-plan-mastery practised", correct + "/" + attempts + " self-marked");
  }

  function planItemNode(item, index) {
    var node = el("div", "cm-plan-item", "");
    var marks = typeof item.marks_budget === "number" ? item.marks_budget : 0;

    var head = el("div", "cm-plan-item-head");
    head.appendChild(el("span", "cm-plan-clo", item.clo_id + " · " + marks + " marks"));
    head.appendChild(masteryBadge(item.clo_id));
    node.appendChild(head);

    var ids = item.question_ids || [];
    node.appendChild(el(
      "div", "cm-plan-questions",
      ids.length ? "Questions: " + ids.join(", ")
                 : "No question fitted this outcome's share."
    ));

    /* Shown, not hidden. The rationale is where the planner says "5 of 20 marks
     * allocated (bank had nothing smaller that fit)" — a student who is told the
     * bank ran short can go find more practice; one who is not will read the
     * plan as complete. */
    if (item.rationale) {
      var rest = rationaleRemainder(item.rationale);
      if (rest) { node.appendChild(el("div", "cm-plan-rationale", rest)); }
    }
    return node;
  }

  /* Marks as a bar. The shortfall is the reason this exists: "80 marks could
   * not be filled" is a sentence people skip, and 80% of a bar in hatching is
   * not. Hatching rather than blank because unavailable is a third state, and
   * neither a filled bar nor an empty one says it. */
  function planBar(items, requested, planned) {
    var bar = el("div", "cm-plan-bar");
    bar.setAttribute("role", "img");
    bar.setAttribute("aria-label",
      planned + " of " + requested + " marks allocated");

    var total = requested > 0 ? requested : planned;
    items.forEach(function (item, i) {
      var marks = typeof item.marks_budget === "number" ? item.marks_budget : 0;
      if (marks <= 0) { return; }
      var seg = el("span", "cm-plan-seg cm-plan-seg-" + (i % 4), String(marks));
      if (seg.style) { seg.style.width = (total ? (marks / total) * 100 : 0) + "%"; }
      bar.appendChild(seg);
    });

    var unspent = requested - planned;
    if (unspent > 0) {
      var gap = el("span", "cm-plan-gap", unspent + " unfilled");
      if (gap.style) { gap.style.width = (total ? (unspent / total) * 100 : 0) + "%"; }
      bar.appendChild(gap);
    }
    return bar;
  }

  function planLegend(items, requested, planned) {
    var legend = el("div", "cm-plan-legend");
    legend.appendChild(el("span", "cm-plan-total",
      planned + " of " + requested + " marks allocated"));

    var keys = el("div", "cm-plan-keys");
    items.forEach(function (item, i) {
      var marks = typeof item.marks_budget === "number" ? item.marks_budget : 0;
      if (marks <= 0) { return; }
      var key = el("span", "cm-plan-key");
      key.appendChild(el("span", "cm-plan-swatch cm-plan-seg-" + (i % 4)));
      key.appendChild(el("span", null, item.clo_id));
      keys.appendChild(key);
    });
    legend.appendChild(keys);
    return legend;
  }

  function renderPlan(plan, requested) {
    var card = el("div", "cm-plan-card", "");
    var items = (plan && plan.items) || [];
    var slot = slotTarget(planSlot);

    var planned = 0;
    items.forEach(function (i) {
      planned += typeof i.marks_budget === "number" ? i.marks_budget : 0;
    });

    if (!items.length) {
      /* An empty plan is NOT an error, and must not render as one. The request
       * was fine; the course has nothing tagged with marks yet. Collapsing the
       * two would send a student to report a fault that does not exist. */
      card.appendChild(el("div", "cm-plan-heading",
        "Study plan — " + planned + " of " + requested + " marks"));
      card.appendChild(el("div", "cm-plan-empty",
        "No past-paper question with a marks value is tagged to this course's "
        + "outcomes yet, so there is nothing to plan from."));
      slot.appendChild(card);
      prepLog.scrollTop = prepLog.scrollHeight;
      return card;
    }

    /* Kept, but not shown, when the bar is there: the legend below already says
     * "20 of 100 marks allocated" in the same words, and printing it twice was
     * the first thing that looked wrong on screen. A bar is not readable by a
     * screen reader, so the sentence still has to exist — it is the same
     * argument as the aria-label, one level up. */
    card.appendChild(el("div", "cm-plan-heading cm-sr-only",
      "Study plan — " + planned + " of " + requested + " marks"));
    card.appendChild(planBar(items, requested, planned));
    card.appendChild(planLegend(items, requested, planned));

    items.forEach(function (item, i) { card.appendChild(planItemNode(item, i)); });

    /* Unspent budget, stated. The service's PlanReport is deliberately not in
     * the StudyPlan contract, so this is derived from what IS: the difference
     * between what was asked for and what the items add up to. Padding the plan
     * to hit the number, or saying nothing, would both be a lie about a bank
     * that is short. */
    var unspent = requested - planned;
    if (unspent > 0) {
      card.appendChild(el("div", "cm-plan-unspent",
        unspent + " marks could not be filled — the question bank has no more "
        + "tagged questions that fit."));
    }

    card.appendChild(el("div", "cm-plan-footnote",
      "Every question above is a real past-paper question. Nothing here is "
      + "AI-generated."));

    slot.appendChild(card);
    prepLog.scrollTop = prepLog.scrollHeight;
    return card;
  }

  function requestStudyPlan(budget) {
    prepNotice.hidden = true;
    budgetInput.disabled = true;
    budgetSend.disabled = true;

    mintToken().then(function (token) {
      if (token.error) { showPrepNotice(token.error); return; }
      var base = prepPanel.dataset.base || token.stream_path.replace(/\/chat$/, "/examprep");

      return fetch(base + "/study-plan", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + token.token
        },
        /* Exactly StudyPlanRequest, and nothing else. No student id and no
         * offering id: the contract has no field for either, so scope comes from
         * the token the service verifies and a forged payload cannot widen it. */
        body: JSON.stringify({ marks_budget: budget, mastery: mastery })
      }).then(function (response) {
        if (!response.ok) {
          /* A refusal is a fault and renders as one — distinct from the empty
           * plan above, which is a correct answer. 429 and 403 have their own
           * wording; anything else is reported as unavailable rather than
           * guessed at. */
          showPrepNotice(response.status === 429 ? "rate_limited"
                       : response.status === 403 ? "not_enrolled"
                       : "unavailable");
          return;
        }
        return response.json().then(function (plan) { renderPlan(plan, budget); });
      });
    }).catch(function () {
      showPrepNotice("unavailable");
    }).then(function () {
      budgetInput.disabled = false;
      budgetSend.disabled = false;
    });
  }

  if (budgetForm) {
    budgetForm.addEventListener("submit", function (event) {
      event.preventDefault();
      /* Validated here as well as by the contract. The service would reject a
       * bad budget with a 422 the student cannot act on, so the browser refuses
       * first and says what is wrong in the panel's own voice. */
      var budget = parseInt(budgetInput.value, 10);
      if (!isFinite(budget) || budget < MIN_BUDGET || budget > MAX_BUDGET) {
        prepNotice.textContent =
          "Enter a session length between " + MIN_BUDGET + " and " + MAX_BUDGET + " marks.";
        prepNotice.className = "cm-prep-notice invalid_budget";
        prepNotice.hidden = false;
        return;
      }
      requestStudyPlan(budget);
    });
  }

  /* One attempt id per generated card, reused by both buttons.
   *
   * `record_attempt` builds its idempotency key from it, so reusing it is what
   * makes a double-click count once. A fresh id per CLICK would let an
   * impatient student inflate their own counters; a fixed id across cards would
   * discard every attempt after the first, freezing their record at whatever
   * they answered once. One per card is the only reading that is right.
   *
   * Not crypto: this identifies an attempt, it does not authorise anything, and
   * the server derives student and offering from the session regardless. */
  function attemptId() {
    return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  }

  /* --- closing the practice loop --------------------------------------- *
   *
   * **Self-assessment, not grading, and the distinction is deliberate.** A
   * past-paper `QuestionRecord` carries the question text and nothing else —
   * there is no answer key anywhere in the system, so nothing here can mark an
   * answer right or wrong. Judging free text would mean a second model call
   * whose accuracy is unmeasured, which is exactly the sort of claim §9.0 says
   * has to be measured before a student sees it.
   *
   * So the student marks their own attempt, and `record_attempt` was always
   * built to be told: it takes `correct` from the payload. That is sound because
   * mastery is not a grade — it ranks the student's OWN study plan and reaches
   * nothing else. A student who lies to it only misdirects their own revision.
   *
   * The written answer never leaves the page. There is nothing to compare it
   * against, so sending it would create a store of student prose with no
   * purpose, inside the retirement boundary, for no gain. */
  function selfAssessment(card, opts) {
    var cloId = opts.cloId;
    var questionId = opts.questionId;
    var band = opts.band || "";
    /* A restored card keeps the id it was generated with. Minting a fresh one
     * would make the same card a second attempt, and both would be counted —
     * which is precisely what one-id-per-card exists to prevent. */
    var attempt = opts.attemptId || attemptId();
    /* Everything needed to re-persist this card WITHOUT losing what is already
     * stored. Re-sending an empty citation list on the answer step would wipe
     * the provenance the card was generated with, because the server replaces
     * a card rather than merging it. */
    var cardText = opts.text || "";
    var cardCitations = opts.citations || [];
    var wrap = el("div", "cm-selfcheck");

    var prompt = el("div", "cm-selfcheck-prompt",
      "Have a go, then mark how it went — this shapes your study plan.");
    wrap.appendChild(prompt);

    var draft = el("textarea", "cm-practice-answer");
    draft.setAttribute("rows", "4");
    draft.setAttribute("aria-label", "Your answer (kept on this page)");
    draft.setAttribute("placeholder", "Your answer — stays on this page");
    wrap.appendChild(draft);

    var status = el("div", "cm-selfcheck-status", "");
    var got = el("button", "cm-selfcheck-got", "I got this");
    var notYet = el("button", "cm-selfcheck-not", "Not yet");
    got.setAttribute("type", "button");
    notYet.setAttribute("type", "button");

    /* Disabling the buttons already stops a second press in a real browser,
     * which does not dispatch click on a disabled control. This flag says the
     * same thing in code rather than borrowing it from the platform: the
     * guarantee being asserted is "one attempt is written per generated
     * question", and that should not depend on a DOM behaviour a test harness —
     * or a keyboard-driven a11y path — can step around. `record_attempt` is
     * idempotent on `attempt_id` as the third layer. */
    var recorded = false;

    function record(correct) {
      if (recorded) { return; }
      recorded = true;
      got.disabled = true;
      notYet.disabled = true;
      status.textContent = "Saving…";

      fetch(recordUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: platformHeaders(),
        body: JSON.stringify({
          clo_id: cloId,
          question_id: questionId,
          attempt_id: attempt,
          difficulty_band: band || "",
          correct: correct
        })
      }).then(function (r) {
        return r.ok ? r.json() : { error: "unavailable" };
      }).then(function (result) {
        if (result && result.error) {
          /* Re-enabled on failure. A silent loss here is the whole defect this
           * closes: the student believes their practice was counted and their
           * plan quietly never changes. */
          status.textContent = "That didn't save — try again.";
          recorded = false;
          got.disabled = false;
          notYet.disabled = false;
          return;
        }
        status.textContent = correct
          ? "Recorded. Your plan will lean away from this outcome."
          : "Recorded. Your plan will give this outcome more time.";
        /* E2: remember that this card is spent, so a reload restores it
         * disabled. Without this a refresh would offer the buttons again and
         * the second press would be discarded as a replay — the student would
         * be told it saved, twice, and the counter would move once. */
        persistPractice({
          attempt_id: attempt,
          question_id: questionId,
          clo_id: cloId,
          difficulty_band: band,
          text: cardText,
          citations: cardCitations,
          answered: true
        });
      }).catch(function () {
        /* Cleared alongside the buttons: nothing was written, so the next press
         * is a first attempt, not a duplicate. */
        status.textContent = "That didn't save — try again.";
        recorded = false;
        got.disabled = false;
        notYet.disabled = false;
      });
    }

    got.addEventListener("click", function () { record(true); });
    notYet.addEventListener("click", function () { record(false); });

    var buttons = el("div", "cm-selfcheck-buttons");
    buttons.appendChild(got);
    buttons.appendChild(notYet);
    wrap.appendChild(buttons);
    wrap.appendChild(status);
    card.appendChild(wrap);

    /* A card restored as already answered comes back spent. Offering the
     * buttons again would let the student press one, be told it saved, and see
     * nothing move — `record_attempt` discards the second write as a replay of
     * the same `attempt_id`. Saying so plainly beats a control that lies. */
    if (opts.answered) {
      recorded = true;
      got.disabled = true;
      notYet.disabled = true;
      status.textContent = "Already marked.";
    }

    /* The id the caller must persist, so a restored card is the SAME attempt. */
    return attempt;
  }

  /* E2. Fire-and-forget, like `persist_turn`: a failed write costs the student
   * this card on their next reload, and blocking the UI on it would cost them
   * the card now. */
  function persistPractice(card) {
    return fetch(persistPracticeUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: platformHeaders(),
      body: JSON.stringify(card)
    }).catch(function () { /* see above */ });
  }

  /* Ends the run. Practice ONLY — mastery lives in a different table on a
   * different lifetime, and a student tidying their screen has not un-practised
   * anything. `clear_practice` shipped in E2 with nothing calling it; this is
   * the control that makes it real. */
  if (clearPracticeButton) {
    clearPracticeButton.addEventListener("click", function () {
      clearPracticeButton.disabled = true;
      fetch(clearPracticeUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: platformHeaders(),
        body: JSON.stringify({})
      }).then(function (r) {
        return r.ok ? r.json() : { cleared: false };
      }).then(function (result) {
        /* Same rule as the chat: only drop what is on screen once the platform
         * confirms its own copy is gone. */
        if (result && result.cleared && practiceSlot) {
          clearNode(practiceSlot);
          if (practiceSend) { practiceSend.textContent = "Generate a question →"; }
        }
        clearPracticeButton.disabled = false;
        syncPracticeTools();
      }).catch(function () {
        clearPracticeButton.disabled = false;
      });
    });
  }

  /* Rebuild the cards the server was holding, oldest first, so a reload lands
   * the student back where they were. Built with the same helpers as a live
   * card so the two cannot drift into looking different. */
  function restorePractice() {
    if (!savedPractice.length || !practiceSlot) { return; }
    savedPractice.forEach(function (saved) {
      if (!saved || !saved.text) { return; }
      var card = el("div", "cm-practice-card", "");
      card.appendChild(el("div", "cm-ai-badge", "AI-generated practice question"));
      card.appendChild(el("div", "cm-practice-text", saved.text));

      var prov = el("div", "cm-provenance", "");
      var cites = saved.citations || [];
      if (cites.length) {
        prov.appendChild(el("span", "cm-sources-label", "Derived from"));
        cites.forEach(function (c) {
          var link = el("a", "cm-chip-link", c.display_name || c.usage_key);
          link.href = safeHref(c.url);
          prov.appendChild(link);
        });
      } else {
        prov.textContent = "Source unavailable for this question.";
      }
      card.appendChild(prov);

      if (saved.question_id) {
        selfAssessment(card, {
          cloId: saved.clo_id,
          questionId: saved.question_id,
          band: saved.difficulty_band,
          attemptId: saved.attempt_id,
          answered: !!saved.answered,
          text: saved.text,
          citations: cites
        });
      }
      practiceTarget().appendChild(card);
    });
    /* The form should invite another question, not the first one. */
    if (practiceSend && practiceSlot.querySelector(".cm-practice-card")) {
      practiceSend.textContent = "Generate another →";
    }
    syncPracticeTools();
  }

  /* --- practice generation ------------------------------------------- *
   * One generated question, streamed. Renders the AI-generated badge and the
   * provenance line from the CITATION frames the service emits — the badge is
   * not a UI decoration, it is the claim §9.0 depends on, so it is attached to
   * the answer node itself rather than sitting statically in the panel. */
  function requestPractice(cloId, band) {
    prepNotice.hidden = true;
    practiceClo.disabled = true;
    practiceBand.disabled = true;
    practiceSend.disabled = true;

    /* Removing a node twice throws NotFoundError, and here that would be worse
     * than the original failure: the throw happens inside .catch(), which skips
     * the .then() that re-enables the controls, so the form stays permanently
     * disabled and the student cannot retry. Reachable whenever the connection
     * drops AFTER an error frame has already cleared the card. */
    function discardCard() {
      if (card && card.parentNode) { card.parentNode.removeChild(card); }
    }

    var card = el("div", "cm-practice-card", "");
    var badge = el("div", "cm-ai-badge", "AI-generated practice question");
    card.appendChild(badge);
    var body = el("div", "cm-practice-text", "");
    card.appendChild(body);
    /* Appended, so earlier questions and the student's self-assessment of them
     * stay on screen. Each card owns its own answer, citations and attempt id;
     * nothing here is shared between cards. */
    practiceTarget().appendChild(card);

    /* **This surface deliberately did NOT get the chat's two 2026-08-14
     * changes** — the waiting indicator and `renderAnswer`. Scoped out, not
     * forgotten, and written here because the divergence is visible: practice
     * questions still show raw markdown and still wait on a bare card.
     *
     * Two reasons it is a separate decision rather than a copy-paste. The
     * construct list was measured over CHAT answers in `eval/reports/`, and a
     * generated practice question is a different prompt and a different output
     * shape — reusing that list here would be applying a measurement someone
     * else earned, which is the same move §9.0 refuses for the rubric. And this
     * card is built by its own code path with its own notice element, so the
     * indicator is not a drop-in.
     *
     * Doing it properly means capturing practice output the way the chat scope
     * was captured, then deciding. Doing it by assumption is how B1/B2 and C2
     * shipped broken. */

    var answer = "";
    var sources = [];

    mintToken().then(function (token) {
      if (token.error) { showPrepNotice(token.error); return; }
      var base = prepPanel.dataset.base || token.stream_path.replace(/\/chat$/, "/examprep");

      return fetch(base + "/practice/stream", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + token.token
        },
        /* PracticeRequest carries no identity: the JWT scopes it, and the
         * service picks the source question itself. */
        body: JSON.stringify({ clo_id: cloId, difficulty_band: band || null })
      }).then(function (response) {
        if (!response.ok || !response.body) { showPrepNotice("unavailable"); return; }
        return readStream(response, function (frame) {
          switch (frame.type) {
            case "token":
              answer += frame.text || "";
              body.textContent = answer;
              prepLog.scrollTop = prepLog.scrollHeight;
              break;
            case "citation":
              sources.push(frame.citation);
              break;
            case "incomplete":
              card.appendChild(el("div", "cm-incomplete",
                "Some information could not be checked (" + (frame.text || "") + ")."));
              break;
            case "degraded":
              card.appendChild(el("div", "cm-degraded",
                "Answered by a fallback model (" + (frame.provider || "") + ")"));
              break;
            case "error":
              /* The card holds nothing yet — remove it so an abstention does not
               * leave an empty AI-generated badge on screen claiming a question
               * that was never written. */
              if (!answer) { discardCard(); }
              showPrepNotice(frame.error_code || "unavailable", "practice");
              break;
            case "done":
              if (frame.truncated) { showPrepNotice("truncated", "practice"); }
              /* Provenance line. §9.0 permits this question to reach the student
               * ungated BECAUSE it is labelled and cited, so a question that
               * arrived with no citation says so rather than looking sourced. */
              var prov = el("div", "cm-provenance", "");
              if (sources.length) {
                prov.appendChild(el("span", "cm-sources-label", "Derived from"));
                sources.forEach(function (c) {
                  var link = el("a", "cm-chip-link", c.display_name || c.usage_key);
                  link.href = safeHref(c.url);
                  prov.appendChild(link);
                });
              } else {
                prov.textContent = "Source unavailable for this question.";
              }
              card.appendChild(prov);
              /* The loop closes here, and only when the service told us which
               * past-paper record this came from. Without a question_id the
               * mastery write has no key, so offering the buttons would give a
               * student a control that silently does nothing — worse than not
               * offering it. An abstention leaves `answer` empty and never
               * reaches this branch. */
              if (frame.question_id && answer) {
                var attempt = selfAssessment(card, {
                  cloId: cloId,
                  questionId: frame.question_id,
                  band: frame.difficulty_band,
                  text: answer,
                  citations: sources
                });
                /* E2: store the card so a reload does not destroy the run.
                 * `attempt` is the id the live card is already using — carried,
                 * never regenerated, so a restored card is the SAME attempt. */
                persistPractice({
                  attempt_id: attempt,
                  question_id: frame.question_id,
                  clo_id: cloId,
                  difficulty_band: frame.difficulty_band || "",
                  text: answer,
                  citations: sources,
                  answered: false
                });
              }
              break;
          }
        });
      });
    }).catch(function () {
      if (!answer) { discardCard(); }
      showPrepNotice("unavailable");
    }).then(function () {
      practiceClo.disabled = false;
      practiceBand.disabled = false;
      practiceSend.disabled = false;
      /* Says what the next press does. The form was always reusable, but the
       * label still read "Generate a question" over a card that already held
       * one, so nothing on screen suggested asking again was possible — which
       * is most of why generation looked like a one-shot action. */
      if (practiceSlot && practiceSlot.querySelector(".cm-practice-card")) {
        practiceSend.textContent = "Generate another →";
      }
      syncPracticeTools();
    });
  }

  if (practiceForm) {
    practiceForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!practiceClo.value) { return; }
      requestPractice(practiceClo.value, practiceBand.value);
    });
  }

  /* E2. After the practice controls exist, so a restored card lands in the slot
   * rather than the shared log fallback. */
  restorePractice();

  prepForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = prepInput.value.trim();
    if (!text) { return; }
    prepInput.value = "";
    requestPlan(text);
  });
}
