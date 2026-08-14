/* Studio view: config plus the bootstrap trigger (design §5.1).
 * Without an index, the tutor answers "not covered in this course" for every
 * question — which the confidence guard makes look like correct behaviour. That
 * is the most likely way a demo fails, so the button lives in front of the
 * person who just added the block. */
function CourseMateTutorStudio(runtime, element) {
  "use strict";

  /* **Studio passes `element` as a jQuery object; the LMS and the workbench
   * pass a DOM node.** Calling `querySelector` on the jQuery wrapper throws
   * `TypeError: element.querySelector is not a function` on the FIRST line that
   * touches it — so this whole function aborted at init, every time, in the only
   * runtime it is ever loaded in.
   *
   * That is why nothing in this panel had ever worked, the index button
   * included: the button rendered, the click bound nothing, and the failure was
   * a console error on a page nobody had a reason to open DevTools on. Found
   * 2026-08-13 while wiring the settings save.
   *
   * `runtime.handlerUrl` is deliberately still given the ORIGINAL `element` —
   * Studio's runtime expects the jQuery object there. */
  var root = (element && element.jquery) ? element[0] : element;

  var indexUrl = runtime.handlerUrl(element, "index_course");
  var saveUrl = runtime.handlerUrl(element, "submit_studio_edits");
  var button = root.querySelector(".cm-index");
  var lastIndexed = root.querySelector(".cm-last-indexed");
  var blockCount = root.querySelector(".cm-block-count");

  /* --- settings --------------------------------------------------------- *
   * This panel had no save path at all. The four controls rendered, an author
   * changed them, and nothing was written — which is how `exam_prep_enabled`
   * could sit at its default of False on every block created through Studio
   * with no way to turn it on. */
  var saveButton = root.querySelector(".cm-cfg-save");
  var saveStatus = root.querySelector(".cm-cfg-status");
  var cfgName = root.querySelector(".cm-cfg-name");
  var cfgEnabled = root.querySelector(".cm-cfg-enabled");
  var cfgExamPrep = root.querySelector(".cm-cfg-exam-prep");
  var cfgMode = root.querySelector(".cm-cfg-mode");

  /* Django rejects a POST without a CSRF token and answers with an HTML error
   * page, which then fails to parse as JSON — surfacing as the misleading
   * "Unexpected token '<'". Read the token the platform already set. */
  function csrfToken() {
    var match = document.cookie.match(/(^|;\s*)csrftoken=([^;]*)/);
    return match ? decodeURIComponent(match[2]) : "";
  }

  function settingsPayload() {
    return {
      display_name: cfgName ? cfgName.value : undefined,
      enabled: cfgEnabled ? cfgEnabled.checked : undefined,
      exam_prep_enabled: cfgExamPrep ? cfgExamPrep.checked : undefined,
      mode: cfgMode ? cfgMode.value : undefined
    };
  }

  if (saveButton) {
    saveButton.addEventListener("click", function () {
      saveButton.disabled = true;
      if (saveStatus) { saveStatus.textContent = "Saving…"; }

      fetch(saveUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
        body: JSON.stringify(settingsPayload())
      }).then(function (r) {
        return r.ok ? r.json() : { error: "unavailable" };
      }).then(function (result) {
        if (result && result.error) {
          /* Named, not collapsed into "failed". "forbidden" tells an author
           * they are not staff on this course, which is actionable; "failed"
           * sends them to re-click a button that cannot ever work. */
          saveStatus.textContent =
            result.error === "forbidden" ? "You need course staff access to change these."
            : result.error === "invalid_mode" ? "That mode is not one this tutor supports."
            : "That didn't save — try again.";
          saveButton.disabled = false;
          return;
        }
        /* "Publish", not "reload". Studio writes the DRAFT branch and the LMS
         * serves the PUBLISHED one, so a saved setting is invisible to students
         * until the unit is published. Measured on the live stack right after
         * this panel started working: draft exam_prep_enabled=True,
         * published=False, and the learner still saw no tab. Telling an author
         * to reload sends them to check the one place that cannot have changed
         * yet, and they conclude the save failed. */
        saveStatus.textContent = "Saved. Publish the unit to show this to students.";
        saveButton.disabled = false;
        /* Tell Studio the block changed so the unit preview re-renders rather
         * than showing the previous fragment until a manual refresh.
         *
         * **Isolated on purpose.** `.catch()` below is attached after this
         * handler, so anything thrown HERE lands there and rewrites a correct
         * "Saved" into "That didn't save" — which is exactly what happened on
         * the first live run: the settings were written, the block was
         * re-indexed, the POST returned 200, and the panel still told the
         * author it had failed. They would then click again and save twice.
         * A cosmetic refresh must never be able to invalidate the reported
         * outcome of the write. */
        if (runtime && typeof runtime.notify === "function") {
          try {
            runtime.notify("save", { state: "end" });
          } catch (e) {
            /* The save stands. Studio just will not repaint on its own, which
             * is what the message above already tells the author to do. */
          }
        }
      }).catch(function () {
        saveStatus.textContent = "That didn't save — try again.";
        saveButton.disabled = false;
      });
    });
  }

  if (!button) { return; }
  button.addEventListener("click", function () {
    button.disabled = true;
    button.textContent = "Indexing…";
    /* CSRF token and same-origin credentials, both previously missing. Django
     * rejects the POST without them and answers with an HTML error page, which
     * then fails to parse as JSON. Invisible until now only because the init
     * above threw before this listener was ever bound. */
    fetch(indexUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      body: JSON.stringify({})
    }).then(function (r) { return r.json(); }).then(function (data) {
      // An in-flight lock means a second click attaches to the running job
      // rather than queueing another (§5.1).
      button.textContent = data.already_running
        ? "Indexing already in progress"
        : "Index this course for the tutor";
      if (data.last_indexed) { lastIndexed.textContent = data.last_indexed; }
      if (typeof data.block_count === "number") { blockCount.textContent = data.block_count; }
      button.disabled = false;
    }).catch(function () {
      button.textContent = "Indexing failed — see logs";
      button.disabled = false;
    });
  });
}
