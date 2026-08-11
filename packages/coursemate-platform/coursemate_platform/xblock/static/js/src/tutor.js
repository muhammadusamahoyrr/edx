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
    not_enrolled: "You don't have access to this course's tutor.",
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

  /* One place that builds a citation line, used by both the live stream and the
   * reloaded history — so a persisted answer looks identical to a fresh one. */
  function citationNode(citation) {
    var wrap = el("div", "cm-citation");
    var link = el("a", null, citation.display_name || citation.usage_key);
    link.href = safeHref(citation.url);
    wrap.appendChild(document.createTextNode("Source: "));
    wrap.appendChild(link);
    return wrap;
  }

  function renderHistory() {
    while (log.firstChild) { log.removeChild(log.firstChild); }
    history.forEach(function (turn) {
      var node = el("div", "cm-turn " + turn.role, turn.content);
      /* Citations are persisted with the turn, so a reloaded answer keeps its
       * sources. Before this they existed only during the live stream, and a
       * refresh silently stripped them — which undercuts the whole point of a
       * tutor that cites. */
      (turn.citations || []).forEach(function (c) { node.appendChild(citationNode(c)); });
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

    var answerNode = el("div", "cm-turn tutor", "");
    log.appendChild(answerNode);
    var answer = "";
    var citations = [];

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
              answerNode.textContent = answer;
              log.scrollTop = log.scrollHeight;
              break;
            case "citation":
              citations.push(frame.citation);
              answerNode.appendChild(citationNode(frame.citation));
              break;
            case "unsupported_claim":
              // Mark it; never silently rewrite text the student already read.
              answerNode.appendChild(el("div", "cm-unsupported", frame.text || ""));
              break;
            case "degraded":
              // An outage must not read as "the tutor got worse this week".
              answerNode.appendChild(
                el("div", "cm-degraded", "Answered by a fallback model (" + (frame.provider || "") + ")")
              );
              break;
            case "incomplete":
              // Distinct from "degraded", which says a different MODEL answered.
              // This says the EVIDENCE was incomplete, which is the more serious
              // of the two and the one a student should weigh.
              answerNode.appendChild(
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
          history.push({ role: "tutor", content: answer, citations: citations });
          // Persist through the platform, which owns conversation state (§3.1).
          fetch(persistUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: platformHeaders(),
            body: JSON.stringify({ question: question, answer: answer, citations: citations })
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

  var prepStatus = prepPanel.querySelector(".cm-prep-status");
  var prepLog = prepPanel.querySelector(".cm-prep-log");
  var prepNotice = prepPanel.querySelector(".cm-prep-notice");
  var prepForm = prepPanel.querySelector(".cm-prep-form");
  var practiceForm = prepPanel.querySelector(".cm-practice-form");
  var practiceClo = prepPanel.querySelector(".cm-practice-clo");
  var practiceBand = prepPanel.querySelector(".cm-practice-band");
  var practiceSend = prepPanel.querySelector(".cm-practice-send");
  var prepInput = prepPanel.querySelector(".cm-prep-input");
  var prepSend = prepPanel.querySelector(".cm-prep-send");

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
        var parts = [status.questions + " past-paper questions",
                     status.clos + " learning outcomes"];
        if (status.earliest_year && status.latest_year) {
          parts.push(status.earliest_year + "–" + status.latest_year);
        }
        /* Soft spots are shown, not hidden. A student who knows some items were
         * hard to extract can discount them; one who does not will read every
         * one as exact. */
        if (status.low_confidence) {
          parts.push(status.low_confidence + " flagged for low extraction confidence");
        }
        prepStatus.textContent = parts.join(" · ");
        prepForm.hidden = false;

        /* The outcome selector. Only enabled when the course actually declares
         * outcomes — an empty dropdown is a control that cannot work, which is
         * the failure §5.1 is about. */
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

    var planNode = el("div", "cm-turn tutor", "");
    prepLog.appendChild(planNode);
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
              planNode.textContent = answer;
              prepLog.scrollTop = prepLog.scrollHeight;
              break;
            case "citation":
              planNode.appendChild(citationNode(frame.citation));
              break;
            case "incomplete":
              planNode.appendChild(
                el("div", "cm-incomplete",
                   "Some information could not be checked (" + (frame.text || "") + ").")
              );
              break;
            case "degraded":
              planNode.appendChild(
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
    prepLog.appendChild(card);

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
              var prov = el("div", "cm-provenance", sources.length ? "Derived from: " : "");
              if (sources.length) {
                sources.forEach(function (c, i) {
                  if (i) { prov.appendChild(document.createTextNode(", ")); }
                  var link = el("a", null, c.display_name || c.usage_key);
                  link.href = safeHref(c.url);
                  prov.appendChild(link);
                });
              } else {
                prov.textContent = "Source unavailable for this question.";
              }
              card.appendChild(prov);
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
