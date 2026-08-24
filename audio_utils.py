import os
import shutil
import subprocess
import time

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
RECORD_PATH = "/tmp/recorded.wav"      # what the caller gets: 16k mono
RAW_PATH    = "/tmp/raw_recorded.wav"  # only used by the INMP441 path
VOICES_DIR  = os.path.join(BASE_DIR, "voices")
PIPER_BIN   = (shutil.which("piper")
               or os.path.join(BASE_DIR, "venv", "bin", "piper")
               or "/usr/local/bin/piper")

# MICROPHONE
#
# Giya has three capture devices once the speaker dongle is plugged in, and
# only two of them are microphones:
#
#   card 3  snd_rpi_googlevoicehat      INMP441      <- fallback
#   card 4  Device [USB Composite Dev]  4c4a:4155    <- wireless lav, priority
#   card ?  USB PnP Sound Device        08bb:2902    <- SPEAKER dongle
#
# The dongle appears under `arecord -l` because the PCM2902 chip has a capture
# side, with nothing plugged into it. The previous code's fallback matched the
# bare keyword "USB", which would select it — and Giya would record silence
# while every log line looked healthy. It is blocked by USB ID below.
#
# THE FORMATS ARE NOT THE SAME, and this is the part that cannot be shared:
#
#   INMP441  is an I2S mic behind the voiceHAT overlay. It presents as
#            2-channel S32_LE at 48 kHz and nothing else, opened on raw `hw:`.
#            Its audio has to be sox'd down to 16 kHz mono afterwards, and
#            `remix 1` picks the single channel that actually carries the mic.
#
#   Lav      is an ordinary USB audio device. It gives 1-channel S16_LE
#            directly and needs no conversion at all — asking it for S32_LE
#            stereo would simply fail.
#
# So each mic carries its own arecord arguments. Both paths finish at
# RECORD_PATH so nothing downstream has to know which mic was used.

MIC_PREFERENCE = [
    {
        "label":   "Wireless lav",
        "usb_id":  "4c4a:4155",
        "keyword": None,
        "device":  "plughw",          # plug layer handles any rate mismatch
        "args":    ['-c', '1', '-r', '48000', '-f', 'S16_LE'],
        "convert": False,             # already usable as-is
    },
    {
        "label":   "INMP441",
        "usb_id":  None,
        "keyword": "googlevoice",
        "device":  "hw",              # I2S: exact format, no plug layer
        "args":    ['-c', '2', '-r', '48000', '-f', 'S32_LE'],
        "convert": True,              # needs the sox step below
    },
]

MIC_BLOCKLIST = {"08bb:2902"}         # the speaker dongle's capture side
SPEAKER_USB_ID = "08bb:2902"

MIC_RETEST_SEC = 30.0

_mic = None                            # (device_string, preference dict)
_mic_failed = {}
_speaker_device = None


def _usb_ids():
    """{card number: 'vendor:product'} for every USB sound card."""
    import glob
    out = {}
    for path in glob.glob('/proc/asound/card*/usbid'):
        try:
            card = int(os.path.basename(os.path.dirname(path))
                       .replace('card', ''))
            with open(path) as f:
                out[card] = f.read().strip().lower()
        except Exception:
            continue
    return out


def _cards(mode='capture'):
    cmd = ['arecord', '-l'] if mode == 'capture' else ['aplay', '-l']
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=5).stdout
    except Exception as e:
        print(f"[AUDIO] {cmd[0]} -l failed: {e}")
        return []
    rows = []
    for line in out.splitlines():
        if not line.lower().startswith('card '):
            continue
        try:
            rows.append((int(line.split(':')[0][5:].strip()), line))
        except ValueError:
            continue
    return rows


def get_card_number(keyword, mode='capture'):
    """Kept for compatibility with anything else that imports it."""
    for card, line in _cards(mode):
        if keyword.lower() in line.lower():
            return card
    return None


def _mic_works(device, pref):
    """Prove the device captures IN ITS OWN FORMAT before committing.

    Testing with generic parameters would pass on a device that then fails
    under the real ones, so the test uses exactly the arguments the recording
    will use.

    A wireless mic that is switched off, flat, or out of range still
    enumerates as a USB device — matching alone will happily choose a dead
    mic, and the only symptom is silence.

    timeout matters: a dongle that has lost its transmitter can open fine and
    then block forever instead of returning frames, which looks like the whole
    robot hanging.
    """
    try:
        r = subprocess.run(
            ['arecord', '-D', device, '-d', '1'] + pref["args"]
            + ['/tmp/mic_test.wav'],
            capture_output=True, timeout=6)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"[MIC] {device} opened but never returned audio")
        return False
    except Exception as e:
        print(f"[MIC] {device} test error: {e}")
        return False


def _pick_mic(refresh=False):
    """Returns (device_string, preference_dict) or (None, None)."""
    global _mic
    if refresh:
        _mic = None
        _mic_failed.clear()

    rows = _cards('capture')
    if not rows:
        return _mic if _mic else (None, None)

    ids = _usb_ids()
    now = time.monotonic()

    for pref in MIC_PREFERENCE:
        for card, line in rows:
            usb = ids.get(card, "")
            if usb in MIC_BLOCKLIST:
                continue
            if pref["usb_id"]:
                if usb != pref["usb_id"]:
                    continue
            elif pref["keyword"] not in line.lower():
                continue

            device = f"{pref['device']}:{card},0"

            if _mic and _mic[0] == device:
                return _mic                       # already proven

            failed_at = _mic_failed.get(device)
            if failed_at and now - failed_at < MIC_RETEST_SEC:
                continue

            if _mic_works(device, pref):
                _mic_failed.pop(device, None)
                print(f"[MIC] Using {pref['label']} on card {card}"
                      + (f" ({usb})" if usb else ""))
                _mic = (device, pref)
                return _mic

            _mic_failed[device] = now
            print(f"[MIC] {pref['label']} on card {card} present but not "
                  f"capturing — retrying in {MIC_RETEST_SEC:.0f}s")

    if _mic:
        return _mic                               # keep the last known-good
    print("[MIC] No working microphone found")
    return (None, None)


def get_mic_device(refresh=False):
    """ALSA device string for the chosen mic, e.g. 'plughw:4,0'."""
    device, _ = _pick_mic(refresh)
    return device or "plughw:0,0"


def get_mic_card():
    """Kept for compatibility — returns just the card number."""
    device, _ = _pick_mic()
    if not device:
        return None
    try:
        return int(device.split(':')[1].split(',')[0])
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════
# SPEAKER
# ═══════════════════════════════════════════════════════════════════════

def get_speaker_device(refresh=False):
    global _speaker_device
    if _speaker_device and not refresh:
        return _speaker_device

    rows = _cards('playback')
    ids = _usb_ids()

    for card, line in rows:                        # exact ID first
        if ids.get(card, "") == SPEAKER_USB_ID:
            print(f"[SPEAKER] USB dongle on card {card}")
            _speaker_device = f"plughw:{card},0"
            return _speaker_device

    for card, line in rows:                        # any USB audio that is not
        low = line.lower()                         # the lav
        if ids.get(card, "") == "4c4a:4155":
            continue
        if 'usb pnp' in low or 'usb audio' in low:
            print(f"[SPEAKER] USB audio on card {card}")
            _speaker_device = f"plughw:{card},0"
            return _speaker_device

    for card, line in rows:                        # voiceHAT has its own amp
        if 'googlevoice' in line.lower():
            print(f"[SPEAKER] voiceHAT amp on card {card}")
            _speaker_device = f"plughw:{card},0"
            return _speaker_device

    print("[SPEAKER] No amplifier found — expect silence")
    _speaker_device = "plughw:0,0"
    return _speaker_device


def get_speaker_card():
    """Kept for compatibility — returns just the card number."""
    dev = get_speaker_device()
    try:
        return int(dev.split(':')[1].split(',')[0])
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════
# RECORDING
# ═══════════════════════════════════════════════════════════════════════

def _record(duration):
    device, pref = _pick_mic()
    if not device:
        time.sleep(1)
        return None

    print(f"[MIC] Recording {duration}s from {pref['label']} ({device})...")
    target = RAW_PATH if pref["convert"] else RECORD_PATH
    try:
        cmd = (['arecord', '-D', device] + pref["args"]
               + ['-d', str(duration), target])
        # timeout: -d alone does not protect against a wedged USB device.
        r = subprocess.run(cmd, capture_output=True, timeout=duration + 10)
        if r.returncode != 0:
            print(f"[MIC] arecord error: {r.stderr.decode(errors='replace')}")
            _pick_mic(refresh=True)      # the mic may have been unplugged
            time.sleep(1)
            return None

        if pref["convert"]:
            # INMP441 only: 48k stereo S32 -> 16k mono. `remix 1` takes the
            # single channel that carries the mic; the other is silent.
            c = subprocess.run(
                ['sox', RAW_PATH, '-r', '16000', '-b', '16', '-c', '1',
                 RECORD_PATH, 'remix', '1'],
                capture_output=True, timeout=30)
            if c.returncode != 0:
                print(f"[MIC] Sox error: {c.stderr.decode(errors='replace')}")
                return None

        print(f"[MIC] Saved -> {RECORD_PATH}")
        return RECORD_PATH

    except subprocess.TimeoutExpired:
        print("[MIC] arecord hung — re-detecting")
        _pick_mic(refresh=True)
        return None
    except Exception as e:
        print(f"[MIC] Exception: {e}")
        return None


def record_audio(duration=3):
    # 3 seconds for wake word detection
    return _record(duration)


def record_question():
    # 5 seconds for the question after the wake word
    return _record(5)


# ═══════════════════════════════════════════════════════════════════════
# TTS
# ═══════════════════════════════════════════════════════════════════════

def _voice_model():
    """A voice that exists on disk, or None. Falls back to the other voice
    rather than going silent: if only one .onnx is installed, selecting the
    missing one would otherwise produce a mute robot."""
    try:
        from settings import load_settings
        voice = load_settings().get('voice', 'female')
    except Exception:
        voice = 'female'
    voice_map = {
        'female': os.path.join(VOICES_DIR, 'en_US-amy-medium.onnx'),
        'male':   os.path.join(VOICES_DIR, 'en_US-ryan-medium.onnx'),
    }
    model = voice_map.get(voice, voice_map['female'])
    if os.path.isfile(model):
        return model
    for name, path in voice_map.items():
        if os.path.isfile(path):
            print(f"[TTS] '{voice}' voice missing, using '{name}'")
            return path
    print(f"[TTS] No voice models in {VOICES_DIR}")
    return None


def speak(text: str):
    if not text or not str(text).strip():
        return

    model = _voice_model()
    if not model:
        return

    raw_path = '/tmp/tts_raw.pcm'
    wav_path = '/tmp/tts_out.wav'
    spk = get_speaker_device()
    print(f"[TTS] Speaking on {spk}")

    try:
        # Text goes to piper's STDIN, not into the shell command.
        #
        # The previous version built  echo "{text}" | piper ...  inside
        # bash -c. Any answer containing a double quote breaks the command,
        # and one containing ; or $(...) executes it. GPT answers contain
        # quotes routinely, so this was a live problem.
        with open(raw_path, 'wb') as out:
            p = subprocess.run(
                [PIPER_BIN, '--model', model, '--output_raw'],
                input=text.encode(), stdout=out,
                stderr=subprocess.PIPE, timeout=60)
        if p.returncode != 0 or os.path.getsize(raw_path) < 512:
            err = p.stderr.decode(errors='replace').strip()[-200:]
            print(f"[TTS] Piper error: {err}")
            return

        c = subprocess.run(
            ['sox', '-t', 'raw', '-r', '22050', '-e', 'signed-integer',
             '-b', '16', '-c', '1', raw_path,
             '-r', '44100', '-e', 'signed-integer', '-b', '16', '-c', '2',
             wav_path],
            capture_output=True, timeout=30)
        if c.returncode != 0:
            print(f"[TTS] Sox error: {c.stderr.decode(errors='replace')}")
            return

        a = subprocess.run(['aplay', '-q', '-D', spk, wav_path],
                           capture_output=True, timeout=60)
        if a.returncode != 0:
            print(f"[TTS] aplay error: "
                  f"{a.stderr.decode(errors='replace').strip()[-200:]}")
            # Card numbers shuffle on replug — re-detect for the next call.
            get_speaker_device(refresh=True)

    except subprocess.TimeoutExpired:
        print("[TTS] timed out")
    except Exception as e:
        print(f"[TTS] Error: {e}")


if __name__ == "__main__":
    # python3 audio_utils.py — shows the choices, records, then speaks
    print("capture devices:")
    ids = _usb_ids()
    for card, line in _cards('capture'):
        usb = ids.get(card, "-")
        tag = "   BLOCKED (speaker dongle)" if usb in MIC_BLOCKLIST else ""
        print(f"   card {card}  {usb:12} "
              f"{line.split(':',1)[1].strip()[:42]}{tag}")
    print()
    dev, pref = _pick_mic()
    print("mic:    ", dev, f"({pref['label']})" if pref else "")
    print("speaker:", get_speaker_device())
    print()
    if record_audio(3):
        print("recording OK")
    speak("Hello, I am Giya. Microphone and speaker are working.")
