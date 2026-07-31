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

TESTS := packages/coursemate-service/tests packages/coursemate-platform/tests/unit

.PHONY: help install test lint-arch check clean

help:
	@echo "install    - venv + editable installs"
	@echo "test       - fast suite (no Open edX, no network)"
	@echo "lint-arch  - the six architectural contracts"
	@echo "check      - what CI runs"

install:
	python -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e packages/coursemate-contracts
	$(PY) -m pip install -q pytest pytest-asyncio pyjwt import-linter ruff \
	                        fastapi pydantic-settings httpx litellm

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

check: lint-arch test

clean:
	rm -rf .venv .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
