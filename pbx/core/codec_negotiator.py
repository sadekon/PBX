"""
Codec/device negotiation for PBX Core.

Detects a phone's model from its SIP User-Agent, maps that model to the
codec set it should be offered, and computes the codec intersection between
a phone's supported set and whatever the other call leg actually answered
with (the PBX relays RTP without transcoding, so both legs must agree on a
single codec). Also resolves the configured DTMF payload type and iLBC mode,
which every codec offer needs regardless of phone model.

Called from every call-setup path that builds an SDP offer/answer:
CallRouter, TransferHandler, VoicemailHandler, AutoAttendantHandler,
PagingHandler, SIPServer, and the WebRTC gateway.
"""

from typing import Any


class CodecNegotiator:
    """Detects phone models and negotiates compatible codec sets."""

    def __init__(self, pbx_core: Any) -> None:
        """
        Initialize CodecNegotiator with a reference to PBXCore.

        Args:
            pbx_core: The PBXCore instance.
        """
        self.pbx_core: Any = pbx_core
        # Cache for device detection results keyed by User-Agent string
        self._device_model_cache: dict[str, str | None] = {}

    def _detect_phone_model(self, user_agent: str | None) -> str | None:
        """
        Detect phone model from User-Agent string

        Args:
            user_agent: User-Agent header string

        Returns:
            Phone model identifier string or None.
            Possible values: 'YEALINK_T23G', 'YEALINK_T33G', 'YEALINK_T46S',
            'YEALINK_T46G', 'YEALINK_T28G', 'ZIP33G', 'ZIP37G', 'CISCO_CP8851',
            or None for unknown/other
        """
        pbx = self.pbx_core

        if not user_agent:
            return None

        if user_agent in self._device_model_cache:
            return self._device_model_cache[user_agent]

        try:
            user_agent_upper = user_agent.upper()
        except (AttributeError, TypeError):
            pbx.logger.debug(f"Invalid User-Agent value for phone detection: {user_agent!r}")
            return None

        model: str | None = None

        if "ZIP33G" in user_agent_upper or "ZIP 33G" in user_agent_upper:
            model = "ZIP33G"
        elif "ZIP37G" in user_agent_upper or "ZIP 37G" in user_agent_upper:
            model = "ZIP37G"
        elif "T33G" in user_agent_upper:
            model = "YEALINK_T33G"
        elif "T46S" in user_agent_upper:
            model = "YEALINK_T46S"
        elif "T46G" in user_agent_upper:
            model = "YEALINK_T46G"
        elif "T23G" in user_agent_upper:
            model = "YEALINK_T23G"
        elif "T28G" in user_agent_upper:
            model = "YEALINK_T28G"
        elif "GRANDSTREAM" in user_agent_upper:
            model = "GRANDSTREAM_HT" if "HT" in user_agent_upper else "GRANDSTREAM"
        elif (
            "CP-8851" in user_agent_upper
            or "CP8851" in user_agent_upper
            or "8851" in user_agent_upper
        ):
            # Cisco IP Phone CP-8851-3PCC (multiplatform desk phone).
            # Checked before the generic Cisco/SPA branch since its User-Agent
            # (e.g. "Cisco-CP-8851-3PCC/11.3.7") also contains "CISCO".
            model = "CISCO_CP8851"
        elif "SPA" in user_agent_upper or "CISCO" in user_agent_upper:
            model = "CISCO_ATA"
        elif "OBI" in user_agent_upper:
            model = "OBI_ATA"
        else:
            pbx.logger.debug(
                f"Unrecognised phone User-Agent: {user_agent!r} — using default codecs"
            )

        self._device_model_cache[user_agent] = model
        return model

    def _should_skip_static_rtpmap(self, phone_model: str | None) -> bool:
        """
        Check whether to omit a=rtpmap lines for static payload types.

        Zultys ZIP 33G/37G phones use non-standard numeric codec names in SDP
        (e.g. ``0/8000`` instead of ``PCMU/8000``).  Their RTP engine (ipph)
        receives the codec name string from the SIP stack (sua) and tries to
        match it against an internal codec table that uses numeric IDs.  This
        fails for both standard names ("PCMU" != "0") and mirrored numeric
        names ("0" != internal lookup).

        The fix is to omit ``a=rtpmap`` lines for static payload types (0-34).
        Per RFC 3551, these have well-defined codec assignments and rtpmap is
        optional.  Without rtpmap, the phone identifies codecs by payload type
        number alone, which its RTP engine handles correctly.

        Args:
            phone_model: Phone model identifier (from _detect_phone_model)

        Returns:
            True if rtpmap lines should be omitted for static payload types.
        """
        return phone_model in ("ZIP33G", "ZIP37G")

    def _get_codecs_for_phone_model(
        self, phone_model: str | None, default_codecs: list[str] | None = None
    ) -> list[str]:
        """
        Get appropriate codec list for a specific phone model

        Args:
            phone_model: Phone model identifier (from _detect_phone_model)
            default_codecs: Default codecs to use if no specific requirement

        Returns:
            list of codec payload types as strings
        """
        pbx = self.pbx_core

        # Get DTMF payload type from config (default 101)
        dtmf_payload_type = pbx.config.get("features.dtmf.payload_type", 101)
        dtmf_pt_str = str(dtmf_payload_type)

        if phone_model == "YEALINK_T23G":
            # Yealink T23G: per provisioning template — PCMU, PCMA, G722, G729, G726-32, iLBC
            # Payload types: 0=PCMU, 8=PCMA, 9=G722, 18=G729, 2=G726-32
            codecs = ["0", "8", "9", "18", "2", dtmf_pt_str]
            pbx.logger.debug(f"Using Yealink T23G codec set: PCMU/PCMA/G722/G729/G726 ({codecs})")
            return codecs

        if phone_model == "YEALINK_T33G":
            # Yealink T33G: per provisioning template — PCMU, PCMA, G722, G729, iLBC
            # Payload types: 0=PCMU, 8=PCMA, 9=G722, 18=G729
            codecs = ["0", "8", "9", "18", dtmf_pt_str]
            pbx.logger.debug(f"Using Yealink T33G codec set: PCMU/PCMA/G722/G729 ({codecs})")
            return codecs

        if phone_model in ("YEALINK_T46S", "YEALINK_T46G"):
            # Yealink T46S/T46G: per provisioning template — PCMU, PCMA, G722,
            # G729, G726-32, iLBC, Speex
            # Payload types: 0=PCMU, 8=PCMA, 9=G722, 18=G729, 2=G726-32
            codecs = ["0", "8", "9", "18", "2", dtmf_pt_str]
            pbx.logger.debug(f"Using {phone_model} codec set: PCMU/PCMA/G722/G729/G726 ({codecs})")
            return codecs

        if phone_model == "YEALINK_T28G":
            # Yealink T28G: per provisioning template — PCMU, PCMA, G722, G729,
            # G726-32, iLBC, Speex
            # Payload types: 0=PCMU, 8=PCMA, 9=G722, 18=G729, 2=G726-32
            codecs = ["0", "8", "9", "18", "2", dtmf_pt_str]
            pbx.logger.debug(f"Using Yealink T28G codec set: PCMU/PCMA/G722/G729/G726 ({codecs})")
            return codecs

        if phone_model == "ZIP37G":
            # ZIP37G (Zultys rebrand of Yealink T46G): supports full codec set
            # per provisioning template — PCMU, PCMA, G722, G729, G726-32, iLBC, Speex
            # Payload types: 0=PCMU, 8=PCMA, 9=G722, 18=G729, 2=G726-32
            codecs = ["0", "8", "9", "18", "2", dtmf_pt_str]
            pbx.logger.debug(f"Using ZIP37G codec set: PCMU/PCMA/G722/G729/G726 ({codecs})")
            return codecs

        if phone_model == "ZIP33G":
            # ZIP33G (Zultys rebrand of Yealink T28G): supports full codec set
            # per provisioning template — PCMU, PCMA, G722, G729, G726-32, iLBC, Speex
            # Payload types: 0=PCMU, 8=PCMA, 9=G722, 18=G729, 2=G726-32
            codecs = ["0", "8", "9", "18", "2", dtmf_pt_str]
            pbx.logger.debug(f"Using ZIP33G codec set: PCMU/PCMA/G722/G729/G726 ({codecs})")
            return codecs

        if phone_model == "GRANDSTREAM_HT":
            # Grandstream HT series ATAs: PCMU, PCMA, G722, G729, G726-32
            # HT801/802/812/814 support these standard codecs
            codecs = ["0", "8", "9", "18", "2", dtmf_pt_str]
            pbx.logger.debug(
                f"Using Grandstream HT ATA codec set: PCMU/PCMA/G722/G729/G726 ({codecs})"
            )
            return codecs

        if phone_model == "GRANDSTREAM":
            # Grandstream phones (GXP series, etc.): full codec support
            codecs = ["0", "8", "9", "18", "2", dtmf_pt_str]
            pbx.logger.debug(f"Using Grandstream codec set: PCMU/PCMA/G722/G729/G726 ({codecs})")
            return codecs

        if phone_model == "CISCO_CP8851":
            # Cisco CP-8851-3PCC multiplatform desk phone: PCMU, PCMA, G722
            # (wideband), G729a — matches the cisco_cp8851 provisioning template.
            # Payload types: 0=PCMU, 8=PCMA, 9=G722, 18=G729
            codecs = ["0", "8", "9", "18", dtmf_pt_str]
            pbx.logger.debug(f"Using Cisco CP-8851-3PCC codec set: PCMU/PCMA/G722/G729 ({codecs})")
            return codecs

        if phone_model in ("CISCO_ATA", "OBI_ATA"):
            # Cisco/Linksys SPA and OBi ATAs: PCMU, PCMA, G722, G729
            codecs = ["0", "8", "9", "18", dtmf_pt_str]
            pbx.logger.debug(f"Using {phone_model} codec set: PCMU/PCMA/G722/G729 ({codecs})")
            return codecs

        # For unknown or other phones, use default behavior
        if default_codecs:
            pbx.logger.debug(f"Using default codec set for unknown phone: {default_codecs}")
            return default_codecs

        # Ultimate fallback - standard codec list
        return ["0", "8", "9", "18", "2", dtmf_pt_str]

    def _get_compatible_codecs(
        self, phone_model: str | None, answered_codecs: list[str] | None
    ) -> list[str]:
        """
        Compute codecs compatible with both the phone model and the answered codec set.

        When the PBX relays RTP without transcoding, both call legs must use the
        same codec.  This method intersects the callee's answered codecs with the
        phone-model-specific set (if any) so the 200 OK sent to the caller only
        offers codecs that both sides support.

        Args:
            phone_model: Phone model identifier (from _detect_phone_model)
            answered_codecs: Codecs the remote side actually selected (from SDP answer)

        Returns:
            list of codec payload types as strings
        """
        pbx = self.pbx_core

        dtmf_payload_type = pbx.config.get("features.dtmf.payload_type", 101)
        dtmf_pt_str = str(dtmf_payload_type)

        model_codecs = self._get_codecs_for_phone_model(phone_model)

        if not answered_codecs:
            return model_codecs

        # Build intersection preserving the answered codec order (callee's preference)
        model_set = set(model_codecs)
        answered_set = set(answered_codecs)
        compatible = [c for c in answered_codecs if c in model_set]

        # Only include DTMF telephone-event if the remote side actually offered
        # it.  Adding telephone-event to an SDP answer when the offer didn't
        # include it violates RFC 3264 and confuses some phone firmware.
        if (
            dtmf_pt_str in model_set
            and dtmf_pt_str in answered_set
            and dtmf_pt_str not in compatible
        ):
            compatible.append(dtmf_pt_str)

        # De-duplicate while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for c in compatible:
            if c not in seen:
                seen.add(c)
                deduped.append(c)
        compatible = deduped

        # Ensure the intersection has at least one audio codec (not just DTMF)
        has_audio_codec = any(c != dtmf_pt_str for c in compatible)
        if compatible and has_audio_codec:
            pbx.logger.debug(f"Compatible codecs for {phone_model or 'unknown'}: {compatible}")
            return compatible

        # If intersection is empty, fall back to the answered codecs to avoid
        # a completely empty SDP.  The phone will 488 if truly incompatible.
        pbx.logger.warning(
            f"No codec overlap between model {phone_model} and answered {answered_codecs}, "
            f"falling back to answered codecs"
        )
        return answered_codecs

    def _get_phone_user_agent(self, extension_number: str) -> str | None:
        """
        Get User-Agent string for a registered phone by extension number

        Args:
            extension_number: Extension number string

        Returns:
            User-Agent string or None if not found
        """
        pbx = self.pbx_core

        if not pbx.registered_phones_db or not pbx.database.enabled:
            return None

        try:
            # Query registered_phones table for this extension
            query = """
            SELECT user_agent FROM registered_phones
            WHERE extension_number = %s
            ORDER BY last_registered DESC
            LIMIT 1
            """

            result = pbx.database.fetch_one(query, (extension_number,))
            if result and result.get("user_agent"):
                return str(result["user_agent"])
        except (KeyError, TypeError, ValueError) as e:
            pbx.logger.debug(f"Error retrieving User-Agent for extension {extension_number}: {e}")

        return None

    def _get_dtmf_payload_type(self) -> int:
        """
        Get DTMF payload type from configuration

        Returns:
            DTMF payload type as integer (default: 101)
        """
        return self.pbx_core.config.get("features.dtmf.payload_type", 101)  # type: ignore[no-any-return]

    def _get_ilbc_mode(self) -> int:
        """
        Get iLBC mode from configuration

        Returns:
            iLBC mode (20 or 30 ms) as integer (default: 30)
        """
        return self.pbx_core.config.get("codecs.ilbc.mode", 30)  # type: ignore[no-any-return]
