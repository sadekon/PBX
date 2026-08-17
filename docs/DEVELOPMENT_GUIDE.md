# Warden VoIP — Development Guide & Feature Status Ledger

> **Purpose:** the single living document tracking what is built, what is partially
> built, and what remains to finish, debug, and deploy every feature in this system.
> Update the status tables here whenever feature work lands.
>
> **Last audited:** 2026-07-20, against branches `auto-attendant` (open as
> [PR #2](https://github.com/sadekon/PBX/pull/2)), `sip-trunk` (current), and `DEV`.
> `voicemail-fix` merged to `DEV` via PR #1 and is no longer a live branch.
>
> Latest on `sip-trunk`: C4 call-originate primitive built and click-to-dial wired onto it;
> outbound caller ID now sends the extension's DID; inbound DID routing data layer (model,
> migrations, API, admin UI) landed — its SIP dispatch is the remaining piece.

## How this document is organized

Two axes, and only two:

1. **Tiers** (top-level sections) — how deeply a module is wired into the running
   system today. This is the honest status ledger.
2. **Core capabilities** (groups within each tier) — the platform capability that
   *gates* each feature. This is the planning tool: build or finish one capability
   and every feature grouped under it becomes finishable.

| Tier | Meaning | Where wired |
|------|---------|-------------|
| **0 — Core platform** | The engine everything depends on | `pbx/core/`, `pbx/sip/`, `pbx/rtp/`, `pbx/api/`, `pbx/utils/` |
| **1 — Wired features** | Instantiated on `PBXCore` at startup, config-gated | `pbx/core/feature_initializer.py` |
| **2 — API-only frameworks** | Full engine + DB persistence + REST CRUD, but **instantiated per-request** in `/api/framework/*` routes; not connected to the live call/media path | `pbx/api/routes/framework.py` |
| **3 — Orphaned modules** | Complete-looking modules with unit tests but **zero runtime references** | (nowhere) |

**Status legend:** ✅ working · 🔶 partial (works with known gaps) · 🧩 framework only (no call-path integration) · 🗃️ orphaned (unwired) · 🚧 active branch work

**Test rigs** (see [Deployed-testing rig matrix](#deployed-testing-rig-matrix)):
**A** = 2 softphones on LAN · **B** = provisioned hardphones · **C** = carrier SIP trunk · **D** = browser/HTTPS · **E** = external services (SMTP, push, LDAP, …) · **F** = full Docker stack

---

## Core capability map

The seven platform capabilities that gate feature completion. Features below are
grouped under the capability that *primarily* gates their deployed testing; a feature
with a second gate says so in its row.

| ID | Capability | Status | Built in | Finishing it unblocks |
|----|-----------|--------|----------|----------------------|
| **C1** | SIP signaling core — registration, dialogs, routing, transfer/hold | ✅ working (UDP-only) | `pbx/sip/`, `core/call_router.py` | Call-handling features work today; TLS/TCP transport is Phase-7 hardening. REFER transfer rewritten on `auto-attendant` (attended via Replaces per RFC 3891, blind via PBX-originated leg); pending field verification with Zultys ZIP phones |
| **C2** | RTP media relay & IVR plumbing — relay, DTMF (RFC 2833 + SIP INFO + in-band), prompt playback, recording tap | ✅ voicemail-fix merged to DEV; DTMF sources unified into one `DTMFMonitor` on `auto-attendant` | `pbx/rtp/`, `core/voicemail_handler.py`, `rtp/handler.py`, `rtp/dtmf_monitor.py` | Voicemail, AA, MoH, recording, paging; codec expansion slots in here |
| **C3** | Audio mixer (N-way media) | ✅ **built** — `MixBridge` primitive, tested; **no feature is wired onto it yet** | `pbx/rtp/mixer.py`, `pbx/rtp/codecs.py`, numpy G.711 LUTs in `pbx/utils/audio.py` | Conference audio, 3-way calling, barge/whisper/monitor, call screening — all now unblocked but still unwired (see C3 section below) |
| **C4** | Call-originate primitive ("PBX creates a leg to X, then bridges") | ✅ built on `sip-trunk` | `core/call_originator.py`, `core/call_router.py` (`_build_and_send_leg_invite`) | `CallOriginator.originate_call()`/`originate_and_bridge()`; click-to-dial wired onto it. Callback completion, predictive dialing, emergency-notification calls, operator console remain to be wired. Field-unverified against real phones. |
| **C5** | Trunk / PSTN connectivity | 🔶 outbound + inbound DID routing built on `sip-trunk`; **field-unverified** (no carrier account) | `features/sip_trunk.py`, `features/inbound_routing.py`, `sip/server.py`, `core/call_router.py` | E911 stack, LCR, STIR/SHAKEN, DNS SRV failover, fraud detection on real traffic, SBC validation |
| **C6** | Analytics tap — live audio/transcript/QoS feed into analysis engines | 🔶 recordings + RTCP QoS data exist; transcripts now persist to `call_transcripts` via `pbx/speech/store.py` (migration 1017), and the schema is deliberately shaped for live use — nullable `media_path`/durations, non-unique `call_id`. The RTP audio tap now exists (`pbx/rtp/tap.py`) and feeds two-channel call recording, so the blocker is gone: a live feed is now a `StreamTranscriber` implementation on an existing tap rather than new media-path work. **Post-call transcription is wired** (`speech/recording.py`): a finished recording is split per participant and each channel transcribed separately, so every segment carries `Segment.speaker` — attribution comes from the channel separation rather than a diarisation model. Channels below the noise floor are never submitted (`utils/audio.active_speech_seconds`), so a participant who only listened costs no model run and cannot hallucinate; silence *within* a channel is skipped by faster-whisper's `vad_filter`, which vosk has no equivalent of | `features/call_recording.py`, `rtp/tap.py`, `speech/recording.py`, `speech/store.py` | Speech analytics, voice biometrics, call tagging, quality prediction, recording analytics, conversational AI |
| **C7** | Data & event plane — CDR, webhooks, DB persistence, migrations | ✅ working | `features/cdr.py`, `features/webhooks.py`, `utils/database.py` | BI export, compliance, retention, residency, geo-redundancy replication build on it |

Features gated only by **credentials or a deployment surface** (SMTP, push keys,
LDAP, CRM APIs, HTTPS/browser) carry no C-number — their gate is **Rig E** or **Rig D**.

---

## Tier 0 — Core platform

The runtime is a single Python process: `main.py` → `PBXCore` (`pbx/core/pbx.py`, ~1 025 lines)
owns everything. `PBXCore.start()` boots, in order: security enforcement → SIP server →
Flask API server → DND scheduler → trunk registration → security monitor → Prometheus
collector → registration-expiry sweep.

On `auto-attendant`, `PBXCore` was split into dedicated modules — `transfer_handler.py`
(blind/attended/REFER transfer), `codec_negotiator.py` (phone-model detection + codec
compatibility), and `registration_handler.py` (SIP registration) — cutting `pbx.py` from
2 717 to ~1 025 lines. Internal handlers (`CallRouter`, `VoicemailHandler`, `TransferHandler`,
`CodecNegotiator`, `RegistrationHandler`) are public attributes on `PBXCore` with no
forwarding methods, so call sites use e.g. `pbx.transfer_handler.start_blind_refer_transfer(...)` directly.

| Component | Files | Status | Notes / remaining work |
|-----------|-------|--------|------------------------|
| SIP server (C1) | `pbx/sip/server.py` (~2 257 ln) | ✅ | **UDP only** — a single `SOCK_DGRAM` socket. No TCP or TLS transport, so no SIPS and no encrypted signaling to carriers/phones. Biggest core limitation. REFER/Replaces transfer parsing and SIP 3xx redirect (call-forwarding) handling landed on `auto-attendant`. |
| SIP message/SDP/transaction (C1) | `pbx/sip/message.py`, `sdp.py`, `transaction.py` | ✅ | Parser, SDP builder/negotiation, transaction state machine. |
| RTP relay + media (C2) | `pbx/rtp/handler.py` (~1 645 ln), `jitter_buffer.py`, `dtmf_monitor.py`, `rtcp_monitor.py` | ✅ | In-process relay, ports 10000–20000, RTCP QoS feed. G.711 µ/A end-to-end; **G.722 is present but broken** (see the C3 G.722 warning). DTMF (RFC 2833 + SIP INFO + in-band) unified into one `DTMFMonitor` shared by AA and voicemail IVR (`auto-attendant`). Mixer (C3) now built — `pbx/rtp/mixer.py`, G.711 only. |
| Call transfer (C1) | `pbx/core/transfer_handler.py` (~862 ln) | ✅ | Blind + attended transfer via REFER/Replaces (RFC 3891); invite-based transfer detection; mandatory Via/Max-Forwards on teardown; auto-attendant calls transfer via the RTP relay instead of REFER (PBX stays in the media path) and are held with MOH during the transfer. Pending field verification with Zultys ZIP phones. |
| Codec negotiation (C2) | `pbx/core/codec_negotiator.py` (~339 ln) | ✅ | Phone-model detection and codec compatibility, extracted out of `PBXCore`. |
| Registration handling (C1) | `pbx/core/registration_handler.py` (~314 ln) | ✅ | SIP REGISTER handling, extracted out of `PBXCore`. |
| Call state machine + router (C1) | `pbx/core/call.py`, `call_router.py` | ✅ | Dialplan patterns route to extensions, conference rooms (`2xxx`), parking (`7x`), queues (`8xxx`), voicemail, AA. Trunk routing added on `sip-trunk` branch. SIP 3xx redirect (call forwarding, e.g. a phone's "always forward") routed here on `auto-attendant`. Leg construction shared with `CallOriginator` via `_build_and_send_leg_invite()`. |
| Call originator (C4) | `pbx/core/call_originator.py` | ✅ | PBX-initiated call legs: `originate_call()` / `originate_and_bridge()`. Shares destination classification (`EXTERNAL_NUMBER_PATTERN`) and leg building with `CallRouter`, but keeps its own call lifecycle (no `original_invite`, own no-answer timer, `Call.originate_callbacks`). Field-unverified against real phones. |
| PBXCore + feature init | `pbx/core/pbx.py`, `feature_initializer.py` | ✅ | Static (not dynamic) initialization of ~30 Tier-1 features. |
| IVR handlers (C2) | `core/voicemail_handler.py` (~1 159 ln), `auto_attendant_handler.py`, `paging_handler.py`, `emergency_handler.py` | ✅ | Voicemail IVR fixes (DTMF reliability, prompts, barge-in) merged via `voicemail-fix` (PR #1). AA transfer now RTP-relay-based with MOH hold (`auto-attendant`); a transfer-failure TODO remains (see below). |
| REST API + admin UI | `pbx/api/` (22 route modules), `admin/` (19 TS pages) | ✅ | Flask app factory, auth, OpenAPI docs; Vite/TS frontend. Known debt: CSP `unsafe-inline` (≈130 inline `onclick`, ≈330 inline `style=` in `index.html`). |
| Config / DB / migrations (C7) | `pbx/utils/config.py`, `database.py`, `migrations.py`, `alembic/` | ✅ | YAML + `.env`; PostgreSQL with SQLite fallback. Debt: only 2 Alembic migrations — feature tables use runtime `CREATE TABLE IF NOT EXISTS`. |
| Security stack | `utils/security*.py`, `encryption.py`, `tls_support.py`, `audit_logger.py` | ✅ | FIPS-oriented encryption, threat detector, runtime security monitor (blocks startup on failed enforcement). TLS applies to the API, **not** SIP. |
| Admin HTTPS | `certs/`, `api.ssl.*` in `config.yml`, `scripts/generate_ssl_cert.py`, `setup_reverse_proxy.sh`, `letsencrypt_manager.py` | 🔶 | **Currently serving plain HTTP on :9000** — `api.ssl.enabled: false`. A self-signed cert exists (`certs/server.crt`, CN=localhost, SANs localhost/127.0.0.1 only, expires 2027-06) but is unused, and would still warn if enabled (untrusted issuer + hostname mismatch off-box). Finish = reverse proxy + Let's Encrypt (needs a public domain), or regenerate with LAN-IP SANs and trust locally for dev. Prerequisite for WebRTC mic access (Rig D). |
| Observability | `utils/prometheus_exporter.py`, `grafana/`, `prometheus.yml` | ✅ | Metrics collector started by core; Grafana dashboards shipped. Verify on Rig F. |
| Licensing | `utils/licensing.py`, `license_admin.py`, `api/license_api.py` | ✅ | Present and wired; verify enforcement behavior you actually want before selling/deploying. |

**Deployed-testing interface for Tier 0:** Rig A covers the whole signaling/media core
(register 2 softphones → call, hold, transfer, DTMF). Rig F validates persistence,
metrics, and the health check.

---

## Tier 1 — Wired features (initialized on PBXCore)

Constructed in `pbx/core/feature_initializer.py` — always-on or gated by a `config.yml`
flag. Grouped by gating capability.

### C1 — Call handling & devices (signaling core ✅ — testable today)

| Feature | Module | Config gate | Status | Remaining work | Test rig |
|---------|--------|------------|--------|----------------|----------|
| Call queues | `call_queue.py` + `core/queue_handler.py` | `features.call_queues` | ✅ | Full ACD: DB-persisted queues/membership/agent state (migration 1013, seeded once from `config.yml` `queues:`), REST API (`/api/queues`), admin UI page. Entry via direct dial `8xxx`, DID→`8xxx`, AA menu, or REFER transfer; callers park on MOH, agents rung serially per strategy (`round_robin`/`least_recent`/`fewest_calls`/`random`; `ring_all` deferred) through per-attempt blind TransferSessions with per-queue ring timeout and auto-pause after N misses. Overflow (full/no-agents/max-wait) → queue mailbox (default = queue number, retrievable via `*8xxx`). Agent star codes `*61`/`*62`. Needs field verification on Rig A (manual SIP recipe in plan). | A |
| Call parking | `call_parking.py` | `features.call_parking` | 🔶 | Slot management wired to `7x` pattern. Verify park/retrieve with real phones (REFER/replaces behavior). | A |
| Find me / follow me | `find_me_follow_me.py` | `features.find_me_follow_me` | 🔶 | **Sequential ring implemented; simultaneous deferred.** The feature module is self-contained: it keeps the config store (REST API `/api/fmfm/*`, PostgreSQL `fmfm_configs` — still created by the module rather than `utils/migrations.py`, admin UI) and now also executes the plan. `FindMeFollowMe` takes `pbx_core` from `FeatureInitializer` (without it, it stays a config store and declines every call), and consults its own `get_ring_strategy()` from `_dial_to_internal_extension()` — deliberately *before* `resolve_extension()`, so an offline desk phone follows to the mobile instead of returning 404, which was the old failure mode. The dialled extension's own phone rings first for `features.find_me_follow_me.initial_ring_time` (default 20 s, 0 disables, skipped when the config places the extension in its own list) — matching FreePBX Follow-Me's Initial Ring Time, so a config of just "my mobile" still reaches the desk first. Then rings each destination for its own `ring_time`, advancing on ring timeout, INVITE-transaction timeout, 486/603 or any final error, or a destination that will not dial; internal destinations go through `_dial_extension_leg()`, external ones through the new `_dial_trunk_leg()` (trunk counterpart sharing `_build_trunk_invite()` with `_route_to_trunk()`, and unlike plain outbound it *does* arm a ring timer). `no_answer_destination` is appended as the last stop; exhaustion falls through to the **dialled extension's** mailbox, not the last destination's. Ring state lives in the feature keyed by Call-ID (nothing added to `Call`, nothing in `core/`), pruned when a new plan starts. Guards: self-reference/caller dropped, dedupe, ring time clamped 5–120 s, max 10 destinations, late-487 rejected by Via branch, generation counter so racing timeouts advance once. A simultaneous config executes sequentially with a warning: that needs a leg-fork on `Call` (it holds exactly one callee leg) with first-answer-wins and CANCEL to losers — the same missing capability as the queue's deferred `ring_all`, so build it once for both. Ring logic covered by `tests/test_fmfm_ring_execution.py` (29 tests, no database needed); the 10 persistence tests still skip without PostgreSQL. Needs field verification on Rig A, and Rig C for external destinations. | A, C |
| Time-based routing | `time_based_routing.py` | config | ✅ | Pure routing logic; unit-testable. | A |
| Skills routing | `skills_routing.py` | `features.skills_routing` | 🔶 | DB-backed router; verify interaction with queue delivery. | A |
| Presence / BLF | `presence.py` | `features.presence` | 🔶 | State tracking present; verify SUBSCRIBE/NOTIFY dialogs against hardphone BLF keys. | B |
| Hot desking | `hot_desking.py` | `features.hot_desking` | 🔶 | Login/logout with VM PIN. Verify against provisioned hardphones. | B |
| Phone provisioning | `phone_provisioning.py`, `provisioning_templates/` | `provisioning.enabled` | 🔶 | Zultys, Cisco (incl. CP-8851-3PCC, ATAs), Polycom templates exist. Each new model = template + quirks (DTMF payload-type table in `config.yml`). Requires real hardware per model. | B |
| Phone book | `phone_book.py` | `features.phone_book` | ✅ | AD auto-sync option; remote-phonebook URL consumed by provisioned phones. | B (+E for AD) |

### C2 — Media & IVR (relay ✅, merged from `voicemail-fix`; transfer/DTMF unification on `auto-attendant`)

| Feature | Module | Config gate | Status | Remaining work | Test rig |
|---------|--------|------------|--------|----------------|----------|
| Voicemail | `voicemail.py` + `core/voicemail_handler.py` | `features.voicemail` | ✅ | RFC 2833 + in-band DTMF in IVR, prompts, barge-in, greeting review — merged to DEV via PR #1 (`voicemail-fix`). Remaining: deployed regression test on real phones. Email notify reworked onto `pbx/mail` — sent asynchronously so SMTP no longer blocks call teardown; needs an SMTP host (Rig E). Transcription service is now constructed by `FeatureInitializer` and shared into every mailbox (it was previously never instantiated, so the feature could not run at all); off by default, and reports `ready` so an enabled-but-missing Vosk model is warned about once at startup rather than failing per message (Rig E). Audio decode fixed: voicemail is stored as G.711, which `wave.open` rejects outright (`unknown format: 7`), so `utils/audio.read_wav_as_pcm16` now decodes µ-law/A-law to PCM16 before the recogniser sees it. The transcript is rendered into the email body, gated by `voicemail.email.include_transcription`. There is deliberately **no** per-extension transcription switch — it follows the extension's voicemail-email subscription, since the email is its only consumer. Install and verify the model with `scripts/install_vosk_model.py` then `scripts/transcribe_voicemail.py` (Rig E). Transcription has since moved into a shared `pbx/speech/` subsystem running on its own daemon thread, off the call-teardown path — the notification email waits up to `deadline_seconds` (60) for a transcript and goes out without one rather than late. Two backends: `vosk` (measured RTF ~0.27, no punctuation) and `faster-whisper` (RTF ~0.47 for `small.en`, punctuates and capitalises, markedly more accurate on 8 kHz audio); pick with `features.voicemail_transcription.provider` and stage the whisper model with `scripts/install_whisper_model.py` (Rig E). Compare them on real recordings first with `scripts/benchmark_transcription.py`. | A (+E) |
| Auto attendant | `auto_attendant.py` + handler | `features.auto_attendant` | 🚧 | Same IVR/DTMF plumbing as voicemail (barge-in landed); DTMF now goes through the shared `DTMFMonitor`. Prompt text config-driven (`auto_attendant.prompts` + `company_name`) via the single `generate_espeak_voices.py` generator. G.711 (PCMU) only — HD/G.722 prompt audio intentionally not supported. Transfers now go via the RTP relay (not REFER) and hold with MOH; transfer-failure UX is still a TODO (`auto_attendant_handler.py` — interim behavior replays the main menu, pending a product decision on voicemail-on-failure vs. apology+hangup). Needs prompt files generated on the box and deployed DTMF/transfer test across phone models. | A |
| Music on hold | `music_on_hold.py` | `features.music_on_hold` | ✅ | Needs audio files in `moh/`. | A |
| Call recording | `call_recording.py` | `features.call_recording` + `recording.consent_acknowledged` | 🔶 | **This row previously read "✅ Records from RTP relay", which was never true.** `start_recording` had no caller, `add_audio` had no feeder, and `recordings/` was empty on every install — the config flag did nothing. Now real: `pbx/rtp/tap.py` copies relayed RTP off the media path (after `sock.sendto`, beside the QoS accounting, so forwarding is untouched) and `CallRecording` is a spooling `TapSink` writing **one channel per participant** — channels open the first time a source is heard, everything decoded to PCM16 8 kHz, aligned by RTP timestamp with gaps filled so channels do not drift, plus a `.json` sidecar naming which channel holds whom (a multi-channel WAV carries no channel names). **Triggered in the RTP layer, not the call router:** `RTPRelayHandler` fires an `on_bridged` callback once, the moment both endpoints are known, and `FeatureInitializer._wire_call_recording` is the only place that knows about both layers (so `pbx/rtp/` gains no dependency on features). Every path that bridges a call is therefore covered — inbound, PBX-originated, WebRTC, redirected — where the earlier router-level hook silently missed all but one. IVR paths (auto attendant, queue hold, voicemail) set only side A, so they never fire it and are never recorded. Ending the call stops the relay, which detaches the tap and closes the file. **Transfer-safe:** `replace_endpoint` puts a different human on a relay side, so the handler stamps a generation onto each source (`b0`→`b1`) and the new party gets their own channel instead of being spliced onto the departed party's. **Session-keyed:** recordings key on the new `Call.session_id`, carried across legs by `Call.join_session` at the two bridge points (`call_originator`, `transfer_handler`), so one conversation is one file rather than one per dialog. Conferences still record nothing — that audio belongs to `pbx/rtp/mixer.py`, which is instantiated as `pbx.rtp_mixer` and bridges nothing; the tap is ready for it (sources are opaque strings) but the mixer must be wired first. **Gated on consent:** `features.call_recording` has been `true` in `config.yml` since before anything could record, so it alone is not enough — `recording.consent_acknowledged` must also be set, and startup warns loudly when the feature is on without it. No announcement is played yet; that is what would let the second key go away. **Verified on hardware (2026-08-06):** a two-party call produced `caller_to_receiver_YYYYMMDD_HHMMSS_<session>.wav` plus its `.json`, with each party correctly on their own channel including while both spoke at once. Note the consent key must live inside the **existing** top-level `recording:` block — config.yml already has one, and a second block is silently discarded by YAML duplicate-key resolution, which reads as "recording is on but nothing records". Still unverified: transfers, long-call drift, hold gaps, and retention sweeping `recordings/` on Rig F. | A |
| Paging | `paging.py` + `core/paging_handler.py` | `features.paging` | 🔶 | Handler contains production-gap language; multicast paging needs real phones on a LAN segment that permits multicast. | B |
| CDR + statistics (C7) | `cdr.py`, `statistics.py` | always | ✅ | Feeds analytics pages and the Tier-2 BI framework. | A + F |

### C3 — Mixer built ✅, no consumer wired yet

The mixer exists and is tested. What is missing is the call-control wiring
between each feature and a bridge — deliberately left for a later pass so the
media layer could be validated on its own.

**The primitive.** `pbx/rtp/mixer.py` gives you `RTPMixer` (attached to
PBXCore as `rtp_mixer`) → `MixBridge` → N `MixPort`s. Each port declares the
set of ports it *hears*; that routing matrix is the entire abstraction, and it
is intentionally not hard-coded to "everyone minus self" because the
supervisor modes are asymmetric. `BridgeMode` builders supply the matrices:

| Mode | Routing |
|---|---|
| `ConferenceMode` / `BargeMode` | every port hears every other port |
| `MonitorMode(supervisor)` | supervisor hears the parties; parties unchanged |
| `WhisperMode(supervisor, target)` | only `target` also hears the supervisor |

Typical use: `bridge = pbx.rtp_mixer.create_bridge(id)`, then `add_port()` per
leg, then `apply_mode(...)`. Features never touch sockets.

**Constraints worth knowing before wiring anything:**

- Audio is normalised to **mono int16 / 8 kHz / 160-sample frames**. Only
  G.711 µ-law and A-law are registered, so **every** other codec —
  including G.722 — is renegotiated to G.711 on joining a bridge (see the
  G.722 defect below).
- `add_port` returns `None` for a payload type with no transcoder (G.729,
  Opus, iLBC, Speex). That is the signal to **re-INVITE that leg to G.711**,
  not an error — see `_send_bridge_reinvite` in `transfer_handler.py` for the
  existing re-negotiation precedent.
- `mixer.max_ports_per_bridge` defaults to **8**, and that is a measured
  number: per 20 ms frame the mix loop costs ~1% of budget at 8 G.711 legs
  (numpy lookup tables). Re-benchmark before raising, especially if a
  non-table codec is ever registered — the same test at 8 G.722 legs cost
  ~49% of budget.
- Two-party calls must stay on the **RTP relay**, which is codec-opaque and
  far cheaper. Promote to a bridge only when a third party appears, via
  `release_relay_keep_port()`; collapse back when it drops to two.
- DTMF (RFC 2833) is **forwarded, never mixed**, and re-stamped onto each
  listener's own SSRC so endpoints see a single stream.

> **⚠ G.722 is broken in this repo — measured, not suspected.**
> Both `pbx/features/g722_codec.py` and `g722_codec_itu.py` are
> non-functional. Feed either one a 1 kHz tone at its native 16 kHz and the
> round trip comes back with its energy at **250 Hz**, 26× stronger than the
> correct frequency. Two independent causes: the sub-band ADPCM predictor
> diverges (decoded output is literally powers of two, doubling every
> sample), and `_qmf_tx_filter` is a stub whose own comment admits "in full
> ITU-T implementation, this uses interpolation filters" — there is no
> synthesis filter, just `(rlow ± rhigh) << 1`.
>
> This was never noticed because nothing had listened critically to G.722
> output: the codec is only reachable through `utils/audio.pcm16_to_g722`
> for offline WAV conversion, and AA/voicemail prompts are PCMU-only by
> design. **Anything that does generate G.722 audio today is producing
> garbage.**
>
> Fixing it is a rewrite, not a patch — polyphase QMF analysis *and*
> synthesis, plus both ADPCM sub-bands with logarithmic scale-factor
> adaptation and 2-pole/6-zero predictors, in exact integer arithmetic
> validated against ITU vectors. A native binding (spandsp or similar) would
> fix correctness and the CPU cost together. The mixer is deliberately
> decoupled from this: `pbx/rtp/mixer.py` needs no change, and re-enabling
> is one line — `register_codec(PT_G722, G722Codec)` in `pbx/rtp/codecs.py`,
> where the wrapper is already written and waiting.

| Feature | Module | Config gate | Status | Remaining work | Test rig |
|---------|--------|------------|--------|----------------|----------|
| Conference | `conference.py` | `features.conference` | 🔶 | State machine only (rooms, participants, mute). Two gaps: it stores a `call_id` per participant but never uses it — that is the handle to pass to `add_port` — and **`2xxx` is only *permitted* by `_check_dialplan`, never routed**: `route_call()` has no conference branch, so dialing `2001` falls through to `_dial_to_internal_extension` and dies. Wire routing + `ConferenceMode`. | A (3 phones) |
| Supervisor monitor / whisper / barge | `advanced_call_features.py` | (no config block yet) | 🔶 | Permissions + a dict; **never instantiated anywhere in `pbx/`**. Needs: a PBXCore attribute, a `config.yml` block, conversion from nested-dict reads to dotted `Config.get`, a supervisor leg via `CallOriginator.originate_call()`, and an invocation path (star code, following `*61`/`*62` in `call_router.py:127`). **Security, unresolved:** `supervisor_id` is a trusted parameter rather than the authenticated registration; `can_monitor()` authorises against an *extension*, not a specific call; and any star code must sit **below** the trunk-origin guard at `call_router.py:85` or an external caller could dial it. Silent monitoring also carries jurisdiction-specific consent/notification obligations. | A (3 phones) |
| Three-way calling | — | — | ❌ | No implementation at all; no in-call feature-code dispatcher exists (`DTMFMonitor` is consumed only by the voicemail and AA IVRs). Needs a hold-then-join flow; `BargeMode` is the routing. | A (3 phones) |
| Call screening | `operator_console.py` | `features.operator_console` | 🔶 | Never instantiated. `screen_call()` rewrites `call.to_extension` without re-routing; `announce_and_transfer()` is an explicit stub. Needs a temporary bridge where one leg is muted rather than the all-or-nothing `Call.hold()`. | A |

### C4 — Originate-gated (primitive ✅ built on `sip-trunk`; consumers still to wire)

| Feature | Module | Config gate | Status | Remaining work | Test rig |
|---------|--------|------------|--------|----------------|----------|
| Callback queue | `callback_queue.py` | config | 🔶 | Queue/persistence wired; *completing* a callback now just needs wiring to `CallOriginator.originate_and_bridge()`. | A |
| Emergency notification | `emergency_notification.py` | `features.emergency_notification` (default on) | 🔶 | **Email now sends for real** via `pbx_core.mailer` (`pbx/mail`), synchronously, audit-logged at the call site. Previously read `pbx_core.email_notifier`, an attribute nothing ever set, so it silently logged instead of sending. Call/page actions can use `CallOriginator`; SMS still needs a provider (Rig E). | A + E |

### C5 — Trunk-gated (outbound 🚧 on branch, inbound 🚧 data layer only)

| Feature | Module | Config gate | Status | Remaining work | Test rig |
|---------|--------|------------|--------|----------------|----------|
| SIP trunks | `sip_trunk.py` | `trunks:` config | 🚧 | See [sip-trunk branch](#the-sip-trunk-branch) — the largest single work item in the system. | C |
| Kari's Law | `karis_law.py` | default on | ✅ | Direct-911 dialing compliance; meaningless without a trunk. **Never test against live 911** — E911 test-mode protection exists (`e911_protection` in `sip_trunk.py`). | C (carrier test DID) |
| E911 location | `e911_location.py` | `features.e911` | 🔶 | Location objects per extension; needs trunk + carrier E911/PIDF-LO support to mean anything deployed. | C |
| Nomadic E911 | `nomadic_e911.py` | (also Tier-2 API) | 🧩/🔶 | Dual-wired (karis_law + framework API). Needs real location-update flow from devices. | B + C |
| Fraud detection | `fraud_detection.py` | config | 🔶 | Heuristics wired at init; only validatable against real PSTN traffic patterns. | C |
| SBC | `session_border_controller.py` | `features.sbc` | 🔶 | Wired at init + admin page; validate topology-hiding/normalization in front of a trunk. | C |

### Rig E/D — External-service & web features (gate = credentials/deployment)

| Feature | Module | Config gate | Status | Remaining work | Test rig |
|---------|--------|------------|--------|----------------|----------|
| WebRTC | `webrtc.py` (1 900+ ln) | `features.webrtc` | 🔶 | Real implementation on **aiortc** (DTLS-SRTP, ICE) with RTP↔browser media bridge; degrades gracefully if aiortc missing. Finish: HTTPS + STUN/TURN deployment test, browser matrix, load. | D |
| MFA | `mfa.py` | `security.mfa.enabled` | 🔶 | TOTP for admin; verify login flow in admin UI. | D |
| Webhooks (C7) | `webhooks.py` | `features.webhooks` | ✅ | Worker threads, retries, HMAC. Needs a receiver endpoint to verify. | E |
| Mobile push | `mobile_push.py` | config | 🔶 | Needs FCM/APNs credentials; only `new_voicemail` event today (see PLANNED_FEATURES for the mobile roadmap). | E |
| CRM screen pop | `crm_integration.py` | `features.crm_integration` | 🔶 | Provider chain (phone book → AD → external CRM). External path needs a live API. | E |
| DND scheduling | `dnd_scheduling.py` | config | 🔶 | Scheduler wired incl. optional Outlook-calendar source. | E |
| Threat detection / security monitor | `utils/security.py`, `security_monitor.py` | default on | ✅ | Runtime enforcement gates startup. | F |
| Directory & app integrations | `integrations/active_directory.py` (init-wired); `jitsi.py`, `matrix.py`, `espocrm.py`, `zoom.py` (config-gated); `teams.py`, `outlook.py`, `lansweeper.py` | per-integration | 🔶 | Each needs a live counterpart service + credentials. AD sync (incl. auto-provisioning) is the most mature. | E |

---

## Tier 2 — API-only frameworks (`/api/framework/*`)

All follow one pattern (`pbx/api/routes/framework.py`, 128 endpoints): the engine class
is **imported and instantiated inside each request handler**, backed by DB tables created
at runtime. Real logic, real persistence — but **nothing in the call/media path invokes
them**. `tests/test_planned_feature_frameworks.py` covers them as frameworks.

**Finishing any Tier-2 feature means the same three steps**, plus its capability gate:

1. Promote to a singleton on `PBXCore` (via `FeatureInitializer`) instead of per-request construction.
2. Subscribe it to live events — call start/end, audio taps, DTMF, QoS.
3. Integrate the real external dependency (ML model, speech engine, provider API).

### C4 — Originate-gated (primitive ✅ built; these are the consumers)

| Feature | Module | What exists | Key missing piece | Test rig |
|---------|--------|-------------|-------------------|----------|
| Click-to-dial | `click_to_dial.py` + admin page | API + UI + **wired to `CallOriginator.originate_and_bridge()`** | Real-phone verification (Rig A) — the one end-to-end path testable today | A |
| Predictive dialing | `predictive_dialing.py` + `_db.py` | Statistical pacing engine | Wire to `CallOriginator` + C5 trunk to reach PSTN; answer detection (C6) | C |
| Predictive VM drop | `predictive_voicemail_drop.py` | Framework | Wire to `CallOriginator` + answering-machine detection on live media (C6) | C |
| Call blending | `call_blending.py` | Framework | Wire to `CallOriginator` + queue integration | A + C |

### C6 — Analytics-tap-gated

| Feature | Module | What exists | Key missing piece | Test rig |
|---------|--------|-------------|-------------------|----------|
| Speech analytics | `speech_analytics.py` | Config CRUD, keyword/sentiment engine | Live audio tap from recordings/RTP; call-id→extension mapping is stubbed | A + E (speech model) |
| Voice biometrics | `voice_biometrics.py` + `_db.py` | Local speaker-verification engine | Enrollment/verify hooks in call flow; see PLANNED_FEATURES §Voice Biometrics | A + E |
| Conversational AI | `conversational_ai.py` + `_db.py` | Intent/response engine, canned responses | Real STT/TTS + LLM hookup; IVR (C2) integration | A + E |
| Call tagging | `call_tagging.py` | Manual/rule/ML tagging | Transcript feed; see PLANNED_FEATURES §Call Tagging | A |
| Recording analytics | `call_recording_analytics.py` | Framework | Recording-file ingestion pipeline | A |
| Call quality prediction | `call_quality_prediction.py` + `_db.py` | Scoring model | Live QoS feed — `rtcp_monitor` already collects the data; connect it | A |

### C5 — Trunk-gated

| Feature | Module | What exists | Key missing piece | Test rig |
|---------|--------|-------------|-------------------|----------|
| DNS SRV failover | `dns_srv_failover.py` | Framework | Wire into trunk registration/INVITE target selection (natural follow-on to sip-trunk branch) | C |
| Mobile number portability | `mobile_number_portability.py` | Framework | Carrier LRN lookup service | E |

### C7 — Data-plane features

| Feature | Module | What exists | Key missing piece | Test rig |
|---------|--------|-------------|-------------------|----------|
| BI integration | `bi_integration.py` | Export queries (`SELECT *` on warehouse tables — known debt) | Scheduled export worker; warehouse target | F |
| Compliance framework | `compliance_framework.py` | Policy CRUD | Enforcement hooks (recording consent, retention) | F |
| Data residency | `data_residency_controls.py` | Policy framework | Storage-routing enforcement | F |
| Geographic redundancy | `geographic_redundancy.py` | Region registry, health scores, manual failover | Everything replication-related; see PLANNED_FEATURES §Geographic Redundancy | 2× Rig F |

### Rig E/D — External-service & web

| Feature | Module | What exists | Key missing piece | Test rig |
|---------|--------|-------------|-------------------|----------|
| Team collaboration | `team_collaboration.py` | Messaging framework | Real transport (Matrix integration is the natural backend) | E |
| CRM integrations (multi) | `crm_integrations.py` | Provider framework | Live provider APIs (Salesforce/HubSpot/…) | E |
| Video conferencing | `video_conferencing.py` | Room framework | Actual video media path (Jitsi bridge is the pragmatic route; full native = C3-scale media work) | D |
| Video codec | `video_codec.py` | Codec descriptor | SDP video negotiation in core (C1/C2) | D |

---

## Tier 3 — Orphaned modules (decide: wire it or cut it)

No imports anywhere outside `pbx/features/` and tests (`test_stub_implementations.py`
covers several). Each needs an explicit wire-or-cut decision before "finished" means anything.

| Module | Gated by | What it is | Recommended disposition |
|--------|----------|-----------|------------------------|
| `stir_shaken.py` | C5 | Caller-ID attestation/verification | **Wire** — `add_stir_shaken_to_invite()` into `_route_to_trunk()` (alongside the caller-ID headers), `verify_stir_shaken_invite()` into the inbound path. Complete but still called from nowhere; needs signing certs from carrier/STI-PA. Rig C. |
| `least_cost_routing.py` | C5 | Multi-trunk cost-based route selection | **Wire** — natural extension of `SIPTrunkSystem.route_outbound()` once >1 trunk exists. Rig C. |
| `operator_console.py` | C4 | Attendant console backend | Wire to admin UI + presence (originate now exists via `CallOriginator`); or defer. |
| `advanced_call_features.py` | C3 | Supervisor whisper/barge/monitor | **Wire** — the mixer it was waiting on is built (`MonitorMode`/`WhisperMode`/`BargeMode`). Remaining work and the open authorisation questions are in the C3 section. Note the actual call *screening* implementation is in `operator_console.py`, not here. |
| `ai_call_routing.py` | C7 | ML route selection | Defer until call-volume data exists. |
| `audio_processing.py` | C2 | Audio effects/normalization | Fold into recording pipeline or cut. |
| `opus_codec.py`, `g729_codec.py`, `g726_codec.py`, `ilbc_codec.py`, `speex_codec.py` | C2 | Codec implementations | Wire into SDP negotiation + transcoding. **Only G.711 µ/A is actually verified end-to-end** — G.722 is present but broken (see the C3 warning), and these five are unimported and unproven, so treat all of them as unvalidated until a round-trip test says otherwise. For mixing, register each with `pbx/rtp/codecs.py::register_codec`; until then a bridge re-INVITEs those legs to G.711. G.729 needs a licensed native library and Opus needs `opuslib`, neither currently installed. |
| `g722_codec_itu.py` | C2 | Alternate G.722 | Duplicate of wired `g722_codec.py` — and **equally broken** (same 1 kHz → 250 Hz round-trip failure). Delete rather than consolidate; see the G.722 warning in the C3 section. |
| `sso_auth.py` | Rig E | SSO for admin UI | Wire into `api/routes/auth.py`; SAML/OIDC needs an IdP to test. |
| `mobile_apps.py` | Rig E | Mobile device registry | Wire alongside mobile_push when mobile clients are real. |

---

## The sip-trunk branch

**This is the gate between "office intercom" and "phone system"** — capability C5.
4 commits, ~8 650 added lines.

### Done on the branch

- **Persistence**: `pbx/models/sip_trunk.py` ORM model, DB + `config.yml` round-trip (`utils/config.py`, `database.py`, `migrations.py`), admin UI page.
- **Registration**: `SIPServer.register_trunk()` → REGISTER with digest auth (`_build_trunk_register`, `_parse_www_authenticate`, `_compute_trunk_digest_response`), periodic re-REGISTER before Expires lapses.
- **Outbound calls**: `CallRouter._route_to_trunk()` — 10/11-digit pattern detection, priority-fallback when no rules configured, capacity-aware failover, codec intersection with caller's SDP (numeric payload-type format), RFC 2833 telephone-event offered, 401/407 INVITE retry with credentials, 480 on no-answer (no bogus voicemail), channel release on end.
- **Safety**: E911 protection blocks 911-pattern dialing in test mode.
- **Call-originate primitive (C4)**: `core/call_originator.py` — `originate_call()` /
  `originate_and_bridge()`, sharing destination classification and leg construction with
  `CallRouter` via `_build_and_send_leg_invite()`. Click-to-dial wired onto it.
- **Outbound caller ID**: `_route_to_trunk()` sends the calling extension's `did_number`
  (a real E.164 number) in From + P-Asserted-Identity/Remote-Party-ID, falling back to the
  extension number. Previously it sent the raw internal extension (e.g. `1001`), which is
  not carrier-dialable and conflicts with Truth-in-Caller-ID.
- **Inbound DID routing — data layer**: `models/inbound_route.py`, migration `1012`,
  `InboundRouteDB`, `features/inbound_routing.py` (`InboundRoutingSystem`), plus optional
  `Extension.did_number` (Alembic `003`, auto-applied at startup). `/api/inbound-routes`
  CRUD + merged effective-routes view; admin UI section on the SIP Trunks tab.
- **Trunk failure handling**: `_handle_trunk_failure()` now re-points affected outbound
  rules to the failover trunk and restores them when the trunk recovers (via the existing
  `_perform_health_checks()` loop). Notification remains log-only.
- **Inbound DID routing — SIP dispatch**: `SIPTrunk.resolved_host_ip` (cached in
  `mark_registered()`) + `SIPTrunkSystem.get_trunk_by_addr()` identify an inbound INVITE
  as trunk-sourced (IP match only, not port). `CallRouter._route_inbound_did()` looks up
  the dialed DID via `inbound_routing.lookup()` and dispatches to an extension (through a
  new `_dial_to_internal_extension()`, extracted from `route_call()` so both paths share
  identical relay/CDR/webhook handling), the auto attendant, or voicemail. The hook in
  `route_call()` runs before the Kari's Law/emergency and dialplan checks, since a
  carrier's From header is caller-ID digits, not an internal extension. Queue as a
  destination type is deliberately out of scope for v1. **The IP-matching heuristic is
  exactly what needs real-carrier field validation** — not treated as verified.
- **Tests**: substantial coverage additions (`test_sip_server_coverage.py`,
  `test_sip_trunk_coverage.py`, `test_call_router_coverage.py`,
  `test_call_originator_coverage.py`, `test_click_to_dial_coverage.py`,
  `test_inbound_routing_coverage.py`, `test_api_features_routes_coverage.py`).

### Remaining to finish

1. **STIR/SHAKEN wiring** — `features/stir_shaken.py` is complete but called from nowhere.
   Wire into the outbound trunk INVITE path (Identity header) and the inbound path (now
   that DID dispatch exists). Needs signing certs from carrier/STI-PA.
2. **NAT/public-address handling** for SDP and Via/Contact toward the carrier (external IP
   config, symmetric RTP) — untested until Rig C exists.
3. **Carrier validation** against a real provider (config templates for AT&T and Comcast
   exist: `config_att_sip.yml`, `config_comcast_sip.yml`; a cheap SIP provider like
   VoIP.ms/Telnyx/Twilio is the low-risk first target). Gates the IP-matching heuristic,
   (1)'s certs, (2) entirely, and end-to-end PSTN in both directions.
4. Follow-ons unlocked afterward: least-cost routing, DNS SRV failover, E911 via carrier,
   FMFM-to-external, predictive dialing.

---

## Branch: voicemail-fix (merged)

Capability C2 work, merged to `DEV` via [PR #1](https://github.com/sadekon/PBX/pull/1).
G.711 µ-law encoder fix, RFC 2833 + in-band DTMF into the voicemail IVR, greeting-review
prompts, regenerated prompt audio, menu re-announcement, and DTMF barge-in (interrupt
prompts by keypress) for both voicemail and AA. Barge-in only peeks the pending-DTMF
source, so the existing DTMF loop still consumes the digit and drives the state machine;
PIN entry is the one exception — it barges only on `#`, so the prompt keeps playing while
the caller dials PIN digits.

**Still outstanding:** deployed regression on real phones (Zultys + Cisco ATA are the
tested targets per commit history) covering PIN entry (including barge-in-on-`#` only),
greeting record/review, message playback/delete/save, and barge-in on every other prompt.

---

## Branch: auto-attendant (current, open as [PR #2](https://github.com/sadekon/PBX/pull/2))

Mostly a C1/C2 refactor-and-build branch, built on top of the merged `voicemail-fix` work.

- **`PBXCore` split**: extracted `pbx/core/transfer_handler.py` (`TransferHandler` — all
  blind/attended/REFER transfer logic), `pbx/core/codec_negotiator.py` (`CodecNegotiator`
  — phone-model detection and codec compatibility), and `pbx/core/registration_handler.py`
  (`RegistrationHandler` — SIP registration). `pbx.py` drops from 2 717 to ~1 025 lines;
  internal handlers are now public attributes on `PBXCore` with no forwarding methods.
- **Call transfer (C1)**: implemented REFER/Replaces-based blind and attended transfer
  (RFC 3891) — invite-based transfer detection, mandatory Via/Max-Forwards headers on
  teardown, fixes for second-transfer-from-peer-leg and malformed/overlapping REFERs,
  BYE sent to the transferor for both legs on completion.
- **Auto attendant transfer**: AA calls now transfer via the RTP relay instead of REFER
  (the PBX stays in the media path — "the Asterisk-standard way to transfer a trunk/inbound
  call") and are held with MOH while the transfer is in progress.
- **Call forwarding**: SIP 3xx redirect handling for a phone's "always forward" configuration.
- **DTMF unification**: consolidated RFC 2833, SIP INFO, and in-band DTMF detection into one
  shared `rtp/dtmf_monitor.py::DTMFMonitor`, used by both auto attendant and voicemail IVR.
- **MOH fixes**: fixed duplicate MOH playback during extended repeated holds; API-driven
  hold/resume now drives MOH the same way phone-initiated holds do.
- Deleted dead `transfer_call()` and `_get_rtpmap_for_phone_model()`; consolidated WAV-header
  building into `utils/audio.py`; consolidated TTS prompt generator scripts.

**Still outstanding:** field verification of REFER/attended transfer with real Zultys ZIP
phones (Rig B); a product decision on AA transfer-failure UX (`auto_attendant_handler.py`
currently replays the main menu as an interim behavior — voicemail-on-failure vs.
apology+hangup is still an open TODO); deployed regression covering transfer + call
forwarding across supported phone models before merge to `DEV`.

---

## Call transcript review — read path built 2026-08-10

Backend post-call transcription was already **complete**: tap → per-participant recording →
per-region transcription → one speaker-attributed transcript. What was missing was anything
that read it. `recordings` had rows since the recorder started registering them and no view
onto them; `admin/js/pages/recordings.ts` is fraud alerts and callback queues despite the name.

Now there is a read API (`pbx/api/routes/recordings.py`) and a **Call Recordings** admin tab
(`admin/js/pages/call-recordings.ts`): list, play, download, read transcript.

### Who may read a recording

Binary, because the token is binary — the session token carries an `is_admin` bool and there
are no roles. Two tiers, no more:

| Tier | Reach |
|---|---|
| Admin | Every recording and transcript |
| Participant | Only calls they were a party to, matched against `recordings.participants` |

Participant access is gated on **`recording.participant_access`, default `false`** — a fresh
install is admin-only. Note the key lives under `recording:`, **not**
`features.call_recording.participant_access`: `features.call_recording` is a bool, and
`Config.get` returns the default the moment a dotted lookup hits a non-dict, so that key could
never have been switched on and would have read as "the flag does nothing".

There is deliberately **no supervisor tier**. Nothing in the schema models teams or reporting
lines, so it would have to be inferred from extension numbers, which is a guess.

`check_recording_access()` in `pbx/api/utils.py` owns the rule; no route re-implements it.

### How the path is kept secure

- **Reads are audited, not just writes** — `AuditLogger.log_recording_access` fires on every
  playback and transcript fetch, and on every denial. Who listened to which call is the
  question asked months later, and only a record made at the time can answer it.
- **Text never appears in a list response.** Listing is metadata only; text comes from a
  per-recording fetch that audits. Otherwise one paginated call drains every transcript and
  lands in the log as a single event.
- **Media resolves from the row, never the caller** — there is no path in any URL. The stored
  path is still confined to `recording.storage_path` / `voicemail.storage_path` before serving,
  so a bad `storage_path` or a future writer cannot turn this into an arbitrary file read.
- **Expired audio returns 410, not 404.** The row outlives the file by design; 404 reads as
  "no such recording" when the recording plainly exists and its transcript usually remains.
- **`no-store` on media and transcripts**, so neither settles into a cache that outlives the
  retention policy.
- **403 is flat.** The body never distinguishes "not yours" from "does not exist", or a caller
  could enumerate which calls a colleague was on.

Audio reaches the browser as a blob fetched with the bearer token, never an `<audio src>`
pointed at the API — the browser's own request carries no `Authorization` header. Same reason
`playVoicemail` already did it that way.

### Retention and analytics endpoints were open to any extension

`/api/recording-retention/*` (policies, statistics, holds), `/api/recording-announcements/*`
and `/recording-analytics/*` were all `@require_auth`, not `@require_admin` — and login
authenticates against the user's **voicemail PIN**. Any extension with a PIN could read
retention policy, search recording analyses, and `POST`/`DELETE` policies and legal holds.
Deleting a retention policy is a data-destruction primitive. All 14 are now `@require_admin`.

### The player was empty, not broken

The first cause of "no supported source was found" was not a codec at all. The card rendered

```html
<audio class="rec-player" controls preload="none"></audio>
```

with **no `src`** — the blob was attached only by a separate Play button in the card header.
So the user saw a fully rendered player, pressed its own play button, and got the browser's
message for an element with no source, which is the same message it gives for an unsupported
codec. That ambiguity is what made this look like a media problem.

The player is now inserted by `attachAudio()` only once it has a source, and the expand
handler loads it, so the controls on screen are always backed by something playable. When the
fetch fails the slot shows the server's reason (415, 410) instead of an empty player.
`admin/tests/call-recordings.test.js` guards the invariant: no `<audio>` element without a
`src`, ever.

### G.711 had to be decoded before a browser would play anything

Playback failed with "no supported source was found" — which reads as a broken URL, not as a
codec problem, because that is the *only* thing an `<audio>` element reports. Telephony audio
is routinely stored as G.711, and no mainstream browser decodes it.

`pbx/utils/audio.wav_as_pcm16_wav` now reads the `fmt` chunk and decides:

| Stored format | Behaviour |
|---|---|
| Linear PCM | Served untouched via `send_file`, so range requests and seeking still work |
| µ-law / A-law | Decoded to PCM16 and rebuilt, using the existing `ulaw_to_pcm16` tables |
| Anything else (G.722) | **415, not served** — sending it reproduces the same silent failure |
| Not a RIFF/WAVE | 422 |

The refusal matters as much as the conversion: serving a format the browser cannot decode is
indistinguishable, from the UI, from the bug this replaced.

### `admin/css/patterns.css` — the shared UI vocabulary

New file, and the intended base for remodelling the other pages. The recordings page was first
built by borrowing the queues page's classes (`.queue-card`, `.qch-stat`, `.en-pill`, `.qbtn`),
which left two pages sharing selectors named after only one of them. `patterns.css` is the same
rules under names that do not presuppose the caller:

| Shared | Replaces (queue-named) |
|---|---|
| `.card-stack`, `.card-shell`, `.card-head`, `.card-body`, `.card-title`, `.card-chevron`, `.card-actions` | `.queues-cards`, `.queue-card*`, `.qch-title` |
| `.meta-stats`, `.meta-stat` + `.k` / `.v` | `.qch-stats`, `.qch-stat` |
| `.pill` + `.pill-ok` / `-warn` / `-danger` / `-muted` / `-info` | `.en-pill.on` / `.off` |
| `.btn-ghost`, `.btn-ghost-danger` | `.qbtn`, `.qbtn-danger` |
| `.filter-bar`, `.filter-label`, `.filter-check`, `.filter-summary` | — |
| `.list-empty`, `.list-loading`, `.group-label`, `.muted-note` | `.queues-empty` |

**Deliberately additive.** The queue-named originals are untouched and the queues page still
uses them, so a handful of rules are duplicated on purpose. Migrating the other pages and
deleting the originals is a separate change. Until then: new pages use the shared names, existing
pages stay on the old ones.

Only genuinely page-specific CSS stays in `admin.css` under the recordings heading — the
transcript layout, which nothing else has.

### Page layout follows the call queues page

One card per recording reusing `.queue-card` / `.queue-card-head` / `.qch-*` / `.en-pill` /
`.qbtn` unchanged — same one-line header, same collapse-as-a-unit body. Only the player,
transcript and filter bar are new CSS.

The transcript shows as a two-line preview (clamped on rendered lines, not characters) and
expands to one row per speaker turn. `/transcript` returns `lines` instead of raw `segments`:
consecutive segments from the same speaker are merged — Whisper emits one segment per decoded
window, so an unmerged sentence renders as three stuttering fragments — and per-word timing is
dropped, since nothing seeks to a word and `words` dominates the payload. Voicemail has no
speaker column at all rather than an empty one, and a transcript with no timing falls back to
prose.

**There is no conference filter, and the kind dropdown is gone.** `RecordingStore` exports only
`KIND_CALL` (`"recording"`) and `KIND_VOICEMAIL`; nothing writes a conference kind, since
`RTPMixer` bridges nothing. Note `KIND_CALL` is the string `"recording"`, not `"call"` — an
earlier filter used the literal and silently matched no rows. The page shows calls, with an
"Include voicemail" checkbox for cross-cutting review, and a debounced participant filter
matching `recordings.participants` on the quoted JSON form (`%"1001"%`) so `100` cannot match
`1001`.

### Paging is a cursor, not an offset

`GET /api/recordings` takes `?before=<id>` and returns `has_more` / `next_before`; the page
loads 50 at a time and appends with "Load more".

`OFFSET` would have been simpler and wrong here. Rows are inserted at the *top* of this
ordering as calls end, so one call finishing between page one and page two shifts everything
down by one and the next page silently skips a recording. The cursor compares on the pair
`(created_at, id)` rather than `created_at` alone, because two recordings can share a
timestamp — and `_select` now orders by both for the same reason, since an ambiguous sort makes
a cursor skip or repeat rows at the boundary.

`has_more` comes from asking for `limit + 1` rows and trimming, rather than a second `COUNT`
over a table that only grows.

One wrinkle: for a non-admin, participant filtering happens in Python after the query, so a
page can return fewer than `limit` rows while `has_more` is still true. That is harmless — the
cursor drives paging, not the count — but it means the count is "loaded so far", not "matching".

### Not done

- Click-to-seek from a transcript line. `start` is carried on every line and the player is in
  the same card, so the wiring is short; it just is not done.
- Bulk export of recordings (voicemail has a ZIP export path; recordings have none).
- Live-transcript view. `transcripts.recording_id` is nullable and `source="live"` is reserved
  for it, but nothing streams yet.
- The token bakes `is_admin` in at login, so **demoting a user leaves them admin until their
  token expires**. That limits how much admin-only can be leaned on as a primary control.

### Recording consent — complete 2026-08-07, verified on real calls

`recording.consent.announce_for` (`external` | `all` | `off`) decides which calls get a spoken
notice; both legs always hear it. Recording starts first so the notice is on its own tape, and
**the recording is discarded if the notice did not play** — `CallRecordingSystem.abandon()`.
The notice is never transcribed: its text is injected verbatim from config, because we already
have the exact wording and a paraphrase is the last thing wanted in evidence.

`recording.consent_acknowledged` is **gone**. It stood in for a notice that did not exist yet;
the announcement is a stronger guarantee than a boolean. Upgrade note: an install with
`features.call_recording: true` and no `recording.consent:` block will now record internal
calls silently — external calls still get a notice or fail closed.

Three bugs here were invisible to unit tests and only appeared on real handsets. Worth knowing
before touching this code:

- **Codec.** `RTPPlayer.play_file` re-encodes 16-bit PCM to G.722, which a phone that
  negotiated µ-law cannot decode. The notice converts to µ-law (PT 0) itself.
- **SSRC collision.** Injecting a second RTP stream into a live session puts two SSRCs on one
  port; phones lock onto whichever arrived first, so the notice reached one end, the other, or
  neither, differing per call. The relay is paused while it plays, which also mutes both
  microphones. **Any future injected audio has the same problem** — see MoH below.
- **Sequential legs.** Playing to each leg in turn meant the second party heard it only after
  the first had finished. Both legs are now driven from one loop.

Also fixed: `is_internal()` called `ExtensionDB.get_extension()`, which does not exist, and a
bare `except Exception` turned the `AttributeError` into "everyone is external" — so
`announce_for: external` announced on internal calls while looking like working policy.

### Retention — reconciled 2026-08-07 (was: two systems, one of them inert)

There were two retention systems and only one of them deleted anything.
`recording_retention.RecordingRetentionManager` owned policies in a **plain Python dict** and
exposed them through `/api/recording-retention/*` and a live admin tab; `cleanup_old_recordings`
had no caller anywhere. `retention.RetentionSweeper` deleted on a `PeriodicTask` but knew only
a single flat `audio_days`. So an operator could add a policy in the UI, watch it save, and
have it govern nothing — and because policies were never persisted, it vanished on restart
anyway. That is why no `retention_policies` table ever appeared in pgAdmin: there was no table.

Now one subsystem in two modules with distinct jobs:

| | `features/retention_policies.py` | `features/retention.py` |
|---|---|---|
| Owns | policies, legal holds, match facts | the timer and the deletion |
| Storage | `retention_policies` / `retention_holds` (migration 1018) | — |
| Entry point | `PolicyStore`, `HoldStore` | `RetentionSweeper.sweep()` |

`recording_retention.py` is **deleted**. `pbx_core.recording_retention` and
`pbx_core.retention_sweeper` are now the same object, so the existing API and admin tab drive
the thing that actually deletes.

**How it resolves.** Every file is matched against enabled policies in priority order (lowest
number wins, ties on `policy_id`); the first match supplies the period, and no match falls back
to `retention:` in config.yml. Match rules are a **closed** vocabulary — `media`,
`extensions`, `min_duration_seconds`, `max_duration_seconds` — validated on save, with unknown
keys rejected rather than ignored, because a dropped condition makes a policy match *more*
files and the thing on the other end is deletion. Facts come from the sidecar manifest the
recorder already writes (participants, duration, session) and from the path layout for
voicemail.

**Two periods per policy**, `audio_days` and `transcript_days`, either nullable to inherit the
fallback — null means inherit, never "delete now". Transcripts resolve policy from
`call_transcripts.participants` and `.session_id` (also migration 1018) rather than from the
sidecar, because audio expires first and the manifest goes with it.

**Legal holds replace the old tag vocabulary.** Tags mapped `legal` to a fixed 2555 days, which
is wrong in both directions: a dispute lasting longer still lost the audio, and one settled in
a month held the recording for another seven years with no way to release it. A hold is keyed
on `session_id` (so it covers every leg of a transferred call), requires a reason and an actor,
suspends expiry entirely at any age, and is released explicitly — released rows are stamped,
never deleted. Both placement and release go through `AuditLogger`.

**Config seeds once.** On the first start against an empty table, `retention.audio_days` and
`transcript_days` are written out as a catch-all policy `default` at priority 1000. After that
the table is authoritative and the UI owns it; editing that policy survives restarts, and
changing config.yml afterwards does **not** move it.

`RecordingTranscriber` now writes `session_id` and `participants` on every transcript it
stores, so transcript policies match on participants from the day this lands.

**Voicemail has three artifacts and now three consistent outcomes.** The `.wav` goes on the
audio clock, and the sweep stamps `audio_deleted_at` on its row when it does — without that
the mailbox kept listing expired messages, counting them toward MWI, with `file_path` pointing
at nothing. That phantom was created by the sweep itself and would have appeared the first
time `dry_run` went off.

The row goes on the transcript clock, deleted outright. A row holding neither audio nor text
is not a voicemail, it is metadata CDR already keeps, and leaving it made
`voicemail_messages` the one tier with no expiry at all. Deleting it takes both copies of the
transcript with it — the `transcription_*` columns the email path reads, and (swept
separately) the `call_transcripts` row.

Remaining gaps: `dry_run` is still on in config.yml, so nothing is deleted yet — read a dry-run
log before turning it off, because the first real sweep on a system that has never expired
anything removes everything already past its period. There is no admin UI for holds yet, only
the API (`GET/POST /api/recording-retention/hold[s]`, `DELETE .../hold/<session_id>`). And rows
written before migration 1018 have `participants` null, so a participant policy will not match
any historical transcript — it will fall through to the catch-all, which is the safe direction.

**Design decisions taken** for the review surface, all still to implement:

- **Purpose: call review / QA.** Supervisors browse and search transcripts, flag or comment on
  a call, mark it reviewed. Operational health monitoring (queue depth, RTF, drops) is a
  separate concern — `TranscriptionWorker.stats()` already exposes it for a later panel.
- **Transcripts only; no audio playback.** Text carries most of the review value at a fraction
  of the privacy exposure, and audio expires at 90 days while transcripts live a year. Serving
  someone's voice over an API is its own decision, deliberately deferred.
- **New `can_read_transcripts` JWT claim**, beside `is_admin` rather than folded into it —
  granting HR full PBX administration to read a transcript is the trade this avoids. Three
  tiers: your own calls always; the claim (or `is_admin`) for anyone else's.
- **Every cross-extension read goes through `AuditLogger.log_data_export`.** This is what makes
  a written retention/access policy enforceable rather than aspirational.

Implied schema work: "was I on this call?" is not cheaply answerable — participants live inside
the `segments` JSON. Access control should not parse JSON per row, so `call_transcripts` needs
an indexed `participants` column (migration 1018).

Note `admin/js/pages/recordings.ts` is **not** about call recordings — it is fraud detection and
callback queue. The review page is new work, not an extension of it.

## Cross-cutting platform limitations

These constrain *every* feature's deployed testing and should be scheduled as platform work:

1. **SIP is UDP-only** (C1). No TCP (large-message fragmentation risk), no TLS (no SIPS),
   no SRTP → no encrypted calling. Carrier trunks increasingly require TLS.
   `utils/tls_support.py` exists for the API layer only.
2. ~~**No conference/N-way audio mixer** (C3)~~ — **built** and tested
   (`pbx/rtp/mixer.py`). No feature is wired onto it yet: conferencing,
   barge/whisper/monitor, 3-way calling and call screening each still need
   their call-control path (see the C3 section). The supervisor modes also
   have unresolved authorisation questions noted there.
3. ~~**No call-originate primitive** (C4)~~ — **built** on `sip-trunk`
   (`core/call_originator.py`); click-to-dial is wired onto it. Predictive dialing,
   callback completion, emergency-notification calls and operator console still need
   wiring, and the primitive itself is field-unverified against real phones.
4. **Single-process, in-process RTP relay** — capacity ceiling; measure before scaling
   claims (see `docs/CAPACITY_PLANNING.md`).
5. **Migration debt** (C7) — runtime `CREATE TABLE IF NOT EXISTS` everywhere; core-table
   changes must move to Alembic.
6. **Admin CSP debt** — `unsafe-inline` for scripts/styles (documented in CLAUDE.md).
7. **Docker base image** is `python:3.14-slim-bookworm` while the project pins 3.13+ —
   confirm intended version.
8. **Terraform/AWS is a template** — outputs reference undefined resources (see
   PLANNED_FEATURES §AWS/Terraform).

---

## Deployed-testing rig matrix

| Rig | Hardware/services | Enables testing of |
|-----|-------------------|--------------------|
| **A — LAN softphones** | Server (or laptop) + 2–3 softphones (Linphone/Zoiper/MicroSIP) | Core calls, hold/transfer, DTMF, voicemail, AA, queues, parking, recording, MoH, FMFM, CDR, presence basics |
| **B — Hardphones** | Rig A + real phones per supported model (Zultys ZIP33G, Cisco 8851-3PCC/ATA, Polycom) + DHCP/HTTP provisioning reachability | Provisioning, hot desking, BLF, paging (multicast), phone book URL, per-model DTMF quirks |
| **C — Carrier trunk** | SIP trunk account (start: VoIP.ms/Telnyx/Twilio; targets: AT&T/Comcast configs) + public IP or port-forwarded NAT + a test DID | Trunk register/outbound/inbound, DID routing, failover, LCR, STIR/SHAKEN, E911 (test mode + carrier test procedures only), SBC, fraud detection |
| **D — Browser/HTTPS** | TLS cert (Let's Encrypt scripts exist), STUN (public) / optional TURN, modern browsers | WebRTC calling, admin UI, MFA, click-to-dial UI |
| **E — External services** | SMTP account; FCM/APNs keys; LDAP/AD server; a speech model — Vosk (`scripts/install_vosk_model.py`) or faster-whisper (`scripts/install_whisper_model.py`); CRM/Jitsi/Matrix/Zoom/Outlook credentials; a webhook receiver | Email notify, transcription, push, AD sync, screen pop, integrations, webhooks, SSO |
| **F — Full stack** | `docker compose up` (PostgreSQL 17, Redis 7, Prometheus, Grafana) | Persistence, retention sweeps, metrics/dashboards, health checks, backup/recovery scripts, HA/geo experiments (2×F) |

---

## Recommended phase plan

Each phase names the capability it finishes and has an observable exit criterion.

**Phase 1 — Land the IVR + transfer work (C1/C2, Rig A).**
`voicemail-fix` merged to DEV (PR #1); regression-test on real phones still outstanding.
Regression-test and merge `auto-attendant` (PR #2 — REFER/attended transfer, RTP-relay AA
transfer with MOH, call forwarding, unified DTMF).
*Exit: voicemail + AA fully driveable by DTMF from every supported phone model; blind and
attended transfer, and call forwarding, verified on real phones.*

**Phase 2 — Finish SIP trunking (C5, Rig C).** Inbound DID routing (data layer + SIP
dispatch), legacy outbound stub removal, and real trunk-failure handling are all code-
complete; remaining is NAT handling and validation against a low-cost carrier, then
AT&T/Comcast configs. *Exit: a PSTN caller reaches an extension via DID, and an
extension dials out — reliably, with failover.*

**Phase 3 — Emergency stack (C4-lite + C5, Rig C + E).** Replace `emergency_notification.py`
stubs with real email/SMS/call/paging actions; E911 location flow through the trunk
(carrier test procedure, never live 911). *Exit: 911-test dial triggers correct trunk
routing + on-site notifications.* Legal-compliance gate for any real deployment.

**Phase 4 — Build the missing core primitives (C3, Rig A).** C4 originate is built —
what remains here is verifying click-to-dial end-to-end on real phones and wiring the
other consumers (callbacks, notification calls, predictive dialing). Still to build:
conference audio mixer (or a deliberate Jitsi-bridge decision); Opus/G.729 negotiation (C2).
*Exit: 3-way conference with mixed audio; click-to-dial verified working from admin UI.*

**Phase 5 — Device & remote-worker surface (Rig B + D).** Provisioning per model,
hot desking, paging, BLF; WebRTC deployment hardening; mobile push with real FCM/APNs.
*Exit: a new phone provisions from scratch; a browser user calls a desk phone.*

**Phase 6 — Tier-2 triage (C6 + C7).** For each framework feature: promote to PBXCore +
hook live events, or explicitly defer (move its entry to PLANNED_FEATURES.md). Highest-value
first: call quality prediction (QoS data already flows), speech analytics (recordings exist),
DNS SRV failover (trunk resilience). *Exit: every Tier-2 row in this doc is either ✅/🔶 or
marked deferred.*

**Phase 7 — Platform hardening & scale (Rig F, 2×F).** SIP TCP/TLS + SRTP; STIR/SHAKEN;
Alembic discipline; CSP cleanup; capacity testing; geographic redundancy replication;
finish Terraform/K8s. *Exit: PRODUCTION_READINESS_CHECKLIST.md passes.*

---

## Office deployment readiness (analog cutover)

> **Context:** office analog lines are decommissioned ~2026-08. Port date = cutover date.
> Review and prune; strike-through or delete rows that don't apply.

### A — Code blockers

| # | Task | Depends on | Status |
|---|------|-----------|--------|
| A1 | Inbound DID routing (finishes C5) | sip-trunk branch | ✅ code-complete (data layer + API + admin UI + SIP dispatch); field-unverified, no carrier account yet |
| A2 | NAT/public-IP handling for trunk SDP/Via/Contact + symmetric RTP | carrier account (B1) | ❌ untested |
| A3 | `voicemail-fix` merged to DEV (PR #1); hardware regression on Zultys/Cisco ATA still outstanding | — | 🚧 |
| A4 | Kari's Law on-site notification: `emergency_notification.py` email action wired to `pbx/mail` — legal requirement | SMTP host (anonymous relay needs no creds) | ✅ done |
| A5 | Merge sip-trunk → DEV; cut stabilization branch (dev continues on features, deploy runs frozen release + hotfixes) | A1–A2 | ❌ |
| A6 | *(Conditional)* International dialing — outbound matcher is NANP-only (`^1?\d{10}$` in `call_router.py:110`) | office need? | ❌ |
| A7 | Merge `auto-attendant` → DEV (PR #2 — REFER/attended transfer, RTP-relay AA transfer + MOH, call forwarding, unified DTMF); real-phone regression for transfer + forwarding first | — | 🚧 |

### B — Carrier & procurement (longest lead times — start first)

| # | Task | Lead time |
|---|------|-----------|
| B1 | Order SIP trunk + **test DID** (VoIP.ms/Telnyx/Twilio first; AT&T/Comcast templates exist for later) | days |
| B2 | **Submit number port (LNP)** for all analog numbers; port date = cutover date; never cancel analog first — losing the numbers is the failure mode | **2–4 weeks** |
| B3 | E911 on trunk: register dispatchable address (RAY BAUM'S Act); validate via 933 only | days |
| B4 | Provider-level failover routing to cell/answering service when PBX unreachable (cutover parachute) | days |
| B5 | **Analog-device inventory**: fax, alarm panel, elevator phone, door buzzer, card terminals. Alarm/elevator → cellular communicator; fax → ATA G.711 passthrough (⚠️ **no T.38 in PBX core** — ATA templates enable it device-side only) or cloud fax | varies — classic cutover killer |

### C — Infrastructure

| # | Task |
|---|------|
| C-1 | Production Ubuntu server (`scripts/setup_ubuntu.py`), sized per CAPACITY_PLANNING, static LAN IP |
| C-2 | Firewall: 5060/udp restricted to provider IPs; RTP 10000–20000/udp; static public IP/DDNS; QoS for voice; PoE; ~100 kbps per concurrent call |
| C-3 | **UPS for server/switch/router** (analog had carrier power; VoIP + 911 die with building power) |
| C-4 | PostgreSQL + scheduled `backup.sh`/`verify_backup.py` + systemd service + log rotation |
| C-5 | Security lockdown: rotate admin creds, strong SIP passwords, fraud/threat settings verified — toll fraud is a live billing risk once trunk is up |
| C-6 | Admin HTTPS (reverse proxy or LAN-trusted cert) — recommended, prunable if LAN-only |

### D — Configuration & content

| # | Task |
|---|------|
| D-1 | Extension plan per employee; voicemail boxes + PINs |
| D-2 | DID→destination map (main → AA/reception; direct DIDs) — needs A1 |
| D-3 | AA greetings (`generate_espeak_voices.py`), business-hours + after-hours routing |
| D-4 | MoH audio (license-safe); provisioning template per phone model; DHCP opt-66 if auto-provisioning |
| D-5 | Dial-plan hygiene: block 900/976, confirm 911/933, dialing conventions |

### E — Validation gates

| # | Gate |
|---|------|
| E-1 | Full Rig A regression (calls/transfer/hold/DTMF/VM/AA/park/queues) |
| E-2 | Rig C: inbound + outbound on test DID; DTMF to external IVRs; PSTN hold/transfer; soak; concurrent-call test at ~2× office peak |
| E-3 | 933 E911 address verification **and** on-site notification email fires |
| E-4 | Reboot/power-fail recovery: auto-start, phones + trunk re-register |
| E-5 | Backup → restore drill |

### F — Cutover

| # | Task |
|---|------|
| F-1 | Pilot 2–3 desks parallel with analog ≥1 week |
| F-2 | Port-day runbook: test script, provider failover armed, analog physically connected until ported-number inbound confirmed |
| F-3 | User training + printed quick reference (dialing, transfer, VM, 911 note) |
| F-4 | 1-week post-cutover watch: trunk-registration alerting, RTCP/QoS, CDR sanity |

**Week map (July → August 2026):** W1 = B1–B5 orders + A1 dev · W2 = A2 carrier/NAT testing + B3/A4 emergency · W3 = D-* configs + F-1 pilot · W4 = B2 port completes + F-2 cutover + F-4 watch.

---

## Office cutover readiness checklist

> **Context:** office analog (POTS) lines terminate ~August 2026. This list is the
> deployment gate — deliberately over-complete; strike items during review.
> Development continues in parallel; only these items block the cutover.

### Start immediately (long-lead, non-code)

- [ ] Order SIP trunk account + one **test DID** (low-cost provider first; AT&T/Comcast templates are the production targets)
- [ ] **Submit number port** for all office numbers (2–4 week lead; requires bill/LOA; numbers are lost if analog disconnects before port completes — schedule the port date explicitly)
- [ ] **Analog-dependents audit**: fax, alarm panel, elevator phone, door buzzer, card terminals, postage meter, paging amp — disposition each (alarm/elevator → cellular communicator, usually legally required; do not route through the PBX)
- [ ] Fax plan: ATA G.711 passthrough test early (ATA templates enable T.38 but the RTP relay has no T.38/UDPTL handling — unverified); fallback = e-fax service
- [ ] Hardware orders: production server, PoE switch capacity, **UPS for server/switch/router** (911 availability during power loss), spare phone + spare ATA
- [ ] ISP: static public IP, bandwidth check (~100 kbps per concurrent call each way), **disable router SIP ALG**

### Code blockers (critical path)

- [ ] **Inbound DID routing** (C5) — code-complete (DID→destination map, API, admin UI, SIP dispatch); needs real-carrier validation of the trunk IP-matching heuristic
- [ ] Outbound trunk validation on test DID: registration, NAT/public-IP in SDP/Via/Contact, DTMF to external IVRs, codec negotiation
- [ ] `voicemail-fix` merged (PR #1); regression on office phone models still outstanding
- [ ] Merge `auto-attendant` (PR #2 — REFER/attended transfer, RTP-relay AA transfer + MOH, call forwarding, unified DTMF); regression-test transfer + forwarding on office phone models
- [x] **Kari's Law notification** — `emergency_notification.py` now sends real email through `pbx/mail` and audit-logs the attempt; see `docs/EMAIL_SETUP.md` for validation. Webhook path and call/page notify remain post-cutover
- [ ] E911: register dispatchable address with provider; verify karis_law/e911_location routing; validate via provider test number (933-style — never live 911; keep test-mode protection on until final check)
- [ ] SIP exposure hardening: 5060/udp restricted to trunk provider IPs; no WAN registrations

### Infrastructure & configuration

- [ ] Production server: Ubuntu 24.04, `make install-production`, systemd service, health check + auto-restart
- [ ] PostgreSQL (not SQLite); migrations run; **backup.sh tested including a restore**
- [ ] Firewall (5060 from provider, RTP 10000–20000/udp, 9000 LAN/proxy-only); voice VLAN / switch QoS (PBX relay does not DSCP-mark its own packets — trust/remark at switch)
- [ ] Admin HTTPS (see Admin HTTPS row, Tier 0) + change default credentials + MFA
- [ ] Extension/numbering plan checked against dialplan collisions (2xxx conf, 7x park, 8xxx queues, 10/11-digit trunk, 911)
- [ ] Provision every phone model on office network; per-model DTMF verification
- [ ] Auto-attendant: menus, business-hours/night mode (time-based routing), generate prompt files; MoH audio in `moh/`
- [ ] Voicemail boxes/PINs; optional voicemail-to-email (SMTP)
- [ ] Outbound caller-ID mapping (station → DID): set each extension's **DID Number** field (Extensions page) — `_route_to_trunk()` uses it as the outbound caller ID, falling back to the extension number if unset. Plus CNAM registration; international-call blocking / fraud limits
- [ ] Monitoring: Prometheus/Grafana or health-check email alerts; NTP; log rotation

### Validation & cutover

- [ ] **Two-week parallel run** on test DIDs while analog still live: full per-phone regression (in/out, transfer, hold, park, VM deposit/retrieve, AA paths, DTMF to external IVRs), soak at expected concurrent load
- [ ] Failure drills: server reboot, power pull, internet drop — documented 911 behavior for each
- [ ] Cutover runbook: per-number port verification, day-of provider contact, rollback notes
- [ ] User training + quick-reference cards; week-one escalation path
- [ ] Accepted gaps recorded at cutover: no conference mixing (C3), no STIR/SHAKEN attestation, callbacks not yet wired to `CallOriginator`, no SIP-TLS/SRTP, WebRTC unhardened (click-to-dial is wired but field-unverified)

---

## Related documents

- `docs/PLANNED_FEATURES.md` — documented-but-unimplemented API surface per feature (kept during a docs accuracy audit; treat as the aspirational backlog)
- `docs/PRODUCTION_READINESS_CHECKLIST.md`, `docs/OPERATIONS_RUNBOOK.md`, `docs/HA_DEPLOYMENT_GUIDE.md`, `docs/CAPACITY_PLANNING.md`
- `CLAUDE.md` — build/test commands, conventions, known technical debt
- `COMPLETE_GUIDE.md` — user-facing setup and configuration
