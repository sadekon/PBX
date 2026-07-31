# Email Setup and Validation

How the PBX sends mail, how to configure it, and — most importantly — how to prove it works
**before** you depend on it for voicemail notification and Kari's Law emergency alerts.

Mail transport lives in `pbx/mail`. It owns connection, TLS, authentication, retry, queueing
and MIME assembly. It owns none of what a message *says*: subject templates and body wording
belong to the feature raising the notification.

---

## 1. Configuration

All transport settings live under the top-level `smtp:` key in `config.yml`. Values are
supplied through `.env`.

```yaml
smtp:
  host: ${SMTP_HOST}
  port: ${SMTP_PORT}
  security: ${SMTP_SECURITY}   # starttls | smtps
  auth: ${SMTP_AUTH}           # none | login
  username: ${SMTP_USERNAME}
  verify_cert: true
  ca_file: ${SMTP_CA_FILE}
  helo_hostname: ''
  from_address: voicemail@corp.com
  from_name: Warden VoIP
  timeout: 10
  max_retries: 3
  max_attachment_bytes: 8388608
```

| Setting | Meaning |
| --- | --- |
| `security` | How the connection is protected. **TLS is mandatory** — there is no plaintext mode. `starttls` connects then upgrades (ports 587/25); `smtps` is encrypted from the first byte (port 465). |
| `auth` | Whether the PBX identifies itself. `none` means anonymous relay, where the server trusts you by source IP. `login` sends a username and password. |
| `verify_cert` | Whether the server's certificate is checked. Leave `true`. |
| `ca_file` | PEM bundle for an internal CA. Required when the mail server's certificate is issued by your own PKI rather than a public authority. |
| `helo_hostname` | The name the PBX announces itself as. Set it if the receive connector validates HELO or reverse DNS — otherwise Python derives one, and the fallback can look malformed. |

`security` and `auth` are **independent**. This matters: they replace a single `use_tls`
boolean that made port 465 impossible to express and made "no authentication wanted"
indistinguishable from "credentials missing".

### The password

Read **only** from the `SMTP_PASSWORD` environment variable, never from `config.yml`. A
password placed in the config file is ignored. `Config.save()` also re-substitutes `${...}`
placeholders on write, so a credential resolved at load time is never written back to disk.

`auth: none` is not the insecure option. Anonymous IP-restricted relay is the normal pattern
for an appliance, the transport is encrypted either way, and it means no credential exists to
store, rotate, or leak.

Anonymous also avoids a *SendAs* problem. With authenticated submission Exchange generally
restricts the sender to the authenticated mailbox, so authenticating as one account while
sending as `voicemail@corp.com` needs an explicit SendAs grant. Anonymous relay has no
authenticated identity, so that check never applies and `from_address` is honoured as-is.

---

## 2. Validating that the PBX can send mail

Four layers, cheapest first. Do not skip ahead — a failure at a lower layer makes every
higher one meaningless.

### Layer 1 — the code (no network)

```bash
make test-python                       # or:
.venv/bin/python -m pytest tests/test_mail_*.py tests/test_config_placeholder_save.py -q
```

Covers settings coercion, MIME structure, header-injection defence, TLS mode selection,
error classification, retry policy, queue behaviour and shutdown drain.

### Layer 2 — the wiring (no network)

```bash
.venv/bin/python -m pytest tests/test_mail_wiring.py -q
```

Proves `PBXCore` is given a `Mailer`, that voicemail and emergency notification both reach
it, and that the `Mailer` exists even when SMTP is unconfigured. That last point is what
removes the need for a `hasattr` guard at each call site — a missing guard is why emergency
email previously logged instead of sending.

### Layer 3 — transport, **from the PBX host**

This is the step that cannot be done from a workstation. Exchange decides whether to accept
mail based on the **connecting IP address**, so a successful test from your laptop proves
nothing about the PBX.

Run this **on the PBX**:

```bash
# Handshake only: connect, EHLO, STARTTLS, AUTH. Sends nothing.
python scripts/send_test_email.py --verify-only --verbose

# Then a real message to an internal recipient.
python scripts/send_test_email.py --to you@corp.com --verbose
```

If the PBX runs in Docker, run it **inside the container** — the container's network identity
is what Exchange sees:

```bash
docker compose exec pbx python scripts/send_test_email.py --verify-only --verbose
```

`--verbose` prints the full SMTP conversation with credentials suppressed. Check:

- the source address the server greets you with (`250 ... Hello [x.x.x.x]`) is the PBX's,
- `250-STARTTLS` appears in the capability list,
- the run ends `SUCCESS` with a `Message-ID`.

Finally, repeat with an **external** recipient. Internal delivery succeeding while external
fails is a relay-permission issue, not a PBX fault — see the troubleshooting table.

### Layer 4 — the features end to end

1. **Leave a voicemail** on a provisioned extension with an email address configured. The
   notification should arrive with the `.wav` attached.
2. **Confirm teardown is not blocked.** Firewall the mail host off and leave another
   voicemail: the call should end promptly and the message should queue. Mail is sent
   asynchronously precisely so SMTP latency never delays call teardown.
3. **Trigger a test 911 call.** The emergency notification should arrive, and the attempt
   should appear in the audit log whether it succeeded or failed.
4. **Restart with mail queued** (`kill -TERM`) to confirm the shutdown drain.

Check the operational counters at any time via `pbx_core.mailer.get_statistics()` —
sent, failed, dropped, retries, queue depth, and the last error.

---

## 3. On-premises Exchange

### Ports

Exchange Server's default receive connectors listen on **25** (Default Frontend) and **587**
(Client Frontend). It does **not** listen on 465 by default, so `security: smtps` will usually
fail to connect. Use `starttls`.

### Anonymous relay vs authenticated submission

For an appliance the usual arrangement is a receive connector permitting anonymous submission
from a fixed IP:

```powershell
Get-ReceiveConnector | fl Name,Bindings,AuthMechanism,PermissionGroups,RemoteIPRanges
```

Set `auth: none` and ensure the PBX's IP is in `RemoteIPRanges`. For authenticated submission
instead, `PermissionGroups` must include `ExchangeUsers`, and `AuthMechanism` must include
`BasicAuth`/`BasicAuthRequireTLS`. With `BasicAuthRequireTLS`, `AUTH ... LOGIN` is advertised
**only after** STARTTLS — seeing `LOGIN` appear in the second EHLO is normal and correct.

### Internal certificate authorities

A mail server using a certificate from your own AD Certificate Services PKI will not verify
against the system trust store. Export the CA and point `ca_file` at it:

```bash
# Inspect what the server presents, including the issuer chain
openssl s_client -connect mail.corp.local:587 -starttls smtp -showcerts </dev/null
```

Use the **root** CA as the trust anchor where possible; an intermediate works but must be
replaced whenever that sub-CA is reissued. Ensure the file is readable by the PBX service
account (`pbx`, UID 1000).

### Installing the CA on the PBX (systemd deployment)

The unit sets `EnvironmentFile=-<project_root>/.env` and runs as the service account, so the
CA must be readable by that user and `.env` must sit beside `config.yml`.

```bash
# 1. Convert to PEM if the export is DER (a .cer from certutil usually is)
openssl x509 -inform DER -in albl-root.cer -out albl-root.pem

# 2. Install it readable by everyone, owned by root
sudo install -o root -g root -m 644 albl-root.pem /etc/ssl/certs/corp-internal-ca.pem

# 3. Confirm the service account can actually read it
sudo -u pbx head -1 /etc/ssl/certs/corp-internal-ca.pem
```

That third step matters. A CA file the service cannot read fails with
`unable to get local issuer certificate` — identical to having no CA configured at all, which
sends you looking in the wrong place.

Then point `.env` at it and restart. `EnvironmentFile` is read only at service start, so a
`.env` edit does nothing until the unit is restarted:

```bash
sudo systemctl restart pbx
sudo -u pbx python scripts/send_test_email.py --verify-only --verbose
```

Run the verification **as the service account** (`sudo -u pbx`), not as root — otherwise you
are testing permissions the PBX itself does not have.

### The hostname trap

`create_default_context()` sets `check_hostname=True`. If the certificate names
`EXCH01.corp.local` but you configured `host: webmail.corp.local`, verification **fails even
with the correct CA loaded**, because the names differ.

The fix is to set `host` to a name on the certificate — check its SAN:

```bash
openssl s_client -connect mail.corp.local:587 -starttls smtp </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -ext subjectAltName
```

Do **not** reach for `verify_cert: false`. It silences the symptom and discards the guarantee
that you are talking to the real server.

---

## 4. Troubleshooting

Errors are classified into three kinds. `EmailTransientError` is retried with backoff;
`EmailPermanentError` and `EmailConfigError` are not.

| Symptom | Class | Cause and fix |
| --- | --- | --- |
| `TLS certificate verification failed ... unable to get local issuer certificate` | Config | Certificate issued by an internal CA. Set `ca_file`. |
| `TLS certificate verification failed ... hostname mismatch` | Config | `smtp.host` is not a name on the certificate. Correct `host`; do not disable verification. |
| `SMTP server does not support a required extension` | Config | The connector does not advertise STARTTLS. Enable TLS on it — the PBX deliberately fails closed rather than sending in the clear. |
| `535 5.7.3 Authentication unsuccessful` | Permanent | Wrong credentials, wrong username format (try `DOMAIN\samaccountname`), a locked or expired account, or a connector lacking `ExchangeUsers` in `PermissionGroups`. Check account state before retrying — repeated attempts can trigger AD lockout. |
| `550 5.7.54 unable to relay` | Permanent | Relaying to an **external** recipient from an anonymous connector. Grant `ms-Exch-SMTP-Accept-Any-Recipient`, or send only to internal recipients. Internal mail working while external fails is the signature. |
| `550 5.7.1 ... not allowed` on `MAIL FROM` | Permanent | `from_address` is in a domain the connector will not accept. Use an address in an Accepted Domain. |
| `SMTP server refused some recipients` | Permanent | Partial delivery. The message reached some recipients but not all; refused addresses are listed in the error detail. |
| `Could not connect` / `Timed out` / `Could not resolve` | Transient | Network, DNS or firewall. Retried automatically. |
| `Attachments exceed the configured size limit` | Config | Recording larger than `max_attachment_bytes`. Exchange's default receive limit is 10 MB; a 180 s WAV is roughly 2.8 MB. |
| `SMTP is not configured` | Config | `smtp.host` or `smtp.from_address` is unset. Note that an unresolved `${SMTP_HOST}` counts as unset. |

### Watching from the server side

```powershell
Set-ReceiveConnector "<name>" -ProtocolLoggingLevel Verbose
# %ExchangeInstallPath%TransportRoles\Logs\FrontEnd\ProtocolLog\SmtpReceive\

Get-MessageTrackingLog -Start (Get-Date).AddMinutes(-15) -Sender "voicemail@corp.com" |
  ft Timestamp,EventId,Recipients,MessageSubject
```

Every message carries a `Message-ID` generated in the sender's domain — that is the value to
search on when tracing a specific notification a user says never arrived.

---

## 5. Security notes

- **TLS is mandatory.** A configuration requesting plaintext is corrected to `starttls` and
  the substitution is reported. If the server will not do STARTTLS, the send fails.
- **Header injection is prevented by construction.** Caller ID arrives from the SIP wire and
  is attacker-controlled; CR, LF and NUL are stripped from every value interpolated into a
  header, so no input can forge one.
- **Passwords never reach logs.** `get_statistics()` and the CLI both redact them, and
  `--verbose` suppresses the AUTH exchange — including the base64 payloads that SASL puts on
  the wire, which is where a naive filter looking for the literal password would miss it.
- Recipient **domains** are logged at INFO; full addresses only at DEBUG.
