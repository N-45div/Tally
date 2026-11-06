// Pins of the OceanLabz "AI Explorer ESP32-S3-CAM" board (ESP32-S3, 16 MB flash, 8 MB PSRAM) with its 1.8" screen,
// INMP441 microphone and MAX98357A speaker amplifier.
//
// Read from the board itself, not from a data sheet: with the board's shipped firmware running, the GPIO matrix was
// dumped over JTAG (the I2S and SPI outputs below); the microphone's data input, which an output dump cannot show,
// is from the board maker's own pin list.
#pragma once

// 1.8" ST7735 128x160 screen (SPI). 43/44 are UART0's default pins, so the console runs over USB.
#define PIN_TFT_CS 45
#define PIN_TFT_DC 38
#define PIN_TFT_RST 1
#define PIN_TFT_SCLK 43
#define PIN_TFT_MOSI 44
#define PIN_TFT_BL 42

// BOOT button: pressed = LOW. Hold to ask; tap to hear the last thing said again.
#define PIN_BUTTON 0

// Speaker amplifier (MAX98357A) on I2S0 and microphone (INMP441) on I2S1.
#define PIN_SPK_BCLK 48
#define PIN_SPK_LRCK 21
#define PIN_SPK_DIN 47
#define PIN_MIC_SCK 40
#define PIN_MIC_WS 39
#define PIN_MIC_SD 41

// USB (19/20) stays free, so the USB serial console, flashing and the boot log work while the device runs.
