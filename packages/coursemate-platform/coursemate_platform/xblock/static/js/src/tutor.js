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

  var mintUrl = runtime.handlerUrl(element, "mint");
  var persistUrl = runtime.handlerUrl(element, "persist_turn");
  var recordUrl = runtime.handlerUrl(element, "record_attempt");

  var history = (initArgs && initArgs.history) || [];
  var mode = (initArgs && initArgs.mode) || "direct";

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
    truncated: "That answer was cut short. Try asking for a smaller piece of it."
  };

  /* Exam prep reuses every notice above, and overrides the two whose wording
   * would be wrong here — "not covered in this course" is about a lesson, and a
   * revision planner's version of the same state is about the question bank. */
  var PREP_NOTICES = {
    abstained: "There isn't enough in this course's material to plan that reliably.",
    preparing: "Past papers for this course haven't been loaded yet."
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
    bubble.appendChild(el("div", "cm-answer", content || ""));
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
    });
    log.scrollTop = log.scrollHeight;
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

    mintToken().then(function (token) {
      if (token.error) { showNotice(token.error); busy(false); return; }

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
          busy(false);
          return;
        }
        return readStream(response, function (frame) {
          switch (frame.type) {
            case "token":
              answer += frame.text || "";
              answerText.textContent = answer;
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
              showNotice(frame.error_code || "unavailable");
              break;
            case "done":
              // A cut-off answer looks identical to a complete one — it just
              // stops, and the student reads that as the tutor not knowing the
              // rest. Say which it was.
              if (frame.truncated) { showNotice("truncated"); }
              break;
          }
        }).then(function () {
          busy(false);
          if (!answer) { return; }
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
      showNotice("unavailable");
      busy(false);
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

  /* Only a real slot is cleared. The fallback is the shared log, and emptying
   * that would delete a plan because a question was generated next to it. */
  function slotTarget(slot) {
    if (!slot) { return prepLog; }
    clearNode(slot);
    return slot;
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

  function showPrepNotice(code) {
    prepNotice.textContent = PREP_NOTICES[code] || NOTICES[code] || "Something went wrong.";
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

  function requestPlan(text) {
    prepNotice.hidden = true;
    prepInput.disabled = true;
    prepSend.disabled = true;

    var planNode = turnNode("tutor", "");
    prepLog.appendChild(planNode);
    var bubble = bubbleOf(planNode);
    var answerNode = answerOf(planNode);
    var answer = "";

    mintToken().then(function (token) {
      if (token.error) { showPrepNotice(token.error); return; }
      var base = prepPanel.dataset.base || token.stream_path.replace(/\/chat$/, "/examprep");

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
              answerNode.textContent = answer;
              prepLog.scrollTop = prepLog.scrollHeight;
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
   * shows ("2/4 correct; 5 of 85 marks allocated (…)"). Printing both would say
   * it twice, so the clause is lifted out for the badge and the remainder stays
   * as the sentence. Anchored and narrow on purpose: a rationale that does not
   * begin with this exact shape is left completely untouched. */
  var MASTERY_CLAUSE = /^(?:not practised yet|\d+\/\d+ correct)\s*;\s*/;

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
    return el("span", "cm-plan-mastery practised", correct + "/" + attempts + " correct");
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
  function selfAssessment(card, cloId, questionId, band) {
    var attempt = attemptId();
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
    /* Replaces the previous question rather than stacking beneath it. */
    slotTarget(practiceSlot).appendChild(card);

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
              showPrepNotice(frame.error_code || "unavailable");
              break;
            case "done":
              if (frame.truncated) { showPrepNotice("truncated"); }
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
                selfAssessment(card, cloId, frame.question_id, frame.difficulty_band);
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
    });
  }

  if (practiceForm) {
    practiceForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!practiceClo.value) { return; }
      requestPractice(practiceClo.value, practiceBand.value);
    });
  }

  prepForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = prepInput.value.trim();
    if (!text) { return; }
    prepInput.value = "";
    requestPlan(text);
  });
}
