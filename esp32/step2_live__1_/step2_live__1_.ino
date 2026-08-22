/*
 ================================================================
  BLDC Motor Health Monitor — ESP32 WROOM-32 (30 pin version)
  FINAL VERSION — Based on your actual wiring
 ================================================================
  YOUR EXACT WIRING:
    I2C Bus 0  SDA=21  SCL=22
      ISM330DHCX   0x6A   (Accel+Gyro)
      OLED #1      0x3C   (Sensor data display)

    I2C Bus 1  SDA=33  SCL=32
      INA219       0x40   (Current + Voltage)
      OLED #2      0x3C   (Fault status display)

    LM35    → GPIO34  (analog temperature)
    IR Sensor → GPIO27 (RPM interrupt)
    ESC Signal → GPIO18 (PWM)

  CHANGE BEFORE UPLOADING:
    WIFI_SSID      your WiFi name
    WIFI_PASSWORD  your WiFi password
    SERVER_URL     your ngrok or Render URL

  Libraries needed:
    SparkFun ISM330DHCX   by SparkFun Electronics
    Adafruit INA219        by Adafruit
    Adafruit SSD1306       by Adafruit
    Adafruit GFX Library   by Adafruit
    ArduinoJson            by Benoit Blanchon

  Board: ESP32 Dev Module | Upload Speed: 921600 | Flash: 4MB
 ================================================================
*/

#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>
#include <SparkFun_ISM330DHCX.h>
#include <Adafruit_INA219.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_GFX.h>

// ── CHANGE THESE ─────────────────────────────────────────────
#define WIFI_SSID     "Chaitanya Chaudhary's iPhone"
#define WIFI_PASSWORD "12345678"
#define SERVER_URL    "https://subside-bankable-mutiny.ngrok-free.dev/data"
#define DEVICE_ID     "ESP32_MOTOR_01"
// ─────────────────────────────────────────────────────────────

// ── PINS ─────────────────────────────────────────────────────
// I2C Bus 0 — ISM330DHCX + OLED #1
#define SDA0     21
#define SCL0     22
// I2C Bus 1 — INA219 + OLED #2 (your 30-pin board uses 33/32)
#define SDA1     33
#define SCL1     32
// Other sensors
#define LM35_PIN 34    // analog input — temperature
#define IR_PIN   27    // interrupt — RPM
#define ESC_PIN  18    // PWM — ESC signal

// ── ESC settings ─────────────────────────────────────────────
#define ESC_CH   0
#define ESC_FREQ 50
#define ESC_RES  16
#define ESC_MIN  3277   // 1000 µs  — arm / zero throttle
#define ESC_SPD  6000   // ~1530 µs — normal running speed

// ── Detection thresholds (tuned to your actual collected data) ─
// Normal:       RPM 1311–1328  |VibZ| 0.014–0.016 g  Temp 29–44°C
// Misalignment: |VibZ| > 0.35 g  (your data goes to -0.726)
// Overload:     RPM < 1290        (your overload data: 1211–1269)
// Overheating:  Temp > 45°C       (your overheating max was 47°C)
#define THRESH_VIBZ    0.35f
#define THRESH_OVL_RPM 1290.0f
#define THRESH_TEMP    45.0f

// ── Objects ──────────────────────────────────────────────────
// TwoWire          Wire1(1);                         // second I2C bus
SparkFun_ISM330DHCX imu;                           // Bus 0  0x6A
Adafruit_INA219  ina219;                           // Bus 1  0x40
Adafruit_SSD1306 oled1(128, 64, &Wire,  -1);      // Bus 0  0x3C
Adafruit_SSD1306 oled2(128, 64, &Wire1, -1);      // Bus 1  0x3C
sfe_ism_data_t   accelData;

// ── Global sensor readings ────────────────────────────────────
volatile unsigned long pulseCount = 0;
unsigned long lastRPMTime  = 0;
unsigned long lastCloudMs  = 0;
float rpm     = 0;
float g_temp  = 0;
float g_ax    = 0, g_ay = 0, g_az = 0;
float g_cur   = 0;
float g_volt  = 0;
// Magnetometer placeholders (MMC5983MA not in your shared code — keep as 0)
float g_mx    = 0, g_my = 0, g_mz = 0;
String g_cond = "Starting...";

// ── RPM interrupt ─────────────────────────────────────────────
void IRAM_ATTR onIRPulse() {
  static unsigned long last = 0;
  unsigned long now = millis();
  if (now - last > 5) { pulseCount++; last = now; }
}

// ── ISM330DHCX init ───────────────────────────────────────────
bool initISM() {
  if (!imu.begin()) {
    Serial.println("ERROR: ISM330DHCX not found on Bus 0 (0x6A)");
    return false;
  }
  imu.deviceReset();
  delay(25);
  while (!imu.getDeviceReset()) {}
  imu.setDeviceConfig();
  imu.setBlockDataUpdate();
  imu.setAccelDataRate(ISM_XL_ODR_104Hz);
  imu.setAccelFullScale(ISM_4g);
  imu.setGyroDataRate(ISM_GY_ODR_104Hz);
  imu.setGyroFullScale(ISM_250dps);
  imu.setAccelFilterLP2();
  imu.setAccelSlopeFilter(ISM_LP_ODR_DIV_100);
  Serial.println("ISM330DHCX OK (104 Hz, ±4g)");
  return true;
}

// ── Read temperature (LM35) ───────────────────────────────────
float readTemp() {
  int   raw = analogRead(LM35_PIN);
  float mV  = (raw / 4095.0f) * 3300.0f;  // 12-bit, 3.3V ref
  float t   = mV / 10.0f;                  // LM35: 10 mV/°C
  if (t < -10 || t > 150) return 0.0f;
  return t;
}

// ── Read vibration (ISM330DHCX) ───────────────────────────────
void readVib() {
  if (imu.checkStatus()) {
    imu.getAccel(&accelData);
    // ISM330DHCX SparkFun library returns raw mg values → convert to g
    g_ax = accelData.xData / 1000.0f;
    g_ay = accelData.yData / 1000.0f;
    g_az = accelData.zData / 1000.0f;
  }
}

// ── Fault detection (rule-based, works without WiFi) ──────────
//
//  Priority order:
//  1. Misalignment — |VibZ| shift is the CLEAREST signal
//     Normal VibZ: +0.014 to +0.016 g
//     Misalign:   -0.726 to -0.398 g  (abs = 0.40–0.73)
//     Threshold:   abs(VibZ) > 0.35 g
//
//  2. Overload — RPM DROPS (motor slows under load)
//     Normal RPM: 1311–1328
//     Overload:   1211–1269
//     Threshold:  RPM < 1290
//
//  3. Overheating — Temperature clearly above Normal max
//     Normal max: 44°C
//     Threshold:  Temp > 45°C
//
String detectFault() {
  if (rpm < 10) return "Motor OFF";

  float vz = abs(g_az);
  if (vz   > THRESH_VIBZ)     return "Misalignment";
  if (rpm  < THRESH_OVL_RPM)  return "Overload";
  if (g_temp > THRESH_TEMP)   return "Overheating";
  return "Normal";
}

// ── OLED #1 — sensor values ───────────────────────────────────
void drawOLED1() {
  oled1.clearDisplay();
  oled1.setTextColor(SSD1306_WHITE);
  oled1.setTextSize(1);

  // Row 0: Temperature + Current
  oled1.setCursor(0, 0);
  oled1.print("T:"); oled1.print(g_temp, 1); oled1.print("C  ");
  oled1.print("I:"); oled1.print(g_cur, 3); oled1.println("A");

  // Row 1: Voltage + RPM
  oled1.setCursor(0, 12);
  oled1.print("V:"); oled1.print(g_volt, 1); oled1.print("V  ");
  oled1.print("RPM:"); oled1.println((int)rpm);

  // Row 2: Accel X and Y
  oled1.setCursor(0, 24);
  oled1.print("Ax:"); oled1.print(g_ax, 3);
  oled1.print("  Ay:"); oled1.println(g_ay, 3);

  // Row 3: Accel Z
  oled1.setCursor(0, 36);
  oled1.print("Az:"); oled1.println(g_az, 3);

  // Row 4: Mag X and Y
  oled1.setCursor(0, 48);
  oled1.print("Mx:"); oled1.print(g_mx, 2);
  oled1.print("  My:"); oled1.println(g_my, 2);

  oled1.display();
}

// ── OLED #2 — fault status ────────────────────────────────────
void drawOLED2() {
  oled2.clearDisplay();
  oled2.setTextColor(SSD1306_WHITE);

  // Big condition name (size 2 = 16px tall)
  oled2.setTextSize(2);
  oled2.setCursor(0, 0);

  if      (g_cond == "Normal")       oled2.println("NORMAL");
  else if (g_cond == "Overheating")  oled2.println("OVERHEAT");
  else if (g_cond == "Misalignment") oled2.println("MISALIGN");
  else if (g_cond == "Overload")     oled2.println("OVERLOAD");
  else if (g_cond == "Motor OFF")    oled2.println("MOTOR OFF");
  else                               oled2.println(g_cond.substring(0, 8).c_str());

  // Small info rows below
  oled2.setTextSize(1);

  // Detection basis — what triggered the detection
  oled2.setCursor(0, 36);
  if (g_cond == "Misalignment") {
    oled2.print("|Az|="); oled2.print(abs(g_az), 3); oled2.println("g");
  } else if (g_cond == "Overload") {
    oled2.print("RPM="); oled2.println((int)rpm);
  } else if (g_cond == "Overheating") {
    oled2.print("T="); oled2.print(g_temp, 1); oled2.println("C");
  } else {
    oled2.print("RPM="); oled2.println((int)rpm);
  }

  // WiFi + cloud status
  oled2.setCursor(0, 50);
  oled2.print("WiFi:");
  oled2.print(WiFi.status() == WL_CONNECTED ? "OK" : "ERR");
  oled2.print("  V="); oled2.print(g_volt, 1);

  oled2.display();
}

// ── POST data to cloud server ─────────────────────────────────
void postToCloud() {
  if (WiFi.status() != WL_CONNECTED) return;

  // Use WiFiClientSecure for https (ngrok) or HTTPClient for http
  WiFiClientSecure client;
  client.setInsecure();   // skip SSL cert validation — fine for ngrok dev URL

  HTTPClient http;
  http.begin(client, SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(8000);

  // Build JSON payload
  StaticJsonDocument<384> doc;
  doc["Temperature"] = round(g_temp * 100) / 100.0;
  doc["VibrationX"]  = round(g_ax   * 10000) / 10000.0;
  doc["VibrationY"]  = round(g_ay   * 10000) / 10000.0;
  doc["VibrationZ"]  = round(g_az   * 10000) / 10000.0;
  doc["Current"]     = round(g_cur  * 1000)  / 1000.0;
  doc["Voltage"]     = round(g_volt * 100)   / 100.0;
  doc["RPM"]         = round(rpm    * 10)    / 10.0;
  doc["MagX"]        = g_mx;
  doc["MagY"]        = g_my;
  doc["MagZ"]        = g_mz;
  doc["device_id"]   = DEVICE_ID;

  String body;
  serializeJson(doc, body);

  int code = http.POST(body);

  if (code == 200) {
    // Parse ML prediction from server response
    String resp = http.getString();
    StaticJsonDocument<512> res;
    if (!deserializeJson(res, resp)) {
      String ml = res["analysis"]["final_prediction"] | "";
      if (ml.length() > 0) {
        // Update condition with ML model prediction
        g_cond = ml;
      }
    }
    Serial.printf("Cloud OK → %s\n", g_cond.c_str());
  } else {
    Serial.printf("Cloud POST failed: HTTP %d\n", code);
    // Fall back to local rule-based result
    g_cond = detectFault();
  }

  http.end();
}

// ── WiFi connect ──────────────────────────────────────────────
void connectWiFi() {
  Serial.printf("Connecting to WiFi: %s", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 40) {
    delay(500);
    Serial.print(".");
    tries++;
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nWiFi connected! IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\nWiFi failed — running in offline mode");
  }
}

// ═════════════════════════════════════════════════════════════
//  SETUP
// ═════════════════════════════════════════════════════════════
void setup() {
  Serial.begin(115200);
  Serial.println("\n=== BLDC Health Monitor — Final Version ===");

  // Start BOTH I2C buses
  Wire.begin(SDA0, SCL0);    // Bus 0: ISM330DHCX + OLED #1
  Wire.setClock(400000);

  Wire1.begin(SDA1, SCL1);   // Bus 1: INA219 + OLED #2
  Wire1.setClock(400000);

  delay(100);

  // ── ISM330DHCX (Bus 0) ──
  if (!initISM()) {
    Serial.println("HALTED — fix ISM330DHCX wiring on Bus 0 (SDA=21, SCL=22)");
    while (1) delay(1000);
  }

  // ── INA219 (Bus 1) ──
  // Pass Wire1 so the library uses your second I2C bus
  if (!ina219.begin(&Wire1)) {
    Serial.println("WARNING: INA219 not found on Bus 1 (SDA=33, SCL=32, addr 0x40)");
    Serial.println("Check wiring. Continuing without current measurement.");
  } else {
    ina219.setCalibration_32V_2A();
    Serial.println("INA219 OK (Bus 1)");
  }

  // ── OLED #1 (Bus 0, same bus as ISM330DHCX) ──
  if (!oled1.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println("WARNING: OLED #1 not found on Bus 0");
  } else {
    oled1.clearDisplay();
    oled1.setTextColor(SSD1306_WHITE);
    oled1.setTextSize(1);
    oled1.setCursor(0, 0);
    oled1.println("BLDC Monitor");
    oled1.println("Sensor Data");
    oled1.display();
    Serial.println("OLED #1 OK (Bus 0)");
  }

  // ── OLED #2 (Bus 1, same bus as INA219) ──
  if (!oled2.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println("WARNING: OLED #2 not found on Bus 1");
  } else {
    oled2.clearDisplay();
    oled2.setTextColor(SSD1306_WHITE);
    oled2.setTextSize(1);
    oled2.setCursor(0, 0);
    oled2.println("BLDC Monitor");
    oled2.println("Fault Status");
    oled2.display();
    Serial.println("OLED #2 OK (Bus 1)");
  }

  // ── IR sensor ──
  pinMode(IR_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(IR_PIN), onIRPulse, RISING);
  Serial.println("IR sensor OK (GPIO27)");

  // ── ESC ──
  // You no longer need to define a channel (ESC_CH)
  ledcAttach(ESC_PIN, ESC_FREQ, ESC_RES);
  ledcWrite(ESC_CH, ESC_MIN);

  // Show arming message
  oled2.clearDisplay();
  oled2.setTextSize(1);
  oled2.setCursor(0, 0);
  oled2.println("Arming ESC...");
  oled2.println("Wait 3 seconds");
  oled2.display();

  Serial.println("Arming ESC (3 seconds)...");
  delay(3000);

  // Ramp motor up gradually — required for ESC
  Serial.println("Ramping motor...");
  for (int s = ESC_MIN; s <= ESC_SPD; s += 30) {
    ledcWrite(ESC_CH, s);
    delay(50);
  }
  Serial.println("Motor running!");

  // ── WiFi ──
  connectWiFi();

  lastRPMTime = millis();
  lastCloudMs = millis();

  Serial.println("=== All systems ready ===");
  Serial.println("Temp,VibX,VibY,VibZ,Current,Voltage,RPM,Fault");
}

// ═════════════════════════════════════════════════════════════
//  MAIN LOOP
// ═════════════════════════════════════════════════════════════
void loop() {
  unsigned long now = millis();

  // ── 1. RPM (computed every 1 second) ──
  if (now - lastRPMTime >= 1000) {
    noInterrupts();
    unsigned long c = pulseCount;
    pulseCount = 0;
    interrupts();
    float elapsed = (now - lastRPMTime) / 1000.0f;
    rpm = (c / elapsed) * 60.0f / 6.0f;   // 6 pulses per revolution
    lastRPMTime = now;
  }

  // ── 2. Read all sensors ──
  g_temp = readTemp();
  readVib();   // updates g_ax, g_ay, g_az
  g_cur  = max(0.0f, ina219.getCurrent_mA() / 1000.0f);
  g_volt = ina219.getBusVoltage_V();
  // g_mx, g_my, g_mz: add MMC5983MA reading here if you have it wired

  // ── 3. Local rule-based fault detection ──
  g_cond = detectFault();

  // ── 4. Serial output ──
  Serial.printf("%.2f,%.4f,%.4f,%.4f,%.4f,%.2f,%.1f,%s\n",
    g_temp, g_ax, g_ay, g_az, g_cur, g_volt, rpm, g_cond.c_str());

  // ── 5. Update both OLEDs ──
  drawOLED1();
  drawOLED2();

  // ── 6. POST to cloud every 2 seconds ──
  if (now - lastCloudMs >= 2000) {
    lastCloudMs = now;
    postToCloud();
    drawOLED2();   // refresh OLED #2 with ML result from server
  }

  // ── 7. Reconnect WiFi if dropped ──
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi lost — reconnecting...");
    connectWiFi();
  }

  delay(500);
}
