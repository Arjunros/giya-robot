import subprocess
import os

RECORD_PATH = "/tmp/recorded.wav"
RAW_PATH    = "/tmp/raw_recorded.wav"

def get_card_number(keyword, mode='capture'):
    cmd = 'arecord -l' if mode == 'capture' else 'aplay -l'
    result = subprocess.run(cmd.split(), capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if keyword.lower() in line.lower():
            parts = line.split(':')
            if parts[0].startswith('card '):
                return int(parts[0].replace('card ', '').strip())
    return None

def get_mic_card():
    card = get_card_number('googlevoice', 'capture')
    if card is not None:
        print(f"[MIC] INMP441 found on card {card}")
        return card
    card = get_card_number('USB', 'capture')
    if card is not None:
        print(f"[MIC] USB mic found on card {card}")
        return card
    print("[MIC] No mic found — using card 2")
    return 2

def get_speaker_card():
    card = get_card_number('USB PnP', 'playback')
    if card is not None:
        print(f"[SPEAKER] USB found on card {card}")
        return card
    print("[SPEAKER] No USB speaker — using card 3")
    return 3

def record_audio(duration=3):
    mic_card = get_mic_card()
    print(f"[MIC] Recording {duration}s from INMP441 (hw:{mic_card},0)...")
    try:
        rec_cmd = [
            'arecord',
            '-D', f'hw:{mic_card},0',
            '-c', '2',
            '-r', '48000',
            '-f', 'S32_LE',
            '-d', str(duration),
            RAW_PATH
        ]
        result = subprocess.run(rec_cmd, capture_output=True)
        if result.returncode != 0:
            print(f"[MIC] arecord error: {result.stderr.decode()}")
            return None

        # Convert → 16kHz mono 16-bit for Whisper
        conv_cmd = [
            'sox', RAW_PATH,
            '-r', '16000',
            '-b', '16',
            '-c', '1',
            RECORD_PATH,
            'remix', '1'
        ]
        conv = subprocess.run(conv_cmd, capture_output=True)
        if conv.returncode != 0:
            print(f"[MIC] Sox error: {conv.stderr.decode()}")
            return None

        print(f"[MIC] Saved → {RECORD_PATH}")
        return RECORD_PATH

    except Exception as e:
        print(f"[MIC] Exception: {e}")
        return None

def speak(text: str):
    from settings import load_settings
    s     = load_settings()
    voice = s.get('voice', 'female')
    voice_map = {
        'female': '/home/ben/pi_assistant/voices/en_US-amy-medium.onnx',
        'male':   '/home/ben/pi_assistant/voices/en_US-ryan-medium.onnx',
    }
    model = voice_map.get(voice, voice_map['female'])
    speaker_card = get_speaker_card()
    print(f"[TTS] Speaking on hw:{speaker_card},0")
    try:
        # Piper raw 22050Hz mono 16-bit
        # → sox converts to 44100Hz stereo S16_LE
        # → aplay sends to USB speaker
        piper_cmd = f'echo "{text}" | piper --model {model} --output_raw'
        play_cmd  = (
            f'sox -t raw -r 22050 -e signed-integer -b 16 -c 1 - '
            f'-t wav -r 44100 -e signed-integer -b 16 -c 2 - | '
            f'aplay -D hw:{speaker_card},0'
        )
        result = subprocess.run(
            f'{piper_cmd} | {play_cmd}',
            shell=True,
            capture_output=True
        )
        if result.returncode != 0:
            print(f"[TTS] Error: {result.stderr.decode()}")
    except Exception as e:
        print(f"[TTS] Error: {e}")
