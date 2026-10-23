#include <HardwareSerial.h>
#include <Wire.h>
#include <ESP32Servo.h>

// ── MOTOR PINS ─────────────────────────────────────────────
#define RPWM_L  10
#define LPWM_L  9
#define RPWM_R  13
#define LPWM_R  12
#define EN_L    3
#define EN_R    11

#define PWM_FREQ  1000
#define PWM_RES   8

#define MIN_MOVE_PWM 120   // lowest PWM that actually moves the robot — tune this
#define MAX_MOVE_PWM 255

// ── SERVO PINS ─────────────────────────────────────────────
#define SERVO_L_PIN  6
#define SERVO_R_PIN  7

// ── LATCH PIN ──────────────────────────────────────────────
#define LATCH_PIN 35

// ── SHUTDOWN BUTTON ────────────────────────────────────────
#define SHUTDOWN_BTN  15
#define LONG_PRESS_MS 3000
#define LATCH_OFF_MS  15000   // time the Pi gets to shut down before power is cut
#define BTN_DEBOUNCE_MS 30

// ── I2C / TF-LUNA ──────────────────────────────────────────
#define SDA_PIN       14
#define SCL_PIN       21
#define LUNA_ADDR     0x10
#define OBSTACLE_DIST 50
#define DETECT_DIST   150
#define COOLDOWN_MS   10000

HardwareSerial PiSerial(2);
Servo servoL;
Servo servoR;

// ── SERVO LIMITS ───────────────────────────────────────────
const int L_MIN    = 90;
const int L_MAX    = 180;
const int L_CENTER = 90;
const int R_MIN    = 90;
const int R_MAX    = 0;
const int R_CENTER = 90;

// ── STATE ──────────────────────────────────────────────────
int           motorSpeed       = 255;
int           servoSpeed       = 100;
int           currentPosL      = 90;
int           currentPosR      = 90;
int           targetPosL       = 90;
int           targetPosR       = 90;
int           lunaDistance     = 999;
bool          obstacleAhead    = false;
char          currentDir[12]   = "stop";
char          savedDir[12]     = "stop";
unsigned long lastGreeted      = 0;
unsigned long lastLuna         = 0;
unsigned long lastServoUpdate  = 0;
unsigned long lastCmdTime      = 0;
bool          robotMoving      = false;
bool          hardwareEnabled  = true;
bool          shutdownTriggered = false;
unsigned long latchOffAt        = 0;   // when power is cut
unsigned long lastCountMsg      = 0;

// ── BUTTON STATE ───────────────────────────────────────────
unsigned long btnPressStart = 0;
bool          btnWasPressed = false;
bool          btnReleasedOnce = false; // ignore the press that switched us ON
bool          btnRaw        = false;
unsigned long btnRawSince   = 0;

// ── SPEED MAPPING ──────────────────────────────────────────
// Convert app speed (0–200) to a usable PWM, with a deadband floor.
// app 0 -> 0 (stopped), app 1..200 -> MIN_MOVE_PWM..MAX_MOVE_PWM
int appSpeedToPWM(int appVal) {
  appVal = constrain(appVal, 0, 200);
  if (appVal == 0) return 0;
  return map(appVal, 1, 200, MIN_MOVE_PWM, MAX_MOVE_PWM);
}

// ── MOTOR HELPERS ──────────────────────────────────────────
void setMotor(int pinFwd, int pinBwd, int speed, bool forward) {
  if (forward) {
    ledcWrite(pinFwd, speed);
    ledcWrite(pinBwd, 0);
  } else {
    ledcWrite(pinFwd, 0);
    ledcWrite(pinBwd, speed);
  }
}

// ── STOP MOTORS ────────────────────────────────────────────
void stopMotors() {
  digitalWrite(EN_L, LOW);
  digitalWrite(EN_R, LOW);
  ledcWrite(RPWM_L, 0);
  ledcWrite(LPWM_L, 0);
  ledcWrite(RPWM_R, 0);
  ledcWrite(LPWM_R, 0);
  strcpy(currentDir, "stop");
  robotMoving = false;
  Serial.println("[MOTOR] STOP");
}

// ── EXECUTE MOVE ───────────────────────────────────────────
void executeMove(const char* dir) {
  strncpy(currentDir, dir, 11);
  currentDir[11] = '\0';

  if (strcmp(dir, "stop") != 0) {
    strncpy(savedDir, dir, 11);
    savedDir[11] = '\0';
    robotMoving = true;
  } else {
    robotMoving = false;
  }

  if (strcmp(dir, "forward") == 0 && obstacleAhead && hardwareEnabled) {
    stopMotors();
    strcpy(savedDir, "stop");
    PiSerial.print("BLOCKED:");
    PiSerial.println(lunaDistance);
    Serial.println("[MOTOR] BLOCKED - OBSTACLE");
    return;
  }

  if (strcmp(dir, "stop") == 0) {
    stopMotors();
    return;
  }

  digitalWrite(EN_L, HIGH);
  digitalWrite(EN_R, HIGH);

  if (strcmp(dir, "forward") == 0) {
    setMotor(RPWM_L, LPWM_L, motorSpeed, true);
    setMotor(RPWM_R, LPWM_R, motorSpeed, true);
    Serial.println("[MOTOR] FORWARD");
  }
  else if (strcmp(dir, "backward") == 0) {
    setMotor(RPWM_L, LPWM_L, motorSpeed, false);
    setMotor(RPWM_R, LPWM_R, motorSpeed, false);
    Serial.println("[MOTOR] BACKWARD");
  }
  else if (strcmp(dir, "left") == 0) {
    setMotor(RPWM_L, LPWM_L, motorSpeed, false);
    setMotor(RPWM_R, LPWM_R, motorSpeed, true);
    Serial.println("[MOTOR] LEFT");
  }
  else if (strcmp(dir, "right") == 0) {
    setMotor(RPWM_L, LPWM_L, motorSpeed, true);
    setMotor(RPWM_R, LPWM_R, motorSpeed, false);
    Serial.println("[MOTOR] RIGHT");
  }
}

// ── SERVO UPDATE ───────────────────────────────────────────
void updateServos() {
  unsigned long now      = millis();
  unsigned long interval = (unsigned long)map(servoSpeed, 0, 100, 50, 5);
  if (now - lastServoUpdate < interval) return;
  lastServoUpdate = now;

  int step = map(servoSpeed, 0, 100, 1, 8);

  if (currentPosL < targetPosL) {
    currentPosL = min(currentPosL + step, targetPosL);
    servoL.write(currentPosL);
  } else if (currentPosL > targetPosL) {
    currentPosL = max(currentPosL - step, targetPosL);
    servoL.write(currentPosL);
  }

  if (currentPosR < targetPosR) {
    currentPosR = min(currentPosR + step, targetPosR);
    servoR.write(currentPosR);
  } else if (currentPosR > targetPosR) {
    currentPosR = max(currentPosR - step, targetPosR);
    servoR.write(currentPosR);
  }
}

// ── MOVE SERVOS ────────────────────────────────────────────
void moveServos(int value, const char* hand) {
  value = constrain(value, 0, 2000);

  if (strcmp(hand, "left") == 0) {
    targetPosL = map(value, 0, 2000, L_MIN, L_MAX);
    Serial.print("[SERVO] LEFT -> "); Serial.println(targetPosL);
  }
  else if (strcmp(hand, "right") == 0) {
    targetPosR = map(value, 0, 2000, R_MIN, R_MAX);
    Serial.print("[SERVO] RIGHT -> "); Serial.println(targetPosR);
  }
  else if (strcmp(hand, "both") == 0) {
    targetPosL = map(value, 0, 2000, L_MIN, L_MAX);
    targetPosR = map(value, 0, 2000, R_MIN, R_MAX);
    Serial.print("[SERVO] BOTH L="); Serial.print(targetPosL);
    Serial.print(" R="); Serial.println(targetPosR);
  }
}

// ── HOME SERVOS ────────────────────────────────────────────
void homeServos() {
  targetPosL  = L_CENTER;
  targetPosR  = R_CENTER;
  currentPosL = L_CENTER;
  currentPosR = R_CENTER;
  servoL.write(L_CENTER);
  servoR.write(R_CENTER);
  Serial.println("[SERVO] HOMED");
}

// ── TF-LUNA ────────────────────────────────────────────────
void readLuna() {
  if (!hardwareEnabled) return;
  if (millis() - lastLuna < 100) return;
  lastLuna = millis();

  Wire.beginTransmission(LUNA_ADDR);
  Wire.write(0x00);
  if (Wire.endTransmission(false) != 0) {
    Serial.println("[LUNA] TX ERROR");
    return;
  }

  Wire.requestFrom((uint8_t)LUNA_ADDR, (uint8_t)2);
  if (Wire.available() < 2) {
    Serial.println("[LUNA] NO DATA");
    return;
  }

  int low  = Wire.read();
  int high = Wire.read();
  int dist = low | (high << 8);

  if (dist <= 0 || dist > 800) return;

  lunaDistance  = dist;
  obstacleAhead = (dist < OBSTACLE_DIST);

  if (obstacleAhead && strcmp(currentDir, "forward") == 0) {
    stopMotors();
    strcpy(savedDir, "stop");
    PiSerial.print("BLOCKED:");
    PiSerial.println(dist);
    Serial.print("[LUNA] AUTO STOP: ");
    Serial.println(dist);
  }

  if (dist < DETECT_DIST
      && !robotMoving
      && strcmp(currentDir, "stop") == 0
      && millis() - lastGreeted > COOLDOWN_MS) {
    lastGreeted = millis();
    stopMotors();
    strcpy(savedDir, "stop");
    PiSerial.print("PERSON_DETECTED:");
    PiSerial.println(dist);
    Serial.print("[LUNA] PERSON DETECTED: ");
    Serial.println(dist);
  }
}

// ── LATCH SHUTDOWN (button or Pi) ─────────────────────────
// NON-BLOCKING. The old version sat in delay() for 15 s: no serial, no
// servo updates, no replies to the Pi. Now the countdown runs from loop()
// and everything else keeps working while it does.
//
// fromButton: the button started it, so the Pi must be told to shut down.
// From LATCH:OFF the Pi is already shutting down.
void startShutdown(bool fromButton) {
  if (shutdownTriggered) {
    // Already counting down (e.g. button pressed, then the Pi's own
    // LATCH:OFF arrives). Confirm again so the Pi stops retrying.
    PiSerial.println("LATCH:COUNTDOWN");
    return;
  }
  shutdownTriggered = true;
  latchOffAt   = millis() + LATCH_OFF_MS;
  lastCountMsg = 0;

  stopMotors();
  strcpy(savedDir, "stop");
  homeServos();

  if (fromButton) {
    PiSerial.println("SHUTDOWN");
    Serial.println("[LATCH] Button held 3s - SHUTDOWN sent to Pi");
  } else {
    Serial.println("[LATCH] Pi/app shutdown");
  }
  PiSerial.println("LATCH:COUNTDOWN");
  Serial.print("[LATCH] power cut in ");
  Serial.print(LATCH_OFF_MS / 1000);
  Serial.println("s");
}

void pollLatch() {
  if (!shutdownTriggered) return;
  unsigned long now = millis();
  if ((long)(now - latchOffAt) >= 0) {
    Serial.println("[LATCH] OFF - cutting power now");
    stopMotors();
    servoL.detach();
    servoR.detach();
    digitalWrite(LATCH_PIN, LOW);
    // If we are still running (USB power on the bench), stay here instead
    // of carrying on as a robot that thinks it has shut down.
    while (true) delay(1000);
  }
  if (now - lastCountMsg >= 1000) {
    lastCountMsg = now;
    Serial.print("[LATCH] Power cut in ");
    Serial.print((latchOffAt - now + 999) / 1000);
    Serial.println("s...");
  }
}

// ── BUTTON CHECK ───────────────────────────────────────────
// Debounced, and ignored until it has been released once after boot: if the
// same button switches the robot ON, holding it a little long at power-up
// must not count as a 3-second shutdown press.
void checkButton() {
  if (shutdownTriggered) return;

  bool raw = (digitalRead(SHUTDOWN_BTN) == HIGH);   // pressed = LOW
  unsigned long now = millis();
  if (raw != btnRaw) { btnRaw = raw; btnRawSince = now; }
  if (now - btnRawSince < BTN_DEBOUNCE_MS) return;  // still bouncing
  bool pressed = btnRaw;

  if (!btnReleasedOnce) {
    if (!pressed) btnReleasedOnce = true;
    return;
  }

  if (pressed && !btnWasPressed) {
    btnPressStart = now;
    btnWasPressed = true;
    Serial.println("[BTN] Pressed...");
  }
  else if (!pressed && btnWasPressed) {
    btnWasPressed = false;
    Serial.println("[BTN] Released - short press ignored");
  }
  else if (pressed && btnWasPressed && now - btnPressStart >= LONG_PRESS_MS) {
    Serial.println("[BTN] Long press 3s - initiating shutdown!");
    startShutdown(true);
  }
}

// ── SERIAL HANDLER ─────────────────────────────────────────
void handleCommand(char* buf);

// Non-blocking line reader. The old one waited up to 30 ms for a line and
// then THREW AWAY everything else in the buffer - so a LATCH:OFF arriving
// right behind another command was silently lost.
void handleSerial() {
  static char line[64];
  static int  len = 0;
  while (PiSerial.available()) {
    char c = PiSerial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      line[len] = '\0';
      if (len > 0) handleCommand(line);
      len = 0;
    } else if (len < 63) {
      line[len++] = c;
    } else {
      len = 0;                      // garbage line, drop it
    }
  }
}

void handleCommand(char* buf) {
  Serial.print("[CMD] "); Serial.println(buf);

  // While the countdown runs, nothing may move the robot.
  if (shutdownTriggered &&
      (strncmp(buf, "MOVE:", 5) == 0 || strncmp(buf, "POS:", 4) == 0 ||
       strcmp(buf, "RESUME") == 0)) {
    Serial.println("[LATCH] shutting down - command ignored");
    return;
  }

  if (strncmp(buf, "MOVE:", 5) == 0) {
    lastCmdTime = millis();
    executeMove(buf + 5);
  }
  else if (strncmp(buf, "SPEED:", 6) == 0) {
    // App sends 0-200
    motorSpeed = appSpeedToPWM(atoi(buf+6));
    Serial.print("[MOTOR SPEED] "); Serial.println(motorSpeed);
    if (robotMoving) executeMove(currentDir);
  }
  else if (strncmp(buf, "TOPSPEED:", 9) == 0) {
    servoSpeed = constrain(atoi(buf+9), 0, 100);
    Serial.print("[SERVO SPEED] "); Serial.println(servoSpeed);
  }
  else if (strcmp(buf, "HOME") == 0 ||
           strcmp(buf, "HOME:left") == 0 ||
           strcmp(buf, "HOME:right") == 0) {
    homeServos();
  }
  else if (strcmp(buf, "RESUME") == 0) {
    executeMove(savedDir);
    PiSerial.println("Resumed");
    Serial.print("[RESUME] "); Serial.println(savedDir);
  }
  else if (strncmp(buf, "POS:", 4) == 0) {
    char* p1 = strchr(buf+4, ':');
    if (!p1) return;
    char* p2 = strchr(p1+1, ':');
    if (!p2) return;
    *p1 = '\0'; *p2 = '\0';
    const char* part = buf+4;
    int         val  = atoi(p1+1);
    const char* hand = p2+1;
    if (strcmp(part, "elbow") == 0) {
      moveServos(val, hand);
    } else {
      Serial.print("[IGNORE] "); Serial.println(part);
    }
  }
  else if (strcmp(buf, "HARDWARE:ON") == 0) {
    hardwareEnabled = true;
    obstacleAhead   = false;
    Serial.println("[HW] Enabled");
    PiSerial.println("HW:ON");
  }
  else if (strcmp(buf, "HARDWARE:OFF") == 0) {
    hardwareEnabled = false;
    obstacleAhead   = false;
    Serial.println("[HW] Disabled");
    PiSerial.println("HW:OFF");
  }
  // ── LATCH OFF — app/Pi initiated shutdown ──────────────
  else if (strcmp(buf, "LATCH:OFF") == 0) {
    startShutdown(false);
  }
}

// ── SETUP ──────────────────────────────────────────────────
void setup() {
  // FIRST: hold the power on. Anything before this line runs on the
  // momentary press alone and the robot would switch off when it is released.
  pinMode(LATCH_PIN, OUTPUT);
  digitalWrite(LATCH_PIN, HIGH);
  pinMode(SHUTDOWN_BTN, INPUT_PULLDOWN);     // button to GND, pressed = LOW
                                           // (plus 10k to 3.3V on the board)
  Serial.begin(115200);
  PiSerial.begin(115200, SERIAL_8N1, 18, 17);
  Serial.println("[LATCH] ON - power held");
  Serial.println("[BTN] Shutdown button ready");

  delay(100);
  while (PiSerial.available()) PiSerial.read();
  Serial.println("[SERIAL] Buffer cleared");

  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);
  Serial.println("[LUNA] I2C started");

  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  ESP32PWM::allocateTimer(2);
  ESP32PWM::allocateTimer(3);

  servoL.setPeriodHertz(50);
  servoR.setPeriodHertz(50);
  servoL.attach(SERVO_L_PIN, 500, 2400);
  servoR.attach(SERVO_R_PIN, 500, 2400);

  ledcAttach(RPWM_L, PWM_FREQ, PWM_RES);
  ledcAttach(LPWM_L, PWM_FREQ, PWM_RES);
  ledcAttach(RPWM_R, PWM_FREQ, PWM_RES);
  ledcAttach(LPWM_R, PWM_FREQ, PWM_RES);

  ledcWrite(RPWM_L, 0); ledcWrite(LPWM_L, 0);
  ledcWrite(RPWM_R, 0); ledcWrite(LPWM_R, 0);

  pinMode(EN_L, OUTPUT); digitalWrite(EN_L, LOW);
  pinMode(EN_R, OUTPUT); digitalWrite(EN_R, LOW);

  lastCmdTime = millis();

  // Default motor speed = app value 100 (until the app sends SPEED:)
  motorSpeed = appSpeedToPWM(100);
  Serial.print("[MOTOR] default speed PWM "); Serial.println(motorSpeed);

  delay(200);
  homeServos();

  Serial.println("[SYSTEM] Giya ready");
  PiSerial.println("Giya ready.");
}

// ── LOOP ───────────────────────────────────────────────────
void loop() {
  checkButton();
  pollLatch();
  if (!shutdownTriggered) readLuna();   // no greetings while going down
  handleSerial();
  updateServos();

  // Watchdog
  if (millis() - lastCmdTime > 500 && robotMoving) {
    Serial.println("[WATCHDOG] No command — stopping motors");
    stopMotors();
  }

  static unsigned long lastPrint = 0;
  if (millis() - lastPrint > 2000) {
    lastPrint = millis();
    Serial.print("Dist:"); Serial.print(lunaDistance);
    Serial.print(" Obs:"); Serial.print(obstacleAhead);
    Serial.print(" HW:"); Serial.print(hardwareEnabled);
    Serial.print(" Dir:"); Serial.print(currentDir);
    Serial.print(" Btn:"); Serial.println(digitalRead(SHUTDOWN_BTN));
  }
}
