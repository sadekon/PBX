"""
Credential redaction in pbx.mail.debug, used by every script's --verbose flag.

Regression test for a real leak: the first implementation searched the transcript for the
*literal* password, but SASL base64-encodes credentials before they reach the wire, so the
match never fired and the password was printed in full. It simultaneously blanked the
harmless ``250-AUTH`` capability advertisement, hiding the diagnostics the flag exists for.

The redactor must fail safe -- suppress anything that could be a credential -- while leaving
server capability lines readable.
"""

import base64

import pytest

from pbx.mail import Redactor, SmtpSettings

PASSWORD = "hunter2-not-real!"
USERNAME = "voicemail@corp.local"


@pytest.fixture
def redactor():
    settings = SmtpSettings(
        host="mail.corp.local",
        from_address="pbx@corp.local",
        auth="login",
        username=USERNAME,
        password=PASSWORD,
    )
    return Redactor(settings)


@pytest.mark.unit
class TestCredentialSuppression:
    def test_reply_to_a_334_challenge_is_redacted(self, redactor):
        """This is the exact line that leaked: the base64 password after 'Password:'."""
        wire = base64.b64encode(PASSWORD.encode()).decode()

        redactor("reply: b'334 UGFzc3dvcmQ6'")
        out = redactor(f"send: '{wire}\\r\\n'")

        assert wire not in out
        assert "redacted" in out

    def test_auth_command_argument_is_redacted(self, redactor):
        wire = base64.b64encode(USERNAME.encode()).decode()

        out = redactor(f"send: 'AUTH LOGIN {wire}'")

        assert wire not in out

    def test_bare_base64_send_payload_is_redacted(self, redactor):
        out = redactor("send: 'dm9pY2VtYWlsQGNvcnAubG9jYWw='")

        assert "dm9pY2VtYWls" not in out

    def test_literal_password_is_redacted_as_a_backstop(self, redactor):
        out = redactor(f"send: 'something {PASSWORD} inline'")

        assert PASSWORD not in out

    def test_plain_mechanism_blob_is_redacted(self, redactor):
        blob = base64.b64encode(f"\0{USERNAME}\0{PASSWORD}".encode()).decode()

        out = redactor(f"send: 'AUTH PLAIN {blob}'")

        assert blob not in out

    def test_full_transcript_never_contains_the_password(self, redactor):
        wire = base64.b64encode(PASSWORD.encode()).decode()
        transcript = [
            "reply: b'220 mail.corp.local Microsoft ESMTP MAIL Service ready'",
            "send: 'ehlo pbx.corp.local'",
            "reply: b'250-AUTH GSSAPI NTLM LOGIN'",
            "send: 'STARTTLS'",
            f"send: 'AUTH LOGIN {base64.b64encode(USERNAME.encode()).decode()}'",
            "reply: b'334 UGFzc3dvcmQ6'",
            f"send: '{wire}'",
            "reply: b'535 5.7.3 Authentication unsuccessful'",
        ]

        rendered = "\n".join(redactor(line) for line in transcript)

        assert PASSWORD not in rendered
        assert wire not in rendered


@pytest.mark.unit
class TestDiagnosticsArePreserved:
    """Over-redaction is its own failure: the flag exists to diagnose connectors."""

    def test_capability_advertisement_is_kept(self, redactor):
        line = "reply: b'250-AUTH GSSAPI NTLM LOGIN'"

        assert redactor(line) == line

    def test_starttls_advertisement_is_kept(self, redactor):
        line = "reply: b'250-STARTTLS'"

        assert redactor(line) == line

    def test_server_error_is_kept(self, redactor):
        line = "reply: b'535 5.7.3 Authentication unsuccessful'"

        assert redactor(line) == line

    def test_envelope_commands_are_kept(self, redactor):
        line = "send: 'MAIL FROM:<pbx@corp.local>'"

        assert redactor(line) == line

    def test_challenge_line_itself_is_kept(self, redactor):
        """The server's prompt is not secret and shows which mechanism is in play."""
        line = "reply: b'334 UGFzc3dvcmQ6'"

        assert redactor(line) == line

    @pytest.mark.parametrize("verb", ["STARTTLS", "quit", "DATA", "rset", "NOOP", "BDAT", "vrfy"])
    def test_smtp_verbs_are_never_mistaken_for_base64(self, redactor, verb):
        """
        STARTTLS and QUIT are pure alphanumerics and matched the blob pattern, so an earlier
        version blanked them -- hiding the protocol steps --verbose exists to show.
        """
        line = f"send: '{verb}\\r\\n'"

        assert verb in redactor(line)

    def test_starttls_stays_visible_in_a_real_transcript(self, redactor):
        transcript = [
            "reply: b'250-STARTTLS'",
            "reply: b'250 SMTPUTF8'",
            "send: 'STARTTLS\\r\\n'",
            "reply: b'220 2.0.0 SMTP server ready'",
        ]

        rendered = "\n".join(redactor(line) for line in transcript)

        assert "send: 'STARTTLS" in rendered

    def test_quit_after_a_failed_auth_stays_visible(self, redactor):
        """Exactly the sequence from the live Exchange run: AUTH fails, then QUIT."""
        transcript = [
            "send: 'AUTH LOGIN dXNlcg=='",
            "reply: b'334 UGFzc3dvcmQ6'",
            "send: 'c2VjcmV0'",
            "reply: b'535 5.7.3 Authentication unsuccessful'",
            "send: 'quit\\r\\n'",
        ]

        rendered = "\n".join(redactor(line) for line in transcript)

        assert "quit" in rendered
        assert "c2VjcmV0" not in rendered
        assert "535 5.7.3" in rendered


@pytest.mark.unit
class TestNoCredentialsConfigured:
    def test_anonymous_relay_transcript_is_untouched(self):
        settings = SmtpSettings(host="h", from_address="a@corp.local", auth="none")
        redactor = Redactor(settings)
        line = "send: 'MAIL FROM:<a@corp.local>'"

        assert redactor(line) == line
