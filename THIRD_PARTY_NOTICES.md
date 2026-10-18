# Third-party notices

LocalCrew is licensed under Apache-2.0 (see `LICENSE`). It does not vendor third-party source code. Dependencies are
installed by pip/npm from their official registries, under their own licenses (checked 2026-10-18 against the
pinned versions in `services/api/requirements.lock` and `apps/web/package-lock.json`):

- Permissive (MIT, BSD-2/3-Clause, Apache-2.0, PSF-2.0, MIT-0): FastAPI, Starlette, Uvicorn, SQLAlchemy, Alembic,
  Pydantic, httpx/httpcore, Playwright (Python), argon2-cffi, cryptography, croniter, tzdata, python-multipart, pypdf,
  React, React DOM, Vite, Tailwind CSS, TypeScript, ESLint, and their transitive dependencies.
- **psycopg / psycopg-binary: LGPL-3.0-only.** Used unmodified as a dynamically imported library. If you distribute
  images that contain it, keep its license and allow users to replace it (pip install of a different build satisfies this).
- **certifi: MPL-2.0.** Used unmodified.

Runtime software that is **not** included and is installed separately: PostgreSQL (PostgreSQL License), Ollama
(MIT), Chromium via Playwright (BSD-style, multiple), Docker images (their respective licenses).

Model weights are not included. Each model has its own license; check before use. As tested:
`qwen2.5:1.5b` is Apache-2.0. `qwen2.5:3b` is under the **Qwen Research License (non-commercial)**.

"Grok" and "xAI" are trademarks of their owners. LocalCrew is not affiliated with, endorsed by, or connected to them.
