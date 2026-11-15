from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    from .app import app

    uvicorn.run(
        app,
        host=os.getenv("TESTSYSTEM_HOST", "127.0.0.1"),
        port=int(os.getenv("TESTSYSTEM_PORT", "8000")),
    )


__all__ = ["main"]
