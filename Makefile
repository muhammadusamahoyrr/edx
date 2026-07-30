PY := .venv/Scripts/python.exe
export PYTHONPATH := packages/coursemate-service;packages/coursemate-platform

.PHONY: help install test test-fast lint-arch check clean

help:
	@echo "install    - venv + editable installs"
	@echo "test       - the fast suite (no Open edX, no network)"
	@echo "lint-arch  - the five architectural contracts"
	@echo "check      - what CI runs"

install:
	python -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e packages/coursemate-contracts
	$(PY) -m pip install -q pytest pyjwt import-linter ruff

# Fast by construction: no Tutor, no containers, no network. A suite that needs a
# platform is a suite nobody runs, which is why tests/platform/ is separate and
# runs only at milestones.
test:
	$(PY) -m pytest packages/coursemate-service/tests packages/coursemate-platform/tests/unit -q

# The highest-leverage target in the repo. Contract 2 in particular is what makes
# "CourseMate cannot degrade your LMS" structurally true rather than aspirational.
lint-arch:
	$(PY) -m importlinter.cli lint --config .importlinter

check: lint-arch test

clean:
	rm -rf .venv .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
