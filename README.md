# Trackify Studio – Invoice Generator (SaaS foundation)

## Phase 1 features
- Self-service signup and login
- Per-user data separation for customers, products, invoices, invoice items and company settings
- Free trial enforcement: 7 days OR 5 invoices, whichever comes first
- Legacy SQLite data migration: existing records are assigned to the original `admin` account
- User-specific invoice numbering and PDF storage
- Secure password hashing, secure session defaults, and POST-only delete actions

## Local run
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
export SECRET_KEY="replace-with-a-long-random-secret"
python app.py
```
Open http://127.0.0.1:5000 and create an account. The old ZIP did not include a database file, so no default admin account is created on a fresh install. Create the first platform admin through a controlled database operation before launch; do not expose public admin signup. Existing installations retain their old admin account.

## Render deployment notes
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Set `SECRET_KEY` to a long random value.
- Set `DATA_DIR` to the mount path of a persistent disk (for example `/var/data`).
- Attach a persistent disk at that same path; SQLite and generated PDFs are stored under it.
- Use HTTPS. Set `COOKIE_SECURE=1` once HTTPS is enabled.

## Important before public sales
Phase 1 is the tenant-isolation and trial foundation, not a completed paid SaaS launch. Phase 2 must add verified payment webhooks, lifetime-license activation, purchase receipts, email verification/password reset, CSRF protection, rate limiting, backups, and a production PostgreSQL migration before scaling. Never mark an order paid based only on a browser redirect.
