import os
import sys
from pathlib import Path

_backend_root = str(Path(__file__).resolve().parent)
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

os.environ["CLOUDFLARE_WORKERS"] = "1"
os.environ["APP_ENV"] = "production"

import anyio.to_thread
from workers import WorkerEntrypoint, asgi


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


# Pre-import database and FastAPI app during isolate startup
# so module compilation does not count against per-request CPU limits!
import database
from routers import app


class Default(WorkerEntrypoint):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._configured = False

    async def fetch(self, request):
        try:
            if not self._configured:
                _configure_environment(self.env)
                database.configure_worker_database(self.env.DB)
                self._configured = True

            return await asgi.fetch(app, request, self.env)
        except Exception as e:
            import traceback
            traceback.print_exc()
            from workers import Response
            return Response(f"Internal Worker Exception: {e}\n{traceback.format_exc()}", status=500)
