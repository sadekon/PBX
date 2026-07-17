#!/usr/bin/env python3
"""
Generate Voice Prompts using gTTS (Google Text-to-Speech)

This script generates actual VOICE prompts using Google Text-to-Speech (gTTS).
The generated files are in proper telephony format: 8000 Hz, 16-bit, mono WAV.

Requirements:
    pip install gTTS pydub

Note: Requires internet connection to use Google TTS API (free, no API key needed)
      Voice quality is natural and professional-sounding.
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Try to import dependencies with helpful error messages
try:
    from pbx.utils.config import Config
    from pbx.utils.logger import PBXLogger, get_logger
    from pbx.utils.tts import get_tts_requirements, is_tts_available, text_to_wav_telephony
except ImportError as e:
    print(f"Error: Could not import PBX utilities: {e}")
    print("Make sure you're running this script from the PBX directory")
    sys.exit(1)

# Check if TTS dependencies are available
if not is_tts_available():
    print("=" * 70)
    print("ERROR: TTS dependencies not installed!")
    print("=" * 70)
    print()
    print("Please install required packages:")
    print(f"  {get_tts_requirements()}")
    print()
    print("After installation, run this script again.")
    print("=" * 70)
    sys.exit(1)


# Default auto attendant prompt texts. Overridden per-key by the
# ``auto_attendant.prompts`` section of config.yml when present. ``{company_name}``
# is substituted at generation time with the resolved company name.
DEFAULT_AA_PROMPTS: dict[str, str] = {
    "welcome": "Thank you for calling {company_name}.",
    "main_menu": (
        "For Sales, press 1. For Support, press 2. For Accounting, press 3. "
        "Or press 0 to speak with an operator."
    ),
    "invalid": "That is not a valid option. Please try again.",
    "timeout": "We did not receive your selection. Please try again.",
    "transferring": "Please hold while we transfer your call.",
}


def load_config() -> Config | None:
    """Load config.yml, returning None if it cannot be loaded.

    Returns:
        A loaded Config instance, or None if config.yml is missing/unreadable
        (in which case built-in defaults are used).
    """
    try:
        return Config()
    except (OSError, ValueError) as e:
        get_logger().warning(f"Could not load config.yml ({e}); using built-in defaults")
        return None


def resolve_aa_prompts(config: Config | None, company_name: str) -> dict[str, str]:
    """Resolve auto attendant prompt texts from config, merged over defaults.

    Reads the ``auto_attendant.prompts`` mapping from config (each key is a
    prompt name such as ``welcome``/``main_menu``), falling back to
    :data:`DEFAULT_AA_PROMPTS` for any key not overridden, then substitutes
    ``{company_name}`` in every prompt.

    Args:
        config: Loaded Config instance, or None to use defaults only.
        company_name: Company name to substitute for ``{company_name}``.

    Returns:
        Mapping of prompt name -> fully-resolved text.
    """
    prompts = dict(DEFAULT_AA_PROMPTS)
    if config is not None:
        aa_config = config.get("auto_attendant", {})
        configured = aa_config.get("prompts") or {}
        prompts.update({str(k): str(v) for k, v in configured.items()})
    return {name: text.replace("{company_name}", company_name) for name, text in prompts.items()}


def generate_auto_attendant_voices(
    prompts: dict[str, str],
    output_dir: str = "auto_attendant",
    sample_rate: int = 8000,
) -> int:
    """
    Generate voice prompts for auto attendant

    Args:
        prompts: Mapping of prompt name -> resolved text (see resolve_aa_prompts)
        output_dir: Directory to save audio files
        sample_rate: Sample rate in Hz (8000 narrowband / 16000 wideband)

    Returns:
        int: Number of files successfully generated
    """
    logger = get_logger()

    # Create output directory
    if not Path(output_dir).exists():
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        logger.info(f"Created directory: {output_dir}")

    logger.info("=" * 70)
    logger.info("Auto Attendant Voice Generator (gTTS)")
    logger.info("=" * 70)
    logger.info("")

    # Generate each prompt
    success_count = 0
    for name, text in prompts.items():
        output_file = Path(output_dir) / f"{name}.wav"

        logger.info(f"Generating {name}.wav...")
        logger.info(f'  Text: "{text}"')

        try:
            if text_to_wav_telephony(text, output_file, sample_rate=sample_rate):
                file_size = Path(output_file).stat().st_size
                logger.info(f"  ✓ SUCCESS - Generated ({file_size:,} bytes)")
                success_count += 1
            else:
                logger.error("  ✗ FAILED to generate")
        except OSError as e:
            logger.error(f"  ✗ ERROR: {e}")

        logger.info("")

    logger.info(f"Generated {success_count}/{len(prompts)} auto attendant prompts")
    logger.info("")

    return success_count


def generate_voicemail_voices(
    output_dir: str = "voicemail_prompts", sample_rate: int = 8000
) -> tuple[int, int]:
    """
    Generate voice prompts for voicemail system

    Voicemail prompt text is intentionally hardcoded here (not config-driven);
    only the auto attendant prompts are configurable via config.yml.

    Args:
        output_dir: Directory to save audio files
        sample_rate: Sample rate in Hz (8000 narrowband / 16000 wideband)

    Returns:
        tuple: (success_count, total_count)
    """
    logger = get_logger()

    # Create output directory
    if not Path(output_dir).exists():
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        logger.info(f"Created directory: {output_dir}")

    # Define prompts with actual text
    prompts = {
        "enter_pin.wav": {
            "text": "Please enter your PIN followed by the pound key.",
            "description": "PIN entry prompt",
        },
        "invalid_pin.wav": {
            "text": "Invalid PIN. Please try again.",
            "description": "Invalid PIN message",
        },
        "main_menu.wav": {
            "text": "To listen to your messages, press 1. For options, press 2. To exit, press star.",
            "description": "Voicemail main menu",
        },
        "options_menu.wav": {
            "text": "Press 1 to record greeting. Press star to return to main menu.",
            "description": "Options menu",
        },
        "message_menu.wav": {
            "text": "To replay this message, press 1. For the next message, press 2. To delete this message, press 3. To return to the main menu, press star.",
            "description": "Message playback menu",
        },
        "no_messages.wav": {
            "text": "You have no new messages.",
            "description": "No messages notification",
        },
        "no_more_messages.wav": {
            "text": "No more messages. Returning to the main menu.",
            "description": "End of message list notification",
        },
        "you_have_messages.wav": {
            "text": "You have new messages.",
            "description": "Message count announcement",
        },
        "goodbye.wav": {"text": "Goodbye.", "description": "Goodbye message"},
        "leave_message.wav": {
            "text": "Please leave a message after the tone. When you are finished, hang up or press pound.",
            "description": "Leave message prompt",
        },
        "record_greeting.wav": {
            "text": "Record your greeting after the tone. When finished, press pound.",
            "description": "Record greeting prompt",
        },
        "greeting_review_menu.wav": {
            "text": "Greeting recorded. Press 1 to listen, 2 to re-record, "
            "3 to delete and use the default, or star to save and return to the main menu.",
            "description": "Greeting review menu prompt",
        },
        "greeting_playback.wav": {
            "text": "Playing your greeting.",
            "description": "Greeting playback lead-in",
        },
        "greeting_saved.wav": {
            "text": "Your greeting has been saved.",
            "description": "Greeting saved confirmation",
        },
        "greeting_deleted.wav": {
            "text": "Custom greeting deleted. Using the default greeting.",
            "description": "Greeting deleted confirmation",
        },
        "error.wav": {
            "text": "There was an error saving your greeting. Please try again.",
            "description": "Greeting save error",
        },
        "message_deleted.wav": {
            "text": "Message deleted.",
            "description": "Message deleted confirmation",
        },
        "end_of_messages.wav": {
            "text": "End of messages.",
            "description": "End of messages notification",
        },
    }

    logger.info("=" * 70)
    logger.info("Voicemail Voice Generator (gTTS)")
    logger.info("=" * 70)
    logger.info("")

    # Generate each prompt
    success_count = 0
    for filename, info in prompts.items():
        output_file = Path(output_dir) / filename
        text = info["text"]

        logger.info(f"Generating {filename}...")
        logger.info(f'  Text: "{text}"')

        try:
            if text_to_wav_telephony(text, output_file, sample_rate=sample_rate):
                file_size = Path(output_file).stat().st_size
                logger.info(f"  ✓ SUCCESS - Generated ({file_size:,} bytes)")
                success_count += 1
            else:
                logger.error("  ✗ FAILED to generate")
        except OSError as e:
            logger.error(f"  ✗ ERROR: {e}")

        logger.info("")

    logger.info(f"Generated {success_count}/{len(prompts)} voicemail prompts")
    logger.info("")

    return success_count, len(prompts)


def main() -> None:
    """Main function."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate voice prompts using gTTS (Google Text-to-Speech)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    Generate all voice prompts
  %(prog)s --aa-only                          Generate only auto attendant
  %(prog)s --vm-only                          Generate only voicemail
  %(prog)s --company "ABC Company"            Use custom company name

This script uses Google Text-to-Speech (gTTS) to generate actual voice prompts.
Requires internet connection but no API key needed - completely free.

Voice Quality:
  - Natural and professional-sounding
  - Sounds like a real person speaking

The generated files are in proper telephony format:
  - Format: WAV
  - Sample Rate: 8000 Hz
  - Bit Depth: 16-bit
  - Channels: Mono
        """,
    )
    parser.add_argument(
        "--aa-only", action="store_true", help="Generate only auto attendant prompts"
    )
    parser.add_argument("--vm-only", action="store_true", help="Generate only voicemail prompts")
    parser.add_argument(
        "--company",
        default=None,
        help="Company name for auto attendant greeting "
        "(default: 'company_name' from config.yml, else 'your company')",
    )
    parser.add_argument(
        "--aa-dir",
        default="auto_attendant",
        help="Output directory for auto attendant prompts (default: auto_attendant)",
    )
    parser.add_argument(
        "--vm-dir",
        default="voicemail_prompts",
        help="Output directory for voicemail prompts (default: voicemail_prompts)",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=8000,
        choices=[8000, 16000],
        help="Sample rate in Hz: 8000 narrowband (PCMU/G.711), 16000 wideband (default: 8000)",
    )

    args = parser.parse_args()

    # Setup logging
    PBXLogger().setup(log_level="INFO", console=True)
    logger = get_logger()

    # Load config for auto attendant prompt text + company name. The CLI
    # --company flag, when given, overrides the config's company_name.
    config = load_config()
    company_name = args.company or (
        config.get("company_name", "your company") if config else "your company"
    )
    aa_prompts = resolve_aa_prompts(config, company_name)

    logger.info("")
    logger.info("=" * 70)
    logger.info("PBX Voice Prompt Generator (gTTS)")
    logger.info("=" * 70)
    logger.info("")
    logger.info("Using Google Text-to-Speech (gTTS)")
    logger.info("Generating REAL VOICE prompts (not tones!)")
    logger.info(f"Company name: {company_name}")
    logger.info("Requires internet connection but no API key needed")
    logger.info("")

    total_success = 0
    total_files = 0

    # Generate auto attendant prompts
    if not args.vm_only:
        aa_count = generate_auto_attendant_voices(aa_prompts, args.aa_dir, args.sample_rate)
        total_success += aa_count
        total_files += len(aa_prompts)

    # Generate voicemail prompts
    if not args.aa_only:
        vm_count, vm_total = generate_voicemail_voices(args.vm_dir, args.sample_rate)
        total_success += vm_count
        total_files += vm_total

    logger.info("=" * 70)
    logger.info(f"TOTAL: Generated {total_success}/{total_files} VOICE prompts")
    logger.info("=" * 70)
    logger.info("")

    if total_success == total_files:
        logger.info("✓ SUCCESS! Real voice prompts with actual speech generated!")
        logger.info("")
        logger.info("Files are in proper telephony format:")
        logger.info("  - WAV format, 8000 Hz, 16-bit, mono")
        logger.info("  - Ready to use with your PBX system")
        logger.info("")
        logger.info("Voice Quality:")
        logger.info("  - Natural and professional-sounding")
        logger.info("  - Suitable for production use")
        logger.info("")
        logger.info("gTTS is the recommended option for voice generation!")
        logger.info("")
    else:
        logger.error(f"✗ WARNING: Only {total_success}/{total_files} prompts generated")
        logger.error("Check that gTTS and pydub are installed:")
        logger.error("  pip install gTTS pydub")
        logger.error("Also ensure you have internet connectivity for gTTS")


if __name__ == "__main__":
    main()
