import os
import sys
from pathlib import Path

import anyio.to_thread
from workers import WorkerEntrypoint, asgi


_backend_root = str(Path(__file__).resolve().parent)
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

_app = None


async def _run_sync_without_threads(
    func,
    *args,
    abandon_on_cancel=False,
    cancellable=None,
    limiter=None,
):
    return func(*args)


anyio.to_thread.run_sync = _run_sync_without_threads


def _configure_environment(env) -> None:
    os.environ["APP_ENV"] = "production"
    os.environ["CLOUDFLARE_WORKERS"] = "1"

    for key in (
        "SECRET_KEY",
        "GROQ_API_KEY",
        "GROQ_MODEL",
        "GROQ_MAX_TOKENS",
        "FRONTEND_ORIGINS",
    ):
        value = getattr(env, key, None)
        if value is not None:
            os.environ[key] = str(value)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        global _app

        if _app is None:
            _configure_environment(self.env)

            from database import configure_worker_database

            configure_worker_database(self.env.DB)

            from routers import app

            _app = app

        return await asgi.fetch(_app, request, self.env)
