# messaging

Templated transactional email pipeline.

- **Email templating is two-layer.** `messaging/template_definitions.py` declares allowed template keys + a context dataclass per key (the dataclass is the contract — `EmailTemplateService.send_email` rejects mismatched context types). The HTML/subject body lives in `EmailTemplateVersion` rows in the DB, edited via admin.
- `send_email` renders, persists an `OutboundEmail` audit row, then enqueues `send_outbound_email_task` which actually calls Django's email backend and updates status. Failed sends can be requeued from the `OutboundEmail` admin.
- **Adding a new template = add a definition entry *and* create the DB row.**
- Allauth's transactional emails (password reset, verification) also flow through `OutboundEmail` + the email queue via `CoreAccountAdapter.send_mail`, though they render from allauth's file-based templates rather than DB rows.
- **Domain apps register their own templates.** `register_template_definition(TemplateDefinition(...))` adds a key + context dataclass to the registry; `guests/email_contexts.py` does this at import time from `GuestsConfig.ready()`, and seeds the DB rows with a data migration so a fresh clone can send. `messaging` never imports a domain app.
- **`EmailTemplateService.queue_email(...)` returns the `OutboundEmail`** it created (same arguments as `send_email`, which returns the task result). Use it when a caller needs to link the row, as campaigns do.
- **`EMAIL_RECIPIENT_ALLOWLIST`** (settings, from env) makes `_send_outbound_email` redirect any off-list recipient to the first allowed address with `[to: original]` in the subject; the row keeps the real recipient. Staging only.
