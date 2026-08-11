"""Test environment defaults.

`Settings` requires a signing key and a service credential with no defaults —
deliberately, so a deployment cannot start unconfigured and silently accept
unsigned tokens. That means importing anything that touches `config` fails
without them.

Supplying them here rather than in the shell makes the suite **self-contained**:
`pytest` works from a clean checkout, and CI needs no secret wiring to run tests
that never touch a real credential. The values are obviously fake and long enough
to satisfy the RFC 7518 minimum that `mint_student_token` enforces.
"""

from __future__ import annotations

import os

_TEST_ENV = {
    "COURSEMATE_JWT_SIGNING_KEY": "test-signing-key-not-a-real-secret-32b+",
    "COURSEMATE_SERVICE_CREDENTIAL": "test-service-credential-not-real-32b+",
    # Off by default in tests: enrollment re-derivation calls the platform, and a
    # unit test that reaches the network is not a unit test. The tests that
    # exercise authorization enable it explicitly and stub the platform.
    "COURSEMATE_ENFORCE_ENROLLMENT": "false",
    # Never write to the real index path from a test run.
    "COURSEMATE_INDEX_PATH": ":memory:",
    "COURSEMATE_EXAMPREP_PATH": ":memory:",
    # The agent ships dark and the default is False, so this is belt and braces —
    # but a test suite whose behaviour depends on an inherited environment
    # variable is a suite that passes on one machine and fails on another.
    "COURSEMATE_AGENT_ENABLED": "false",
}

for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)
