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
    not_enrolled: "You don't have access to this course's tutor."
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
            try { onFrame(JSON.parse(payload)); } catch (e) { /* ignore keep-alives */ }
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
            case "error":
              showNotice(frame.error_code || "unavailable");
              break;
            case "done":
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
}
