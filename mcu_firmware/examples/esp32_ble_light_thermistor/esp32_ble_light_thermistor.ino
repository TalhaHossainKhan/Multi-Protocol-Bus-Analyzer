/*
  esp32_ble_light_thermistor.ino — ESP32 BLE Nordic UART example for DAQ Platform

  Hardware:
    ESP32 dev board
    Photoresistor (LDR) — light:
      3.3V → LDR → (junction = GPIO 33) → 10k → GND
    NTC thermistor — temperature:
      3.3V → NTC → (junction = GPIO 32) → 10k → GND

  Connection flow:
    1. Upload to ESP32. Open Serial Monitor at 115200 baud.
    2. In the analyzer: Transport → Bluetooth LE, click Scan.
    3. Select "ESP32-DAQ" from the list.
    4. Software auto-detects "Light" and "Temperature" channels and plots live.

  Library requirements (install via Arduino Library Manager):
    - ESP32 BLE Arduino (included with the ESP32 board package)

  Output format: key:value tab-separated, sent over the Nordic UART Service
  TX characteristic.

  NOTE on the thermistor: defaults assume a 10k NTC, Beta 3950, 10k series
  resistor. If yours differs, change BETA / R0 / R_FIXED to match the datasheet.
*/

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include <math.h>

// Nordic UART Service UUIDs — recognised by all BLE debugger apps.
#define NUS_SERVICE_UUID  "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
#define NUS_TX_UUID       "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  // notify → host
#define NUS_RX_UUID       "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  // write ← host

// ── Sensor pins ───────────────────────────────────────────────────────────────
const int LIGHT_PIN = 33;   // LDR divider junction
const int THERM_PIN = 32;   // thermistor divider junction

// ── Thermistor parameters (match your part) ───────────────────────────────────
const float BETA    = 3950.0;     // Beta coefficient (K)
const float R0      = 10000.0;    // resistance at 25 °C
const float K0      = 298.15;     // 25 °C in Kelvin
const float R_FIXED = 10000.0;    // series resistor to GND
const float ADC_MAX = 4095.0;     // ESP32 12-bit ADC

// ── Sample interval ───────────────────────────────────────────────────────────
const int SAMPLE_MS = 200;   // 5 Hz

BLECharacteristic* pTxChar = nullptr;
bool deviceConnected = false;

// ── BLE server callbacks ──────────────────────────────────────────────────────
class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer* pServer) override {
    deviceConnected = true;
    Serial.println("BLE client connected");
  }
  void onDisconnect(BLEServer* pServer) override {
    deviceConnected = false;
    Serial.println("BLE client disconnected — restarting advertising");
    pServer->startAdvertising();
  }
};
// ─────────────────────────────────────────────────────────────────────────────

float read_temperature_c() {
  int adc = analogRead(THERM_PIN);                  // 0..4095
  if (adc <= 0 || adc >= (int)ADC_MAX) return NAN;  // open / shorted

  // Wiring: 3.3V → NTC → junction → R_FIXED → GND
  //   Rntc = R_FIXED * (ADC_MAX - adc) / adc
  float rntc = R_FIXED * (ADC_MAX - adc) / adc;

  // Beta equation: 1/T = 1/K0 + (1/BETA) * ln(Rntc / R0)
  float tK = 1.0 / ((1.0 / K0) + (1.0 / BETA) * log(rntc / R0));
  return tK - 273.15;                               // → °C
}

void setup() {
  Serial.begin(115200);
  analogReadResolution(12);   // ESP32 ADC: 0..4095

  // Initialise BLE.
  BLEDevice::init("ESP32-DAQ");
  BLEServer*  pServer  = BLEDevice::createServer();
  pServer->setCallbacks(new ServerCallbacks());

  BLEService* pService = pServer->createService(NUS_SERVICE_UUID);

  // TX characteristic — MCU notifies host with sensor data.
  pTxChar = pService->createCharacteristic(
    NUS_TX_UUID,
    BLECharacteristic::PROPERTY_NOTIFY
  );
  pTxChar->addDescriptor(new BLE2902());

  // RX characteristic — host can write (the analyzer may send commands; ignored here).
  pService->createCharacteristic(
    NUS_RX_UUID,
    BLECharacteristic::PROPERTY_WRITE
  );

  pService->start();
  BLEDevice::startAdvertising();
  Serial.println("BLE advertising as 'ESP32-DAQ'");
}

void loop() {
  if (!deviceConnected) {
    delay(100);
    return;
  }

  int   lightRaw = analogRead(LIGHT_PIN);   // 0..4095 (brighter = higher)
  float tempC    = read_temperature_c();

  if (isnan(tempC)) {
    delay(SAMPLE_MS);
    return;
  }

  // key:value tab-separated — newline-terminated — auto-detected by the analyzer.
  String line = String("Light:") + String(lightRaw) +
                "\tTemperature:" + String(tempC, 1) + "\n";
  pTxChar->setValue(line.c_str());
  pTxChar->notify();

  delay(SAMPLE_MS);
}
