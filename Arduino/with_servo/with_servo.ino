#include <HardwareSerial.h>
#include <Wire.h>
#include <ESP32Servo.h>

// ── MOTOR PINS ─────────────────────────────────────────────
#define RPWM_L  33
#define LPWM_L  25
#define RPWM_R  13
#define LPWM_R  15
#define EN_L    26
#define EN_R    2

// ── SERVO PINS ─────────────────────────────────────────────
#define SERVO_L_PIN  34   // Forearm Left
#define SERVO_R_PIN  35   // Forearm Right

// ── TF-LUNA I2C ────────────────────────────────────────────
#define SDA_PIN        21
#define SCL_PIN        22
#define LUNA_ADDR      0x10
#define OBSTACLE_DIST  50
#define DETECT_DIST    150
#define COOLDOWN_MS    10000

// ── PWM ────────────────────────────────────────────────────
#define PWM_FREQ  5000
#define PWM_RES   8

HardwareSerial PiSerial(2);  // RX=16, TX=17

Servo servoL;
Servo servoR;

int           currentSpeed  = 150;
int           lunaDistance  = 999;
bool          obstacleAhead = false;
char          currentDir[12] = "stop";
char          savedDir[12]   = "stop";
unsigned long lastGreeted   = 0;
unsigned long lastLuna      = 0;

#define SERVO_MIN    0
#define SERVO_MAX    180
#define SERVO_CENTER 90

void stopMotors();
void executeMove(const char* dir);

int mapServoVal(int val, int minA, int maxA) {
  return map(val, 0, 2000, minA, maxA);
}

// ── TF-LUNA ────────────────────────────────────────────────
void parseLuna() {
  if (millis() - lastLuna < 100) return;
  lastLuna = millis();

  Wire.beginTransmission(LUNA_ADDR);
  Wire.write(0x00);
  if (Wire.endTransmission(false) != 0) return;

  Wire.requestFrom(LUNA_ADDR, 9);
  if (Wire.available() < 9) return;

  uint8_t data[9];
  for (int i = 0; i < 9; i++) data[i] = Wire.read();

  int dist = data[0] | (data[1] << 8);
  if (dist <= 0 || dist >= 800) return;

  bool wasBlocked = obstacleAhead;
  lunaDistance    = dist;
  obstacleAhead   = (dist <= OBSTACLE_DIST);

  if (obstacleAhead && !wasBlocked) {
    strcpy(savedDir, currentDir);
    stopMotors();
    PiSerial.print("OBSTACLE:"); PiSerial.println(dist);
    Serial.print("[LUNA] OBSTACLE: "); Serial.println(dist);
  }

  if (!obstacleAhead && wasBlocked) {
    PiSerial.println("CLEAR:resuming");
    Serial.println("[LUNA] CLEAR");
  }

  unsigned long now = millis();
  if (dist >= 10 && dist <= DETECT_DIST && now - lastGreeted > COOLDOWN_MS) {
    lastGreeted = now;
    strcpy(savedDir, currentDir);
    stopMotors();
    PiSerial.print("PERSON_DETECTED:"); PiSerial.println(dist);
    Serial.print("[LUNA] PERSON: "); Serial.println(dist);
  }
}

// ── MOTOR CONTROL ──────────────────────────────────────────
void stopMotors() {
  ledcWrite(RPWM_L, 0); ledcWrite(LPWM_L, 0);
  ledcWrite(RPWM_R, 0); ledcWrite(LPWM_R, 0);
  strcpy(currentDir, "stop");
  Serial.println("[MOTOR] STOP");
}

void executeMove(const char* dir) {
  strncpy(currentDir, dir, 11);
  currentDir[11] = '\0';

  if (strcmp(dir, "forward") == 0 && obstacleAhead) {
    stopMotors();
    PiSerial.print("BLOCKED:"); PiSerial.println(lunaDistance);
    return;
  }
  if (strcmp(dir, "forward") == 0) {
    ledcWrite(RPWM_L, currentSpeed); ledcWrite(LPWM_L, 0);
    ledcWrite(RPWM_R, currentSpeed); ledcWrite(LPWM_R, 0);
    Serial.println("[MOTOR] FORWARD");
  }
  else if (strcmp(dir, "backward") == 0) {
    ledcWrite(RPWM_L, 0); ledcWrite(LPWM_L, currentSpeed);
    ledcWrite(RPWM_R, 0); ledcWrite(LPWM_R, currentSpeed);
    Serial.println("[MOTOR] BACKWARD");
  }
  else if (strcmp(dir, "left") == 0) {
    ledcWrite(RPWM_L, 0);            ledcWrite(LPWM_L, currentSpeed);
    ledcWrite(RPWM_R, currentSpeed); ledcWrite(LPWM_R, 0);
    Serial.println("[MOTOR] LEFT");
  }
  else if (strcmp(dir, "right") == 0) {
    ledcWrite(RPWM_L, currentSpeed); ledcWrite(LPWM_L, 0);
    ledcWrite(RPWM_R, 0);            ledcWrite(LPWM_R, currentSpeed);
    Serial.println("[MOTOR] RIGHT");
  }
  else {
    stopMotors();
  }
}

// ── SERVO CONTROL ──────────────────────────────────────────
void moveServos(int val, const char* hand) {
  if (strcmp(hand, "left") == 0) {
    int angle = constrain(mapServoVal(val, SERVO_MIN, SERVO_MAX), SERVO_MIN, SERVO_MAX);
    servoL.write(angle);
    Serial.print("[SERVO] L="); Serial.println(angle);
  }
  else if (strcmp(hand, "right") == 0) {
    int angle = constrain(mapServoVal(val, SERVO_MIN, SERVO_MAX), SERVO_MIN, SERVO_MAX);
    servoR.write(angle);
    Serial.print("[SERVO] R="); Serial.println(angle);
  }
  else if (strcmp(hand, "both") == 0) {
    int angle = constrain(mapServoVal(val, SERVO_MIN, SERVO_MAX), SERVO_MIN, SERVO_MAX);
    servoL.write(angle);
    servoR.write(angle);
    Serial.print("[SERVO] L=R="); Serial.println(angle);
  }
}

void homeServos() {
  servoL.write(SERVO_CENTER);
  servoR.write(SERVO_CENTER);
  Serial.println("[SERVO] Homed");
}

// ── SERIAL HANDLER ─────────────────────────────────────────
void handleSerial() {
  if (!PiSerial.available()) return;

  char buf[32];
  int len = 0;
  unsigned long t = millis();
  while (millis() - t < 50 && len < 31) {
    if (PiSerial.available()) {
      char c = PiSerial.read();
      if (c == '\n') break;
      buf[len++] = c;
    }
  }
  buf[len] = '\0';
  if (len > 0 && buf[len-1] == '\r') buf[--len] = '\0';
  if (len == 0) return;

  Serial.print("[CMD] "); Serial.println(buf);

  if (strncmp(buf, "MOVE:", 5) == 0) {
    executeMove(buf + 5);
  }
  else if (strncmp(buf, "SPEED:", 6) == 0) {
    currentSpeed = map(atoi(buf+6), 0, 100, 0, 255);
    Serial.print("[SPEED] "); Serial.println(currentSpeed);
  }
  else if (strncmp(buf, "TOPSPEED:", 9) == 0) {
    currentSpeed = map(atoi(buf+9), 0, 100, 0, 255);
  }
  else if (strncmp(buf, "POS:", 4) == 0) {
    char* p1 = strchr(buf+4, ':');
    if (!p1) return;
    char* p2 = strchr(p1+1, ':');
    if (!p2) return;
    *p1 = '\0'; *p2 = '\0';
    int val        = atoi(p1+1);
    const char* hand = p2+1;
    moveServos(val, hand);
  }
  else if (strcmp(buf, "HOME") == 0 ||
           strcmp(buf, "HOME:left") == 0 ||
           strcmp(buf, "HOME:right") == 0) {
    homeServos();
  }
  else if (strcmp(buf, "RESUME") == 0) {
    Serial.println("[MOTOR] RESUME");
    executeMove(savedDir);
    PiSerial.println("Resumed");
  }
}

// ── SETUP ──────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  PiSerial.begin(115200, SERIAL_8N1, 16, 17);

  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);
  Serial.println("[LUNA] I2C started");

  pinMode(EN_L, OUTPUT); digitalWrite(EN_L, HIGH);
  pinMode(EN_R, OUTPUT); digitalWrite(EN_R, HIGH);

  ledcAttach(RPWM_L, PWM_FREQ, PWM_RES);
  ledcAttach(LPWM_L, PWM_FREQ, PWM_RES);
  ledcAttach(RPWM_R, PWM_FREQ, PWM_RES);
  ledcAttach(LPWM_R, PWM_FREQ, PWM_RES);

  servoL.attach(SERVO_L_PIN);
  servoR.attach(SERVO_R_PIN);
  homeServos();

  stopMotors();
  Serial.println("Giya ready.");
  PiSerial.println("Giya ready.");
}

// ── LOOP ───────────────────────────────────────────────────
void loop() {
  parseLuna();
  handleSerial();

  static unsigned long lastPrint = 0;
  if (millis() - lastPrint > 2000) {
    lastPrint = millis();
    Serial.print("Dist:"); Serial.print(lunaDistance);
    Serial.print(" Obs:"); Serial.print(obstacleAhead);
    Serial.print(" Dir:"); Serial.println(currentDir);
  }
}
