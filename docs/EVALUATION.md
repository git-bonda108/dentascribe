# Evaluation

An honest account of what is tested, what the code visibly defends against, and what a real evaluation harness for this system should look like. Nothing here is aspirational unless explicitly labeled **Proposed**.

## What exists: the automated test suite

`tests/` contains **102 tests, all passing** (`uv run pytest`, ~1 s, no network and no API keys — everything runs through demo mode and deterministic fixtures).

| File | Tests | What it covers |
|---|---|---|
| `tests/test_smoke.py` | 2 | Repo skeleton and manifest presence. |
| `tests/test_batch2.py` | 7 | SOAP JSON Schema shape, blank Texas template conformance, tooth normalizer (colloquial → Universal 1–32, FDI conversion), surface normalizer and surface counting. |
| `tests/test_batch3.py` | 14 | `LLMClient` demo mode (text + JSON), data-asset loaders, prompt builders, and the four-layer validator: a valid note passes, a hallucinated CDT code errors, an ungrounded claim errors, a missing license errors, score degrades with issues. |
| `tests/test_batch4.py` | 8 | End-to-end swarm runs in demo mode for both locked cases; CDT correctness; composite surface-count code upgrade; validator score; compliance checklist; drug-interaction flag surfaced by Second-Opinion; streaming yields stages in order; audit records are produced. |
| `tests/test_batch5.py` | 11 | Transcript parsing and speaker assignment, demo transcription passthrough, Deepgram keyword-boost selection, SQLite persistence round-trips, retention flagging and two-step purge (dry-run semantics). |
| `tests/test_batch6.py` | 5 | UI modules import cleanly; theme helpers; score-chip severity thresholds. |
| `tests/test_coach_agent.py` | 23 | All six deterministic Coach tools directly (drug interaction, glossary lookup, CDT candidates, pulpal heuristic, visit-type requirements, TSBDE anchor check), the demo-mode coach end-to-end, dedupe/fingerprint stability, and the fence-stripping JSON parser. |
| `tests/test_cost.py` | 6 | Token → USD math against published rates, zero-token and unknown-model handling, per-agent aggregation, demo calls priced at $0. |
| `tests/test_post_correction.py` | 8 | Dictionary ASR corrections apply verbatim; phonetic fuzzy pass catches near-misses and glued split words; protected English words are never corrupted; correct dental terms pass through. |
| `tests/test_soap_exports.py` | 9 | DOCX/PDF bytes are valid documents (magic numbers), all clinical sections render when populated, empty input produces a non-crashing blank, provider/patient identity appears in output. |
| `tests/test_tts_synthesis.py` | 9 | Dialogue script parsing, WAV stitching, resampling — the network-free parts of the TTS path. |

**What the suite does not cover:** any live LLM or STT call. All model-dependent behavior (Scribe extraction quality, Coder accuracy on unseen transcripts, Second-Opinion flag quality, live Coach tool selection) is untested by automation. The suite proves the deterministic machinery and the demo path, not model output quality.

## Metrics that exist in the repo, with sources

- **`eval/reports/baseline.json`** is a preserved report from a previous evaluation harness whose generating code **is no longer in the repository** (it was removed in an earlier cleanup commit; `eval/` now contains only this report and one smoke script). The file itself records, per demo fixture: entity_recall 1.0 (threshold 0.8), entity_precision 0.767 (threshold 0.4), CDT recall@k 1.0, and an aggregate signability of 0.982, stamped 2026-05-19 with `llm_provider: anthropic`. Treat these numbers as a historical snapshot: they are not reproducible from the current tree.
- **`eval/soap_validator_smoke.py` is stale and does not run.** It calls `SOAPValidator().validate(soap, lines, raise_on_error=False)` and reads `report.ok` with a 0.95 score threshold — but the current validator API is `validate(soap, transcript="")`, the report field is `valid`, and the score is an integer 0–100. It is a leftover from the removed harness's era, kept as a shape reference.
- Per-consultation cost figures shown in the UI sidebar (≈$0.02–0.10, plus ≈$0.10–0.20 with Coach) are estimates displayed in `app.py`; the actual measured cost per run is computed from real token counts in `core/cost.py` and shown after each live run.

## Edge cases the code visibly handles

Enumerated from the code, with locations:

**LLM layer** (`core/llm_client.py`)
- Non-JSON model output → fence stripping, brace-slice recovery, then exactly one retry with a corrective nudge at temperature 0; failure is reported as a status, never an exception.
- No API key or SDK not installed → demo mode with caller-supplied fixtures.
- API exception → captured into the `LLMCall` record (`status="error"`, message, latency), empty string returned.

**Pipeline** (`agents/swarm.py`, `agents/*.py`)
- Scribe failure aborts the run (nothing downstream is meaningful).
- Coder is skipped when no procedures are documented; null-code entries are dropped from billing and separately warned on by the validator.
- Second-Opinion failure degrades to a warn ("proceed with caution") with an empty flag list — it never blocks.
- Scribe metadata omissions are backfilled from encounter metadata.

**Validator** (`core/soap_validator.py`)
- Missing transcript → grounding layer skipped with an explicit warning rather than false failures.
- Null CDT codes → grounding check skipped for that entry (no claim to ground), warned in the CDT layer.
- Malformed code format and non-allow-list codes are distinct errors.

**Audio** (`audio/*`, `utils/audio.py`)
- DSP preprocessing failure (non-WAV input, missing scipy/noisereduce) → silent pass-through; STT proceeds on original bytes ("STT is the load-bearing thing").
- Deepgram unavailable (no key/SDK) → empty transcript returned so callers fall back to the paste/demo path.
- Live path: resample/send failures and WebSocket errors are captured into session state (surfaced in the UI) instead of raised across threads; transcript callbacks are fully defensive; the session auto-starts on first frame and keepalive is enabled for long silences.
- TTS provider chain: ElevenLabs failure falls through to OpenAI TTS; neither configured → explicit `RuntimeError` naming both env vars.

**Coach** (`agents/coach_agent.py`)
- Empty transcript → no-op. Tool dispatch is defensive against unknown names and bad arguments. The tool-use loop is bounded at 5 turns. Recommendations are capped at 3 per call and deduped by fingerprint across the encounter.

**Persistence / retention** (`storage/*`)
- Purge is dry-run by default and requires an explicit second step; naive date strings are coerced to timezone-aware UTC before arithmetic (a real bug noted in the docstring).

**UI routing** (`ui/pages/record_page.py`)
- Demo fixtures are only served for recognizably canonical sample transcripts; any other input (live recordings, edited text) silently routes to live mode so users never see a prebaked SOAP that contradicts their recording.
- Attestation is disabled until signability ≥ 85 and zero validator errors.

## Proposed: the evaluation harness this system should have

No model-quality harness currently exists. The design below is grounded in the shapes already present (demo fixtures, locked test cases, `baseline.json` metric names, the audit log).

**Golden dataset.** Start from the two locked cases and the fixtures in `utils/fixtures.py`; grow to ~25–50 transcripts spanning the 8 visit types in `data/visit_type_templates.json`. Each item: `{transcript, visit_type, expected: {exam_findings[], diagnoses[] (with ICD-10), procedures[], cdt_codes[], expected_flags[]}}`, authored by a dentist reviewer. Include adversarial items: transcripts that mention procedures that must map to `code: null`, drugs that must trigger interaction flags, and claims that must *not* appear (negative grounding).

**Metrics** (matching the preserved baseline shapes where sensible):
- Schema validity rate (layer-1 pass without error) — gate: 100%.
- Grounding pass rate (share of emitted claims whose `source_span` verifies) — gate: 100%; any failure is a release blocker since it is the product's core promise.
- CDT precision/recall vs expected set, plus a hard invariant: zero codes outside the allow-list post-validator.
- Entity recall/precision on teeth, surfaces, diagnoses (the baseline's `entity_recall` / `entity_precision`).
- Second-Opinion flag recall on planted safety issues (e.g., NSAID + ACE-inhibitor).
- Signability score distribution; cost and latency per encounter from the audit records the pipeline already emits.

**Mechanics.** A runner that executes the real swarm in live mode against the golden set, writes a timestamped report next to `eval/reports/baseline.json` (the directory's `.gitignore` already anticipates `eval-*.json`), and compares against the checked-in baseline with per-metric thresholds. Run on demand and before release; the deterministic 102-test suite remains the fast CI gate. Repair or delete `eval/soap_validator_smoke.py` as part of this work.

**STT track (separate).** Word-error-rate and dental-term-accuracy on a small recorded audio set, measured before and after `audio/post_correction.py`, to quantify what the correction layer actually buys.
