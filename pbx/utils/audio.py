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


def float_samples_to_pcm16(samples: list[float], amplitude: float = DEFAULT_AMPLITUDE) -> bytes:
    """
    Encode normalized float samples to raw 16-bit PCM.

    The inverse of :func:`g711_to_float_samples`, and the bridge between the generators that
    work in floats (DTMFGenerator) and the RTP path, which wants PCM bytes to hand to
    :func:`pcm16_to_ulaw`.

    Samples are clamped rather than allowed to wrap: a DTMF digit is the sum of two sine
    waves and can exceed 1.0 on its peaks, and an integer that wraps turns a clean tone into
    a burst of noise the far end will not recognise as a digit.

    Args:
        samples: Audio samples, nominally in [-1.0, 1.0].
        amplitude: Fraction of full scale to use. Matches generate_beep_tone's default, which
            leaves headroom rather than driving the line flat out.

    Returns:
        bytes: Raw PCM audio data (16-bit signed, little-endian).
    """
    peak = MAX_16BIT_SIGNED * amplitude
    return b"".join(
        struct.pack("<h", int(max(-MAX_16BIT_SIGNED, min(MAX_16BIT_SIGNED, sample * peak))))
        for sample in samples
    )


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


def read_wav_format(path: "str | Path") -> tuple[int, int, int] | None:
    """
    Read a WAV file's ``fmt`` chunk without decoding the audio.

    Returns:
        ``(audio_format, sample_rate, channels)``, or None when the file is not a readable
        RIFF/WAVE. ``audio_format`` is the raw code -- compare against ``WAV_FORMAT_*``.
    """
    try:
        with Path(path).open("rb") as f:
            if f.read(4) != b"RIFF":
                return None
            f.read(4)
            if f.read(4) != b"WAVE":
                return None

            while True:
                chunk_id = f.read(4)
                if not chunk_id or len(chunk_id) < 4:
                    return None
                size_bytes = f.read(4)
                if len(size_bytes) < 4:
                    return None
                chunk_size = struct.unpack("<I", size_bytes)[0]

                if chunk_id == b"fmt ":
                    fmt = f.read(chunk_size)
                    if len(fmt) < 16:
                        return None
                    return (
                        struct.unpack("<H", fmt[0:2])[0],
                        struct.unpack("<I", fmt[4:8])[0],
                        struct.unpack("<H", fmt[2:4])[0],
                    )
                # Chunks are word-aligned; an odd size carries a pad byte.
                f.seek(chunk_size + (chunk_size & 1), 1)
    except (OSError, struct.error):
        return None


def wav_as_pcm16_wav(path: "str | Path") -> tuple[bytes | None, int | None]:
    """
    Read a WAV file as one a browser will actually play.

    Telephony audio is routinely stored as G.711, which every mainstream browser refuses --
    an ``<audio>`` element given µ-law reports "no supported source was found" rather than any
    decode error, so the failure looks like a broken URL. Decoding to linear PCM here is what
    makes voicemail and call audio playable from the admin UI at all.

    Returns:
        ``(data, audio_format)``. ``data`` is None when the file needs no conversion (already
        linear PCM -- serve the file directly so range requests keep working) **or** when the
        format cannot be decoded; the two cases are told apart by ``audio_format``, which is
        None only when the file could not be parsed. Callers should refuse to serve a format
        that is neither PCM nor G.711 rather than send bytes that will not play.
    """
    header = read_wav_format(path)
    if header is None:
        return None, None

    audio_format, sample_rate, channels = header

    if audio_format == WAV_FORMAT_PCM:
        # Already playable *if* the header is truthful. `wave.open(..., "wb")` patches the RIFF
        # and data sizes in on close(), so a recording whose writer was killed -- or one still
        # being written -- carries size 0 while otherwise looking like a valid WAV. A browser
        # decodes that to zero samples and reports "no supported sources", identical to a codec
        # failure. Rebuilding the header from the bytes actually present makes it playable.
        payload, declared_ok = _read_wav_data_chunk(path)
        if payload is None:
            return None, audio_format
        if declared_ok:
            return None, audio_format

        repaired = build_wav_header(
            len(payload), sample_rate=sample_rate, channels=channels, bits_per_sample=16
        )
        return repaired + payload, audio_format

    if audio_format not in (WAV_FORMAT_ULAW, WAV_FORMAT_ALAW):
        return None, audio_format

    # Read the data chunk directly rather than through the stdlib: wave.open refuses G.711
    # outright with "unknown format: 7", which is the whole reason this function exists.
    payload, _ = _read_wav_data_chunk(path)
    if payload is None:
        return None, audio_format

    decode = ulaw_to_pcm16 if audio_format == WAV_FORMAT_ULAW else alaw_to_pcm16
    try:
        samples = decode(payload).astype("<i2").tobytes()
    except (ValueError, TypeError):
        return None, audio_format

    header_bytes = build_wav_header(
        len(samples), sample_rate=sample_rate, channels=channels, bits_per_sample=16
    )
    return header_bytes + samples, audio_format


def _read_wav_data_chunk(path: str | Path) -> tuple[bytes | None, bool]:
    """
    The raw contents of a WAV's ``data`` chunk, whatever the encoding.

    The declared chunk size is treated as a hint, not a fact. A file whose writer never closed
    reports 0, and a truncated one reports more than it holds; in both cases the audio that is
    actually present is still perfectly decodable, so the bytes from here to end-of-file are
    returned instead.

    Returns:
        ``(payload, declared_size_was_correct)``. The payload is None when the file is not a
        readable RIFF/WAVE.
    """
    payload: bytes | None = None
    declared_ok = True

    try:
        with Path(path).open("rb") as f:
            if f.read(4) != b"RIFF":
                return None, False
            f.read(4)
            if f.read(4) != b"WAVE":
                return None, False

            while True:
                chunk_id = f.read(4)
                if not chunk_id or len(chunk_id) < 4:
                    break
                size_bytes = f.read(4)
                if len(size_bytes) < 4:
                    break
                chunk_size = struct.unpack("<I", size_bytes)[0]

                if chunk_id == b"data":
                    rest = f.read()
                    if chunk_size == 0 or chunk_size > len(rest):
                        # Unfinalised or truncated: keep everything that is really there.
                        payload, declared_ok = rest, False
                    else:
                        payload, declared_ok = rest[:chunk_size], True
                    break

                f.seek(chunk_size + (chunk_size & 1), 1)
    except (OSError, struct.error):
        return None, False

    return payload, declared_ok


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


#: RMS below which a 20 ms frame is treated as silence rather than speech, for int16 samples.
#:
#: G.711 idle and comfort noise sit well under 100; telephone speech runs in the low thousands.
#: 200 is comfortably between the two, and deliberately near the noise end -- the cost of
#: calling silence speech is one wasted transcription, while the cost of calling speech silence
#: is a transcript that is missing what somebody said.
SILENCE_RMS_FLOOR = 200.0

#: Frame length used when measuring activity. Matches one RTP packet of G.711.
_ACTIVITY_FRAME_SAMPLES = 160


def active_speech_seconds(
    pcm16: bytes, sample_rate: int = 8000, floor: float = SILENCE_RMS_FLOOR
) -> float:
    """
    How many seconds of this audio are above the noise floor.

    Used to decide whether audio is worth handing to a speech model at all. It answers only
    "is there anything here", not "where is the speech" -- the recogniser does the second part
    far better, and faster-whisper's ``vad_filter`` already skips non-speech internally.

    This matters most when a call is transcribed per participant: each channel is silent for
    the whole time the other party is speaking, and a channel where somebody never spoke at
    all -- a leg on hold, a participant who only listened -- would otherwise cost a full model
    run to produce nothing, or worse, produce a hallucination.

    Args:
        pcm16: Mono PCM16 little-endian samples.
        sample_rate: Samples per second.
        floor: RMS below which a frame counts as silence.

    Returns:
        Seconds of audio above the floor. 0.0 for empty input.
    """
    if not pcm16 or sample_rate <= 0:
        return 0.0

    samples = np.frombuffer(pcm16, dtype="<i2")
    if samples.size == 0:
        return 0.0

    # The remainder that does not fill a frame is at most 20 ms and cannot change the
    # decision, except when the whole clip is shorter than one frame.
    rms = _frame_rms(samples, _ACTIVITY_FRAME_SAMPLES)
    if rms.size == 0:
        whole = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        return samples.size / sample_rate if whole > floor else 0.0

    return float(np.count_nonzero(rms > floor) * _ACTIVITY_FRAME_SAMPLES / sample_rate)


#: Silence that must elapse before audio either side of it is treated as separate speech.
#:
#: Deliberately generous. Splitting audio before it reaches a speech model costs quality: the
#: model punctuates and capitalises from context, so a cut mid-sentence produces a lowercase
#: fragment with no closing punctuation and worse accuracy on the words either side. The only
#: safe place to cut is a gap no sentence would contain.
#:
#: Pauses inside ordinary speech run to about 0.3 s, and clause boundaries to about 1 s.
#: A conversational turn -- the case this exists for, where the gap is the other participant's
#: entire turn -- is several seconds. 1.5 s sits clearly above the first and below the second.
#: Raising it is the safe direction; lowering it risks cutting sentences in half.
DEFAULT_SPLIT_GAP_SECONDS = 1.5

#: Audio kept either side of a region, so a cut never lands on a leading consonant.
#: Must stay below half of the gap threshold or padded regions merge back together.
DEFAULT_REGION_PAD_SECONDS = 0.2

#: Regions shorter than this are dropped. A fragment this brief carries no context for the
#: model, which is the condition under which it invents words.
DEFAULT_MIN_REGION_SECONDS = 0.3


def _frame_rms(samples: "np.ndarray", frame: int) -> "np.ndarray":
    """RMS per fixed-length frame. Trailing samples that do not fill a frame are ignored."""
    frames = samples.size // frame
    if frames == 0:
        return np.zeros(0)
    block = samples[: frames * frame].astype(np.float64).reshape(frames, frame)
    return np.sqrt(np.mean(block**2, axis=1))


def frame_rms(pcm16: bytes) -> "np.ndarray":
    """
    RMS per 20 ms frame, for callers measuring audio they are reading in pieces.

    One float per 20 ms is about 400 KB per hour per channel, so a caller can summarise a
    whole call this way and never hold the audio itself. That is the difference between
    bounded memory and holding every channel of every simultaneously-ending call.
    """
    return _frame_rms(np.frombuffer(pcm16, dtype="<i2"), _ACTIVITY_FRAME_SAMPLES)


def frame_seconds(sample_rate: int = 8000) -> float:
    """How much time one :func:`frame_rms` frame covers."""
    return _ACTIVITY_FRAME_SAMPLES / sample_rate


def regions_from_rms(
    rms: "np.ndarray",
    *,
    seconds_per_frame: float,
    duration: float,
    floor: float = SILENCE_RMS_FLOOR,
    min_gap_seconds: float = DEFAULT_SPLIT_GAP_SECONDS,
    pad_seconds: float = DEFAULT_REGION_PAD_SECONDS,
    min_region_seconds: float = DEFAULT_MIN_REGION_SECONDS,
) -> list[tuple[float, float]]:
    """
    Speech regions from precomputed frame energies. See :func:`speech_regions` for the why.

    Split out so a caller streaming a long file can summarise it frame by frame and decide
    where to cut without ever holding the audio.
    """
    if rms.size == 0:
        return []

    active = rms > floor
    if not active.any():
        return []

    # Runs of consecutive active frames. diff on the padded boolean array marks every
    # transition, so starts and ends come out in pairs.
    edges = np.diff(np.concatenate(([0], active.view(np.int8), [0])))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)

    # Join runs separated by less than the gap threshold: those are pauses within speech,
    # not turn boundaries, and cutting there is what damages the transcript.
    merged: list[list[float]] = []
    for start, end in zip(starts * seconds_per_frame, ends * seconds_per_frame, strict=True):
        if merged and start - merged[-1][1] < min_gap_seconds:
            merged[-1][1] = end
        else:
            merged.append([start, end])

    regions: list[tuple[float, float]] = []
    for start, end in merged:
        if end - start < min_region_seconds:
            continue
        padded_start = float(max(0.0, start - pad_seconds))
        padded_end = float(min(duration, end + pad_seconds))
        # Padding cannot reintroduce an overlap while pad_seconds stays below half the gap
        # threshold, but clamp anyway rather than trust the caller's arithmetic.
        if regions and padded_start <= regions[-1][1]:
            regions[-1] = (regions[-1][0], padded_end)
        else:
            regions.append((padded_start, padded_end))

    return regions


def speech_regions(
    pcm16: bytes,
    sample_rate: int = 8000,
    *,
    floor: float = SILENCE_RMS_FLOOR,
    min_gap_seconds: float = DEFAULT_SPLIT_GAP_SECONDS,
    pad_seconds: float = DEFAULT_REGION_PAD_SECONDS,
    min_region_seconds: float = DEFAULT_MIN_REGION_SECONDS,
) -> list[tuple[float, float]]:
    """
    Find stretches of speech separated by gaps long enough to be real turn boundaries.

    Exists because a speech model will not split a segment across silence it never receives.
    faster-whisper's VAD removes non-speech and transcribes what remains as one stream, so two
    utterances either side of a long pause arrive adjacent and come back as a single segment
    spanning both. On a per-participant recording that pause is the other person's entire turn,
    which makes the merged output actively wrong rather than merely ugly.

    Splitting the audio *before* the model sees it is the only way to force the boundary. The
    cost is that every cut removes context the model uses for punctuation and capitalisation,
    so this cuts as rarely as possible: only at gaps of `min_gap_seconds`, which no sentence
    contains, and never inside continuous speech however long it runs.

    Args:
        pcm16: Mono PCM16 little-endian samples.
        sample_rate: Samples per second.
        floor: RMS below which a frame counts as silence.
        min_gap_seconds: Silence shorter than this never splits a region.
        pad_seconds: Audio kept either side of each region.
        min_region_seconds: Regions shorter than this are discarded.

    Returns:
        (start, end) pairs in seconds, in order, non-overlapping. Empty when there is no
        speech. A single region covering everything means there was nothing safe to split on.
    """
    if not pcm16 or sample_rate <= 0:
        return []

    samples = np.frombuffer(pcm16, dtype="<i2")
    return regions_from_rms(
        _frame_rms(samples, _ACTIVITY_FRAME_SAMPLES),
        seconds_per_frame=frame_seconds(sample_rate),
        duration=samples.size / sample_rate,
        floor=floor,
        min_gap_seconds=min_gap_seconds,
        pad_seconds=pad_seconds,
        min_region_seconds=min_region_seconds,
    )


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
