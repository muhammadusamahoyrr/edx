# Portable across Windows (Git Bash) and Linux/macOS.
#
# The venv layout and PYTHONPATH separator both differ by platform, and hardcoding
# either meant `make check` worked locally and failed in CI — so the detection is
# here rather than duplicated into the workflow.
ifeq ($(OS),Windows_NT)
    VENV_BIN := .venv/Scripts
    PATHSEP  := ;
else
    VENV_BIN := .venv/bin
    PATHSEP  := :
endif

PY   := $(VENV_BIN)/python
LINT := $(VENV_BIN)/lint-imports

export PYTHONPATH := packages/coursemate-service$(PATHSEP)packages/coursemate-platform

# eval/tests is included even though contract 5 forbids the runtime packages from
# importing eval/ — the contract is about the SHIPPED code, and the rubric is a
# control §9.0's no-instructor-gate argument depends on. A control nobody tests is
# an assumption.
TESTS := packages/coursemate-service/tests packages/coursemate-platform/tests/unit eval/tests

#: What `make coverage` must not drop below, for the SERVICE and CONTRACTS only.
#: Set at the measured value (81% at the time of writing) rather than an
#: aspirational one: a threshold nobody meets gets lowered or removed, and then it
#: measures nothing. Raise it when the number rises, never to make a point.
COVER_MIN := 80

.PHONY: help install test test-js lint-arch check coverage agent-eval openapi openapi-check clean

help:
	@echo "install    - venv + editable installs"
	@echo "test       - fast suite (no Open edX, no network)"
	@echo "lint-arch  - the six architectural contracts"
	@echo "check      - what CI runs"
	@echo "test-js    - browser-side tests for the XBlock UI (needs node)"
	@echo "coverage   - line coverage of the two shipped packages"
	@echo "agent-eval - the agent regression gates (no provider needed)"
	@echo "openapi    - regenerate docs/openapi.json from the routes"
	@echo "openapi-check - fail if the committed spec is stale (part of check)"

# django + XBlock + web_fragments are here for the PLATFORM-side unit tests, and
# they are not the Open edX runtime — they are the two libraries the plugin's
# models and block are built on, installable offline in seconds. Without them the
# mastery idempotency guarantee and the exam-prep handler are untestable outside a
# container, which in practice means untested. `pytest-django` is deliberately NOT
# installed: it calls `setup_test_environment()` at session start and collides
# with the fixture in tests/unit/conftest.py, which needs to own that lifecycle.
install:
	python -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e packages/coursemate-contracts
	$(PY) -m pip install -q pytest pytest-asyncio pyjwt import-linter ruff \
	                        fastapi pydantic-settings httpx litellm \
	                        "django>=4.2,<6" XBlock web_fragments

# Fast by construction: no Tutor, no containers, no network. Test credentials come
# from tests/conftest.py so a clean checkout works with no shell setup.
# Tests needing a running platform live in packages/coursemate-platform/tests/platform/
# and run at milestones — a suite that needs a platform is a suite nobody runs.
test:
	$(PY) -m pytest $(TESTS) -q --asyncio-mode=auto

# The highest-leverage target here. Contract 2 in particular is what makes
# "CourseMate cannot degrade your LMS" structurally true rather than aspirational,
# and it has been verified to fail on a deliberate violation.
lint-arch:
	$(LINT) --config .importlinter

# The XBlock UI is the one surface Python cannot reach. `node` is not a hard
# dependency — a machine without it skips these and says so, rather than failing
# a build over a test runner.
#
# `if/else`, not `A && B || echo`. The `||` form was here first and it made a
# FAILING test print "SKIPPED: node not found" and exit 0 — a lie and a green
# build, so these tests gated nothing. Found while adding the study-plan suite,
# by running a deliberately failing file through the old shape.
test-js:
	@if node --version >/dev/null 2>&1; then \
		node packages/coursemate-platform/tests/js/test_practice_ui.mjs && \
		node packages/coursemate-platform/tests/js/test_study_plan_ui.mjs && \
		node packages/coursemate-platform/tests/js/test_error_notices.mjs && \
		node packages/coursemate-platform/tests/js/test_history_marks.mjs; \
	else \
		echo "SKIPPED: node not found — the XBlock UI is untested on this machine"; \
	fi

# The committed spec describes the API to anyone integrating with it, and
# "generated" was not enough to keep it true — generating it was a step someone
# had to remember, and Phase 2 forgot. This makes forgetting fail the build,
# the same move `.importlinter` makes for the module graph.
openapi-check:
	$(PY) tools/ops/dump_openapi.py --check

check: lint-arch openapi-check test test-js

# Line coverage, not branch: branch coverage on this codebase is dominated by the
# `if settings.X` guards, and chasing it rewards tests that flip flags over tests
# that exercise behaviour.
#
# The number is a floor, not a target. Coverage says which lines ran, never
# whether the assertion that ran them meant anything — this repo has shipped
# 100%-covered code that returned success while doing nothing, twice.
#
# **The gate covers the service and contracts only, and that is not a dodge.**
# Those two packages run entirely offline, so a low number there means untested
# code. `coursemate_platform` is mostly adapters, Celery tasks and event
# receivers that need a live Open edX to execute at all — it sits near 26%, and
# blending it in produced a single figure that measured "how much of this needs a
# platform" rather than how well tested anything is. It is reported separately
# and ungated, with `tests/platform/` as the honest home for the rest.
coverage:
	$(PY) -m pytest $(TESTS) -q --asyncio-mode=auto \
		--cov=coursemate_service --cov=coursemate_contracts \
		--cov-report=term-missing --cov-fail-under=$(COVER_MIN)
	@echo ""
	@echo "--- coursemate_platform (reported, not gated: needs Open edX to execute) ---"
	@$(PY) -m pytest packages/coursemate-platform/tests/unit -q --asyncio-mode=auto \
		--cov=coursemate_platform --cov-report=term | tail -3

# Runs with no provider configured and still measures the three regression gates,
# because they are decided by tool outcomes rather than by a model. Tool-SELECTION
# accuracy is reported as NOT MEASURED until `--live` has one.
agent-eval:
	$(PY) eval/run_agent_eval.py

openapi:
	$(PY) tools/ops/dump_openapi.py

clean:
	rm -rf .venv .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
