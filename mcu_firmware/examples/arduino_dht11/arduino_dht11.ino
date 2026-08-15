/*
  arduino_dht11.ino — Arduino USB/Serial DHT11 example for DAQ Platform

  Hardware:
    DHT11 temperature + humidity sensor
      VCC  → 5V (or 3.3V)
      GND  → GND
      DATA → digital pin 7   (10k pull-up DATA→VCC; most 3-pin modules
                              already include it on board)

  Connection:
    Arduino USB  →  USB cable  →  PC

  In the the analyzer:
    Transport : USB / UART
    Port      : (auto-detected)
    Baud rate : 115200

  Output format: Arduino Serial Plotter (key:value tab-separated).
  The software auto-detects this and creates channels named
  Temperature and Humidity.

  Library required (Arduino IDE → Tools → Manage Libraries…):
    "DHT sensor library" by Adafruit  (pulls in "Adafruit Unified Sensor")
*/

#include <DHT.h>

#define DHTPIN   7
#define DHTTYPE  DHT11

DHT dht(DHTPIN, DHTTYPE);

void setup() {
  Serial.begin(115200);
  dht.begin();
  // Give the host time to open the port before data starts.
  delay(1000);
}

void loop() {
  float humidity    = dht.readHumidity();      // %
  float temperature = dht.readTemperature();   // °C

  // DHT11 occasionally returns NaN on a flaky read — skip and retry.
  if (isnan(humidity) || isnan(temperature)) {
    delay(2000);
    return;
  }

  // key:value tab-separated — auto-detected by the analyzer.
  Serial.print("Temperature:"); Serial.print(temperature, 1);
  Serial.print("\tHumidity:");  Serial.println(humidity, 1);

  // DHT11 max sample rate is ~1 Hz; 2 s keeps reads reliable.
  delay(2000);
}
