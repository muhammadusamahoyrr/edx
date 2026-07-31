"""Smoke-test XBlock — the smallest thing that proves the integration path works.

Phase 3's risk is not our logic, it is the chain that has to hold before any
logic matters:

    entry point registered  ->  package installed in the image  ->  block listed
    in Advanced Modules  ->  renders in Studio  ->  renders in LMS  ->  a JSON
    handler round-trips

Six links, each with its own failure mode, and a failure anywhere produces the
same symptom: "the block doesn't show up." Debugging that with the full tutor
block in place means debugging six things at once.

**This is deliberately not throwaway.** It stays in the repository as an
environment smoke test: any time a new Open edX release, a rebuilt image, or a
changed mount breaks the integration path, this block tells you whether the
problem is the platform or CourseMate — in about thirty seconds.

Dependencies are deliberately minimal (no pydantic, no Django settings, no
contracts import) so that a failure here is unambiguously about XBlock plumbing.
Its counterpart `tutor_block.py` carries the real dependencies; if smoke passes
and tutor fails, the difference is our code, not the platform.
"""

from __future__ import annotations

from xblock.core import XBlock
from xblock.fields import Integer, Scope, String

try:  # pragma: no cover - import path differs across releases
    from web_fragments.fragment import Fragment
except ImportError:  # pragma: no cover
    from xblock.fragment import Fragment


class CourseMateSmokeXBlock(XBlock):
    """Renders a line of text and echoes a JSON handler call."""

    display_name = String(
        display_name="Display name",
        default="CourseMate Smoke Test",
        scope=Scope.settings,
    )

    #: Per-student, so a successful increment also proves user_state persistence
    #: works — which is the same scope the real tutor keeps chat history in.
    clicks = Integer(default=0, scope=Scope.user_state)

    def student_view(self, context=None):  # noqa: ARG002
        html = f"""
        <div class="coursemate-smoke" style="border:1px solid #4a7;padding:12px;border-radius:6px">
          <p><strong>CourseMate smoke test.</strong> If you can see this, the block
             is installed, listed, and rendering in the LMS.</p>
          <p>Handler round-trips so far: <span class="cm-count">{self.clicks}</span></p>
          <button class="cm-ping" type="button">Ping the JSON handler</button>
          <p class="cm-result" style="font-family:monospace"></p>
        </div>
        """
        fragment = Fragment(html)
        fragment.add_javascript(_JS)
        fragment.initialize_js("CourseMateSmoke")
        return fragment

    def studio_view(self, context=None):  # noqa: ARG002
        """Proves the block is editable in Studio, which is a separate code path
        from student_view and fails independently of it."""
        html = """
        <div class="coursemate-smoke-studio" style="padding:12px">
          <p><strong>CourseMate smoke test — Studio view.</strong></p>
          <p>Rendering here proves the CMS loaded the block and its studio view.</p>
        </div>
        """
        return Fragment(html)

    @XBlock.json_handler
    def ping(self, data, suffix=""):  # noqa: ARG002
        """Round-trip a JSON payload and persist per-student state.

        Proves three things at once: handler URL dispatch resolves, JSON encoding
        works both directions, and a Scope.user_state write survives the request.
        """
        self.clicks += 1
        return {
            "pong": True,
            "echo": (data or {}).get("message", ""),
            "clicks": self.clicks,
            "block": str(self.scope_ids.usage_id),
        }

    @staticmethod
    def workbench_scenarios():
        return [("CourseMate Smoke", "<coursemate_smoke/>")]


_JS = """
function CourseMateSmoke(runtime, element) {
  var url = runtime.handlerUrl(element, 'ping');
  var button = element.querySelector('.cm-ping');
  var result = element.querySelector('.cm-result');
  var count  = element.querySelector('.cm-count');

  /* Django rejects an unauthenticated POST and returns an HTML error page, which
   * response.json() then fails to parse -- surfacing as the misleading
   * "Unexpected token '<'". The real cause is a missing CSRF token, so read it
   * from the cookie the platform already set. */
  function csrfToken() {
    var match = document.cookie.match(/(^|;\\s*)csrftoken=([^;]*)/);
    return match ? decodeURIComponent(match[2]) : '';
  }

  button.addEventListener('click', function () {
    result.textContent = '';
    fetch(url, {
      method: 'POST',
      /* Send the session cookie. Explicit rather than relying on the default,
       * because the block renders inside an MFE iframe. */
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken()
      },
      body: JSON.stringify({message: 'hello from the browser'})
    })
    .then(function (r) {
      /* Report the status rather than blindly parsing: a 403 or a login
       * redirect returns HTML, and "invalid JSON" hides which one happened. */
      if (!r.ok) { throw new Error('HTTP ' + r.status + ' ' + r.statusText); }
      return r.json();
    })
    .then(function (data) {
      result.textContent = JSON.stringify(data);
      count.textContent = data.clicks;
    })
    .catch(function (e) { result.textContent = 'handler failed: ' + e.message; });
  });
}
"""
