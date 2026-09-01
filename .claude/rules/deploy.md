---
paths:
  - "fly*.toml"
  - "entry.sh"
  - "Dockerfile*"
  - ".github/**"
---

# Deployment

Fly.io single-machine deploy backed by Postgres (attach a cluster with `fly postgres attach`, which sets the `DATABASE_URL` secret — see README). Machines are stateless: no volumes, no Litestream. `entry.sh` is the container entrypoint — it starts as root, re-execs itself as the `nonroot` user, then runs migrations, `createcachetable`, the `steady_queue` worker, and gunicorn (3 workers). If any supervised process dies, the entrypoint exits so Fly restarts the machine. Two Fly apps: `fly.toml` (production) and `fly.staging.toml`. The CI workflows live in `.github/_workflows/` and are renamed to `.github/workflows/` by `make setup`.
