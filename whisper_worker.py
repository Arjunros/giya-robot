#!/usr/bin/env python3
"""
whisper_worker.py — speech to text in a separate, long-lived process.

    from whisper_worker import transcribe, start, stop
    text = transcribe("/tmp/recorded.wav")

Run directly, it IS the worker:
    python3 whisper_worker.py            # reads paths on stdin, writes JSON out

═══════════════════════════════════════════════════════════════════════
WHY A SEPARATE PROCESS, AND WHY A PERSISTENT ONE
═══════════════════════════════════════════════════════════════════════
This robot segfaults inside faster-whisper. Not an exception — SIGSEGV in
native code, which no try/except can catch. In the main process that kills the
whole service and systemd restarts it.

The ORIGINAL code forked a child per utterance and so never saw the crash: the
child died, the parent read an empty result, and the symptom was

    [STT] Heard: ''

every time, with a 15-second delay. Those two complaints were one cause. The
delay was the model reloading on every fork; the empty results were the crash.
Removing the fork fixed the delay and exposed the crash.

Neither obvious structure works:

    fork per utterance   isolates the crash, reloads the model every time
    same process         model stays warm, one crash kills the robot
    persistent worker    both — this file

The worker loads the model once and lives for the life of the service. If it
dies, the parent notices, returns "" for that utterance, and starts a
replacement. The robot loses one turn instead of restarting.

═══════════════════════════════════════════════════════════════════════
WHY A SUBPROCESS RATHER THAN multiprocessing
═══════════════════════════════════════════════════════════════════════
multiprocessing with 'spawn' re-imports the parent's __main__ in the child. On
this robot __main__ is main.py, which imports server.py at module level — and
server.py OPENS THE SERIAL PORT. The child would take a second handle on
/dev/ttyAMA0, and two readers on one serial port each receive roughly half the
bytes with no error from either.

'fork' avoids the re-import but copies the parent's address space, including
the already-loaded InsightFace and onnxruntime state and their thread pools.
Native libraries that have started threads do not survive a fork, which is a
well-known source of exactly the crash being worked around here.

A plain subprocess running this file has neither problem: a fresh interpreter
that imports only faster_whisper, talking over pipes.
"""

import json
import os
import subprocess
import sys
import threading
import time

WHISPER_MODEL   = "base.en"     # "tiny" is faster and noticeably worse
COMPUTE_TYPE    = "int8"        # what fits on a Pi 4
TRANSCRIBE_WAIT = 30.0          # seconds before giving up on the worker
START_WAIT      = 120.0         # a cold model load can be slow

_proc = None
_lock = threading.Lock()
_crashes = 0
_ready = False


# ══════════════════════════════════════════════════════════════════════
# THE WORKER — what runs when this file is executed directly
# ══════════════════════════════════════════════════════════════════════
def _serve():
    """One JSON object per line on stdout; one file path per line on stdin.

    Line-delimited JSON rather than pickle: it is readable in a log, and a
    partially written line is obviously broken rather than silently
    mis-decoded.
    """
    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        emit({"t": "fatal", "msg": f"faster_whisper import failed: {e}"})
        return

    try:
        t0 = time.time()
        model = WhisperModel(WHISPER_MODEL, device="cpu",
                             compute_type=COMPUTE_TYPE)
        emit({"t": "ready",
              "msg": f"{WHISPER_MODEL} {COMPUTE_TYPE} in {time.time()-t0:.1f}s"})
    except Exception as e:
        emit({"t": "fatal", "msg": f"model load failed: {e}"})
        return

    for line in sys.stdin:
        path = line.strip()
        if not path:
            continue
        if path == "__quit__":
            return
        try:
            segments, _ = model.transcribe(
                path,
                language="en",
                beam_size=1,                       # greedy: ample here
                vad_filter=True,                   # skip silence
                condition_on_previous_text=False,  # stops repetition loops
            )
            text = " ".join(s.text for s in segments).lower().strip()
            for ch in ".,!?":
                text = text.replace(ch, "")
            emit({"t": "ok", "text": " ".join(text.split())})
        except Exception as e:
            # Recoverable: report it and keep serving. Only a SEGV takes the
            # process down, and the parent watches for that.
            emit({"t": "err", "msg": f"{type(e).__name__}: {e}"})


# ══════════════════════════════════════════════════════════════════════
# THE CLIENT — what the robot imports
# ══════════════════════════════════════════════════════════════════════
def _spawn():
    """Start the worker. Returns True once it reports ready."""
    global _proc, _ready
    if _proc is not None and _proc.poll() is None and _ready:
        return True

    _ready = False
    here = os.path.abspath(__file__)
    env = dict(os.environ)
    # One thread. The Pi 4 has four cores and the rest of the robot needs
    # them; more importantly, contention between this and the face model's
    # thread pool is a plausible contributor to the crash being worked around.
    env.setdefault("OMP_NUM_THREADS", "2")
    env.setdefault("OPENBLAS_NUM_THREADS", "2")

    try:
        _proc = subprocess.Popen(
            [sys.executable, "-u", here],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,      # its stderr is noise; faults show
                                            # up as the process simply dying
            text=True, env=env,
            cwd=os.path.dirname(here) or ".")
    except Exception as e:
        print(f"[STT] could not start the worker: {e}")
        _proc = None
        return False

    print(f"[STT] worker starting (pid {_proc.pid})")

    # Wait for ready, so the first utterance does not pay the load time on top
    # of transcription — the first thing anybody says is otherwise the one
    # thing the robot misses.
    deadline = time.time() + START_WAIT
    while time.time() < deadline:
        if _proc.poll() is not None:
            print(f"[STT] worker died while loading (exit {_proc.returncode})")
            _proc = None
            return False
        line = _proc.stdout.readline()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("t") == "ready":
            print(f"[STT] worker ready: {msg.get('msg')}")
            _ready = True
            return True
        if msg.get("t") == "fatal":
            print(f"[STT] worker cannot start: {msg.get('msg')}")
            _kill()
            return False
    print("[STT] worker did not become ready in time")
    _kill()
    return False


def _kill():
    global _proc, _ready
    _ready = False
    p, _proc = _proc, None
    if p is None:
        return
    try:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except Exception:
                p.kill()
    except Exception:
        pass


def start():
    """Start the worker early, so the model is warm before anybody speaks."""
    with _lock:
        return _spawn()


def transcribe(wav_path):
    """Transcribe a file. Returns text, or "" on any failure.

    Always safe to call: a dead worker, a stuck worker or a missing file all
    give "" rather than raising, and the file is removed either way.
    """
    global _crashes
    if not wav_path or not os.path.exists(wav_path):
        return ""

    try:
        with _lock:
            if not _spawn():
                return ""
            t0 = time.time()
            try:
                _proc.stdin.write(wav_path + "\n")
                _proc.stdin.flush()
            except Exception as e:
                print(f"[STT] worker gone while sending ({e}) — restarting")
                _kill()
                return ""

            # A reader with a deadline. readline() on a dead process returns
            # '' immediately, which is how a crash is detected rather than
            # waiting out the timeout.
            deadline = time.time() + TRANSCRIBE_WAIT
            while time.time() < deadline:
                line = _proc.stdout.readline()
                if line == "":
                    if _proc.poll() is not None:
                        # THE SEGV CASE. The worker is gone, the robot is
                        # fine. One utterance is lost and the next call starts
                        # a replacement — where previously the whole service
                        # died and systemd restarted it.
                        _crashes += 1
                        rc = _proc.returncode
                        print(f"[STT] worker CRASHED (exit {rc}, "
                              f"crash #{_crashes}) — restarting. "
                              f"This utterance is lost.")
                        _kill()
                        return ""
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                t = msg.get("t")
                if t == "ok":
                    text = msg.get("text", "")
                    print(f"[STT] {time.time()-t0:.1f}s -> {text!r}")
                    return text
                if t == "err":
                    print(f"[STT] worker error: {msg.get('msg')}")
                    return ""
                # 'ready' from a restart, or anything else: keep reading.

            print(f"[STT] no answer in {TRANSCRIBE_WAIT:.0f}s — the worker is "
                  f"stuck, restarting it")
            _kill()
            return ""
    finally:
        try:
            os.remove(wav_path)
        except Exception:
            pass


def stop():
    """Shut the worker down, so it does not linger holding the model in
    memory. Called on the robot's own shutdown path."""
    with _lock:
        try:
            if _proc is not None and _proc.poll() is None:
                _proc.stdin.write("__quit__\n")
                _proc.stdin.flush()
                _proc.wait(timeout=3)
        except Exception:
            pass
        _kill()


def status():
    return {
        "alive": bool(_proc is not None and _proc.poll() is None),
        "pid": _proc.pid if _proc is not None else None,
        "ready": _ready,
        "crashes": _crashes,
        "model": WHISPER_MODEL,
        "compute_type": COMPUTE_TYPE,
    }


if __name__ == "__main__":
    _serve()
