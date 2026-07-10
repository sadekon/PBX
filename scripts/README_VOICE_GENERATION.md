# Voice Prompt Generation Scripts

This directory contains scripts to generate voice prompts for the PBX system using **gTTS (Google Text-to-Speech)**.

## Primary Script: `generate_espeak_voices.py` (Recommended)

Generates **PROFESSIONAL-QUALITY VOICE** prompts using gTTS (Google Text-to-Speech).

**Advantages:**
-Natural, human-like American English voice
-Professional quality suitable for production
-Free to use (no API key required)
-Simple setup
-Automatic generation from text prompts
-Optimized for US English accent

**Usage:**
```bash
# Install dependencies
uv pip install gTTS pydub

# Generate all prompts with default company name
python3 scripts/generate_espeak_voices.py

# Custom company name
python3 scripts/generate_espeak_voices.py --company "Your Company Name"

# Only auto attendant
python3 scripts/generate_espeak_voices.py --aa-only

# Only voicemail
python3 scripts/generate_espeak_voices.py --vm-only
```

**Requirements:**
```bash
uv pip install gTTS pydub
```

**Voice Quality:** Natural and professional-sounding American English voice. **This is the ONLY supported voice generation method.**

---

## Admin Panel Integration

You can configure voice prompts directly from the Admin Panel:

1. Navigate to **Auto Attendant** tab
2. Scroll to **Voice Prompts Configuration** section
3. Edit the prompt texts:
   - Company Name
   - Welcome Greeting
   - Main Menu
   - Invalid Option
   - Timeout
   - Transferring
4. Click **Save & Regenerate Voices**
5. Voices will be automatically regenerated using gTTS with US English accent

---

## Generated Files

The script generates files in these directories:
- `auto_attendant/` - Auto attendant prompts (5 files)
- `voicemail_prompts/` - Voicemail system prompts (18 files)

All files are in telephony format:
- Format: WAV
- Sample Rate: 8000 Hz
- Bit Depth: 16-bit
- Channels: Mono

---

## Voice Configuration

The system uses optimized gTTS settings for the most American English human-like sound:
- **Language:** English (`lang='en'`)
- **TLD:** google.com (`tld='com'`) - Provides US English accent
- **Speed:** Natural speaking rate (`slow=False`)

These settings provide the most natural and professional-sounding voice for business use.

---

## Customizing Voice Prompts

### Option 1: Use Admin Panel (Recommended)

The easiest way to customize prompts:
1. Open Admin Panel → Auto Attendant tab
2. Modify the text in Voice Prompts Configuration section
3. Click "Save & Regenerate Voices"
4. Voices are automatically regenerated with your custom text

### Option 2: Edit `config.yml` and Regenerate

Auto attendant prompt text is read from the `auto_attendant.prompts` section of
`config.yml`, falling back to built-in defaults for any key you omit. Edit it,
then regenerate:

```yaml
auto_attendant:
  prompts:
    welcome: "Thank you for calling {company_name}."
    main_menu: "For Sales, press 1. For Support, press 2. For Accounting, press 3."
    # invalid / timeout / transferring may also be overridden
```

`{company_name}` is substituted from the top-level `company_name` key in
`config.yml` (or the `--company` flag). Then run:

```bash
python3 scripts/generate_espeak_voices.py --company "Your Company"
```

Voicemail prompt text is **not** config-driven — to change it, edit the
`prompts` dict inside `generate_voicemail_voices()` in
`scripts/generate_espeak_voices.py`.

### Option 3: Replace Files (Best Quality)

Record your own professional prompts and save them as:
- `auto_attendant/welcome.wav`
- `auto_attendant/main_menu.wav`
- etc.

Make sure they're in the correct format (8000 Hz, 16-bit, mono).

---

## Troubleshooting

### "No module named 'gtts'"
```bash
uv pip install gTTS pydub
```

### "Failed to connect" or Network Error
- Check internet connection (gTTS requires internet)
- Verify firewall allows HTTPS to translate.google.com
- Try again after a few minutes

### "No module named 'pydub'"
```bash
uv pip install pydub
```

### Voice sounds choppy
- Regenerate the prompts
- Check internet connection quality during generation

### Want different accent
gTTS supports multiple accents:
- US English: `tld='com'` (default)
- British English: `tld='co.uk'`
- Australian English: `tld='com.au'`
- Canadian English: `tld='ca'`

Edit the script and change the `tld` parameter to your preferred accent.

---

## Why gTTS Only?

**gTTS is the only supported voice generation method because:**
1. **Best Quality** - Natural, human-like voice
2. **Free** - No API keys or costs
3. **Simple** - Easy to install and use
4. **Reliable** - Backed by Google's TTS infrastructure
5. **Professional** - Suitable for production business use
6. **Maintained** - Active development and support

Other TTS methods (espeak, pyttsx3, festival) produce robotic voices unsuitable for professional business use and have been removed.

---

## Related Documentation

- [COMPLETE_GUIDE.md](../COMPLETE_GUIDE.md) - Comprehensive PBX documentation
- [TROUBLESHOOTING.md](../TROUBLESHOOTING.md) - Troubleshooting guide
