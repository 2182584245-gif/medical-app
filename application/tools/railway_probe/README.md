# Temporary credential-free Railway probe

Deploy **only this directory**, never the application root or `.local` directory.
No variables other than Railway's injected `PORT` are needed. Do not add database
URLs, passwords, API keys, certificates, or account tokens to this service.

- Railpack detects Python from `requirements.txt`, reads `.python-version`, and
  uses the start command in `railpack.json`: `python -B probe.py`.
- Set the service health check to `/health`; generate a public HTTPS domain.
- Start-up performs one DNS lookup and at most one TCP connection to the fixed
  Supabase pooler host on port 5432, within a shared five-second deadline. It
  sends no database-protocol bytes and closes the socket immediately.
- Start-up stdout contains only `railway_probe_start` and fixed `dns`/`tcp` enums.
- GET `/health` returns only `{"status":"ok"}`. GET `/probe` returns the cached
  network enums and redacted peer/header classifications. It does not repeat the
  outbound check. Other paths are 404 and write methods are rejected.
- To check edge header overwrite behavior, send synthetic `X-Real-IP`,
  `X-Forwarded-For`, and `CF-Connecting-IP` values of `198.51.100.27`. The response
  says only whether each value was absent/duplicate/valid and matched that fixed
  sentinel. It never returns raw IPs, hostnames, headers, or environment values.
- This small stdlib HTTP server is a temporary diagnostic only, not the health
  application or a production server. Stop/remove its deployment after review so
  it does not continue consuming trial credits. Do not keep a secret-bearing
  service and this probe in the same deployment.
- `tcp=connected` is not proof of PostgreSQL authentication, TLS trust, schema
  readiness, or production safety. A failed single-IP attempt is not proof that
  every possible pooler address is unavailable.

Official deployment references checked 2026-09-07:

- https://railpack.com/languages/python/
- https://railpack.com/config/file/
- https://docs.railway.com/deployments/healthchecks
- https://docs.railway.com/networking/public-networking/specs-and-limits

`railway.json` was intentionally not added: Railway's current Config as Code
reference marks that legacy format deprecated for new deployments.
