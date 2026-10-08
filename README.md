# sarahandfrankwedding

## Background

sarahandfrankwedding is...

## Requirements

1. `uv`

## Getting Started

1. Clone this repo and `cd` into it
1. `cp .env.example .env` and set `SECRET_KEY`
1. `make setup` — install deps, migrate, and create the `admin`/`admin` superuser
1. `cd static/css && curl -sL daisyui.com/fast | bash` — install Tailwind + DaisyUI
1. `make css` — watch and rebuild Tailwind CSS (separate terminal)
1. `make dev` — start the development server

Run `make help` to see all available commands.

## First Deploys

1. Create a Fly.io app for staging `fly apps create --name staging-<app-id> -o <staging-org-id>`
2. Create a Fly.io app for production `fly apps create --name <app-id> -o <org-id>`
3. Set appropriate secrets in the Fly.io console

## Database

Local development and tests run on SQLite (`db.sqlite3`, created by `make setup` — no services to install).
Staging and production run on Postgres: Django switches automatically whenever the `DATABASE_URL`
environment variable is set, so leave it unset locally.

On Fly.io, create a Postgres cluster and attach it to each app (attaching sets the
`DATABASE_URL` secret):

```sh
fly postgres create --name sarahandfrankwedding-db
fly postgres attach sarahandfrankwedding-db --app sarahandfrankwedding
fly postgres attach sarahandfrankwedding-db --app sarahandfrankwedding-staging  # or a separate staging cluster
```

Any other Postgres provider works too — just set the `DATABASE_URL` secret yourself
(`fly secrets set DATABASE_URL=postgres://...`).
