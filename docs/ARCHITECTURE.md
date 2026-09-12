# Architecture

This document maps the system as it exists in the code. Module paths are real; behavior descriptions are checkable against the referenced files.

## Component map

| Area | Module(s) | Responsibility |
|---|---|---|
| Entry point | `app.py`, `streamlit_app.py` | Streamlit page config, secrets bridge (`st.secrets` → `os.environ`), sidebar (mode, coach toggle, cost chip), page router. `streamlit_app.py` is a one-line shim so Streamlit Cloud deploys land on `app.py`. |
| Pages | `ui/pages/record_page.py`, `audit_page.py`, `admin_page.py`, `how_it_works_page.py` | Record (capture → swarm → review → sign → export), per-encounter audit log, Texas retention sweep admin, static explainer. |
| UI components | `ui/components/*` (transcript panel, agent swarm, tooth chart, review panel, validator panel, attestation, export buttons, live recording, recommendations), `ui/theme.py`, `ui/styles.py` | Rendering only; no business logic beyond shape-adapting swarm output for display. |
| Orchestration | `agents/swarm.py` | `Orchestrator` + `SwarmRun`: runs the five-stage pipeline, collects `AgentResult`s, exposes `audit_records()` for persistence/cost. |
| Agents | `agents/base.py`, `clinical_agents.py`, `compliance_agent.py`, `second_opinion_agent.py`, `coach_agent.py` | `BaseAgent`/`AgentResult` contract; Scribe + Coder (LLM); Compliance (deterministic TSBDE checklist); Second-Opinion (LLM peer review); live Coach (LLM tool-use loop + 6 deterministic tools). |
| LLM access | `core/llm_client.py` | The single canonical Claude client: demo mode, JSON-mode with one retry, per-call `LLMCall` audit record. Every pipeline LLM call goes through it. |
| Prompts | `prompts/soap_prompt.py`, `prompts/clinical_prompts.py`, plus the Coach system prompt in `agents/coach_agent.py` | Prompt builders that inject the glossary, CDT allow-list, visit-type hints, and blank SOAP template. No prompts inline elsewhere in agent code. |
| Validation | `core/soap_validator.py` | Four-layer validator (structural / grounding / CDT / Texas) producing a `ValidationReport` with a 0–100 signability score. |
| Data assets | `data/*.json`, `data/tooth_norm.py`, `data/surface_norm.py`, `core/glossary_loader.py` | JSON Schema for SOAP, blank Texas template, 8 visit-type templates, dental glossary (incl. ASR corrections and drug watch-lists), CDT allow-list (39 codes) + catalog (51), tooth/surface normalizers, cached loaders. |
| Audio | `audio/transcript_types.py`, `deepgram_stt.py`, `live_streaming.py`, `diarization.py`, `post_correction.py`, `tts_synthesis.py`, `utils/audio.py` | Vendor-neutral transcript types; Deepgram REST (files) and WebSocket (live) paths; provider/patient role heuristics; two-pass post-STT lexical correction; TTS dialogue synthesis for pipeline testing; pre-STT audio DSP (high-pass, denoise, normalize, quality score). |
| Persistence | `storage/db.py`, `storage/retention.py` | SQLite schema (encounters, transcripts, versioned soap_notes, audit_log, attestations, exports) and the two-step Texas retention sweep. |
| Exports | `exports/soap_docx_template.py`, `exports/soap_pdf_template.py` | Printable DOCX/PDF from the structured SOAP JSON, including compliance block and attestation with AI-assisted disclosure. |
| Cost | `core/cost.py` | Token → USD computation from audit records, per-agent breakdown, demo calls priced at $0. |
| Legacy (still tracked, superseded) | `core/config.py`, `core/db.py`, `core/state.py`, `core/soap_schema.py` (flat-note mapping), `utils/llm.py`, `utils/fixtures.py`, `utils/tooth_norm.py`, `utils/surface_norm.py`, `utils/text_correction.py` (partially reused), `exporters/docx_export.py`, `exporters/pdf_export.py`, `ui/components.py` | An earlier iteration's config, dual-provider client, flat `SoapNote` state, and exporters. The current pipeline does not depend on them, with one exception: `audio/post_correction.py` imports phonetic primitives from `utils/text_correction.py`. |

## Data flow, end to end

1. **Capture** (`ui/pages/record_page.py`). Three input modes: paste a `Doctor:/Patient:` transcript, upload prerecorded audio, or record live in the browser. A fourth path synthesizes two-voice demo audio via TTS (`audio/tts_synthesis.py`: ElevenLabs preferred, OpenAI TTS fallback) to exercise the real STT path without a live patient.
2. **Speech-to-text** (`audio/deepgram_stt.py`, `audio/live_streaming.py`). Uploaded files go through optional DSP preprocessing (`utils/audio.py`) then Deepgram's prerecorded REST endpoint with diarization and a capped dental keyword boost (≤50 terms sourced from the glossary). Live audio goes over a Deepgram WebSocket: `streamlit-webrtc` frames are normalized by a PyAV resampler to 16-bit mono 16 kHz before sending; interim and final transcripts stream back on Deepgram's thread into a lock-protected `LiveDeepgramSession`.
3. **Post-STT correction** (`audio/post_correction.py`). Two passes: an explicit ASR-corrections dictionary from the glossary, then a conservative phonetic + edit-distance fuzzy pass (including a "glue" repair for split words) that only snaps to known dental vocabulary. Every correction is recorded in an audit list.
4. **Diarization roles** (`audio/diarization.py`). Deepgram speaker tags are mapped to provider/patient with lexicon and question-ratio heuristics; extra speakers become "assistant".
5. **The swarm** (`agents/swarm.py`). See orchestration analysis below. Output: a SOAP dict, a validation report, and per-stage `AgentResult`s with LLM audit records.
6. **Review and attestation** (`ui/components/*`). The UI shows the SOAP, tooth chart, validator issues grouped by severity with a signability chip, and the Second-Opinion flags. The attestation block collects provider name, license, and statement text, and its sign button is disabled until the caller passes `can_sign` (the validator gate).
7. **Export** (`exports/*`). DOCX and PDF render the structured SOAP with the TSBDE checklist and an attestation section carrying the AI-assisted disclosure; JSON export is the raw SOAP.
8. **Persistence** (`storage/db.py`). Encounters, transcripts (tagged by source), versioned SOAP notes with validation JSON and score, one audit row per LLM call (model, tokens, latency, prompt hash), attestations (with a signature hash), and export records (with file SHA-256).

## Orchestration analysis

**The pipeline is strictly sequential, by design.** Each stage consumes the previous stage's output: Compliance and Coder read the Scribe's SOAP; the Validator needs the SOAP plus the Coder's billing block; Second-Opinion reviews the completed note. There is no fan-out to parallelize — the data dependency chain is linear. Stage order and short-circuiting are visible in `Orchestrator.run_streaming()`:

- **Scribe** (LLM, JSON mode). On error (non-JSON twice), the run aborts — nothing downstream can work without a SOAP.
- **Compliance** (deterministic, no LLM). Ticks the 9-field TSBDE checklist mechanically; missing items degrade status to warn/error but never block the pipeline.
- **Coder** (LLM, JSON mode, temperature 0). Skipped entirely when the SOAP documents no procedures. Null-code entries (procedure documented but no allow-listed code fits) are dropped from billing; the Validator warns about them separately. A deterministic post-processor upgrades composite codes by surface count (D2391→D2392/3/4).
- **Validator** (deterministic). Reported in the swarm as a stage result so the UI treats it uniformly.
- **Second-Opinion** (LLM). Failure is non-fatal: the stage degrades to a warn with an empty flag list rather than blocking sign-off.

**Streaming, not batching.** `run_streaming()` is a generator yielding each `AgentResult` as it completes; the blocking `run()` just drains it. This exists purely so the UI can animate per-stage progress without threads.

**Async lives only at the audio edge.** Deepgram's WebSocket callbacks fire on the SDK's own thread; `LiveDeepgramSession` marshals them into state guarded by a `threading.Lock`, and the Streamlit fragment polls an immutable `snapshot()` every ~0.5 s. There is also an `asyncio` generator variant (`deepgram_stt.stream_microphone`). The LLM pipeline itself is synchronous — appropriate, since its stages cannot overlap.

**The Coach is a separate, event-driven agent — not a pipeline stage.** During live recording it is invoked on speaker-turn change or a 15-second ceiling (debounced in `ui/components/live_recording.py`), runs an Anthropic tool-use loop bounded at 5 turns over six pure-Python tools (drug-interaction check, glossary lookup, CDT candidate search, pulpal-status heuristic, visit-type requirements, TSBDE anchor check), and emits at most 3 recommendations per call. A fingerprint dedupe cache prevents the same recommendation firing twice per encounter. Its system prompt requires every recommendation to carry either a verbatim transcript quote or a tool name — the same grounding discipline as the Scribe, enforced at a different layer (prompt + tool design rather than validator).

**One display caveat, honestly noted:** the UI swarm panel (`ui/components/agent_swarm.py`) renders seven named agent cards (adding Triage, Terminologist, Reviewer), but the pipeline produces five stage results with different names; the three extra cards never receive live status. The five-stage pipeline described above is the ground truth.

## State and context engineering

**Session state.** Streamlit `st.session_state` holds the UI mode (`ds_mode`), coach toggle, last-run cost, the live `LiveDeepgramSession`, and accumulated coach recommendations. State is per browser session and lost on restart unless persisted to SQLite.

**Persistent state.** `storage/db.py` is the current store (see schema in the file's docstring); columns are deliberately ANSI-portable for a later Postgres migration. `core/db.py` is a legacy, simpler store not used by the current pipeline.

**Context assembly is static and bounded.** Prompts are assembled from versioned JSON assets, not retrieval:

- The Scribe system prompt embeds the compact glossary, per-visit-type hints, and the entire blank SOAP template as the output shape.
- The Coder system prompt embeds the full 39-code CDT allow-list and states it is the complete permitted set.
- The Second-Opinion prompt embeds the glossary's drug watch-lists.
- The Coach user message is explicitly bounded: the rolling transcript, the SOAP draft truncated to 2,000 characters, and only the last 12 recommendation fingerprints.

Because the vocabulary is small (glossary ≈ 40 terms across categories, CDT allow-list 39 codes), full-injection is cheaper and more auditable than retrieval. Prompt hashes are recorded on every call, so any prompt drift is visible in the audit log.

**Output contract.** All LLM agents must return a single JSON object. `LLMClient.complete_json()` strips markdown fences, attempts brace-slice recovery, and retries exactly once with a corrective nudge at temperature 0 before reporting failure.

## Design decisions and trade-offs visible in the code

1. **One LLM chokepoint.** `core/llm_client.py`'s docstring forbids instantiating the SDK elsewhere; demo mode, auditability, and cost accounting all depend on it. The Coach's tool-use loop is the one deviation — it needs the raw `messages` API with `tools`, which the chokepoint doesn't expose (a noted consolidation target below).
2. **Demo-first design.** Every LLM and STT call site accepts a deterministic fixture path, so the full product runs with zero keys. The cost: demo fixtures are only clinically coherent for two locked cases, so `record_page._run_orchestrator()` silently downgrades demo mode to live for any unrecognized transcript — a routing subtlety documented in that function.
3. **Deny-by-default billing.** The Coder may only emit allow-listed CDT codes; the Validator independently re-checks format and membership, treating unknown codes as errors ("likely hallucinated"). Two layers because the first is a prompt (soft) and the second is code (hard).
4. **Grounding as substring containment.** Every clinical claim carries a `source_span`; the Validator normalizes both span and transcript (lowercase, strip punctuation) and requires containment. Simple and fast, but it cannot localize the quote or catch a quote used to justify the wrong claim — see Extending below.
5. **Deterministic compliance.** The TSBDE checklist and retention rules are plain Python (`compliance_agent.py`, `storage/retention.py`), on the stated principle that regulatory logic should not depend on model behavior. Retention purge is two-step and dry-run by default — never automatic.
6. **WebSocket over chunked REST for live STT.** `audio/live_streaming.py`'s docstring records the failure that motivated this: 1-second REST clips contain no complete utterance and return empty results. The WebSocket path streams interim words with diarization across the whole session. Keyword boost is deliberately omitted on this path (the REST keyword format is rejected by the WS API); the post-STT correction layer compensates downstream.
7. **Resampler as the audio contract.** Rather than guessing browser sample rates and channel layouts, every input frame is run through a PyAV `AudioResampler` to 16-bit mono 16 kHz — one canonical format, eliminating a class of "0 transcripts" bugs the comments describe.
8. **Signability as a linear penalty score.** 100 minus 25/error, 5/warning, 1/info, floored at 0. Blunt but legible: a provider can see exactly which issues cost what.
9. **Duplicated legacy layers kept in-tree.** Two DB layers (`storage/db.py` vs `core/db.py`), two export layers (`exports/` vs `exporters/`), two LLM clients (`core/llm_client.py` vs `utils/llm.py`), and duplicate normalizers (`data/` vs `utils/`) coexist; the current pipeline uses the first of each pair. This is transition residue, not intent — consolidation is the first item below.

## Extending this system

Grounded next steps that the current architecture makes natural:

1. **Consolidate the duplicated layers.** Retire `core/db.py`, `exporters/`, `utils/llm.py`, `core/config.py`, and the `utils/` normalizer copies in favor of their current counterparts, and move the Coach's tool-use loop behind `core/llm_client.py` (add a `complete_with_tools()` that returns an `LLMCall` like the other paths). This closes the one gap in the audit chokepoint and removes the main source of reader confusion.
2. **Prompt caching for the static prompt blocks.** The Scribe and Coder system prompts re-embed the full glossary, CDT list, and blank template on every call, and the audit log already stores prompt hashes — so cache hit rates are directly measurable. Anthropic prompt caching on those static system blocks would cut per-encounter input tokens substantially with no behavior change.
3. **Span-offset grounding.** Replace substring containment with character-offset spans (the transcript segments already carry timestamps). This enables the hover-highlighting the transcript panel is built for, catches duplicate-quote misuse, and makes grounding failures precisely localizable in the UI.
4. **Rebuild the evaluation harness against the swarm's public API.** `eval/reports/baseline.json` preserves the metric shapes a previous harness produced (entity recall/precision, CDT recall@k, signability aggregate), but the generating code is no longer in the repo. The demo fixtures and two locked test cases are a ready-made golden dataset seed; see EVALUATION.md for the proposed design.
5. **Postgres migration behind the existing schema.** `storage/db.py` was written ANSI-portable for exactly this. Moving to Postgres (with encryption at rest) is the storage prerequisite for multi-user deployment described in HARDENING.md, and requires no schema redesign.
