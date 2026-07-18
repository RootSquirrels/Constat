"""Module entrypoint: `python -m constat_api` starts the API.

This is the invocation documented in AGENTS.md and the development
docs. It exists so the documented command is true — the alternative
(equivalent) is `uvicorn constat_api.main:app`. Defaults are
dev-oriented (localhost:8000); the Dockerfile and ECS task invoke
uvicorn directly with their own flags.
"""

from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run("constat_api.main:app", port=8000)


if __name__ == "__main__":
    main()
