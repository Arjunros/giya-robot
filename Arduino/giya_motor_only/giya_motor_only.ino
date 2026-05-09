#include <HardwareSerial.h>

// BTN7960B pins (same GPIO numbers, repurposed)
#define RPWM_L  36   // was ENA  → Left  RPWM (forward)
#define LPWM_L  35   // was IN2  → Left  LPWM (backward)
#define RPWM_R  38   // was IN3  → Right RPWM (forward)
#define LPWM_R  37   // was IN4  → Right LPWM (backward)
#define EN_L    16   // was IN1  → Left  EN (always HIGH)
#define EN_R    15    // was ENB  → Right EN (always HIGH)

#define LUNA_RX 6
#define LUNA_TX 5
#define OBSTACLE_DISTANCE 50
#define DETECT_DISTANCE   150
#define COOLDOWN_MS       10000
#define PWM_FREQ          5000
#define PWM_RES           8

HardwareSerial LunaSerial(1);

int           currentSpeed  = 150;
int           lunaDistance  = 999;
bool          obstacleAhead = false;
char          currentDir[12] = "stop";
unsigned long lastGreeted   = 0;
unsigned long lastPrint     = 0;

byte lunaBuf[9];
int  lunaIdx  = 0;
bool lunaSync = false;

void stopMotors();
void moveForward();

void parseLuna() {
  while (LunaSerial.available()) {
    byte b = LunaSerial.read();
    if (!lunaSync) {
      if (b == 0x59) { lunaBuf[0]=b; lunaIdx=1; lunaSync=true; }
      continue;
    }
    lunaBuf[lunaIdx++] = b;
    if (lunaIdx==2 && lunaBuf[1]!=0x59) { lunaSync=false; lunaIdx=0; continue; }
    if (lunaIdx==9) {
      lunaSync=false; lunaIdx=0;
      int dist = lunaBuf[3]*256 + lunaBuf[2];
      if (dist>0 && dist<800) {
        bool wasBlocked = obstacleAhead;
        lunaDistance    = dist;
        obstacleAhead   = (dist <= OBSTACLE_DISTANCE);

        if (obstacleAhead && !wasBlocked && strcmp(currentDir,"forward")==0) {
          stopMotors();
          Serial.print("OBSTACLE:"); Serial.print(dist); Serial.println("cm");
        }
        if (!obstacleAhead && wasBlocked && strcmp(currentDir,"forward")==0) {
          moveForward();
          Serial.println("CLEAR:resuming");
        }

        unsigned long now = millis();
        if (dist>=10 && dist<=DETECT_DISTANCE && now-lastGreeted>COOLDOWN_MS) {
          lastGreeted = now;
          Serial.print("PERSON_DETECTED:"); Serial.println(dist);
        }
      }
    }
  }
}

// ── BTN7960B Motor Control ──────────────────────────────────

void stopMotors() {
  ledcWrite(RPWM_L, 0); ledcWrite(LPWM_L, 0);
  ledcWrite(RPWM_R, 0); ledcWrite(LPWM_R, 0);
}

void moveForward() {
  if (obstacleAhead) {
    stopMotors();
    Serial.print("BLOCKED:"); Serial.println(lunaDistance);
    return;
  }
  ledcWrite(RPWM_L, currentSpeed); ledcWrite(LPWM_L, 0);
  ledcWrite(RPWM_R, currentSpeed); ledcWrite(LPWM_R, 0);
}

void moveBackward() {
  ledcWrite(RPWM_L, 0); ledcWrite(LPWM_L, currentSpeed);
  ledcWrite(RPWM_R, 0); ledcWrite(LPWM_R, currentSpeed);
}

void turnLeft() {
  ledcWrite(RPWM_L, 0); ledcWrite(LPWM_L, currentSpeed);
  ledcWrite(RPWM_R, currentSpeed); ledcWrite(LPWM_R, 0);
}

void turnRight() {
  ledcWrite(RPWM_L, currentSpeed); ledcWrite(LPWM_L, 0);
  ledcWrite(RPWM_R, 0); ledcWrite(LPWM_R, currentSpeed);
}

void executeMove(const char* dir) {
  if      (strcmp(dir,"forward")==0)  moveForward();
  else if (strcmp(dir,"backward")==0) moveBackward();
  else if (strcmp(dir,"left")==0)     turnLeft();
  else if (strcmp(dir,"right")==0)    turnRight();
  else                                stopMotors();
}

// ── Setup ───────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  LunaSerial.begin(115200, SERIAL_8N1, LUNA_RX, LUNA_TX);

  // EN pins always HIGH
  pinMode(EN_L, OUTPUT); digitalWrite(EN_L, HIGH);
  pinMode(EN_R, OUTPUT); digitalWrite(EN_R, HIGH);

  // PWM on RPWM/LPWM pins
  ledcAttach(RPWM_L, PWM_FREQ, PWM_RES);
  ledcAttach(LPWM_L, PWM_FREQ, PWM_RES);
  ledcAttach(RPWM_R, PWM_FREQ, PWM_RES);
  ledcAttach(LPWM_R, PWM_FREQ, PWM_RES);

  stopMotors();
  Serial.println("Giya ready.");
}

// ── Loop ────────────────────────────────────────────────────

void loop() {
  parseLuna();

  unsigned long now = millis();
  if (now - lastPrint > 500) {
    lastPrint = now;
    Serial.print("Dist:"); Serial.print(lunaDistance);
    Serial.print(" Obs:"); Serial.print(obstacleAhead);
    Serial.print(" Dir:"); Serial.println(currentDir);
  }

  if (Serial.available()) {
    char buf[32];
    int len = 0;
    unsigned long t = millis();
    while (millis()-t < 50 && len < 31) {
      if (Serial.available()) {
        char c = Serial.read();
        if (c=='\n') break;
        buf[len++] = c;
      }
    }
    buf[len] = '\0';
    if (len>0 && buf[len-1]=='\r') buf[--len]='\0';
    if (len==0) return;

    Serial.print("CMD:"); Serial.println(buf);

    if (strncmp(buf,"MOVE:",5)==0) {
      strncpy(currentDir, buf+5, 11);
      currentDir[11] = '\0';
      executeMove(currentDir);
    }
    else if (strncmp(buf,"SPEED:",6)==0) {
      currentSpeed = map(atoi(buf+6), 0, 100, 0, 255);
    }
    else if (strncmp(buf,"TOPSPEED:",9)==0) {
      currentSpeed = map(atoi(buf+9), 0, 100, 0, 255);
    }
  }
}
