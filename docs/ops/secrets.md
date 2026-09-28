# Secrets: where they live and how to rotate them

PLAN §5.3: all configuration comes from the environment; secrets live **only** on the VM, root-owned `0600` files
under `/srv/flarize/`, injected by compose `env_file`. Integration secrets (Twilio, Bunny, SMTP) that Admin edits
in Studio are stored Fernet-encrypted in `company_integration`. Nothing secret is ever committed: `.gitignore`
excludes `.env*`, `var/` and dumps; the pre-commit hooks `forbid-secret-files`/`forbid-data-dumps` refuse keys,
certificates and dumps; gitleaks runs in pre-commit and CI (`.gitleaks.toml`, including our `fl_…` agent-token
format).

## Inventory

| Secret | Where | Read by | Rotation |
|---|---|---|---|
| `SECRET_KEY` | `/srv/flarize/.env` | Django (signing: media signed URLs, document download links) | [1] |
| JWT RS256 key pair | `/srv/flarize/keys/jwt_private.pem`, `jwt_public.pem` (0600/0644, mounted read-only at `/run/flarize-keys`) — `JWT_PRIVATE_KEY_PATH`/`JWT_PUBLIC_KEY_PATH` in `.env` | api | [2] |
| `FERNET_KEYS` | `/srv/flarize/.env` (comma-separated, first key encrypts) | api, workers (`company_integration.config`, bank account numbers, blog revalidation secret) | [3] |
| App DB role password (`flarize_app`) | `/srv/flarize/.env` (`DB_USER`/`DB_PASSWORD`) + PgBouncer `userlist.txt` | api, workers, beat (through PgBouncer) | [4] |
| Owner DB role password (`flarize_owner`) | `/srv/flarize/owner.env` | the one-off `migrate` service only | [4] |
| Postgres superuser | `/srv/flarize/db.env` (`POSTGRES_PASSWORD`) | the `db` container at first init | [4] |
| PgBouncer auth file | `/srv/flarize/pgbouncer/userlist.txt` (SCRAM verifiers) | pgbouncer | [4] |
| SMTP / Twilio / Bunny | Studio → Settings → Integrations (`company_integration`, Fernet). Env fallbacks `EMAIL_HOST_PASSWORD`, `BUNNY_STORAGE_ACCESS_KEY` in `.env` | api, workers | [5] |
| Office agent tokens (`fl_<prefix>_<secret>`) | only their sha256 in `core_service_credential`; the token is shown once | agents | [6] |
| Terminal ADMS tokens | only their sha256 on `devices_device` | terminals | Studio → Devices → ADMS enable (re-issue) |
| Smoke-test user | `/srv/flarize/smoke.env` (`SMOKE_EMAIL`, `SMOKE_PASSWORD`) — a staff user with the `dashboard.view`-only role | `deploy/release.sh` | [7] |
| TLS certificates | `letsencrypt` volume (certbot renews every 12 h) | nginx | automatic |

File permissions: `chown root:root /srv/flarize/*.env /srv/flarize/keys/* /srv/flarize/pgbouncer/userlist.txt &&
chmod 0600` on each (the public JWT key may be `0644`). Containers run as uid 10001 and read keys through the
read-only mount, never from the image.

## Rotation procedures

**[1] `SECRET_KEY`.** Generate `python -c "import secrets; print(secrets.token_urlsafe(64))"`, replace it in `.env`,
run `deploy/release.sh <current sha>`. Effect: outstanding media signed URLs and document download links (≤ 10
minutes old) stop working; nothing else (JWTs are RS256, not `SECRET_KEY`).

**[2] JWT key pair.** Tokens carry no key id, so a rotation signs everyone out (15-minute access tokens, refresh
tokens bound to sessions). Generate `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out
jwt_private.pem && openssl pkey -in jwt_private.pem -pubout -out jwt_public.pem`, replace both files, redeploy.
Do it outside office hours; the Studio BFF sends users back to the login page on `token_invalid`.

**[3] `FERNET_KEYS`.** Zero-downtime, three steps:
1. prepend the new key: `FERNET_KEYS=<new>,<old>`; redeploy (new writes use the new key, old values still decrypt);
2. `docker compose -f deploy/docker-compose.yml exec api-a python manage.py reencrypt_secrets` (every
   `EncryptedTextField`/`EncryptedJSONField` of every model is re-encrypted under the first key; `--dry-run` counts
   first; a value no key can decrypt aborts that table unchanged — fail closed);
3. remove the old key: `FERNET_KEYS=<new>`; redeploy.
Never remove a key before step 2 finished: the app refuses to decrypt (fail closed) and integrations stop.

**[4] Database passwords.** `ALTER ROLE flarize_app PASSWORD '…'` (as superuser), regenerate its SCRAM verifier in
`userlist.txt` (`SELECT rolname, rolpassword FROM pg_authid WHERE rolname='flarize_app'` → `"flarize_app"
"SCRAM-SHA-256$…"`), update `.env`, then `docker compose restart pgbouncer` and `deploy/release.sh <current sha>`.
The owner role only needs `owner.env` updated. Keep the old password valid until the release finished (PgBouncer
holds server connections for `server_lifetime`).

**[5] Integration secrets.** Studio → Settings → Integrations: send the new value (secrets are write-only; the
screen shows only whether one is set). The next e-mail / upload / OTP uses it — nothing is cached. Every change is
in the audit log (`company.integration_updated`, values masked). Env fallbacks are used only while no enabled
integration row exists.

**[6] Agent tokens.** Studio → Devices → Agents → Rotate token (shown once; `agent.ini` download) — the old token
stops working immediately. Revoke a lost agent with Revoke.

**[7] Smoke user.** Studio → Users → force reset, set a new password via the link, update `smoke.env`.

## If a secret leaked

1. Rotate it now (above). 2. Search the audit log for its use window (`audit/?action=accounts.login*`,
`core.service_credential_*`). 3. If it reached git: rotate first, then purge history (`git filter-repo`) and
force-push only with the team's agreement — rotation is what protects you, not the purge.
