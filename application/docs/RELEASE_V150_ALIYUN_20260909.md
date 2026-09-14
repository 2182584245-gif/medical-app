# v1.5.0 Windows demo and Aliyun deployment receipt

This is an observed deployment record dated 2026-09-09, not a promise about future
availability. It contains no passwords, API keys, tokens, or private deployment keys.

## Actual production state

- Default endpoint: `https://39.106.166.15/aliyun`.
- Only local and Aliyun business modes remain in the current desktop UI.
- Supabase API container is stopped with restart policy `no`; `/supabase` and
  `/supabase/health/ready` return 410. Original Supabase data was not queried,
  altered, or deleted during this retirement.
- Aliyun database revision: `platform_0003`. The medical-category CHECK migration
  preserved every existing row; unrelated permissions and runtime passwords were
  not changed.
- Final API image: `sha256:01607fa7f6f892b04147c9d5f9ca21aaa02324c82f6b6e8080804eaf24be1279`.
- Final transfer image: `sha256:585dd32e0a502fc84a96145d8b185303d1819c8144d4fc4487fbc5f4c62d775f`.
- Caddy, PostgreSQL data volumes, internal certificate validation and API credential
  mounts were retained. PostgreSQL and API have no published host ports.
- Backup and capacity timers were verified active and enabled. Capacity check was
  healthy with approximately 14.1% disk usage.
- The temporary deployment SSH public key was removed after acceptance; an exact
  key-only reconnection failed with `Permission denied`. Other public-key entries
  and the server password were preserved. New SSH deployment requires fresh authority.

## Data and online acceptance

- Appended the synthetic demo only: 5 users, 156 life records, 6 products, and
  related data; 449 inserted rows across the 29 business tables. No original
  target rows were merged or overwritten.
- Original example usernames/password hashes were retained. Cloud IDs are 24–28.
  Passwords are delivered separately in the private demo instructions, not here.
- Six image mappings correspond to five distinct PNG files, packaged as local
  content-addressed aliases matching the cloud rows. This is not arbitrary remote
  product-image downloading.
- Five exact demo accounts logged in and out after the final r2 deployment.
- Two independent clients for one member verified stable-request deduplication and
  record visibility. Encrypted mirror/outbox were reopened during a simulated
  disconnected stage. Only the exact synthetic acceptance record was deleted;
  both members' original 78 records each were preserved.
- Default authenticated AI proxy completed one tiny text and one streaming
  DeepSeek v4 Flash request. No real health context was submitted. Vision and
  physical microphone interaction were not included in these live tests.
- Post-release backup was restored into an owned disposable database; all 32
  tables, row counts and forced RLS were verified before removing only that
  disposable restore. The dump remains on the server, not an off-site backup.
- Backup SHA256: `d5a61657b0f70381ddc37886cff6653c84ba607164e709509cefaf46516421a2`.

## Windows release

- Version: 1.5.0, onedir demo only, no UPX, no Android rebuild.
- EXE SHA256: `d2274dd156f3917975a2bd43afa48090bc477da33e5d36fdf2e04774a2315855`.
- ZIP SHA256: `769dc0a289289cf86012e3ab764ed7c2be25d0190cb53b10b78117592f6ed179`.
- ZIP size: 399,067,642 bytes; expanded application: 3,773 files / 970,108,837 bytes.
- All 96 embedded application modules match the final source. Directory and ZIP
  validation use the independently reviewed six-image mapping and strict manifests.
- A newly isolated process ran for eight seconds with no error markers. This was
  not full native UI automation or a graceful-close acceptance test.
- Windows Defender custom scan found no threats. The executable is not commercially
  signed; this does not guarantee that every security product will approve it.
- Desktop tests: 879 passed, no failures or skips. Server suite: 1,191 passed,
  34 environment-gated skips, no failures. Skipped cases are not counted as passes.
- The last application-code fix allows at most five seconds of future `issued_at`
  clock skew, without extending expiry, maximum lease span, or rollback tolerance.

## Important delivery boundaries

The service was verified from the current PC using strict certificate validation
and no environment proxy, not from every mainland ISP or enterprise network.
Offline access is permission-limited and time-limited. Reminder popups require
the app to be running and the user logged in. Customer support and selected
life-generation menu items remain explicitly marked as upcoming; purchases are
simulated. Advisor AI summaries retain their existing personal/local channel.
No Git commit or push was performed during this release operation. Earlier local
release artifacts were retained as history, not presented as the final demo.
