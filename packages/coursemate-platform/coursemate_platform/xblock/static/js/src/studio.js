/* Studio view: config plus the bootstrap trigger (design §5.1).
 * Without an index, the tutor answers "not covered in this course" for every
 * question — which the confidence guard makes look like correct behaviour. That
 * is the most likely way a demo fails, so the button lives in front of the
 * person who just added the block. */
function CourseMateTutorStudio(runtime, element) {
  "use strict";
  var indexUrl = runtime.handlerUrl(element, "index_course");
  var button = element.querySelector(".cm-index");
  var lastIndexed = element.querySelector(".cm-last-indexed");
  var blockCount = element.querySelector(".cm-block-count");

  if (!button) { return; }
  button.addEventListener("click", function () {
    button.disabled = true;
    button.textContent = "Indexing…";
    fetch(indexUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
