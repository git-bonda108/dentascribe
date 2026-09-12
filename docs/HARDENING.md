# Hardening

Current security posture as found in the code, an honest statement of the health-data situation, and a staged ladder from the present single-tenant MVP to a production deployment. Claims about the present are checkable against the referenced files; the ladder is forward-looking guidance grounded in what the code actually is.

## Current posture

**Authentication and authorization: none.** The Streamlit app has no login. Anyone who can reach the port can run the pipeline, read the audit page, and — most sensitively — use the Admin page's retention purge, which hard-deletes encounter data (mitigated only by its two-step, dry-run-by-default design in `storage/retention.py`). Provider identity in generated notes is currently hardcoded demo metadata in `ui/pages/record_page.py:_build_metadata()`, which its own docstring marks as a hardening TODO.

**Secrets handling: correct by discipline.** No credentials exist in the repository (verified at HEAD across all tracked files). All keys are read from environment variables; `.env` is gitignored and `.env.example` ships empty values; Streamlit Cloud secrets are bridged into the environment at startup (`app.py`). Keys are never persisted or logged by application code. There is no secret-manager integration and no rotation story — keys live wherever the process environment does.

**Error handling: graceful degradation, sometimes too quiet.** The dominant pattern is defensive try/except that degrades functionality rather than crashing: STT preprocessing failures pass audio through unchanged, LLM failures become status records, WebSocket errors land in session state for the UI. The cost is a number of `except Exception: pass` blocks (e.g., DB init in `app.py`, correction attachment in `audio/deepgram_stt.py`) that swallow failures without any log line — acceptable for a demo, an observability gap for production.

**Observability: audit trail yes, operational telemetry no.** Every LLM call produces an `LLMCall` record (model, token counts, latency, prompt hash, status) persisted to the SQLite `audit_log` table and surfaced on the Audit page, with per-run cost computed from real token counts (`core/cost.py`). There is no structured application logging, no metrics endpoint, no error tracking, and no alerting.

**Data at rest: unencrypted SQLite.** Encounters, full transcripts, SOAP notes, attestations, and export records live in a local SQLite file. `storage/db.py`'s docstring is explicit: do not store PHI here without disk-level encryption. Attestations store a signature hash and exports store a file SHA-256, which gives basic tamper-evidence but not immutability.

**Transport and network:** whatever Streamlit serves — no TLS termination, no rate limiting, no session management in the app itself. The live-microphone path uses WebRTC (`streamlit-webrtc`), which additionally needs STUN/TURN configuration to work reliably anywhere but localhost.

**Model-facing input:** transcripts are untrusted input that flows into prompts. The design already limits the blast radius: all agent outputs must be JSON conforming to a schema, billing codes are validated against a hard allow-list in code, every clinical claim must quote the transcript verbatim, and the compliance/validator layers are deterministic Python that a prompt cannot influence. Residual risk remains wherever model text is rendered (`unsafe_allow_html` is used in the UI theme) and in the Second-Opinion/Coach free-text fields.

## Health-data posture, honestly

This codebase processes what would be Protected Health Information the moment it is used with a real patient. The current state:

- **Everything in the repository is synthetic.** Sample transcripts, fixtures, and demo SOAP notes use invented patients, providers, and license numbers. No real patient data is present.
- **Live mode sends transcript content to third parties.** Anthropic (agents), Deepgram (audio), and optionally ElevenLabs/OpenAI (demo TTS) all receive encounter content. Production use with real patients requires Business Associate Agreements with each vendor in the path, and removal of any vendor that won't sign one.
- **The app is not HIPAA-compliant as deployed.** No access control, no encryption at rest, no transport security, no PHI-free logging guarantee, no audit immutability. Nothing in this repository should be pointed at real patients until Stages 1–4 below are complete.
- **What the design already gets right for a clinical context:** a mandatory provider attestation gate (sign-off blocked until signability ≥ 85 with zero validator errors), an AI-assisted disclosure embedded in the attestation statement and printed on every export, per-call audit records, Texas retention rules implemented as reviewable code with a deliberately manual purge, and a `.gitignore` that pre-blocks common PHI paths (`patient_data/`, `recordings/`, `*.db`).
- **Not a medical device claim:** the system drafts documentation for provider review; it does not diagnose, and the Coach prompt explicitly forbids clinical opinions. It is not FDA-cleared and the README says so. Keeping the provider-in-the-loop gate intact is a safety property, not just a compliance one.

## Ladder to production

### Stage 1 — Identity and keys
- Put authentication in front of the app (OIDC via a reverse proxy, or an identity-aware load balancer); scope the Admin page to an admin role.
- Replace the hardcoded encounter metadata with real provider identity from the auth layer — this simultaneously fixes documentation integrity (correct license number on every note).
- Move API keys into a secret manager with rotation; separate keys per environment.
- Add a lockfile / pinned dependency set and a CI job that runs the 102-test suite on every change.

### Stage 2 — Monitoring
- Introduce structured, PHI-free application logging (IDs and hashes, never transcript text — the prompt-hash pattern in `LLMCall` is the template) and replace silent `except: pass` blocks with logged warnings.
- Error tracking and alerting; cost alerts on the per-run token accounting that already exists.
- Health checks for the Deepgram WebSocket and Anthropic API paths; surface `LiveDeepgramSession.last_error` operationally, not just in the UI.

### Stage 3 — Deployment
- Containerize; TLS everywhere; deploy a TURN server so live-mic capture works across clinic networks.
- Migrate persistence to Postgres with encryption at rest — the schema in `storage/db.py` was written ANSI-portable for exactly this — plus backups and tested restore.
- Encrypt or eliminate local export files; serve exports from the store that records their SHA-256.
- Wire the retention sweep to a scheduled job that only ever *flags* (the two-step human purge stays).

### Stage 4 — Compliance
- BAAs with Anthropic and Deepgram (and any TTS vendor kept in production); drop demo-only vendors from the live path.
- Evaluate PHI de-identification before LLM calls for defense in depth.
- Append-only or externally anchored audit log (the audit table is the right shape; it needs immutability).
- License the full CDT code set from the ADA (the bundled 39-code subset is development-only).
- Legal review of the attestation and AI-disclosure language; extend the retention module beyond Texas if deploying in other states; incident-response and access-review procedures.

## Secrets removed from HEAD

None — a full sweep of every tracked file found no credentials, tokens, key files, or credentialed connection strings at HEAD. (Unrelated hygiene performed in the same pass: a stray tracked runtime log that exposed the development machine's network addresses was removed and the pattern gitignored.)
