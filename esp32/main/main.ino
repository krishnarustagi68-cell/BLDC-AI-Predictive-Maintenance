/* ================================================================
  BLDC Motor Health Monitor — FINAL VERSION WITH CUSTOM OLED LAYOUT
 ================================================================
*/

#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <SparkFun_ISM330DHCX.h>
#include <Adafruit_INA219.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_GFX.h>
#include <WiFiClientSecure.h>

// ── CONFIGURATION ──────────────────────
#define WIFI_SSID     "1000024289"   
#define WIFI_PASSWORD "1000024289"  
#define SERVER_URL    "http://10.3.0.200:8000/data"
#define DEVICE_ID     "ESP32_MOTOR_01"

// ── PINS ─────────────────────────
#define SDA0 21
#define SCL0 22
#define SDA1 33
#define SCL1 32
#define LM35_PIN 34
#define IR_PIN   27
#define ESC_PIN  18

// ── ESC SETTINGS ─────────────────
#define ESC_FREQ 50
#define ESC_RES  16
#define ESC_MIN  4915
#define ESC_SPD  6000 

// ── GLOBAL OBJECTS ───────────────
// BUS 0 (Default: Pins 21 SDA, 22 SCL)
// Devices: Screen 1 and the Accelerometer/Gyro/Mag board
Adafruit_SSD1306 oled1(128, 64, &Wire, -1);
SparkFun_ISM330DHCX imu; 

// BUS 1 (Secondary: Pins 33 SDA, 32 SCL)
// Devices: Screen 2 and the INA219 Current/Voltage Sensor
Adafruit_SSD1306 oled2(128, 64, &Wire1, -1);
Adafruit_INA219 ina219;

// ── GLOBAL VARIABLES ─────────────
volatile unsigned long pulseCount = 0;
unsigned long lastRPMTime = 0;
float rpm = 0, g_temp = 0, g_cur = 0, g_volt = 0;
String g_cond = "Normal";
float g_ax = 0, g_ay = 0, g_az = 0;
float g_mx = 0, g_my = 0, g_mz = 0;

// ── INTERRUPT FOR RPM ────────────
void IRAM_ATTR onIRPulse() { pulseCount++; }

// ── HELPER: NEW OLED LAYOUT FUNCTION  ──
void updateOLED(Adafruit_SSD1306 &display, String status, float temp, float current, float voltage, int rpm_val, float ax, float az) {
  display.clearDisplay();
  display.setCursor(0, 0);
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);

  display.print("T:"); display.print(temp, 1); display.print("C  I:"); display.print(current, 3); display.println("A");
  display.print("V:"); display.print(voltage, 1); display.print("V  RPM:"); display.println(rpm_val);
  display.print("Ax:"); display.println(ax, 3);
  display.print("Az:"); display.println(az, 3);
  
  // Now using the real global variables
  display.print("Mx:"); display.print(g_mx, 2); 
  display.print(" My:"); display.println(g_my, 2);
  
  display.display();
}

// ── SENSOR READERS ───────────────
float readTemp() {
  int raw = analogRead(LM35_PIN);
  float mv = (raw / 4095.0) * 3300.0;
  return mv / 10.0;
}

void readVib() {
  if (imu.checkStatus()) {
    sfe_ism_data_t data;
    imu.getAccel(&data);
    // Most SparkFun/SmartElex libs return 'g' directly, 
    // only divide by 1000 if your serial shows huge numbers like 980 for gravity.
    g_ax = data.xData; 
    g_ay = data.yData; 
    g_az = data.zData; 
  }
}

void readMag() {
  // If you are using the SparkFun_MMC5983MA library:
  // mag.getMeasurementXYZ(&g_mx, &g_my, &g_mz); 
  
  // If you haven't initialized the 'mag' object yet, 
  // use these lines just to verify the Serial Print works:
  g_mx = 0.12; // Placeholder
  g_my = -0.45; // Placeholder
  
  Serial.print(" | Mx: "); Serial.print(g_mx, 2);
  Serial.print(" | My: "); Serial.print(g_my, 2);
}

String detectFault() {
  if (rpm < 10) return "Motor OFF"; // Priority check for motor off 
  if (abs(g_az - 0.015) > 0.35) return "Misalignment";
  if (rpm < 1290) return "Overload"; // Threshold from dataset [cite: 1]
  return "Normal";
}

void setup() {
  Serial.begin(115200);
  
  // Start BOTH I2C buses
  Wire.begin(SDA0, SCL0);   // Standard bus
  Wire1.begin(SDA1, SCL1);  // Second bus

  // Initialize Sensors
  if (!imu.begin() || !ina219.begin()) {
    Serial.println("Sensor Init Failed!");
  }

  // CRITICAL FIX: Initialize both OLEDs
  // Screen 1 on Wire (Standard)
  if(!oled1.begin(SSD1306_SWITCHCAPVCC, 0x3C)) { 
    Serial.println("OLED1 failed"); 
  }
  
  // Screen 2 on Wire1 (The one that was blank)
  if(!oled2.begin(SSD1306_SWITCHCAPVCC, 0x3C)) { 
    Serial.println("OLED2 failed"); 
  }

  oled1.clearDisplay();
  oled2.clearDisplay();
  oled1.display();
  oled2.display();

  // ... (rest of your ESC setup and WiFi)
}

void loop() {
  unsigned long now = millis();

  // 1. Start both I2C buses
  Wire.begin(21, 22);   // Bus 0
  Wire1.begin(33, 32);  // Bus 1

  // 2. Initialize IMU on Bus 0
  if (!imu.begin(Wire)) {
    Serial.println("ISM330DHCX (Bus 0) Error!");
  }

  // 3. Initialize INA219 on Bus 1 (THIS FIXES THE ZEROS)
  if (!ina219.begin(&Wire1)) {
    Serial.println("INA219 (Bus 1) Error!");
  }

  // 4. Initialize OLEDs
  oled1.begin(SSD1306_SWITCHCAPVCC, 0x3C);
  oled2.begin(SSD1306_SWITCHCAPVCC, 0x3C);
  
  // ... rest of your setup ...
}
  
  // 1. Update RPM (Every 1 second)
  if (now - lastRPMTime >= 1000) {
    noInterrupts();
    unsigned long c = pulseCount; pulseCount = 0;
    interrupts();
    rpm = (c * 60.0) / 6.0; 
    lastRPMTime = now;
  }

  // 2. Read Sensors
  g_temp = readTemp();
  
  // Read ISM330DHCX (Accel/Gyro)
  if (imu.checkStatus()) {
    readVib(); 
  }

  // Read MMC5983MA (Magnetometer)
  // Note: Ensure you have initialized your magnetometer object (e.g., 'mag') in setup
  // mag.getMeasurementXYZ(&g_mx, &g_my, &g_mz);

  g_cur  = ina219.getCurrent_mA() / 1000.0f;
  g_volt = ina219.getBusVoltage_V();
  g_cond = detectFault(); 

  // 3. SERIAL PORT OUTPUT
  Serial.print(">>> MOTOR DATA | ");
  Serial.print("Status: "); Serial.print(g_cond);
  Serial.print(" | RPM: ");   Serial.print(rpm);
  Serial.print(" | Ax: ");    Serial.print(g_ax, 3);
  Serial.print(" | Az: ");    Serial.print(g_az, 3);
  Serial.print(" | Mx: ");    Serial.print(g_mx, 2);
  Serial.print(" | My: ");    Serial.println(g_my, 2);

  // 4. Update OLED 1 (Parameter List)
  updateOLED(oled1, g_cond, g_temp, g_cur, g_volt, (int)rpm, g_ax, g_az);

  // 5. Update OLED 2 (Big Status)
  oled2.clearDisplay();
  oled2.setTextColor(SSD1306_WHITE);
  oled2.setTextSize(2);
  oled2.setCursor(0, 10);
  oled2.println(g_cond); 
  oled2.setTextSize(1);
  oled2.setCursor(0, 45);
  oled2.print("Cloud: "); 
  oled2.println(WiFi.status() == WL_CONNECTED ? "OK" : "ERR");
  oled2.display(); 

  // 6. Cloud Update
  static unsigned long lastCloud = 0;
  if (now - lastCloud > 2000 && WiFi.status() == WL_CONNECTED) {
    // [Your HTTP Post Logic]
    lastCloud = now;
  }
}