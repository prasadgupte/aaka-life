# Security Policy

## Scope

Aaka is a personal assistant that runs on your own machine and VPS. There is no multi-tenant infrastructure or shared backend — all data stays on hardware you control.

Areas in scope for security reports:

- Authentication bypass in the Telegram/WhatsApp channel gate
- Credential or token leakage via logs, queue DB, or API responses
- Command injection via message parsing
- Insecure defaults that expose data to unintended senders

Out of scope: denial-of-service against a personal instance, issues in third-party dependencies (report those upstream).

## Reporting a vulnerability

Email: **prasadgupte@gmail.com** with subject `[aaka security]`.

Please include:
1. What the vulnerability is
2. How to reproduce it
3. What impact it could have

Response target: 5 business days for acknowledgement, 30 days for a fix or mitigation.

## Known design decisions

- **Channel gate** — unknown Telegram senders get a one-line reply with their user ID and no other data. Group messages from unknown senders are silently dropped.
- **OAuth credentials** — `config/credentials.json` contains a Desktop app client secret. Desktop app secrets are intentionally distributable (same model as `rclone`, `gcloud auth login`, and similar tools). The token produced by OAuth is yours and lives only on your machine.
- **SQLite queue** — `butler.db` is local-only. The VPS sync copies it over SSH. No external database.
