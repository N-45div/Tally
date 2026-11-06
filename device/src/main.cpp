// Tally's desk device.
//
// It announces invoices as Tally holds them ("call them on the number on file"), and answers questions about the
// queue: hold the BOOT button, ask, let go. It can put an invoice on hold when asked to. It cannot approve anything:
// the server has no call for that, whatever is said to it. Everything it says is written by Tally's server from
// Tally's records; the device only plays it and shows it.
//
//   GET  /device/hello, /device/news?after=<id>   every few seconds: the queue's counts and new announcements
//   POST /device/ask                              a recorded question (16 kHz mono WAV)
//   GET  /device/ask/<id>?wait=15                 its answer; then GET the answer's audio and play it
//
// Settings (server URL and the device's token) are set over USB with provision.py and kept in the board's NVS. The
// Wi-Fi network is the one saved on the board, or one set the same way. HTTPS is checked against the roots in
// roots.h; there is no insecure mode. Logs and a small command set are on the USB serial console (type "help").

#include <Adafruit_GFX.h>
#include <Adafruit_ST7735.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <SPI.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <stdarg.h>
#include <time.h>

#include "audio.h"
#include "board.h"
#include "roots.h"

#ifndef TALLY_FIRMWARE
#define TALLY_FIRMWARE "tally-desk/dev"
#endif

static const uint32_t POLL_MS = 8000;
static const uint32_t TALK_AFTER_MS = 250;   // a press held this long starts listening; shorter is a tap
static const uint32_t MIN_TALK_MS = 700;
static const size_t SKIP_SAMPLES = MIC_RATE * 15 / 100;  // the beep's tail and the button's click
static const size_t MAX_AUDIO = 3 * 1024 * 1024;

Adafruit_ST7735 tft(&SPI, PIN_TFT_CS, PIN_TFT_DC, PIN_TFT_RST);
Preferences prefs;

struct Settings {
  String server, token;
  int rotation = 0;
} settings;

struct Counts {
  int held = -1, ready = -1, waiting = -1;
  bool operator!=(const Counts &o) const { return held != o.held || ready != o.ready || waiting != o.waiting; }
} counts;

String statusLine = "Starting";
String heardText, sayText;
int lastNews = -1;  // -1: not asked yet, so news from before this start is not replayed
uint32_t nextPollMs = 0, wifiLostMs = 0;
int lastHttp = 0;
uint8_t *lastAudio = nullptr;
size_t lastAudioSize = 0;
bool pressed = false, talking = false;
uint32_t pressedAt = 0, talkStartMs = 0, talkLimitMs = MAX_TALK_MS;

void logLine(const char *fmt, ...) {
  char buf[300];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof buf, fmt, ap);
  va_end(ap);
  Serial.printf("[%8lu] %s\n", (unsigned long)millis(), buf);
}

bool configured() { return settings.server.length() && settings.token.length(); }

void loadSettings() {
  prefs.begin("tally", true);
  settings.server = prefs.getString("server", "");
  settings.token = prefs.getString("token", "");
  settings.rotation = prefs.getInt("rotation", 0);
  prefs.end();
}

void saveSettings() {
  prefs.begin("tally", false);
  prefs.putString("server", settings.server);
  prefs.putString("token", settings.token);
  prefs.putInt("rotation", settings.rotation);
  prefs.end();
}

// The screen.

String ascii(const String &s) {  // the built-in font has ASCII only
  String out;
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    if ((uint8_t)c >= 0x80) {
      while (i + 1 < s.length() && ((uint8_t)s[i + 1] & 0xC0) == 0x80) i++;
      out += '?';
    } else if (c >= 32) {
      out += c;
    }
  }
  return out;
}

// Word-wrapped lines of at most `width` characters, at most `maxLines` of them.
int wrap(const String &text, int width, String *lines, int maxLines) {
  int n = 0;
  String rest = ascii(text);
  rest.trim();
  while (rest.length() && n < maxLines) {
    if ((int)rest.length() <= width) {
      lines[n++] = rest;
      break;
    }
    int cut = rest.lastIndexOf(' ', width);
    if (cut <= 0) cut = width;
    lines[n++] = rest.substring(0, cut);
    rest = rest.substring(cut);
    rest.trim();
  }
  if (rest.length() > lines[max(n - 1, 0)].length() && n == maxLines) lines[n - 1] = lines[n - 1].substring(0, width - 3) + "...";
  return n;
}

void drawScreen() {
  int w = tft.width(), h = tft.height();
  tft.fillScreen(ST77XX_BLACK);
  tft.fillRect(0, 0, w, 16, 0x18E3);
  tft.setTextSize(1);
  tft.setTextColor(ST77XX_WHITE);
  tft.setCursor(4, 4);
  tft.print("Tally");
  bool online = WiFi.status() == WL_CONNECTED;
  tft.fillCircle(w - 8, 8, 3, !online ? ST77XX_RED : lastHttp == 200 ? ST77XX_GREEN : ST77XX_YELLOW);

  const char *labels[3] = {"held", "ready", "waiting"};
  int values[3] = {counts.held, counts.ready, counts.waiting};
  uint16_t colors[3] = {0xFB08, 0x07E8, ST77XX_WHITE};
  int col = w / 3;
  for (int i = 0; i < 3; i++) {
    String v = values[i] < 0 ? "-" : String(values[i]);
    tft.setTextSize(2);
    tft.setTextColor(colors[i]);
    tft.setCursor(col * i + (col - 12 * v.length()) / 2, 22);
    tft.print(v);
    tft.setTextSize(1);
    tft.setTextColor(0xAD55);
    tft.setCursor(col * i + (col - 6 * strlen(labels[i])) / 2, 40);
    tft.print(labels[i]);
  }
  tft.drawFastHLine(0, 52, w, 0x4208);

  int chars = (w - 4) / 6, y = 56, room = (h - 14 - y) / 10;
  String lines[16];
  if (heardText.length()) {
    int n = wrap("\"" + heardText + "\"", chars, lines, min(3, room));
    tft.setTextColor(0x9CF3);
    for (int i = 0; i < n; i++, y += 10) {
      tft.setCursor(2, y);
      tft.print(lines[i]);
    }
    y += 3;
    room = (h - 14 - y) / 10;
  }
  int n = wrap(sayText, chars, lines, min(room, 16));
  tft.setTextColor(ST77XX_WHITE);
  for (int i = 0; i < n; i++, y += 10) {
    tft.setCursor(2, y);
    tft.print(lines[i]);
  }
  tft.fillRect(0, h - 12, w, 12, 0x18E3);
  tft.setTextColor(ST77XX_YELLOW);
  tft.setCursor(2, h - 10);
  tft.print(ascii(statusLine).substring(0, chars));
}

void setStatus(const String &s) {
  statusLine = s;
  drawScreen();
}

String idleLine() {
  if (!configured()) return "Set up over USB";
  if (WiFi.status() != WL_CONNECTED) return "No Wi-Fi";
  if (lastHttp == 401) return "Token refused";
  if (lastHttp != 200 && lastHttp != 0) return "Server unreachable";
  return "Hold BOOT to ask";
}

// Talking to the server.

bool https() { return settings.server.startsWith("https://"); }

void prepare(HTTPClient &http, WiFiClientSecure &secure, WiFiClient &plain, const String &path, uint32_t timeoutMs) {
  if (https()) {
    secure.setCACert(TRUSTED_ROOTS);
    secure.setHandshakeTimeout(15);
    http.begin(secure, settings.server + path);
  } else {
    http.begin(plain, settings.server + path);
  }
  http.setConnectTimeout(8000);
  http.setTimeout(timeoutMs);
  http.addHeader("Authorization", "Bearer " + settings.token);
  http.addHeader("X-Tally-Firmware", TALLY_FIRMWARE);
}

int call(const char *method, const String &path, String *reply, const uint8_t *body = nullptr, size_t len = 0,
         const char *type = nullptr, uint32_t timeoutMs = 20000) {
  WiFiClientSecure secure;
  WiFiClient plain;
  HTTPClient http;
  prepare(http, secure, plain, path, timeoutMs);
  if (type) http.addHeader("Content-Type", type);
  int code = body ? http.sendRequest(method, (uint8_t *)body, len) : http.sendRequest(method);
  if (reply) *reply = code > 0 ? http.getString() : String();
  http.end();
  lastHttp = code;
  if (code <= 0) logLine("%s %s failed: %s", method, path.c_str(), HTTPClient::errorToString(code).c_str());
  return code;
}

size_t readFully(WiFiClient *s, uint8_t *dst, size_t want, uint32_t timeoutMs) {
  size_t have = 0;
  uint32_t last = millis();
  while (have < want && millis() - last < timeoutMs) {
    int avail = s->available();
    if (avail > 0) {
      have += s->read(dst + have, min((size_t)avail, want - have));
      last = millis();
    } else if (!s->connected()) {
      break;
    } else {
      delay(2);
    }
  }
  return have;
}

// Download a WAV into PSRAM, play it, and keep it to say again on a tap.
bool fetchAndPlay(const String &path) {
  WiFiClientSecure secure;
  WiFiClient plain;
  HTTPClient http;
  prepare(http, secure, plain, path, 20000);
  uint32_t started = millis();
  int code = http.GET();
  int size = http.getSize();
  if (code != 200 || size <= 44 || (size_t)size > MAX_AUDIO) {
    logLine("audio %s: HTTP %d, %d bytes", path.c_str(), code, size);
    http.end();
    return false;
  }
  uint8_t *wav = (uint8_t *)ps_malloc(size);
  if (!wav) {
    http.end();
    return false;
  }
  size_t got = readFully(http.getStreamPtr(), wav, size, 8000);
  http.end();
  if (got != (size_t)size) {
    logLine("audio %s: got %u of %d bytes", path.c_str(), (unsigned)got, size);
    free(wav);
    return false;
  }
  uint32_t fetched = millis() - started;
  float seconds = playWav(wav, size);
  logLine("said %.1f s (downloaded %d bytes in %u ms)", seconds, size, (unsigned)fetched);
  if (lastAudio) free(lastAudio);
  lastAudio = wav;
  lastAudioSize = size;
  return seconds > 0;
}

void readCounts(JsonDocument &doc) {
  JsonVariant c = doc["counts"];
  if (c.isNull()) return;
  Counts fresh;
  fresh.held = c["held"] | -1;
  fresh.ready = c["ready"] | -1;
  fresh.waiting = c["waiting"] | -1;
  if (fresh != counts) {
    counts = fresh;
    drawScreen();
  }
}

void poll() {
  String body;
  int before = lastHttp;
  int code = lastNews < 0 ? call("GET", "/device/hello", &body) : call("GET", "/device/news?after=" + String(lastNews), &body);
  if (code != 200) {
    if (code != before) {
      logLine("poll: HTTP %d %s", code, body.substring(0, 120).c_str());
      setStatus(idleLine());
    }
    return;
  }
  JsonDocument doc;
  if (deserializeJson(doc, body)) return;
  if (before != 200) setStatus(idleLine());
  readCounts(doc);
  if (lastNews < 0) {
    lastNews = doc["latest"] | 0;
    logLine("hello: I am \"%s\"; announcements after %d", (const char *)(doc["name"] | "?"), lastNews);
    return;
  }
  for (JsonObject n : doc["news"].as<JsonArray>()) {
    lastNews = max(lastNews, (int)(n["id"] | 0));
    heardText = "";
    sayText = (const char *)(n["text"] | "");
    logLine("news %d: %s", lastNews, sayText.c_str());
    setStatus("New hold");
    chime();
    String audio = (const char *)(n["audio"] | "");
    if (audio.length()) fetchAndPlay(audio);
    setStatus(idleLine());
  }
}

// Asking.

void ask(size_t from, size_t to) {
  Level l = scaleRecording(from, to);
  logLine("sending %.1f s of speech (mic first %ld, peak %ld after the filter, gain 1/%.0f)", (to - from) / (float)MIC_RATE, (long)l.dc, (long)l.peak,
          1.0f / l.gain);
  size_t size = 0;
  uint8_t *wav = recordingWav(to - from, &size);
  if (!wav) {
    errorBeep();
    setStatus("Out of memory");
    return;
  }
  heardText = "";
  sayText = "";
  setStatus("Thinking...");
  String body;
  int code = call("POST", "/device/ask", &body, wav, size, "audio/wav", 30000);
  free(wav);
  JsonDocument doc;
  if (code != 202 || deserializeJson(doc, body)) {
    logLine("ask: HTTP %d %s", code, body.substring(0, 160).c_str());
    errorBeep();
    sayText = code == 429 ? "One question at a time: wait for the answer." : "The question did not reach Tally.";
    setStatus(idleLine());
    return;
  }
  int id = doc["id"] | -1;
  uint32_t started = millis();
  for (int tries = 0; tries < 6; tries++) {
    code = call("GET", "/device/ask/" + String(id) + "?wait=15", &body, nullptr, 0, nullptr, 30000);
    if (code != 200 || deserializeJson(doc, body)) break;
    String state = (const char *)(doc["status"] | "");
    if (state != "done" && state != "failed") continue;
    heardText = (const char *)(doc["heard"] | "");
    sayText = (const char *)(doc["answer"] | "");
    logLine("heard \"%s\" -> \"%s\" (%u ms)", heardText.c_str(), sayText.c_str(), (unsigned)(millis() - started));
    readCounts(doc);
    setStatus("Speaking");
    String audio = (const char *)(doc["audio"] | "");
    if (!audio.length() || !fetchAndPlay(audio)) chime();
    setStatus(idleLine());
    return;
  }
  logLine("ask %d: no answer (HTTP %d)", id, code);
  errorBeep();
  sayText = "No answer came back. Try again.";
  setStatus(idleLine());
}

void startTalking(uint32_t limitMs) {
  if (!micReady || !configured() || WiFi.status() != WL_CONNECTED) {
    errorBeep();
    setStatus(!micReady ? "No microphone" : idleLine());
    return;
  }
  listenBeep();
  startRecording();
  talking = true;
  talkStartMs = millis();
  talkLimitMs = limitMs;
  setStatus("Listening... let go to send");
}

void finishTalking() {
  if (!talking) return;
  talking = false;
  stopRecording();
  uint32_t ms = millis() - talkStartMs;
  if (ms < MIN_TALK_MS || talkSamples <= SKIP_SAMPLES + MIC_RATE / 4) {
    logLine("too short to send (%u ms)", (unsigned)ms);
    errorBeep();
    setStatus(idleLine());
    return;
  }
  gotItBeep();
  ask(SKIP_SAMPLES, talkSamples);
  nextPollMs = millis() + 1000;
}

void sayAgain() {
  if (!lastAudio) return;
  setStatus("Speaking");
  playWav(lastAudio, lastAudioSize);
  setStatus(idleLine());
}

void readButton() {
  bool down = digitalRead(PIN_BUTTON) == LOW;
  uint32_t now = millis();
  if (down && !pressed) {
    pressed = true;
    pressedAt = now;
  } else if (down && pressed && !talking && now - pressedAt >= TALK_AFTER_MS && now - pressedAt < TALK_AFTER_MS + 100) {
    startTalking(MAX_TALK_MS);
  } else if (!down && pressed) {
    pressed = false;
    if (talking) {
      finishTalking();
    } else if (now - pressedAt < TALK_AFTER_MS) {
      sayAgain();
    }
  }
  if (talking && (!readMic() || millis() - talkStartMs >= talkLimitMs)) finishTalking();
}

// Wi-Fi.

void connectWifi(const char *ssid = nullptr, const char *pass = nullptr) {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname("tally-desk");
  WiFi.setSleep(false);
  if (ssid) {
    WiFi.disconnect();
    WiFi.begin(ssid, pass);  // saved on the board for next time
  } else {
    WiFi.begin();  // the network already saved on the board
  }
  setStatus("Joining Wi-Fi...");
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) delay(200);
  if (WiFi.status() == WL_CONNECTED) {
    logLine("Wi-Fi \"%s\", %d dBm, %s", WiFi.SSID().c_str(), WiFi.RSSI(), WiFi.localIP().toString().c_str());
    configTime(0, 0, "pool.ntp.org", "time.google.com");
  } else {
    logLine("Wi-Fi: not connected (saved network: \"%s\")", WiFi.SSID().c_str());
  }
  setStatus(idleLine());
}

// The USB console: setup and checks. Nothing secret is printed.

void micTest() {
  if (!micReady) {
    Serial.println("ERR mic: not ready");
    return;
  }
  startRecording();
  uint32_t start = millis();
  while (millis() - start < 2000 && readMic()) {
  }
  stopRecording();
  int64_t sum = 0;
  for (size_t i = 0; i < talkSamples; i++) sum += rawBuf[i];
  int32_t dc = talkSamples ? sum / (int64_t)talkSamples : 0, peak = 0;
  double energy = 0;
  for (size_t i = 0; i < talkSamples; i++) {
    int32_t d = rawBuf[i] - dc;
    peak = max(peak, abs(d));
    energy += (double)d * d;
  }
  Serial.printf("OK mic samples=%u dc=%ld peak=%ld rms=%.0f (of 8388607)\n", (unsigned)talkSamples, (long)dc, (long)peak,
                talkSamples ? sqrt(energy / talkSamples) : 0.0);
}

void printStatus() {
  struct tm t;
  bool clock = getLocalTime(&t, 10);
  Serial.printf("OK status firmware=%s wifi=%s ssid=\"%s\" rssi=%d ip=%s server=%s token=%s last_http=%d news_after=%d "
                "held=%d ready=%d waiting=%d speaker=%s mic=%s psram_free=%u clock=%s\n",
                TALLY_FIRMWARE, WiFi.status() == WL_CONNECTED ? "up" : "down", WiFi.SSID().c_str(), WiFi.RSSI(),
                WiFi.localIP().toString().c_str(), settings.server.length() ? settings.server.c_str() : "(none)",
                settings.token.length() ? "set" : "missing", lastHttp, lastNews, counts.held, counts.ready, counts.waiting,
                speakerReady ? "ok" : "FAILED", micReady ? "ok" : "FAILED", (unsigned)ESP.getFreePsram(), clock ? "set" : "unset");
}

void command(String line) {
  line.trim();
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String arg = sp < 0 ? "" : line.substring(sp + 1);
  arg.trim();
  if (cmd == "status") {
    printStatus();
  } else if (cmd == "server") {
    while (arg.endsWith("/")) arg.remove(arg.length() - 1);
    if (!arg.startsWith("https://") && !arg.startsWith("http://")) {
      Serial.println("ERR server: give a URL starting with https:// (or http:// for a test server)");
      return;
    }
    settings.server = arg;
    saveSettings();
    lastNews = -1;
    lastHttp = 0;
    nextPollMs = 0;
    Serial.println("OK server");
    setStatus(idleLine());
  } else if (cmd == "token") {
    if (arg.length() < 20 || arg.indexOf(' ') >= 0) {
      Serial.println("ERR token: paste the token the Devices page showed");
      return;
    }
    settings.token = arg;
    saveSettings();
    lastNews = -1;
    lastHttp = 0;
    nextPollMs = 0;
    Serial.println("OK token");
    setStatus(idleLine());
  } else if (cmd == "wifi") {
    int tab = arg.indexOf('\t');
    String ssid = tab < 0 ? arg : arg.substring(0, tab), pass = tab < 0 ? "" : arg.substring(tab + 1);
    connectWifi(ssid.c_str(), pass.c_str());
    Serial.println(WiFi.status() == WL_CONNECTED ? "OK wifi " + WiFi.localIP().toString() : String("ERR wifi: could not join"));
  } else if (cmd == "rotation") {
    settings.rotation = constrain(arg.toInt(), 0, 3);
    saveSettings();
    tft.setRotation(settings.rotation);
    drawScreen();
    Serial.println("OK rotation");
  } else if (cmd == "beep") {
    chime();
    Serial.println("OK beep");
  } else if (cmd == "mic") {
    micTest();
  } else if (cmd == "ask") {  // ask without the button: record for that many seconds, then send
    startTalking(constrain(arg.toInt(), 1, (int)(MAX_TALK_MS / 1000)) * 1000);
    Serial.println(talking ? "OK ask: listening" : "ERR ask: cannot listen now");
  } else if (cmd == "news") {
    nextPollMs = 0;
    Serial.println("OK news");
  } else if (cmd == "reboot") {
    Serial.println("OK reboot");
    delay(100);
    ESP.restart();
  } else if (cmd.length()) {
    Serial.println("ERR commands: status, server <url>, token <token>, wifi <name><TAB><password>, rotation <0-3>, "
                   "beep, mic, ask <seconds>, news, reboot");
  }
}

String serialLine;

void readSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (serialLine.length()) command(serialLine);
      serialLine = "";
    } else if (serialLine.length() < 400) {
      serialLine += c;
    }
  }
}

void setup() {
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);  // never wait for a USB host that is not reading
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  pinMode(PIN_TFT_BL, OUTPUT);
  digitalWrite(PIN_TFT_BL, HIGH);
  loadSettings();
  SPI.begin(PIN_TFT_SCLK, -1, PIN_TFT_MOSI, PIN_TFT_CS);
  tft.initR(INITR_BLACKTAB);
  tft.setRotation(settings.rotation);
  sayText = "Tally's desk device. It reads out invoices as they are held, and answers questions about the queue.";
  setStatus("Starting");
  setupAudio();
  logLine("%s: speaker %s, mic %s, %u bytes of PSRAM free", TALLY_FIRMWARE, speakerReady ? "ok" : "FAILED", micReady ? "ok" : "FAILED",
          (unsigned)ESP.getFreePsram());
  connectWifi();
  if (WiFi.status() == WL_CONNECTED) chime();
  if (!configured()) logLine("not set up: run provision.py to set the server and the device's token");
}

void loop() {
  readSerial();
  readButton();
  if (talking) return;
  if (WiFi.status() != WL_CONNECTED) {
    if (!wifiLostMs) {
      wifiLostMs = millis();
      setStatus("No Wi-Fi");
    } else if (millis() - wifiLostMs > 15000) {
      WiFi.reconnect();
      wifiLostMs = millis();
    }
  } else if (wifiLostMs) {
    wifiLostMs = 0;
    logLine("Wi-Fi back, %d dBm", WiFi.RSSI());
    setStatus(idleLine());
  }
  if (configured() && WiFi.status() == WL_CONNECTED && millis() >= nextPollMs) {
    poll();
    nextPollMs = millis() + POLL_MS;
  }
  delay(5);
}
