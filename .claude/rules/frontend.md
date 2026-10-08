---
paths:
  - "templates/**"
  - "**/templates/**"
  - "static/**"
---

# Frontend

- Tailwind + DaisyUI. The Tailwind binary lives at `static/css/tailwindcss` (installed by `make setup` via daisyui.com/fast). Input is `static/css/input.css`, output `static/css/output.css`. `make dev` does a one-shot build only — for live reload during CSS work, run `make css-watch` in a second terminal.
- Templates: project-level overrides in `templates/` (allauth, error pages); app templates under `core/templates/core/`, `pages/templates/pages/`, `guests/templates/guests/`, and `messaging/templates/`.
- HTMX is available globally (`django-htmx` middleware + `request.htmx`).
