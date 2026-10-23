import sys, os, glob
# Use the project venv's packages when present (older installs run the system
# python3). Globbed, so it works on any Python version and any username.
for _sp in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "venv", "lib", "python3*", "site-packages")):
    sys.path.insert(0, _sp)
import board
import neopixel
import threading
import time
import math

NUM_LEDS   = 9
BRIGHTNESS = 0.3

left_eye  = neopixel.NeoPixel(board.D12, NUM_LEDS, brightness=BRIGHTNESS, auto_write=False)
right_eye = neopixel.NeoPixel(board.D10, NUM_LEDS, brightness=BRIGHTNESS, auto_write=False)

_state   = "idle"
_step    = 0

# App connection shown in the RESTING colour: whenever the eyes are idle they
# breathe ORANGE until the phone app connects, BLUE while it is connected.
# Every other state (listening, speaking, ...) still shows as before and then
# returns to idle, which picks up the right colour by itself.
_app_connected = False


# Resting colour while the app is connected. The app's colour slider sets it
# (GET /eyes/color?r=&g=&b=). Saved in settings.json as "eye_color", so the
# chosen colour comes back after a reboot. Blue until someone picks one.
DEFAULT_EYE_COLOR = (0, 0, 255)
_idle_color = DEFAULT_EYE_COLOR


def _load_saved_color():
    global _idle_color
    try:
        from settings import load_settings
        c = load_settings().get("eye_color")
        if isinstance(c, (list, tuple)) and len(c) == 3:
            _idle_color = tuple(max(0, min(255, int(v))) for v in c)
    except Exception as e:
        print(f"[EYES] could not load saved colour: {e}")


def set_color(r, g, b):
    """New colour from the app slider. Only the breathing-blue patterns use
    it (idle while the app is connected, and eyes1). Every other pattern is
    left exactly as it was."""
    global _idle_color
    _idle_color = (max(0, min(255, int(r))),
                   max(0, min(255, int(g))),
                   max(0, min(255, int(b))))
    return _idle_color


def get_color():
    return _idle_color


def set_app_connected(connected):
    global _app_connected
    connected = bool(connected)
    if connected != _app_connected:
        _app_connected = connected
        print(f"[EYES] app {'connected - slider colour' if connected else 'not connected - orange'}")

_running = False
_thread  = None

def _show(color_l, color_r=None):
    if color_r is None:
        color_r = color_l
    left_eye.fill(color_l)
    right_eye.fill(color_r)
    left_eye.show()
    right_eye.show()

def _eye_loop():
    global _step, _running, _state
    _step = 0
    while _running:
        s = _state
        try:
            if s == "idle":
                val = int((1 + math.sin(_step * 0.1)) * 60)
                if _app_connected:
                    k = max(0.05, val / 120)          # same breathing as before
                    _show(tuple(int(c * k) for c in _idle_color))  # slider colour
                else:
                    _show((max(5, val * 2), max(2, int(val * 0.7)), 0))  # orange
                time.sleep(0.05)

            elif s == "eyes1":
                val = int((1 + math.sin(_step * 0.06)) * 80)
                k = max(0.03, val / 160)
                _show(tuple(int(c * k) for c in _idle_color))  # slider colour
                time.sleep(0.05)

            elif s == "eyes2":
                phase = _step % 20
                if   phase < 2:  _show((255, 20, 80))
                elif phase < 4:  _show((80,  5,  25))
                elif phase < 6:  _show((255, 20, 80))
                elif phase < 12: _show((0,   0,   0))
                else:            _show((15,  0,   6))
                time.sleep(0.08)

            elif s == "eyes3":
                rainbow = [
                    (255,0,0),(255,127,0),(255,255,0),(0,255,0),
                    (0,0,255),(75,0,130),(148,0,211),(255,0,255),
                ]
                _show(rainbow[_step % len(rainbow)])
                time.sleep(0.12)

            elif s == "wake":
                _show((255,255,255) if _step%2==0 else (0,0,0))
                time.sleep(0.1)

            elif s == "listening":
                _show((0,255,0) if _step%6<3 else (0,60,0))
                time.sleep(0.1)

            elif s == "speaking":
                _show((0,255,255) if _step%4<2 else (0,60,60))
                time.sleep(0.12)

            elif s == "obstacle":
                _show((255,0,0) if _step%4<2 else (0,0,0))
                time.sleep(0.1)

            elif s == "forward":
                _show((0,255,0))
                time.sleep(0.1)

            elif s == "backward":
                _show((255,200,0))
                time.sleep(0.1)

            elif s == "left":
                _show((0,0,255), (0,0,30))
                time.sleep(0.1)

            elif s == "right":
                _show((0,0,30), (0,0,255))
                time.sleep(0.1)

            elif s == "stop":
                _show((40,40,40))
                time.sleep(0.1)

            elif s == "person":
                _show((128,0,128) if _step%6<3 else (0,0,0))
                time.sleep(0.1)

            elif s == "thinking":
                _show((255,80,0) if _step%4<2 else (60,20,0))
                time.sleep(0.15)

            elif s == "face":
                rainbow = [
                    (255,0,0),(255,127,0),(255,255,0),(0,255,0),
                    (0,0,255),(75,0,130),(148,0,211),(255,0,255),
                ]
                _show(rainbow[_step % len(rainbow)])
                time.sleep(0.12)

            else:
                _show((0,0,0))
                time.sleep(0.1)

            _step += 1

        except Exception as e:
            print(f"[EYES] Error: {e}")
            time.sleep(0.1)

    _show((0,0,0))

def set_state(state):
    global _state, _step
    if _state == state:
        return
    print(f"[EYES] State: {state}")
    _state = state
    _step  = 0

def start_eyes():
    global _running, _thread
    _load_saved_color()
    _running = True
    _thread  = threading.Thread(target=_eye_loop, daemon=True)
    _thread.start()
    print("[EYES] Started")

def stop_eyes():
    global _running
    _running = False
    time.sleep(0.3)
    _show((0,0,0))
    print("[EYES] Stopped")
