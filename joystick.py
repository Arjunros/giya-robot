#!/usr/bin/env python3
# =====================================================
#  Giya Robot — Joystick Controller
#  Reads shanwan Android GamePad via /dev/input/js0
#  Sends MOVE commands to ESP32 via serial
# =====================================================
#
#  Left stick:
#    Axis 1 (Y) push up    → MOVE:forward
#    Axis 1 (Y) push down  → MOVE:backward
#    Axis 0 (X) push left  → MOVE:left
#    Axis 0 (X) push right → MOVE:right
#    Center                → MOVE:stop
#
#  Buttons:
#    BtnA (0) → MOVE:stop
#    BtnTR (7) → speed up
#    BtnTL (6) → speed down

import struct
import serial
import time
import threading

JOYSTICK_DEV = "/dev/input/js0"
SERIAL_PORT  = "/dev/ttyAMA10"
BAUD_RATE    = 115200
DEADZONE     = 5000   # ignore small movements

# joystick event format: time(4), value(2), type(1), number(1)
EVENT_SIZE = 8
EVENT_FMT  = "IhBB"

axis    = [0] * 8
buttons = [0] * 15

currentDir   = "stop"
currentSpeed = 70   # 0-100

def send(ser, cmd):
    try:
        ser.write((cmd + '\n').encode())
        ser.flush()
        print(f"[JOY] {cmd}")
    except Exception as e:
        print(f"[JOY] Serial error: {e}")

def get_direction():
    x = axis[0]
    y = axis[1]

    if abs(x) < DEADZONE and abs(y) < DEADZONE:
        return "stop"

    # Y axis dominates if stronger
    if abs(y) >= abs(x):
        if y < -DEADZONE:
            return "forward"
        elif y > DEADZONE:
            return "backward"
    else:
        if x < -DEADZONE:
            return "left"
        elif x > DEADZONE:
            return "right"

    return "stop"

def joystick_loop(ser):
    global currentDir, currentSpeed

    try:
        js = open(JOYSTICK_DEV, "rb")
    except Exception as e:
        print(f"[JOY] Cannot open joystick: {e}")
        return

    print(f"[JOY] Joystick connected: {JOYSTICK_DEV}")
    send(ser, f"SPEED:{currentSpeed}")

    while True:
        try:
            event = js.read(EVENT_SIZE)
            if not event:
                break

            t, value, etype, number = struct.unpack(EVENT_FMT, event)

            # ignore init events
            if etype & 0x80:
                continue

            # axis event
            if etype == 2:
                if number < len(axis):
                    axis[number] = value

                newDir = get_direction()
                if newDir != currentDir:
                    currentDir = newDir
                    send(ser, f"MOVE:{currentDir}")

            # button event
            elif etype == 1:
                if value == 1:  # button pressed
                    if number == 0:   # BtnA — stop
                        currentDir = "stop"
                        send(ser, "MOVE:stop")
                    elif number == 7: # BtnTR — speed up
                        currentSpeed = min(100, currentSpeed + 10)
                        send(ser, f"SPEED:{currentSpeed}")
                        print(f"[JOY] Speed: {currentSpeed}%")
                    elif number == 6: # BtnTL — speed down
                        currentSpeed = max(10, currentSpeed - 10)
                        send(ser, f"SPEED:{currentSpeed}")
                        print(f"[JOY] Speed: {currentSpeed}%")

        except Exception as e:
            print(f"[JOY] Error: {e}")
            break

    js.close()
    send(ser, "MOVE:stop")
    print("[JOY] Joystick disconnected")

if __name__ == "__main__":
    print("[JOY] Connecting to ESP32...")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        time.sleep(1)
        print(f"[JOY] Connected to {SERIAL_PORT}")
    except Exception as e:
        print(f"[JOY] Cannot open serial: {e}")
        exit(1)

    send(ser, "MOVE:stop")

    try:
        joystick_loop(ser)
    except KeyboardInterrupt:
        send(ser, "MOVE:stop")
        ser.close()
        print("\n[JOY] Stopped.")