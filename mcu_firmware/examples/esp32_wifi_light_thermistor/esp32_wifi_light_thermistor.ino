/*
  esp32_wifi_light_thermistor.ino — ESP32 TCP/WiFi example for DAQ Platform

  Hardware:
    ESP32 dev board
    Photoresistor (LDR) — light:
      3.3V → LDR → (junction = GPIO 33) → 10k → GND
    NTC thermistor — temperature:
      3.3V → NTC → (junction = GPIO 32) → 10k → GND

  Connection flow:
    1. Update SSID and PASSWORD below.
    2. Upload to ESP32. Open Serial Monitor at 115200 baud.
    3. The IP address is printed — note it (e.g. 192.168.1.50).
    4. In the analyzer: Transport → TCP/Wi-Fi, enter IP:7777.
    5. Software auto-detects "Light" and "Temperature" channels and plots live.

  Output format: key:value tab-separated, one line per sample.

  NOTE on the thermistor: defaults assume a 10k NTC, Beta 3950, 10k series
  resistor. If yours differs, change BETA / R0 / R_FIXED to match the datasheet.
*/

#include <WiFi.h>
#include <math.h>

// ── Configuration ─────────────────────────────────────────────────────────────
const char* SSID     = "Talha";
const char* PASSWORD = "Talha2005";
const int   PORT     = 7777;

// Sensor pins
const int LIGHT_PIN = 33;   // LDR divider junction
const int THERM_PIN = 32;   // thermistor divider junction

// Thermistor parameters (match your part)
const float BETA    = 3950.0;     // Beta coefficient (K)
const float R0      = 10000.0;    // resistance at 25 °C
const float K0      = 298.15;     // 25 °C in Kelvin (T0 clashes with ESP32 touch macro)
const float R_FIXED = 10000.0;    // series resistor to GND
const float ADC_MAX = 4095.0;     // ESP32 12-bit ADC

// Sample interval
const int SAMPLE_MS = 200;   // 5 Hz
// ─────────────────────────────────────────────────────────────────────────────

WiFiServer server(PORT);

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

  Serial.print("Connecting to ");
  Serial.println(SSID);
  WiFi.begin(SSID, PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("Connected. IP: ");
  Serial.println(WiFi.localIP());

  server.begin();
  Serial.print("TCP server listening on port ");
  Serial.println(PORT);
}

void loop() {
  WiFiClient client = server.available();
  if (!client) return;

  Serial.println("DAQ client connected");

  while (client.connected()) {
    int   lightRaw = analogRead(LIGHT_PIN);   // 0..4095 (brighter = higher)
    float tempC    = read_temperature_c();

    if (isnan(tempC)) {
      delay(SAMPLE_MS);
      continue;
    }

    // key:value tab-separated — auto-detected by the analyzer.
    client.print("Light:");
    client.print(lightRaw);
    client.print("\tTemperature:");
    client.print(tempC, 1);
    client.print("\n");

    delay(SAMPLE_MS);
  }

  client.stop();
  Serial.println("DAQ client disconnected");
}
