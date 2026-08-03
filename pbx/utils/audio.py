"""
Audio utilities for PBX system
Provides audio generation and processing functions
"""

import math
import struct
import warnings
from pathlib import Path

import numpy as np

# Audio generation constants
MAX_16BIT_SIGNED = 32767  # Maximum value for 16-bit signed integer
DEFAULT_AMPLITUDE = 0.5  # Default amplitude (50% of maximum)

# Audio format codes for WAV files
WAV_FORMAT_PCM = 1  # Pulse Code Modulation (linear PCM)
WAV_FORMAT_ULAW = 7  # μ-law (G.711)
WAV_FORMAT_ALAW = 6  # A-law (G.711)
WAV_FORMAT_G722 = 0x0067  # G.722 (HD Audio)

# μ-law encoding constants
_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635


def generate_tts_audio(text: str, sample_rate: int = 8000) -> bytes | None:
    """Generate telephony-format WAV audio bytes from text using TTS.

    Wraps :func:`pbx.utils.tts.text_to_wav_telephony`, returning the rendered
    WAV audio as raw bytes (suitable for RTP playback), or ``None`` if TTS is
    unavailable or generation fails.

    Args:
        text: Text to synthesize into speech.
        sample_rate: Output sample rate in Hz (default 8000 for G.711 telephony).

    Returns:
        The WAV audio as bytes, or None if TTS is unavailable or generation fails.
    """
    import tempfile

    from pbx.utils.tts import is_tts_available, text_to_wav_telephony

    if not is_tts_available():
        return None

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        if text_to_wav_telephony(text, tmp_path, sample_rate=sample_rate):
            return Path(tmp_path).read_bytes()
        return None
    except Exception:
        return None
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


def pcm16_to_ulaw(pcm_data: bytes) -> bytes:
    """
    Convert 16-bit PCM audio data to G.711 μ-law format.

    Implements the ITU-T G.711 μ-law companding algorithm directly in pure
    Python. Each 16-bit linear PCM sample is compressed to an 8-bit μ-law
    code word consisting of a sign bit, a 3-bit exponent, and a 4-bit
    mantissa.

    Args:
        pcm_data: Raw 16-bit PCM audio data (little-endian signed).

    Returns:
        G.711 μ-law encoded audio data (8-bit per sample).
    """
    ulaw_data = bytearray()

    for i in range(0, len(pcm_data), 2):
        # Read 16-bit little-endian sample
        if i + 1 >= len(pcm_data):
            break
        sample = struct.unpack("<h", pcm_data[i : i + 2])[0]

        # Get sign and magnitude
        sign = 0x80 if sample < 0 else 0x00
        sample = abs(sample)

        # Clip the sample
        sample = min(sample, _ULAW_CLIP)

        # Add bias
        sample = sample + _ULAW_BIAS

        # Find exponent (segment/chord number). For the biased 14-bit
        # magnitude, the exponent is the position of the highest set bit
        # among bits 8..14 minus 7 (exponent 7 -> bit 14, exponent 1 -> bit 8;
        # exponent 0 when no bit above bit 7 is set). Start from the highest
        # exponent and work down.
        exponent = 0
        for exp in range(7, -1, -1):
            if sample & (1 << (exp + 7)):
                exponent = exp
                break

        mantissa = (sample >> (exponent + 3)) & 0x0F

        # Compose μ-law byte (ones-complement)
        ulaw_byte = ~(sign | (exponent << 4) | mantissa) & 0xFF
        ulaw_data.append(ulaw_byte)

    return bytes(ulaw_data)


def _ulaw_byte_to_linear(ulaw_byte: int) -> int:
    """Decode a single G.711 μ-law byte to a 16-bit linear PCM sample."""
    ulaw_byte = ~ulaw_byte & 0xFF
    sign = ulaw_byte & 0x80
    exponent = (ulaw_byte >> 4) & 0x07
    mantissa = ulaw_byte & 0x0F
    sample = (((mantissa << 3) + _ULAW_BIAS) << exponent) - _ULAW_BIAS
    return -sample if sign else sample


def _alaw_byte_to_linear(alaw_byte: int) -> int:
    """Decode a single G.711 A-law byte to a 16-bit linear PCM sample."""
    alaw_byte ^= 0x55
    sign = alaw_byte & 0x80
    exponent = (alaw_byte >> 4) & 0x07
    mantissa = alaw_byte & 0x0F
    if exponent == 0:
        sample = (mantissa << 4) + 8
    else:
        sample = ((mantissa << 4) + 0x108) << (exponent - 1)
    return -sample if sign else sample


def g711_to_float_samples(payload: bytes, payload_type: int = 0) -> list[float]:
    """
    Decode G.711 audio bytes to normalized float samples in [-1.0, 1.0].

    Used for in-band DTMF tone detection on recorded RTP audio, which
    requires linear samples -- treating companded G.711 bytes as linear
    PCM makes the tone frequencies unrecognizable.

    Args:
        payload: Raw G.711 audio bytes (one byte per sample).
        payload_type: RTP payload type (0 = PCMU/μ-law, 8 = PCMA/A-law).
            Other payload types are decoded as μ-law.

    Returns:
        List of float samples normalized to [-1.0, 1.0].
    """
    decode = _alaw_byte_to_linear if payload_type == 8 else _ulaw_byte_to_linear
    return [decode(b) / 32768.0 for b in payload]


# ---------------------------------------------------------------------------
# Vectorised G.711 (for the audio mixer's 20 ms budget)
#
# The scalar helpers above walk one sample at a time, which is fine for a
# 0.8 s DTMF window but far too slow to decode and re-encode N legs every
# 20 ms. These lookup-table versions do the same arithmetic as a single
# numpy fancy-index. Decode tables are generated *from* the scalar decoders,
# so the two paths cannot drift apart.
# ---------------------------------------------------------------------------

_ULAW_DECODE_TABLE = np.array([_ulaw_byte_to_linear(b) for b in range(256)], dtype=np.int16)
_ALAW_DECODE_TABLE = np.array([_alaw_byte_to_linear(b) for b in range(256)], dtype=np.int16)


def _build_ulaw_encode_table() -> "np.ndarray":
    """μ-law code for every int16, mirroring pcm16_to_ulaw exactly."""
    x = np.arange(-32768, 32768, dtype=np.int32)
    sign = np.where(x < 0, 0x80, 0x00).astype(np.int32)
    magnitude = np.minimum(np.abs(x), _ULAW_CLIP).astype(np.int32) + _ULAW_BIAS

    # Highest set bit among bits 8..14, taking the first (highest) match --
    # the vectorised form of the scalar loop's `break`.
    exponent = np.zeros_like(magnitude)
    assigned = np.zeros(magnitude.shape, dtype=bool)
    for exp in range(7, -1, -1):
        hit = ~assigned & ((magnitude & (1 << (exp + 7))) != 0)
        exponent[hit] = exp
        assigned |= hit

    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8)


def _build_alaw_encode_table() -> "np.ndarray":
    """
    A-law code for every int16, by nearest-level inversion of the decoder.

    There is no scalar A-law encoder to mirror, so the table is derived from
    _alaw_byte_to_linear: each input maps to whichever code decodes closest
    to it. That makes encode/decode a true round trip by construction.
    """
    order = np.argsort(_ALAW_DECODE_TABLE)
    levels = _ALAW_DECODE_TABLE[order].astype(np.int32)

    x = np.arange(-32768, 32768, dtype=np.int32)
    upper = np.clip(np.searchsorted(levels, x), 1, len(levels) - 1)
    below, above = levels[upper - 1], levels[upper]
    nearest = np.where(np.abs(x - below) <= np.abs(above - x), upper - 1, upper)
    return order[nearest].astype(np.uint8)


_ULAW_ENCODE_TABLE = _build_ulaw_encode_table()
_ALAW_ENCODE_TABLE = _build_alaw_encode_table()


def ulaw_to_pcm16(payload: bytes) -> "np.ndarray":
    """Decode G.711 μ-law bytes to an int16 sample array."""
    return _ULAW_DECODE_TABLE[np.frombuffer(payload, dtype=np.uint8)]


def alaw_to_pcm16(payload: bytes) -> "np.ndarray":
    """Decode G.711 A-law bytes to an int16 sample array."""
    return _ALAW_DECODE_TABLE[np.frombuffer(payload, dtype=np.uint8)]


def _encode_index(samples: "np.ndarray") -> "np.ndarray":
    """Shift int16 samples into the 0..65535 range the encode tables use."""
    return np.asarray(samples, dtype=np.int16).astype(np.int32) + 32768


def samples_to_ulaw(samples: "np.ndarray") -> bytes:
    """Encode an int16 sample array to G.711 μ-law bytes."""
    return _ULAW_ENCODE_TABLE[_encode_index(samples)].tobytes()


def samples_to_alaw(samples: "np.ndarray") -> bytes:
    """Encode an int16 sample array to G.711 A-law bytes."""
    return _ALAW_ENCODE_TABLE[_encode_index(samples)].tobytes()


def pcm16_to_alaw(pcm_data: bytes) -> bytes:
    """
    Convert 16-bit PCM audio data to G.711 A-law format.

    The A-law counterpart of :func:`pcm16_to_ulaw`, which had no equivalent
    until the mixer needed to send to A-law endpoints.

    Args:
        pcm_data: Raw 16-bit PCM audio data (little-endian signed).

    Returns:
        G.711 A-law encoded audio data (8-bit per sample).
    """
    usable = len(pcm_data) - (len(pcm_data) % 2)
    return samples_to_alaw(np.frombuffer(pcm_data[:usable], dtype="<i2"))


def pcm16_to_g722(pcm_data: bytes, sample_rate: int = 8000) -> bytes:
    """
    Convert 16-bit PCM audio data to G.722 format

    G.722 is a wideband codec that operates at 16kHz. If the input is at 8kHz,
    it will be upsampled to 16kHz before encoding.

    Args:
        pcm_data: Raw 16-bit PCM audio data (little-endian signed)
        sample_rate: Sample rate of input PCM data (8000 or 16000 Hz)

    Returns:
        bytes: G.722 encoded audio data

    Note:
        This uses the G722Codec class which provides a complete ITU-T G.722
        sub-band ADPCM (SB-ADPCM) implementation with proper quantization
        tables, adaptive prediction, and QMF filtering (see g722_codec_itu.py).
    """
    # Import G.722 codec
    from pbx.features.g722_codec import G722Codec

    # Validate input data
    if len(pcm_data) < 2:
        # Not enough data for even one 16-bit sample
        return b""

    # Ensure data length is even (complete 16-bit samples)
    if len(pcm_data) % 2 != 0:
        # Truncate incomplete last sample
        pcm_data = pcm_data[:-1]

    # Upsample from 8kHz to 16kHz if needed
    if sample_rate == 8000:
        # Simple linear interpolation upsampling (2x)
        # For each sample, insert an interpolated sample between current and
        # next
        upsampled = bytearray()
        num_samples = len(pcm_data) // 2

        if num_samples == 0:
            return b""

        for i in range(num_samples - 1):
            # Read current and next sample
            current = struct.unpack("<h", pcm_data[i * 2 : (i + 1) * 2])[0]
            next_sample = struct.unpack("<h", pcm_data[(i + 1) * 2 : (i + 2) * 2])[0]

            # Add current sample
            upsampled.extend(struct.pack("<h", current))

            # Add interpolated sample (average of current and next)
            interpolated = (current + next_sample) // 2
            upsampled.extend(struct.pack("<h", interpolated))

        # Add the last sample twice (duplicate to maintain 2:1 ratio)
        last_sample = struct.unpack("<h", pcm_data[(num_samples - 1) * 2 : num_samples * 2])[0]
        upsampled.extend(struct.pack("<h", last_sample))
        upsampled.extend(struct.pack("<h", last_sample))

        pcm_data = bytes(upsampled)

    # Create G.722 encoder
    codec = G722Codec(bitrate=64000)

    # Encode PCM to G.722
    g722_data = codec.encode(pcm_data)

    return g722_data if g722_data is not None else b""


def convert_pcm_wav_to_g722_wav(
    input_wav_path: str | Path, output_wav_path: str | Path | None = None
) -> bool:
    """
    Convert a PCM WAV file to G.722 WAV format

    Reads a PCM WAV file, converts the audio data to G.722 encoding,
    and writes it back as a G.722 WAV file.

    Args:
        input_wav_path: Path to input PCM WAV file
        output_wav_path: Path to output G.722 WAV file (default: overwrite input)

    Returns:
        bool: True if successful, False otherwise
    """
    if output_wav_path is None:
        output_wav_path = input_wav_path

    try:
        with Path(input_wav_path).open("rb") as f:
            # Read RIFF header
            riff = f.read(4)
            if riff != b"RIFF":
                return False

            _ = struct.unpack("<I", f.read(4))[0]  # file_size
            wave = f.read(4)
            if wave != b"WAVE":
                return False

            # Find fmt chunk
            sample_rate = None
            audio_data = None

            while True:
                chunk_id = f.read(4)
                if not chunk_id or len(chunk_id) < 4:
                    break

                chunk_size = struct.unpack("<I", f.read(4))[0]

                if chunk_id == b"fmt ":
                    # Parse format chunk
                    fmt_data = f.read(chunk_size)
                    audio_format = struct.unpack("<H", fmt_data[0:2])[0]
                    _ = struct.unpack("<H", fmt_data[2:4])[0]  # channels
                    sample_rate = struct.unpack("<I", fmt_data[4:8])[0]
                    _ = struct.unpack("<H", fmt_data[14:16])[0]  # bits_per_sample

                    # Only process PCM files
                    if audio_format != WAV_FORMAT_PCM:
                        return False

                elif chunk_id == b"data":
                    # Read PCM audio data
                    audio_data = f.read(chunk_size)
                else:
                    # Skip unknown chunks
                    f.read(chunk_size)

            if audio_data is None or sample_rate is None:
                return False

            # Convert PCM to G.722
            g722_data = pcm16_to_g722(audio_data, sample_rate)

            if not g722_data:
                return False

            # Write G.722 WAV file
            with Path(output_wav_path).open("wb") as out_f:
                # G.722 WAV uses format code 0x0067 and 8kHz clock rate
                # Note: G.722 actually samples at 16kHz but uses 8kHz clock
                # rate per RFC
                header = build_wav_header(
                    len(g722_data),
                    sample_rate=8000,  # G.722 clock rate (8kHz)
                    channels=1,
                    bits_per_sample=8,
                    audio_format=WAV_FORMAT_G722,
                )
                out_f.write(header)
                out_f.write(g722_data)

            return True

    except (KeyError, OSError, TypeError, ValueError, struct.error) as e:
        warnings.warn(f"Failed to convert WAV to G.722: {e}", stacklevel=2)
        return False


def generate_beep_tone(
    frequency: int = 1000, duration_ms: int = 500, sample_rate: int = 8000
) -> bytes:
    """
    Generate a simple beep tone in raw PCM format

    Args:
        frequency: Frequency in Hz (default 1000 Hz)
        duration_ms: Duration in milliseconds (default 500ms)
        sample_rate: Sample rate in Hz (default 8000 Hz for telephony)

    Returns:
        bytes: Raw PCM audio data (16-bit signed, little-endian)
    """
    num_samples = int(sample_rate * duration_ms / 1000)
    samples = []

    for i in range(num_samples):
        # Generate sine wave
        t = i / sample_rate
        value = int(MAX_16BIT_SIGNED * DEFAULT_AMPLITUDE * math.sin(2 * math.pi * frequency * t))
        samples.append(struct.pack("<h", value))  # 16-bit signed little-endian

    return b"".join(samples)


def resample_pcm16(pcm16: bytes, from_rate: int, to_rate: int) -> bytes:
    """
    Resample mono 16-bit little-endian PCM.

    Needed because speech models are almost universally trained at 16 kHz while telephony is
    8 kHz, and Kaldi refuses the mismatch outright rather than resampling silently::

        Sampling frequency mismatch, expected 16000, got 8000

    Upsampling 8 kHz cannot invent detail above 4 kHz, so accuracy is still below what the
    same model achieves on true wideband audio -- but it is the difference between a usable
    transcript and none at all.

    Args:
        pcm16: Mono PCM16 little-endian samples.
        from_rate: Sample rate of `pcm16`.
        to_rate: Desired sample rate.

    Returns:
        Resampled PCM16 little-endian bytes, or the input unchanged when the rates match.
    """
    if from_rate == to_rate or not pcm16:
        return pcm16

    samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32)

    try:
        from math import gcd

        from scipy.signal import resample_poly

        divisor = gcd(from_rate, to_rate)
        resampled = resample_poly(samples, to_rate // divisor, from_rate // divisor)
    except ImportError:
        # scipy is a declared dependency, so this is insurance rather than a supported path.
        # Linear interpolation passes more imaging noise than a polyphase filter, which costs
        # some recognition accuracy but still beats refusing to transcribe.
        target_count = round(len(samples) * to_rate / from_rate)
        resampled = np.interp(
            np.linspace(0, len(samples) - 1, target_count),
            np.arange(len(samples)),
            samples,
        )

    return np.clip(np.round(resampled), -32768, 32767).astype("<i2").tobytes()


def read_wav_as_pcm16(path: str | Path) -> tuple[bytes, int]:
    """
    Read a WAV file and return its audio as mono 16-bit little-endian PCM.

    This exists because :mod:`wave` supports only linear PCM. The PBX stores voicemail as
    G.711 -- see ``PBXCore._build_wav_file`` -- so ``wave.open`` on a real recording raises
    ``wave.Error: unknown format: 7`` for u-law, or ``unknown format: 6`` for A-law. Both are
    decoded here instead, which lets callers that need linear samples (speech recognition
    above all) take a recording exactly as the PBX wrote it, with no transcoding step.

    The RIFF walk is deliberately tolerant: chunks other than ``fmt `` and ``data`` are
    skipped, and the odd-length pad byte is honoured, because recorders vary in what
    metadata they emit.

    Args:
        path: Path to the WAV file.

    Returns:
        Tuple of (PCM16 little-endian bytes, sample rate in Hz).

    Raises:
        ValueError: If the file is not a WAV, is not mono, or is in a format with no defined
            conversion to linear PCM.
    """
    audio_format = channels = sample_rate = bits_per_sample = None
    data: bytes | None = None

    with Path(path).open("rb") as f:
        if f.read(4) != b"RIFF":
            raise ValueError("Not a RIFF file")
        f.read(4)  # Declared file size; not trusted, the chunk walk is authoritative.
        if f.read(4) != b"WAVE":
            raise ValueError("Not a WAVE file")

        while True:
            header = f.read(8)
            if len(header) < 8:
                break
            chunk_id, chunk_size = struct.unpack("<4sI", header)
            payload = f.read(chunk_size)
            if chunk_size % 2:
                f.read(1)  # RIFF chunks are word-aligned.

            if chunk_id == b"fmt " and len(payload) >= 16:
                audio_format, channels, sample_rate = struct.unpack("<HHI", payload[:8])
                bits_per_sample = struct.unpack("<H", payload[14:16])[0]
            elif chunk_id == b"data":
                data = payload

    if audio_format is None or data is None:
        raise ValueError("WAV file is missing a fmt or data chunk")
    if channels != 1:
        raise ValueError(f"Audio must be mono, got {channels} channels")

    if audio_format == WAV_FORMAT_PCM and bits_per_sample == 16:
        return data, sample_rate
    if audio_format == WAV_FORMAT_ULAW:
        return ulaw_to_pcm16(data).astype("<i2").tobytes(), sample_rate
    if audio_format == WAV_FORMAT_ALAW:
        return alaw_to_pcm16(data).astype("<i2").tobytes(), sample_rate
    if audio_format == WAV_FORMAT_PCM and bits_per_sample == 8:
        # 8-bit PCM in a WAV file is unsigned and centred on 128, unlike every other width.
        samples = (np.frombuffer(data, dtype=np.uint8).astype(np.int16) - 128) << 8
        return samples.astype("<i2").tobytes(), sample_rate

    raise ValueError(
        f"Cannot convert WAV format {audio_format} at {bits_per_sample}-bit to linear PCM"
    )


def build_wav_header(
    data_size: int,
    sample_rate: int = 8000,
    channels: int = 1,
    bits_per_sample: int = 16,
    audio_format: int = WAV_FORMAT_PCM,
) -> bytes:
    """
    Build a WAV file header

    Args:
        data_size: Size of audio data in bytes
        sample_rate: Sample rate in Hz
        channels: Number of audio channels
        bits_per_sample: Bits per sample (8 or 16)
        audio_format: Audio format code (WAV_FORMAT_PCM, WAV_FORMAT_ULAW, etc.)

    Returns:
        bytes: WAV header
    """
    # Calculate byte rate and block align based on format
    if audio_format == WAV_FORMAT_G722:  # G.722
        # G.722 uses 8-bit samples at 8kHz clock rate (actual 16kHz sampling)
        # Bitrate is 64 kbit/s = 8000 bytes/s
        byte_rate = 8000 * channels
        block_align = 1 * channels
        bits_per_sample = 8  # G.722 uses 8-bit encoded samples
    else:
        byte_rate = sample_rate * channels * bits_per_sample // 8
        block_align = channels * bits_per_sample // 8

    header = b"RIFF"
    header += struct.pack("<I", 36 + data_size)  # File size - 8
    header += b"WAVE"
    header += b"fmt "
    header += struct.pack("<I", 16)  # Subchunk1Size (16 for basic formats)
    header += struct.pack("<H", audio_format)  # AudioFormat
    header += struct.pack("<H", channels)
    header += struct.pack("<I", sample_rate)
    header += struct.pack("<I", byte_rate)
    header += struct.pack("<H", block_align)
    header += struct.pack("<H", bits_per_sample)
    header += b"data"
    header += struct.pack("<I", data_size)

    return header


def generate_voicemail_beep() -> bytes:
    """
    Generate a voicemail beep tone (single beep, 1000 Hz, 500ms)

    Returns:
        bytes: Complete WAV file with beep tone
    """
    pcm_data = generate_beep_tone(frequency=1000, duration_ms=500, sample_rate=8000)
    header = build_wav_header(len(pcm_data), sample_rate=8000)
    return header + pcm_data


def generate_ring_tone(rings: int = 1) -> bytes:
    """
    Generate a ring tone (2 seconds ring, 4 seconds silence, repeated)

    Args:
        rings: Number of rings to generate

    Returns:
        bytes: Complete WAV file with ring tone
    """
    sample_rate = 8000
    ring_duration_ms = 2000  # 2 seconds ring
    silence_duration_ms = 4000  # 4 seconds silence

    # Generate ring sound (dual tone: 440 Hz + 480 Hz)
    ring_samples = int(sample_rate * ring_duration_ms / 1000)
    silence_samples = int(sample_rate * silence_duration_ms / 1000)

    pcm_data = b""

    for _ in range(rings):
        # Ring sound
        for i in range(ring_samples):
            t = i / sample_rate
            # Mix two frequencies
            value = int(
                32767 * 0.3 * (math.sin(2 * math.pi * 440 * t) + math.sin(2 * math.pi * 480 * t))
            )
            pcm_data += struct.pack("<h", value)

        # Silence
        pcm_data += b"\x00\x00" * silence_samples

    header = build_wav_header(len(pcm_data), sample_rate=sample_rate)
    return header + pcm_data


def generate_busy_tone() -> bytes:
    """
    Generate a busy tone (500 Hz, pulsed)

    Returns:
        bytes: Complete WAV file with busy tone
    """
    sample_rate = 8000
    tone_duration_ms = 500  # 500ms on
    silence_duration_ms = 500  # 500ms off
    repeats = 3

    pcm_data = b""

    for _ in range(repeats):
        # Tone
        tone_samples = int(sample_rate * tone_duration_ms / 1000)
        for i in range(tone_samples):
            t = i / sample_rate
            value = int(32767 * 0.5 * math.sin(2 * math.pi * 500 * t))
            pcm_data += struct.pack("<h", value)

        # Silence
        silence_samples = int(sample_rate * silence_duration_ms / 1000)
        pcm_data += b"\x00\x00" * silence_samples

    header = build_wav_header(len(pcm_data), sample_rate=sample_rate)
    return header + pcm_data


def generate_voice_prompt(prompt_type: str, sample_rate: int = 8000) -> bytes:
    """
    Generate a voice-like prompt using tone sequences

    Since we don't have TTS, we use distinctive tone patterns to represent different prompts.
    These tones are designed to be recognizable and indicate specific messages.

    Args:
        prompt_type: type of prompt to generate:
            - 'leave_message': "Please leave a message after the tone"
            - 'enter_pin': "Please enter your PIN"
            - 'main_menu': "Main menu. Press 1 to listen to messages..."
            - 'message_menu': "Press 1 to replay, 2 for next, 3 to delete..."
            - 'no_messages': "You have no messages"
            - 'goodbye': "Goodbye"
        sample_rate: Sample rate in Hz

    Returns:
        bytes: Complete WAV file with prompt tones

    Note: In production, these would be replaced with actual recorded voice prompts
    """
    # Define tone sequences for different prompts
    # Format: [(frequency_hz, duration_ms), ...]
    tone_sequences = {
        "leave_message": [
            # Ascending tones to indicate "please leave a message"
            (600, 200),
            (700, 200),
            (800, 200),
            (900, 300),
        ],
        "enter_pin": [
            # Short repeating tone for PIN entry
            (1000, 150),
            (0, 100),
            (1000, 150),
            (0, 100),
            (1000, 150),
        ],
        "main_menu": [
            # Two-tone pattern for menu
            (800, 200),
            (600, 200),
            (800, 200),
        ],
        "message_menu": [
            # Quick triple beep for options
            (900, 100),
            (0, 50),
            (900, 100),
            (0, 50),
            (900, 100),
        ],
        "no_messages": [
            # Descending tones for "no messages"
            (800, 200),
            (600, 200),
            (400, 300),
        ],
        "no_more_messages": [
            # End-of-list notification, distinct from no_messages
            (700, 200),
            (500, 200),
            (500, 300),
        ],
        "goodbye": [
            # Descending dual tone for goodbye
            (700, 250),
            (500, 300),
        ],
        "invalid_option": [
            # Low buzz for invalid option
            (300, 400),
        ],
        "error": [
            # Generic error/failure buzz (e.g. greeting save failed)
            (300, 300),
            (0, 100),
            (300, 300),
            (0, 100),
            (300, 300),
        ],
        "you_have_messages": [
            # Cheerful ascending tones
            (600, 150),
            (750, 150),
            (900, 200),
        ],
        "auto_attendant_welcome": [
            # Welcoming ascending tones
            (500, 250),
            (650, 250),
            (800, 250),
            (950, 350),
        ],
        "auto_attendant_menu": [
            # Menu option tones - distinctive pattern
            (700, 200),
            (0, 100),
            (800, 200),
            (0, 100),
            (700, 200),
            (0, 100),
            (900, 300),
        ],
        "timeout": [
            # Warning tone for timeout
            (400, 300),
            (0, 150),
            (400, 300),
        ],
        "transferring": [
            # Success tone for transfer
            (800, 150),
            (1000, 150),
            (1200, 200),
        ],
        "invalid_pin": [
            # Error tone for invalid PIN
            (300, 400),
            (0, 100),
            (300, 400),
        ],
        "record_greeting": [
            # Recording prompt tones
            (700, 200),
            (900, 200),
            (1100, 300),
        ],
        "greeting_saved": [
            # Success confirmation
            (1000, 200),
            (1200, 200),
            (1000, 200),
        ],
        "greeting_review_menu": [
            # Menu options tone pattern (listen/re-record/delete/save)
            (800, 150),
            (0, 80),
            (900, 150),
            (0, 80),
            (700, 150),
            (0, 80),
            (1000, 250),
        ],
        "greeting_deleted": [
            # Deletion confirmation - descending, distinct from message_deleted
            (850, 150),
            (650, 150),
            (450, 250),
        ],
        "greeting_playback": [
            # Playback starting indicator
            (1000, 100),
            (0, 50),
            (1000, 100),
        ],
        "message_deleted": [
            # Deletion confirmation - descending
            (900, 150),
            (700, 150),
            (500, 200),
        ],
        "end_of_messages": [
            # End notification - neutral tone
            (600, 300),
            (500, 300),
        ],
        "beep": [
            # Standard recording beep - single high tone
            (1000, 400),
        ],
    }

    sequence = tone_sequences.get(prompt_type, [(800, 300)])  # Default tone

    pcm_data = b""
    for frequency, duration_ms in sequence:
        if frequency == 0:
            # Silence
            num_samples = int(sample_rate * duration_ms / 1000)
            pcm_data += b"\x00\x00" * num_samples
        else:
            # Generate tone
            num_samples = int(sample_rate * duration_ms / 1000)
            for i in range(num_samples):
                t = i / sample_rate
                value = int(
                    MAX_16BIT_SIGNED * DEFAULT_AMPLITUDE * math.sin(2 * math.pi * frequency * t)
                )
                pcm_data += struct.pack("<h", value)

    header = build_wav_header(len(pcm_data), sample_rate=sample_rate)
    return header + pcm_data


def load_prompt_file(prompt_type: str, prompt_dir: str = "voicemail_prompts") -> bytes | None:
    """
    Load a voice prompt WAV file from disk

    Args:
        prompt_type: type of prompt (e.g., 'enter_pin', 'main_menu', 'goodbye')
        prompt_dir: Directory containing prompt files (default: 'voicemail_prompts')

    Returns:
        bytes: WAV file contents if file exists, None otherwise

    Note: This function attempts to load actual recorded voice prompts.
          If the file doesn't exist, the caller should fall back to generate_voice_prompt()
    """
    # Build path to prompt file. If prompt_dir is relative, resolve it
    # relative to the project root (two levels up from this file) so that
    # prompts are found regardless of the process working directory
    # (e.g., when launched via systemd or Docker).
    prompt_path = Path(prompt_dir)
    if not prompt_path.is_absolute():
        project_root = Path(__file__).resolve().parent.parent.parent
        prompt_path = project_root / prompt_path
    prompt_file = prompt_path / f"{prompt_type}.wav"

    # Check if file exists
    if prompt_file.exists():
        try:
            with prompt_file.open("rb") as f:
                return f.read()
        except OSError as e:
            # If we can't read the file (permission issues, disk errors, etc.),
            # return None so caller can use fallback tone generation
            # In production, consider logging this error for debugging
            import warnings

            warnings.warn(f"Failed to read prompt file {prompt_file}: {e}", stacklevel=2)
            return None

    return None


def get_prompt_audio(
    prompt_type: str, prompt_dir: str = "voicemail_prompts", sample_rate: int = 8000
) -> bytes:
    """
    Get voice prompt audio, trying to load from file first, then generating tones as fallback

    This is a convenience function that combines load_prompt_file() and generate_voice_prompt().
    It first attempts to load a recorded WAV file, and if that fails, generates tone-based prompts.

    Args:
        prompt_type: type of prompt (e.g., 'enter_pin', 'main_menu', 'goodbye')
        prompt_dir: Directory containing prompt files (default: 'voicemail_prompts')
        sample_rate: Sample rate for generated prompts if file not found

    Returns:
        bytes: Complete WAV file data (either from file or generated)
    """
    # Try to load from file first
    audio_data = load_prompt_file(prompt_type, prompt_dir)

    if audio_data is not None:
        return audio_data

    # Fallback to generated tone prompts
    return generate_voice_prompt(prompt_type, sample_rate)
