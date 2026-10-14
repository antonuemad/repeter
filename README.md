# Repeater

منصة SaaS لإعادة نشر الفيديوهات القصيرة تلقائيًا: انشر على منصة واحدة، والنظام يلتقط الفيديو الجديد ويعيد نشره على تيكتوك ويوتيوب شورتس وانستجرام ريلز وفيسبوك ريلز وفق جدول دوران، عبر APIs النشر الرسمية، مع الرفع اليدوي والمصادر المدعومة للاستيراد.

Repeater watches a creator's source platform (or a Google Drive folder, or manual uploads), stores each new short video once, encodes one file that all four platforms accept, and re-publishes it to the other platforms on a rotation schedule using the official APIs.

## Status

Phase 0 is deployed at **https://repeater.magic-lap.com** on Magic Lap’s VPS. Platform app credentials and account connections are configured separately. The product and technical plan, with every platform fact sourced from official documentation, is in [PLAN.md](PLAN.md) (Arabic).

The next milestone is a trial for selected invited customers. The [launch readiness review](docs/ar/06-readiness.md) records the remaining gates; server health alone does not mean the integrations or customer lifecycle are ready. [PRODUCT.md](PRODUCT.md) and [DESIGN.md](DESIGN.md) preserve the agreed product scope and the Magic Lap interface system.

| Area | Where |
|---|---|
| Platform adapters (OAuth, publish, list/download) | `repeater/platforms/` |
| Import sources (Instagram, Facebook, Drive, opt-in yt-dlp, manual) and media processing | `repeater/sources/`, `repeater/media/` |
| Rotation planner, publish pipeline, worker | `repeater/engine/`, `repeater/jobs/`, `repeater/queue.py` |
| Dashboard (Arabic and English), auth, OAuth connect, signed media URLs, Meta callbacks | `repeater/web/` |
| Docker, Compose, deploy scripts, CI | `Dockerfile`, `deploy/`, `.github/workflows/` |
| Setup guides per developer app and operations (Arabic) | `docs/ar/` |

## Develop

```bash
uv venv .venv && . .venv/bin/activate
uv pip install -e ".[dev]"
# tests need a PostgreSQL server whose user can CREATE DATABASE:
export REPEATER_TEST_PG=postgresql+psycopg://postgres@127.0.0.1:5432/postgres
python -m pytest -q
ruff check . && ruff format --check .
```

Requires Python 3.12+, ffmpeg and PostgreSQL 16. Each test session creates and drops its own database.

## Deploy

See [docs/ar/00-deploy.md](docs/ar/00-deploy.md): DNS record, GitHub secrets, a read-only "Server inspect" workflow to run first, then the "Deploy" workflow or `deploy/bootstrap.sh`.
