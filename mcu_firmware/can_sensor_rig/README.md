# CAN Sensor Rig — STM32 Blue Pill

Blue Pill (STM32F103C8T6) reads HC-SR04, DHT11, and a photoresistor, then
broadcasts a single CAN frame every 500 ms.  The analyzer decodes the
frame via the supplied DBC file.

## Files

| File | Purpose |
|---|---|
| `main.c` | Full firmware — helper functions + copy-paste USER CODE blocks |
| `can_sensor_rig.dbc` | DBC used to decode the CAN frame into named signals |

## CubeMX Setup (STM32F103C8T6)

| Setting | Value |
|---|---|
| HSE crystal | 8 MHz |
| SYSCLK (PLL ×9) | 72 MHz |
| APB1 | 36 MHz |
| APB2 / ADC prescaler | 72 MHz / /6 = 12 MHz |
| **CAN** Prescaler | 6 |
| **CAN** BS1 | 8 TQ |
| **CAN** BS2 | 3 TQ |
| **CAN** SJW | 1 TQ |
| → baud rate | **500 kbps**, SP 75 % |
| **TIM1** Prescaler | 71 (1 µs/tick) |
| **TIM1** Period | 65535 |
| **ADC1** IN0 | 239.5 cycles |
| **GPIO** PB8 | Output (TRIG) |
| **GPIO** PB9 | Input (ECHO) |
| **SYS** Debug | Serial Wire (PA13/PA14) |

## Pin Map

```
PA0  ADC1_IN0   LDR midpoint  (LDR to 3V3, 10k to GND)
PA11 CAN_RX     SN65HVD230 CRX
PA12 CAN_TX     SN65HVD230 CTX
PA13 SWDIO      ST-LINK
PA14 SWCLK      ST-LINK
PB6  DHT11 DATA (10k pull-up to 3V3)
PB8  HC-SR04 TRIG
PB9  HC-SR04 ECHO (via 1k/2k divider — ECHO is 5V, divider gives ~3.3V)
PC13 Heartbeat LED (toggles each TX)
```

## CAN Frame (ID 0x123, DLC 8, every 500 ms)

| Bytes | Signal | Unit |
|---|---|---|
| 0–1 | distance_mm (uint16 LE) | mm |
| 2 | temperature_c (int8) | °C |
| 3 | humidity_pct (uint8) | % |
| 4–5 | light_raw (uint16 LE) | 0–4095 |
| 6 | light_pct (uint8) | % |
| 7 | seq (uint8) | counter |

## Flashing

1. Wire ST-LINK → SWDIO/SWCLK/GND. Power Blue Pill from USB.
2. BOOT0 = 0, BOOT1 = 0 (default flash boot).
3. In CubeIDE: Build → Run (or Debug). ST-LINK detected automatically.
4. If "cannot connect": *Debug Configurations → Debugger → Connect under reset*.
5. Success: PC13 LED toggles every 500 ms.
