"""Celery tasks.

**This file must import every task module, and that is not a style choice.**

Celery's `autodiscover_tasks()` imports `<installed_app>.tasks` and registers
whatever `@shared_task` ran during that import. When `tasks` is a *module*, the
decorators run. When it is a *package* — as here — importing it runs only
`__init__.py`, so an empty one registers nothing at all.

The failure is silent on the side that matters. `.delay()` still succeeds: the
receiver serialises the message, the broker accepts it, Studio's Publish button
returns 200, and nothing in the request path is any the wiser. The error appears
only in the worker's log, as

    Received unregistered task of type 'coursemate_platform.tasks.ingest.…'
    KeyError: 'coursemate_platform.tasks.ingest.ingest_published_block'

and the message is discarded. Publishing a unit therefore *looked* like it was
indexing content while indexing none of it. The index stayed correct only
because it had been populated by `coursemate_reindex`, which calls the service
directly and never goes near Celery.

`drift` is deliberately absent: it holds the sweep's decision logic and defines
no tasks, so importing it here would say something untrue about its role.
"""

from . import bootstrap, ingest, reconcile  # noqa: F401
