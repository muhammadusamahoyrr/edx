"""Minimal Django for the model tests.

`coursemate_platform.models` is an ordinary Django app, so its behaviour —
notably the mastery idempotency guarantee — is testable with Django and SQLite
alone. No Open edX, no Tutor, no containers, no network. That keeps it in the
fast suite where it will actually be run, rather than in `tests/platform/`, which
by design runs only at milestones.

The settings here are deliberately the smallest thing that lets migrations apply:
an in-memory database and the one app. Importing the real
`coursemate_platform.settings.common` would drag in the LMS's own settings
machinery, which is exactly the dependency this suite exists without.

**The app is registered through a test-local `AppConfig`**, not by naming the
package. Naming the package makes Django import `coursemate_platform.apps`, whose
plugin registration imports `edx_django_utils` and `openedx.core` — the whole
Open edX runtime, to test two tables. The real `apps.py` is not under test here;
it is exercised by `tests/platform/`, where a platform exists.
"""

from __future__ import annotations

import sys
import types

import pytest

try:
    import django
    from django.apps import AppConfig
    from django.conf import settings as django_settings
except ImportError:  # pragma: no cover - django absent
    django = None


def _register_test_app_module() -> str:
    """Publish a synthetic module holding a minimal AppConfig.

    Injected into `sys.modules` rather than written to disk so it cannot be
    imported by accident from anywhere else, and so nothing test-only lands in
    the shipped package.
    """
    module = types.ModuleType("coursemate_platform_test_app")

    class CourseMateTestConfig(AppConfig):
        name = "coursemate_platform"
        label = "coursemate_platform"

    module.CourseMateTestConfig = CourseMateTestConfig
    sys.modules[module.__name__] = module
    return f"{module.__name__}.CourseMateTestConfig"


def pytest_configure():
    if django is None or django_settings.configured:
        return
    django_settings.configure(
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            _register_test_app_module(),
        ],
        DATABASES={
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
        },
        USE_TZ=True,
        DEFAULT_AUTO_FIELD="django.db.models.AutoField",
    )
    django.setup()


@pytest.fixture(scope="session")
def _migrated_db():
    """Apply migrations once. SQLite `:memory:` lives on the connection, so this
    cannot be per-test: `destroy_test_db` leaves the connection open and the
    tables behind, which is how rows leaked between tests on the first attempt."""
    if django is None:  # pragma: no cover
        pytest.skip("django not installed")

    from django.db import connection
    from django.test.utils import setup_test_environment, teardown_test_environment

    setup_test_environment()
    connection.creation.create_test_db(verbosity=0, serialize=False)
    try:
        yield connection
    finally:
        connection.creation.destroy_test_db(":memory:", verbosity=0)
        teardown_test_environment()


@pytest.fixture
def django_db(_migrated_db):
    """One test's worth of database, rolled back afterwards.

    The rollback is the point, and it is not tidiness. An idempotency test that
    inherits a previous test's rows can pass for exactly the reason it is meant
    to catch — a second write that appeared not to apply because the state was
    already there. Isolation is what makes the assertion mean anything.
    """
    from django.db import transaction

    atomic = transaction.atomic()
    atomic.__enter__()
    try:
        yield _migrated_db
    finally:
        transaction.set_rollback(True)
        atomic.__exit__(None, None, None)
