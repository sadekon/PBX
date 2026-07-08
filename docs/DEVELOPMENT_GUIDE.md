# Warden VoIP — Development Guide & Feature Status Ledger

> **Purpose:** the single living document tracking what is built, what is partially
> built, and what remains to finish, debug, and deploy every feature in this system.
> Update the status tables here whenever feature work lands.
>
> **Last audited:** 2026-07-07, against branches `voicemail-fix` (current), `sip-trunk`, and `DEV`.

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
| **C1** | SIP signaling core — registration, dialogs, routing, transfer/hold | ✅ working (UDP-only) | `pbx/sip/`, `core/call_router.py` | Call-handling features work today; TLS/TCP transport is Phase-7 hardening |
| **C2** | RTP media relay & IVR plumbing — relay, DTMF (RFC 2833 + in-band), prompt playback, recording tap | 🚧 solid, DTMF/prompt fixes landing on `voicemail-fix` | `pbx/rtp/`, `core/voicemail_handler.py`, `rtp/handler.py` | Voicemail, AA, MoH, recording, paging; codec expansion slots in here |
| **C3** | **Audio mixer (N-way media) — does not exist** | ❌ missing | (to build: sum/mix G.711 streams per participant, or bridge via Jitsi) | Conference audio, 3-way calling, barge/whisper/screening |
| **C4** | **Call-originate primitive — does not exist** ("PBX creates a leg to X, then bridges") | ❌ missing | (to build in `core/` + SIP server) | Click-to-dial, callback completion, predictive dialing, emergency-notification calls, operator console actions |
| **C5** | Trunk / PSTN connectivity | 🔶 outbound done on `sip-trunk` branch; **inbound (DID) missing** | `features/sip_trunk.py`, `sip/server.py`, `core/call_router.py` | DID routing, E911 stack, LCR, STIR/SHAKEN, DNS SRV failover, fraud detection on real traffic, SBC validation |
| **C6** | Analytics tap — live audio/transcript/QoS feed into analysis engines | 🔶 recordings + RTCP QoS data exist; no live feed wiring | `features/call_recording.py`, `rtp/rtcp_monitor.py` | Speech analytics, voice biometrics, call tagging, quality prediction, recording analytics, conversational AI |
| **C7** | Data & event plane — CDR, webhooks, DB persistence, migrations | ✅ working | `features/cdr.py`, `features/webhooks.py`, `utils/database.py` | BI export, compliance, retention, residency, geo-redundancy replication build on it |

Features gated only by **credentials or a deployment surface** (SMTP, push keys,
LDAP, CRM APIs, HTTPS/browser) carry no C-number — their gate is **Rig E** or **Rig D**.

---

## Tier 0 — Core platform

The runtime is a single Python process: `main.py` → `PBXCore` (`pbx/core/pbx.py`, 2 255 lines)
owns everything. `PBXCore.start()` boots, in order: security enforcement → SIP server →
Flask API server → DND scheduler → trunk registration → security monitor → Prometheus
collector → registration-expiry sweep.

| Component | Files | Status | Notes / remaining work |
|-----------|-------|--------|------------------------|
| SIP server (C1) | `pbx/sip/server.py` (1 961 ln) | ✅ | **UDP only** — a single `SOCK_DGRAM` socket. No TCP or TLS transport, so no SIPS and no encrypted signaling to carriers/phones. Biggest core limitation. |
| SIP message/SDP/transaction (C1) | `pbx/sip/message.py`, `sdp.py`, `transaction.py` | ✅ | Parser, SDP builder/negotiation, transaction state machine. |
| RTP relay + media (C2) | `pbx/rtp/handler.py` (1 445 ln), `jitter_buffer.py`, `rfc2833.py`, `rtcp_monitor.py` | ✅ | In-process relay, ports 10000–20000, RFC 2833 DTMF send/receive, RTCP QoS feed. G.711 µ/A end-to-end; G.722 partially via `utils/audio`. No mixer (C3) and no originate primitive (C4) — the two missing core builds. |
| Call state machine + router (C1) | `pbx/core/call.py`, `call_router.py` (853 ln) | ✅ | Dialplan patterns route to extensions, conference rooms (`2xxx`), parking (`7x`), queues (`8xxx`), voicemail, AA. Trunk routing added on `sip-trunk` branch. |
| PBXCore + feature init | `pbx/core/pbx.py`, `feature_initializer.py` | ✅ | Static (not dynamic) initialization of ~30 Tier-1 features. |
| IVR handlers (C2) | `core/voicemail_handler.py` (1 371 ln), `auto_attendant_handler.py`, `paging_handler.py`, `emergency_handler.py` | 🚧 | Voicemail/AA IVR under active repair on `voicemail-fix` (DTMF reliability, prompts, barge-in). |
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
| Call queues | `call_queue.py` | `features.call_queues` | 🔶 | Queue/agent state logic present, dialplan `8xxx` routes into it. Verify end-to-end: hold music while queued (C2), agent delivery, wrap-up. | A |
| Call parking | `call_parking.py` | `features.call_parking` | 🔶 | Slot management wired to `7x` pattern. Verify park/retrieve with real phones (REFER/replaces behavior). | A |
| Find me / follow me | `find_me_follow_me.py` | `features.find_me_follow_me` | 🔶 | Sequential/simultaneous ring logic; external destinations also need C5. | A, C |
| Time-based routing | `time_based_routing.py` | config | ✅ | Pure routing logic; unit-testable. | A |
| Skills routing | `skills_routing.py` | `features.skills_routing` | 🔶 | DB-backed router; verify interaction with queue delivery. | A |
| Presence / BLF | `presence.py` | `features.presence` | 🔶 | State tracking present; verify SUBSCRIBE/NOTIFY dialogs against hardphone BLF keys. | B |
| Hot desking | `hot_desking.py` | `features.hot_desking` | 🔶 | Login/logout with VM PIN. Verify against provisioned hardphones. | B |
| Phone provisioning | `phone_provisioning.py`, `provisioning_templates/` | `provisioning.enabled` | 🔶 | Zultys, Cisco (incl. CP-8851-3PCC, ATAs), Polycom templates exist. Each new model = template + quirks (DTMF payload-type table in `config.yml`). Requires real hardware per model. | B |
| Phone book | `phone_book.py` | `features.phone_book` | ✅ | AD auto-sync option; remote-phonebook URL consumed by provisioned phones. | B (+E for AD) |

### C2 — Media & IVR (relay ✅, fixes landing 🚧)

| Feature | Module | Config gate | Status | Remaining work | Test rig |
|---------|--------|------------|--------|----------------|----------|
| Voicemail | `voicemail.py` + `core/voicemail_handler.py` | `features.voicemail` | 🚧 | Active on `voicemail-fix`: RFC 2833 + in-band DTMF in IVR, prompts, barge-in, greeting review. Finish: deployed test on real phones, merge to DEV. Email notify needs SMTP (Rig E); transcription needs a Vosk model on disk (Rig E). | A (+E) |
| Auto attendant | `auto_attendant.py` + handler | `features.auto_attendant` | 🚧 | Same IVR/DTMF plumbing as voicemail (barge-in landed). Needs prompt files (`scripts/` generator) and deployed DTMF test across phone models. | A |
| Music on hold | `music_on_hold.py` | `features.music_on_hold` | ✅ | Needs audio files in `moh/`. | A |
| Call recording | `call_recording.py` | `features.call_recording` | ✅ | Records from RTP relay. Retention (`recording_retention.py`) and announcements (`recording_announcements.py`) wired. Verify storage growth + retention sweeps on Rig F. | A |
| Paging | `paging.py` + `core/paging_handler.py` | `features.paging` | 🔶 | Handler contains production-gap language; multicast paging needs real phones on a LAN segment that permits multicast. | B |
| CDR + statistics (C7) | `cdr.py`, `statistics.py` | always | ✅ | Feeds analytics pages and the Tier-2 BI framework. | A + F |

### C3 — Mixer-gated (mixer ❌ — blocked until built)

| Feature | Module | Config gate | Status | Remaining work | Test rig |
|---------|--------|------------|--------|----------------|----------|
| Conference | `conference.py` | `features.conference` | 🔶 | **State machine only** — rooms, participants, mute all tracked, dialplan `2xxx` routes in. **No RTP audio mixer exists anywhere in `pbx/rtp/`**, so N-way audio does not actually mix. Build the mixer (sum G.711 per participant, minus own audio) or bridge via Jitsi. | A (3 phones) |

### C4 — Originate-gated (originate primitive ❌ — blocked until built)

| Feature | Module | Config gate | Status | Remaining work | Test rig |
|---------|--------|------------|--------|----------------|----------|
| Callback queue | `callback_queue.py` | config | 🔶 | Queue/persistence wired; *completing* a callback requires the PBX to originate a call leg. | A |
| Emergency notification | `emergency_notification.py` | `features.emergency_notification` (default on) | 🔶 | **Stub actions**: logs "Would call/email/SMS …" (lines ~485–614). Calls/paging need C4; email/SMS need SMTP + SMS provider (Rig E). | A + E |

### C5 — Trunk-gated (outbound 🚧 on branch, inbound ❌)

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

### C4 — Originate-gated

| Feature | Module | What exists | Key missing piece | Test rig |
|---------|--------|-------------|-------------------|----------|
| Click-to-dial | `click_to_dial.py` + admin page | API + UI | Originate primitive (C4) | A |
| Predictive dialing | `predictive_dialing.py` + `_db.py` | Statistical pacing engine | C4 to place calls + C5 trunk to reach PSTN; answer detection (C6) | C |
| Predictive VM drop | `predictive_voicemail_drop.py` | Framework | C4 + answering-machine detection on live media (C6) | C |
| Call blending | `call_blending.py` | Framework | C4 + queue integration | A + C |

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
| `stir_shaken.py` | C5 | Caller-ID attestation/verification | **Wire** — belongs in trunk INVITE path (Identity header) once sip-trunk lands; needs signing certs from carrier/STI-PA. Rig C. |
| `least_cost_routing.py` | C5 | Multi-trunk cost-based route selection | **Wire** — natural extension of `SIPTrunkSystem.route_outbound()` once >1 trunk exists. Rig C. |
| `operator_console.py` | C4 | Attendant console backend | Wire to admin UI + presence once originate exists; or defer. |
| `advanced_call_features.py` | C3 | Call screening/whisper/barge | Same mixer investment as conference — schedule together. |
| `ai_call_routing.py` | C7 | ML route selection | Defer until call-volume data exists. |
| `audio_processing.py` | C2 | Audio effects/normalization | Fold into recording pipeline or cut. |
| `opus_codec.py`, `g729_codec.py`, `g726_codec.py`, `ilbc_codec.py`, `speex_codec.py` | C2 | Codec implementations | Wire into SDP negotiation + transcoding in `utils/audio.py` (only G.711/G.722 live). Opus first — WebRTC config already advertises it (PT 111). |
| `g722_codec_itu.py` | C2 | Alternate G.722 | Duplicate of wired `g722_codec.py` — consolidate or delete. |
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
- **Tests**: substantial coverage additions (`test_sip_server_coverage.py` +337 ln, `test_sip_trunk_coverage.py` +324 ln, `test_call_router_coverage.py` +175 ln).

### Remaining to finish

1. **Inbound calls (DID routing) — not implemented at all.** No inbound rule model,
   no recognition of INVITEs arriving from a registered trunk's host, no DID→extension/AA/queue
   mapping. Without it the trunk is outbound-only. Design: match source against trunk hosts →
   look up To-user in an inbound-route table → hand to `CallRouter` as an internal destination.
2. **`SIPTrunkSystem.make_outbound_call()` is a legacy stub** (TODO at `sip_trunk.py:757`) —
   superseded by `CallRouter._route_to_trunk()`; delete or delegate to avoid two code paths.
3. **`_handle_trunk_failure()` only logs** (TODO at `sip_trunk.py:798`) — per-call failover
   works, but persistent rule re-pointing/recovery monitoring is unimplemented.
4. **NAT/public-address handling** for SDP and Via/Contact toward the carrier (external IP
   config, symmetric RTP) — untested until Rig C exists.
5. **Carrier validation** against a real provider (config templates for AT&T and Comcast
   exist: `config_att_sip.yml`, `config_comcast_sip.yml`; a cheap SIP provider like
   VoIP.ms/Telnyx/Twilio is the low-risk first target).
6. Follow-ons unlocked afterward: STIR/SHAKEN, least-cost routing, DNS SRV failover,
   E911 via carrier, FMFM-to-external, predictive dialing.

---

## Branch: voicemail-fix (current)

Capability C2 work. Recent commits: G.711 µ-law encoder fix, RFC 2833 + in-band DTMF
into the voicemail IVR, greeting-review prompts, regenerated prompt audio, menu
re-announcement, and DTMF barge-in (interrupt prompts by keypress) for both voicemail and AA.
Barge-in only peeks the pending-DTMF source, so the existing DTMF loop still consumes the
digit and drives the state machine; PIN entry is the one exception — it barges only on `#`,
so the prompt keeps playing while the caller dials PIN digits.

**To finish:** deployed regression on real phones (Zultys + Cisco ATA are the tested
targets per commit history) covering PIN entry (including barge-in-on-`#` only), greeting
record/review, message playback/delete/save, and barge-in on every other prompt — then
merge to DEV.

---

## Cross-cutting platform limitations

These constrain *every* feature's deployed testing and should be scheduled as platform work:

1. **SIP is UDP-only** (C1). No TCP (large-message fragmentation risk), no TLS (no SIPS),
   no SRTP → no encrypted calling. Carrier trunks increasingly require TLS.
   `utils/tls_support.py` exists for the API layer only.
2. **No conference/N-way audio mixer** (C3) — blocks real conferencing, barge/whisper,
   and 3-way calling.
3. **No call-originate primitive** (C4) — blocks click-to-dial, predictive dialing,
   emergency-notification calls, callback completion. One build unblocks four-plus features.
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
| **E — External services** | SMTP account; FCM/APNs keys; LDAP/AD server; Vosk model file; CRM/Jitsi/Matrix/Zoom/Outlook credentials; a webhook receiver | Email notify, transcription, push, AD sync, screen pop, integrations, webhooks, SSO |
| **F — Full stack** | `docker compose up` (PostgreSQL 17, Redis 7, Prometheus, Grafana) | Persistence, retention sweeps, metrics/dashboards, health checks, backup/recovery scripts, HA/geo experiments (2×F) |

---

## Recommended phase plan

Each phase names the capability it finishes and has an observable exit criterion.

**Phase 1 — Land the IVR work (C2, Rig A).**
Regression-test `voicemail-fix` on real phones; merge to DEV.
*Exit: voicemail + AA fully driveable by DTMF from every supported phone model.*

**Phase 2 — Finish SIP trunking (C5, Rig C).** Implement inbound DID routing; remove the
legacy outbound stub; NAT handling; validate against a low-cost carrier, then AT&T/Comcast
configs. *Exit: a PSTN caller reaches an extension via DID, and an extension dials out —
reliably, with failover.*

**Phase 3 — Emergency stack (C4-lite + C5, Rig C + E).** Replace `emergency_notification.py`
stubs with real email/SMS/call/paging actions; E911 location flow through the trunk
(carrier test procedure, never live 911). *Exit: 911-test dial triggers correct trunk
routing + on-site notifications.* Legal-compliance gate for any real deployment.

**Phase 4 — Build the missing core primitives (C3 + C4, Rig A).** Call-originate
primitive (unblocks click-to-dial, callbacks, notification calls, predictive dialing);
conference audio mixer (or a deliberate Jitsi-bridge decision); Opus/G.729 negotiation (C2).
*Exit: 3-way conference with mixed audio; click-to-dial works from admin UI.*

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
| A1 | Inbound DID routing (finishes C5) | sip-trunk branch | ❌ not started |
| A2 | NAT/public-IP handling for trunk SDP/Via/Contact + symmetric RTP | carrier account (B1) | ❌ untested |
| A3 | Land `voicemail-fix` (hardware regression on Zultys/Cisco ATA, merge to DEV) | — | 🚧 |
| A4 | Kari's Law on-site notification: wire `emergency_notification.py` email action to existing SMTP (`email_notification.py`) — legal requirement | SMTP creds | ❌ stubbed |
| A5 | Merge sip-trunk → DEV; cut stabilization branch (dev continues on features, deploy runs frozen release + hotfixes) | A1–A2 | ❌ |
| A6 | *(Conditional)* International dialing — outbound matcher is NANP-only (`^1?\d{10}$` in `call_router.py:110`) | office need? | ❌ |

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
| D-3 | AA greetings (`generate_tts_prompts.py`), business-hours + after-hours routing |
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

- [ ] **Inbound DID routing** (C5) — recognize INVITEs from trunk hosts, DID → extension/AA/queue map (does not exist today)
- [ ] Outbound trunk validation on test DID: registration, NAT/public-IP in SDP/Via/Contact, DTMF to external IVRs, codec negotiation
- [ ] Finish + merge `voicemail-fix` (regression on office phone models)
- [ ] **Kari's Law notification** — replace `emergency_notification.py` "Would email…" stubs with real email (SMTP) and/or webhook minimum (call/page notify needs C4 — post-cutover)
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
- [ ] Outbound caller-ID mapping (station → DID) + CNAM registration; international-call blocking / fraud limits
- [ ] Monitoring: Prometheus/Grafana or health-check email alerts; NTP; log rotation

### Validation & cutover

- [ ] **Two-week parallel run** on test DIDs while analog still live: full per-phone regression (in/out, transfer, hold, park, VM deposit/retrieve, AA paths, DTMF to external IVRs), soak at expected concurrent load
- [ ] Failure drills: server reboot, power pull, internet drop — documented 911 behavior for each
- [ ] Cutover runbook: per-number port verification, day-of provider contact, rollback notes
- [ ] User training + quick-reference cards; week-one escalation path
- [ ] Accepted gaps recorded at cutover: no conference mixing (C3), no click-to-dial/callbacks (C4), no SIP-TLS/SRTP, WebRTC unhardened

---

## Related documents

- `docs/PLANNED_FEATURES.md` — documented-but-unimplemented API surface per feature (kept during a docs accuracy audit; treat as the aspirational backlog)
- `docs/PRODUCTION_READINESS_CHECKLIST.md`, `docs/OPERATIONS_RUNBOOK.md`, `docs/HA_DEPLOYMENT_GUIDE.md`, `docs/CAPACITY_PLANNING.md`
- `CLAUDE.md` — build/test commands, conventions, known technical debt
- `COMPLETE_GUIDE.md` — user-facing setup and configuration
