"""
Codec registry for the audio mixer.

The RTP relay never looks at a payload -- it forwards the received bytes
verbatim -- which is why two-party calls work with any negotiated codec. A
mixer cannot do that: to sum several legs together it has to decode each one
to linear PCM and re-encode the result per listener. Codec support therefore
stops being free, and this module is where it is paid for.

Everything the mixer handles is normalised to one domain: **mono int16 at
8 kHz, 160 samples (20 ms) per frame**. A codec is responsible for getting
its own wire format into and out of that domain.

Codecs are obtained per port via :func:`make_codec`, never shared, because
some (G.722) carry adaptive state that must follow a single stream. A
payload type with no registered codec returns None, and the caller is
expected to renegotiate that leg to G.711 rather than guess.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import numpy as np

from pbx.features.g722_codec import G722Codec as _ItuG722
from pbx.utils.audio import (
    alaw_to_pcm16,
    samples_to_alaw,
    samples_to_ulaw,
    ulaw_to_pcm16,
)

if TYPE_CHECKING:
    from collections.abc import Callable

#: The mixer's internal audio domain.
MIX_RATE = 8000
FRAME_MS = 20
FRAME_SAMPLES = MIX_RATE * FRAME_MS // 1000  # 160

#: Static RTP payload types this module knows about.
PT_PCMU = 0
PT_PCMA = 8
PT_G722 = 9

#: RFC 2833 telephone-event. Never mixed -- see MixBridge, which forwards
#: these verbatim so DTMF still works from inside a bridge.
PT_TELEPHONE_EVENT = 101


class Codec(Protocol):
    """Converts one wire format to and from the mixer's 8 kHz int16 domain."""

    payload_type: int

    def decode(self, payload: bytes) -> np.ndarray:
        """Wire bytes -> int16 samples at MIX_RATE."""
        ...

    def encode(self, frame: np.ndarray) -> bytes:
        """int16 samples at MIX_RATE -> wire bytes."""
        ...


class G711Codec:
    """
    G.711 mu-law / A-law.

    Stateless, and already the mixer's native rate, so both directions are a
    single lookup-table index (see pbx/utils/audio.py).
    """

    def __init__(
        self,
        payload_type: int,
        decoder: Callable[[bytes], np.ndarray],
        encoder: Callable[[np.ndarray], bytes],
    ) -> None:
        self.payload_type = payload_type
        self._decoder = decoder
        self._encoder = encoder

    def decode(self, payload: bytes) -> np.ndarray:
        return self._decoder(payload)

    def encode(self, frame: np.ndarray) -> bytes:
        return self._encoder(frame)


class G722Codec:
    """
    G.722 wideband, resampled into the mixer's narrowband domain.

    **Not registered** -- see the note on ``_FACTORIES``. The adapter is
    correct as far as it goes, but the ITU codec it wraps is broken, so
    registering this would produce distorted audio. It is kept ready for the
    day that codec is replaced.

    Two things to know when that happens. First, G.722 is adaptive sub-band
    ADPCM, so encoder and decoder state must persist across frames; this
    holds its own instances rather than calling
    ``pbx.utils.audio.pcm16_to_g722``, which constructs a fresh codec on
    every call and would reset the predictor fifty times a second.

    Second, audio is decimated 16 kHz -> 8 kHz on the way in and
    interpolated back on the way out, so a G.722 leg would keep its
    negotiated codec but be *mixed at narrowband*. Genuine wideband mixing
    means raising the whole mixer's internal rate, a separate change.
    """

    payload_type = PT_G722

    def __init__(self) -> None:
        self._encoder = _ItuG722(bitrate=64000)
        self._decoder = _ItuG722(bitrate=64000)

    def decode(self, payload: bytes) -> np.ndarray:
        wideband = self._decoder.decode(payload)
        if not wideband:
            return np.zeros(0, dtype=np.int16)
        samples = np.frombuffer(wideband, dtype="<i2")
        if samples.size < 2:
            return np.zeros(0, dtype=np.int16)
        # Average sample pairs rather than dropping every other one: crude,
        # but it keeps some of the energy that plain decimation would alias.
        usable = samples.size - (samples.size % 2)
        pairs = samples[:usable].astype(np.int32)
        return ((pairs[0::2] + pairs[1::2]) // 2).astype(np.int16)

    def encode(self, frame: np.ndarray) -> bytes:
        if frame.size == 0:
            return b""
        # Linear interpolation up to 16 kHz, duplicating the final sample --
        # matching what pcm16_to_g722 does for file conversion.
        widened = np.empty(frame.size * 2, dtype=np.int16)
        widened[0::2] = frame
        nxt = np.empty(frame.size, dtype=np.int32)
        nxt[:-1] = frame[1:]
        nxt[-1] = frame[-1]
        widened[1::2] = ((frame.astype(np.int32) + nxt) // 2).astype(np.int16)

        encoded = self._encoder.encode(widened.tobytes())
        return encoded or b""


#: Codecs a bridge can actually mix.
#:
#: G.722 is deliberately absent even though :class:`G722Codec` is implemented
#: here: the underlying ITU codec in pbx/features/ is non-functional (its
#: sub-band ADPCM predictor diverges -- decoded output is powers of two
#: doubling per sample -- and its QMF synthesis is a stub, so a 1 kHz tone
#: comes back at 250 Hz). Until that is replaced, G.722 legs are better served
#: by renegotiating to G.711 than by mixing distorted audio. Re-enable with
#: ``register_codec(PT_G722, G722Codec)`` once the codec is fixed; nothing
#: else has to change.
_FACTORIES: dict[int, Callable[[], Codec]] = {
    PT_PCMU: lambda: G711Codec(PT_PCMU, ulaw_to_pcm16, samples_to_ulaw),
    PT_PCMA: lambda: G711Codec(PT_PCMA, alaw_to_pcm16, samples_to_alaw),
}


def register_codec(payload_type: int, factory: Callable[[], Codec]) -> None:
    """
    Add or replace a codec.

    The factory is called once per port, so stateful codecs get their own
    instance per stream.
    """
    _FACTORIES[payload_type] = factory


def make_codec(payload_type: int) -> Codec | None:
    """
    A fresh codec for one stream, or None if the payload type is unsupported.

    None is not an error -- it is the signal to renegotiate that leg to
    G.711 before adding it to a bridge.
    """
    factory = _FACTORIES.get(payload_type)
    return factory() if factory is not None else None


def is_supported(payload_type: int) -> bool:
    """Whether a leg on this payload type can join a bridge as-is."""
    return payload_type in _FACTORIES


def supported_payload_types() -> frozenset[int]:
    """Every payload type the mixer can currently transcode."""
    return frozenset(_FACTORIES)
