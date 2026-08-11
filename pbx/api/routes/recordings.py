"""Recording and transcript read API.

The admin view onto ``recordings`` and ``transcripts``. Read-only: recordings are produced by
the call path and expired by the retention sweeper, and nothing here should be a second way to
create or destroy them.

**Access.** Admins read anything. Everyone else reads only recordings they were a party to, and
only when ``recording.participant_access`` is on -- it defaults to off, so a fresh install is
admin-only. :func:`~pbx.api.utils.check_recording_access` owns that rule; no route
re-implements it.

**Every read is audited**, not just every write. Who listened to which call is the question
asked months later, and it can only be answered if the read was recorded when it happened.
Denials are logged too: a run of them against calls someone was not on is what an attempt
looks like.

**Text never appears in a list response.** Listing returns metadata only, and transcript text
comes from a single-item fetch that is audited per recording. Otherwise one paginated call
quietly drains every transcript in the system and lands in the audit log as a single event.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from flask import Blueprint, current_app, request, send_file

from pbx.api.utils import (
    check_recording_access,
    get_pbx_core,
    participant_access_enabled,
    require_auth,
    send_json,
    validate_limit_param,
)
from pbx.features.recording_store import KIND_CALL, KIND_VOICEMAIL
from pbx.utils.audio import WAV_FORMAT_PCM, read_wav_format, wav_as_pcm16_wav
from pbx.utils.audit_logger import get_audit_logger
from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from flask import Response

logger = get_logger()

recordings_bp = Blueprint("recordings", __name__, url_prefix="/api/recordings")

#: Fields a list response may carry. Named rather than passing the row through, so a column
#: added to ``recordings`` later -- a note field, an internal path -- is not published by
#: accident. ``path`` is deliberately absent: the caller has no use for a server-side filename
#: and it discloses the storage layout.
_LIST_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "session_id",
    "call_id",
    "kind",
    "duration_seconds",
    "participants",
    "started_at",
    "ended_at",
    "audio_deleted_at",
    "created_at",
)


def _stores() -> tuple[Any, Any]:
    """The recording and transcript stores, or (None, None) when unavailable."""
    pbx_core = get_pbx_core()
    if not pbx_core:
        return None, None
    recording_system = getattr(pbx_core, "recording_system", None)
    return (
        getattr(recording_system, "store", None),
        getattr(pbx_core, "transcript_store", None),
    )


def _caller() -> tuple[str, str | None]:
    """The authenticated extension and client IP, for the audit trail."""
    payload = getattr(request, "auth_payload", None) or {}
    return str(payload.get("extension", "unknown")), request.remote_addr


def _media_roots() -> list[Path]:
    """Directories a recording's media may legitimately live under.

    Two of them because ``recordings`` holds voicemail as well as call audio -- a voicemail is
    a recording with a mailbox attached, and the two are written to different trees.
    """
    pbx_core = get_pbx_core()
    if not pbx_core or not getattr(pbx_core, "config", None):
        return []

    roots = []
    for key, default in (
        ("recording.storage_path", "recordings"),
        ("voicemail.storage_path", "voicemail"),
    ):
        try:
            roots.append(Path(pbx_core.config.get(key, default)).resolve())
        except OSError:
            continue
    return roots


def _resolve_media(recording: dict[str, Any]) -> Path | None:
    """The recording's file, if it exists and sits under a configured root.

    The path comes from the row, never from the caller, so this is not defending against a
    traversal in the URL -- there is no path in the URL. It defends against a stored path that
    escapes the tree, which a misconfigured storage_path or a future writer could produce, and
    which would otherwise turn this endpoint into an arbitrary file read.
    """
    raw = recording.get("path")
    if not raw:
        return None

    try:
        candidate = Path(raw).resolve()
    except OSError:
        return None

    roots = _media_roots()
    if not roots or not any(candidate.is_relative_to(root) for root in roots):
        logger.warning(
            "Recording %s has a path outside every configured storage root; refusing to serve it",
            recording.get("id"),
        )
        return None

    return candidate if candidate.is_file() else None


def _visible(recording: dict[str, Any]) -> dict[str, Any]:
    """Strip a row to the fields a client may see."""
    return {field: recording.get(field) for field in _LIST_FIELDS}


def _no_store(response: Response) -> Response:
    """Mark a response uncacheable.

    Recording audio and transcript text must not settle into a browser cache or an
    intermediate proxy, where they outlive both the session and the retention policy.
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    return response


def _load(recording_id: str, kind: str) -> tuple[dict[str, Any] | None, Response | None]:
    """Fetch a recording and authorise the caller, auditing a denial.

    Returns:
        tuple of (recording, error_response). Exactly one is None.
    """
    store, _ = _stores()
    if store is None or not store.enabled:
        return None, send_json({"error": "Recording storage not available"}, 503)

    recording = store.get(recording_id)
    if not recording:
        return None, send_json({"error": "Recording not found"}, 404)

    allowed, reason = check_recording_access(recording)
    user, ip = _caller()
    if not allowed:
        get_audit_logger().log_recording_access(
            user=user,
            recording_id=recording_id,
            kind=kind,
            ip_address=ip,
            granted=False,
            reason=reason,
        )
        # Flat 403 whatever the reason: distinguishing "not yours" from "does not exist"
        # would let a caller enumerate which calls a colleague was on.
        return None, send_json({"error": "Not authorized to access this recording"}, 403)

    return recording, None


@recordings_bp.route("", methods=["GET"])
@require_auth
def list_recordings() -> Response:
    """List recordings the caller may see. Metadata only -- never transcript text."""
    store, _ = _stores()
    if store is None or not store.enabled:
        return send_json({"error": "Recording storage not available"}, 503)

    payload = getattr(request, "auth_payload", None) or {}
    is_admin = bool(payload.get("is_admin", False))

    if not is_admin and not participant_access_enabled():
        return send_json({"error": "Not authorized to access recordings"}, 403)

    # Calls unless voicemail is asked for. Voicemail rows live in the same table but have
    # their own page, so surfacing them here by default would show every mailbox message
    # twice over and bury the call recordings this page exists for.
    #
    # These are the only two kinds anything writes: CallRecordingSystem registers KIND_CALL
    # and the mailbox registers KIND_VOICEMAIL. There is deliberately no conference option --
    # RTPMixer is constructed on PBXCore but bridges nothing, so it has no writer.
    kinds = [KIND_CALL]
    if request.args.get("include_voicemail", "0") in {"1", "true"}:
        kinds.append(KIND_VOICEMAIL)

    participant = (request.args.get("participant") or "").strip() or None
    if participant and len(participant) > 50:
        return send_json({"error": "Participant filter is too long"}, 400)

    limit = validate_limit_param(default=50, max_value=500)
    if limit is None:
        return send_json({"error": "Invalid limit parameter"}, 400)

    try:
        rows = store.recent(limit=limit, kinds=kinds, participant=participant)
    except Exception as e:
        logger.error(f"Could not list recordings: {e}")
        return send_json({"error": "Could not list recordings"}, 500)

    if not is_admin:
        # Filtered here rather than in SQL because participants is a JSON column and the
        # store has no predicate for it. Bounded by `limit`, so this stays cheap -- but it
        # does mean a non-admin's page can come back shorter than the limit they asked for.
        rows = [row for row in rows if check_recording_access(row)[0]]

    return send_json({"recordings": [_visible(row) for row in rows], "count": len(rows)})


@recordings_bp.route("/<recording_id>", methods=["GET"])
@require_auth
def get_recording(recording_id: str) -> Response:
    """One recording, with metadata for each transcript of it. Still no transcript text."""
    recording, error = _load(recording_id, kind="metadata")
    if recording is None:
        return error or send_json({"error": "Recording not found"}, 404)

    _, transcripts = _stores()
    runs: list[dict[str, Any]] = []
    if transcripts is not None and transcripts.enabled:
        try:
            runs = [
                {
                    "id": t.get("id"),
                    "source": t.get("source"),
                    "provider": t.get("provider"),
                    "model": t.get("model"),
                    "language": t.get("language"),
                    "confidence": t.get("confidence"),
                    "created_at": t.get("created_at"),
                    # So the UI can show "transcript available" without fetching the text,
                    # which would be an unaudited read of the thing we are protecting.
                    "characters": len(t.get("text") or ""),
                }
                for t in transcripts.for_recording(recording_id)
            ]
        except Exception as e:
            logger.error(f"Could not read transcripts for recording {recording_id}: {e}")

    data = _visible(recording)
    data["audio_available"] = bool(recording.get("path")) and not recording.get("audio_deleted_at")
    data["transcripts"] = runs
    return _no_store(send_json(data))


@recordings_bp.route("/<recording_id>/audio", methods=["GET"])
@require_auth
def get_recording_audio(recording_id: str) -> Response:
    """Stream a recording's audio.

    Range requests are honoured so a player can seek without pulling the whole file, which for
    a long call is the difference between a usable player and a stall.
    """
    recording, error = _load(recording_id, kind="audio")
    if recording is None:
        return error or send_json({"error": "Recording not found"}, 404)

    # The row outlives its file by design: audio and text expire on separate clocks. Say so
    # rather than 404, which reads as "no such recording" when the recording plainly exists.
    if recording.get("audio_deleted_at"):
        return send_json(
            {
                "error": "Audio has been deleted under the retention policy",
                "transcript_may_remain": True,
            },
            410,
        )

    media = _resolve_media(recording)
    if media is None:
        return send_json({"error": "Audio file not found"}, 404)

    user, ip = _caller()
    get_audit_logger().log_recording_access(
        user=user, recording_id=recording_id, kind="audio", ip_address=ip, granted=True
    )

    download = request.args.get("download", "0") in {"1", "true"}
    filename = f"recording_{recording_id}.wav"

    # Telephony audio is often stored as G.711, which no mainstream browser will decode -- an
    # <audio> element reports "no supported source was found", which reads as a broken URL
    # rather than an unsupported codec. Convert to linear PCM on the way out.
    converted, audio_format = wav_as_pcm16_wav(media)

    # Logged on every serve, not only on failure. When a player refuses audio the browser
    # says only "no supported sources" whatever the reason, so the server log is the one
    # place the actual shape of the file is recoverable.
    header = read_wav_format(media)
    logger.info(
        "Serving recording %s: format=%s rate=%s channels=%s bytes=%s converted=%s",
        recording_id,
        header[0] if header else "unreadable",
        header[1] if header else "?",
        header[2] if header else "?",
        media.stat().st_size if media.exists() else "?",
        converted is not None,
    )

    if audio_format is None:
        logger.error("Recording %s is not a readable WAV: %s", recording_id, media.name)
        return send_json({"error": "Audio file is not a readable WAV"}, 422)

    if converted is not None:
        response = current_app.response_class(converted, mimetype="audio/wav")
        response.headers["Content-Length"] = str(len(converted))
        if download:
            response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return _no_store(response)

    if audio_format != WAV_FORMAT_PCM:
        # Serving it anyway would produce the same silent failure this function exists to
        # avoid. G.722 is the likely one here -- it has no decoder in this codebase yet.
        logger.error(
            "Recording %s is WAV format %s, which the browser cannot play and we cannot convert",
            recording_id,
            audio_format,
        )
        return send_json(
            {
                "error": "Audio is in a format this browser cannot play",
                "wav_format": audio_format,
            },
            415,
        )

    # Already linear PCM: serve the file itself so range requests keep working, which is what
    # lets a player seek in a long call without pulling the whole recording first.
    response = send_file(
        media,
        mimetype="audio/wav",
        conditional=True,
        as_attachment=download,
        download_name=filename,
    )
    return _no_store(response)


@recordings_bp.route("/<recording_id>/transcript", methods=["GET"])
@require_auth
def get_recording_transcript(recording_id: str) -> Response:
    """The text of a recording's transcripts. Audited per read."""
    recording, error = _load(recording_id, kind="transcript")
    if recording is None:
        return error or send_json({"error": "Recording not found"}, 404)

    _, transcripts = _stores()
    if transcripts is None or not transcripts.enabled:
        return send_json({"error": "Transcript storage not available"}, 503)

    try:
        runs = transcripts.for_recording(recording_id)
    except Exception as e:
        logger.error(f"Could not read transcripts for recording {recording_id}: {e}")
        return send_json({"error": "Could not read transcripts"}, 500)

    if not runs:
        return send_json({"error": "No transcript for this recording"}, 404)

    user, ip = _caller()
    get_audit_logger().log_recording_access(
        user=user, recording_id=recording_id, kind="transcript", ip_address=ip, granted=True
    )

    # `lines` replaces `segments` rather than joining it: they carry the same content, and
    # sending both would ship the per-word timing this strips out in the first place.
    shaped = [
        {
            **{k: v for k, v in run.items() if k != "segments"},
            "lines": _speaker_lines(run.get("segments")),
        }
        for run in runs
    ]
    return _no_store(send_json({"recording_id": recording.get("id"), "transcripts": shaped}))


def _speaker_lines(segments: Any) -> list[dict[str, Any]]:
    """Collapse raw segments into one line per turn.

    Whisper emits a segment per decoded window rather than per utterance, so a single spoken
    sentence routinely arrives as three fragments with the same speaker. Rendered one-per-line
    that reads as stuttering rather than conversation, so consecutive segments from the same
    speaker are joined and keep the first one's start time.

    Per-word timing is dropped: nothing in the UI seeks to a word, and `words` is by far the
    largest part of a segment -- carrying it would multiply the response size for no reader.

    Returns an empty list when timing was never recorded, which is the signal to fall back to
    the flat ``text`` field rather than render an empty transcript.
    """
    if not isinstance(segments, list):
        return []

    lines: list[dict[str, Any]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text") or "").strip()
        if not text:
            continue

        speaker = str(segment.get("speaker") or "")
        if lines and lines[-1]["speaker"] == speaker:
            lines[-1]["text"] = f"{lines[-1]['text']} {text}"
            lines[-1]["end"] = segment.get("end")
        else:
            lines.append(
                {
                    "speaker": speaker,
                    "text": text,
                    "start": segment.get("start"),
                    "end": segment.get("end"),
                }
            )

    return lines
