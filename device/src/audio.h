// The device's voice and ears: the speaker amplifier (MAX98357A, I2S0) and the microphone (INMP441, I2S1).
//
// Recording: 16 kHz, 24-bit samples in 32-bit slots, kept as they came until the recording ends; then the DC offset
// is removed and the loudest moment scaled to a fixed level (never more than a set gain, so silence is not blown up)
// and it becomes the 16-bit mono WAV the server takes.
// Playing: a whole WAV from the server (16-bit mono, any rate), held in PSRAM, played on both channels.
#pragma once

#include <Arduino.h>
#include <WiFiClient.h>
#include <driver/i2s.h>

#include "board.h"

static const int MIC_RATE = 16000;
static const uint32_t MAX_TALK_MS = 12000;
static const size_t TALK_MAX_SAMPLES = MIC_RATE * MAX_TALK_MS / 1000;
static const float MIC_PEAK = 26000.0f;     // where the loudest moment of a recording ends up (of 32767)
static const float MAX_GAIN = 1.0f / 8.0f;  // 24-bit to 16-bit is 1/256: at most 32 times louder than that
float volume = 1.0f;  // set with the console's volume command (the server already levels speech to -16 dBFS)

bool speakerReady = false, micReady = false;
uint32_t speakerRate = 0;
int32_t *rawBuf = nullptr;   // what the microphone heard, 24-bit
int16_t *talkBuf = nullptr;  // what is sent, 16-bit
size_t talkSamples = 0;

bool installSpeaker() {
  i2s_config_t spk = {};
  spk.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
  spk.sample_rate = 24000;
  spk.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  spk.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  spk.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  spk.dma_buf_count = 16;
  spk.dma_buf_len = 512;
  spk.tx_desc_auto_clear = true;
  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = PIN_SPK_BCLK;
  pins.ws_io_num = PIN_SPK_LRCK;
  pins.data_out_num = PIN_SPK_DIN;
  pins.data_in_num = I2S_PIN_NO_CHANGE;
  speakerRate = spk.sample_rate;
  return i2s_driver_install(I2S_NUM_0, &spk, 0, nullptr) == ESP_OK && i2s_set_pin(I2S_NUM_0, &pins) == ESP_OK;
}

void setSpeakerRate(uint32_t rate) {  // the legacy driver restarts the peripheral on every change: only on a change
  if (rate == speakerRate) return;
  i2s_set_clk(I2S_NUM_0, rate, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_STEREO);
  speakerRate = rate;
}

void setupAudio() {
  speakerReady = installSpeaker();
  i2s_config_t mic = {};
  mic.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  mic.sample_rate = MIC_RATE;
  mic.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;  // INMP441: 24-bit data in 32-bit slots
  mic.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;
  mic.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  mic.dma_buf_count = 8;
  mic.dma_buf_len = 256;
  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = PIN_MIC_SCK;
  pins.ws_io_num = PIN_MIC_WS;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = PIN_MIC_SD;
  micReady = i2s_driver_install(I2S_NUM_1, &mic, 0, nullptr) == ESP_OK && i2s_set_pin(I2S_NUM_1, &pins) == ESP_OK;
  if (micReady) i2s_stop(I2S_NUM_1);  // runs only while someone is talking
  rawBuf = (int32_t *)ps_malloc(TALK_MAX_SAMPLES * sizeof(int32_t));
  talkBuf = (int16_t *)ps_malloc(TALK_MAX_SAMPLES * sizeof(int16_t));
  if (!rawBuf || !talkBuf) micReady = false;
}

// Mono samples to both channels. Never waits for ever: a stuck driver would hang the board.
bool playMono(const int16_t *samples, size_t count) {
  static int16_t stereo[512];
  for (size_t i = 0; i < count;) {
    size_t n = min(count - i, (size_t)256);
    for (size_t j = 0; j < n; j++) stereo[2 * j] = stereo[2 * j + 1] = (int16_t)(samples[i + j] * volume);
    size_t written = 0;
    i2s_write(I2S_NUM_0, stereo, n * 4, &written, 300 / portTICK_PERIOD_MS);
    if (written < n * 4) {
      i2s_driver_uninstall(I2S_NUM_0);
      speakerReady = installSpeaker();
      return false;
    }
    i += n;
  }
  return true;
}

// What is queued plays out only if more follows: push half a second of silence after it.
void finishSound() {
  static int16_t silence[256] = {0};
  for (uint32_t left = speakerRate / 2; left > 0;) {
    size_t n = min(left, (uint32_t)256);
    if (!playMono(silence, n)) return;
    left -= n;
  }
}

void tone(int freq, int ms) {
  if (!speakerReady) return;
  setSpeakerRate(16000);
  int16_t buf[256];
  int total = 16000 * ms / 1000;
  for (int done = 0; done < total;) {
    int n = min(total - done, 256);
    for (int j = 0; j < n; j++) buf[j] = (int16_t)(9000 * sinf(2 * PI * freq * (done + j) / 16000.0f));
    playMono(buf, n);
    done += n;
  }
  delay(ms + 20);
}

void chime() { tone(660, 90); tone(990, 140); }      // something to hear
void listenBeep() { tone(880, 60); }                  // listening
void gotItBeep() { tone(660, 60); }                   // recording sent
void errorBeep() { tone(220, 250); }

// WAV files from the server: 16-bit PCM, or 8-bit G.711 mu-law (format 7), mono, 8 to 48 kHz. The server sends
// 8 kHz mu-law: a sixth the size of 24 kHz PCM, because the board downloads slowly over a long round trip.
struct Wav {
  uint16_t format = 0, channels = 0, bits = 0;
  uint32_t rate = 0;
  size_t start = 0, end = 0;  // the samples, as offsets into the file
};

int16_t ulaw(uint8_t u) {
  u = ~u;
  int magnitude = ((((u & 0x0F) << 3) + 0x84) << ((u >> 4) & 7)) - 0x84;
  return (u & 0x80) ? -magnitude : magnitude;
}

// The format and where the samples are, from the first `have` bytes of a file of `size` bytes.
bool parseWav(const uint8_t *wav, size_t have, size_t size, Wav &w) {
  if (have < 12 || memcmp(wav, "RIFF", 4) || memcmp(wav + 8, "WAVE", 4)) return false;
  size_t pos = 12;
  while (pos + 8 <= have) {
    uint32_t len = wav[pos + 4] | (wav[pos + 5] << 8) | (wav[pos + 6] << 16) | ((uint32_t)wav[pos + 7] << 24);
    if (!memcmp(wav + pos, "fmt ", 4) && len >= 16 && pos + 24 <= have) {
      w.format = wav[pos + 8] | (wav[pos + 9] << 8);
      w.channels = wav[pos + 10] | (wav[pos + 11] << 8);
      w.rate = wav[pos + 12] | (wav[pos + 13] << 8) | (wav[pos + 14] << 16) | ((uint32_t)wav[pos + 15] << 24);
      w.bits = wav[pos + 22] | (wav[pos + 23] << 8);
    } else if (!memcmp(wav + pos, "data", 4)) {
      bool pcm = w.format == 1 && w.bits == 16, mulaw = w.format == 7 && w.bits == 8;
      if (w.channels != 1 || !(pcm || mulaw) || w.rate < 8000 || w.rate > 48000) return false;
      w.start = pos + 8;
      w.end = min(w.start + len, size);
      return true;
    }
    pos += 8 + len + (len & 1);
  }
  return false;
}

// Up to 256 samples from `p`, decoded and played.
void playSamples(const uint8_t *p, size_t count, const Wav &w) {
  static int16_t buf[256];
  for (size_t i = 0; i < count; i++) buf[i] = w.format == 7 ? ulaw(p[i]) : (int16_t)(p[2 * i] | (p[2 * i + 1] << 8));
  playMono(buf, count);
}

// A WAV file already in memory. Returns the seconds played, or a negative number if unplayable.
float playWav(const uint8_t *wav, size_t size) {
  Wav w;
  if (!speakerReady || !parseWav(wav, size, size, w)) return -1;
  size_t bps = w.bits / 8, samples = (w.end - w.start) / bps;
  setSpeakerRate(w.rate);
  for (size_t done = 0; done < samples;) {
    size_t n = min(samples - done, (size_t)256);
    playSamples(wav + w.start + done * bps, n, w);
    done += n;
  }
  finishSound();
  return samples / (float)w.rate;
}

// A WAV file played while it downloads: it starts once a second of sound (or all of it) has arrived, then reads
// what the network has between writes to the speaker. The whole file ends up in `buf` (`size` bytes) so it can be
// said again. `*got` is how much arrived. Returns the seconds played, or a negative number if unplayable.
float streamWav(WiFiClient *s, uint8_t *buf, size_t size, size_t *got, uint32_t idleMs) {
  size_t have = 0;
  uint32_t last = millis();
  Wav w;
  while (!parseWav(buf, have, size, w)) {  // the header first
    if (have >= min(size, (size_t)512) || millis() - last > idleMs) {
      *got = have;
      return -1;
    }
    int avail = s->available();
    if (avail > 0) {
      int n = s->read(buf + have, min((size_t)avail, size - have));
      if (n > 0) have += n, last = millis();
    } else if (!s->connected()) {
      *got = have;
      return -1;
    } else {
      delay(2);
    }
  }
  if (!speakerReady) {
    *got = have;
    return -1;
  }
  size_t bps = w.bits / 8, played = w.start, ready = min(w.end, w.start + (size_t)w.rate * bps);
  bool started = false;
  setSpeakerRate(w.rate);
  while (played + bps <= w.end) {
    int avail = have < size ? s->available() : 0;
    if (avail > 0) {
      int n = s->read(buf + have, min((size_t)avail, size - have));
      if (n > 0) have += n, last = millis();
    }
    if (!started && have >= ready) started = true;
    size_t buffered = have > played ? (min(have, w.end) - played) / bps : 0;
    if (started && buffered > 0) {
      size_t n = min(buffered, (size_t)256);
      playSamples(buf + played, n, w);  // waits while the speaker's buffer is full
      played += n * bps;
    } else if (have < size && (avail < 0 || (avail == 0 && !s->connected()))) {
      break;  // the download ended early: what arrived has been played
    } else if (millis() - last > idleMs) {
      break;
    } else {
      delay(2);
    }
  }
  finishSound();
  *got = have;
  return (played - w.start) / bps / (float)w.rate;
}

// Recording.

void startRecording() {
  talkSamples = 0;
  i2s_zero_dma_buffer(I2S_NUM_1);
  i2s_start(I2S_NUM_1);
}

// Read whatever the microphone has; returns false once the recording is full.
bool readMic() {
  int32_t raw[256];
  size_t got = 0;
  i2s_read(I2S_NUM_1, raw, sizeof(raw), &got, 20 / portTICK_PERIOD_MS);
  for (size_t i = 0; i < got / 4 && talkSamples < TALK_MAX_SAMPLES; i++) rawBuf[talkSamples++] = raw[i] >> 8;  // 24 bits, left-aligned
  return talkSamples < TALK_MAX_SAMPLES;
}

void stopRecording() { i2s_stop(I2S_NUM_1); }

struct Level {
  int32_t dc, peak;
  float gain;
};

// Samples [from, to) of what was heard, into talkBuf: the slow drift taken out, the loudest moment at MIC_PEAK.
//
// This microphone's output wanders slowly (measured on this board: 99.9% of a quiet room's energy was below 100 Hz,
// most of it below 3 Hz), so removing the average is not enough: scaled to its peak, the wander drowned the speech.
// Two first-order DC blockers (about 50 Hz) take it out first; they cut a quiet room's level about twentyfold.
static const float DC_BLOCK = 0.98f;

Level scaleRecording(size_t from, size_t to) {
  Level l = {0, 1, 0};
  if (to <= from) return l;
  l.dc = rawBuf[from];
  float x1 = rawBuf[from], y1 = 0, x2 = 0, y2 = 0;
  for (size_t i = from; i < to; i++) {
    float x = rawBuf[i];
    y1 = x - x1 + DC_BLOCK * y1;
    x1 = x;
    y2 = y1 - x2 + DC_BLOCK * y2;
    x2 = y1;
    rawBuf[i] = (int32_t)y2;
    l.peak = max(l.peak, abs(rawBuf[i]));
  }
  l.gain = min(MIC_PEAK / (float)l.peak, MAX_GAIN);
  for (size_t i = from; i < to; i++) talkBuf[i - from] = (int16_t)constrain(lroundf(rawBuf[i] * l.gain), -32767L, 32767L);
  return l;
}

void putLE(uint8_t *p, uint32_t v, int bytes) {
  for (int i = 0; i < bytes; i++) p[i] = (v >> (8 * i)) & 0xff;
}

// talkBuf[0, count) as a WAV file in PSRAM; the caller frees it.
uint8_t *recordingWav(size_t count, size_t *size) {
  size_t data = count * 2;
  uint8_t *wav = (uint8_t *)ps_malloc(44 + data);
  if (!wav) return nullptr;
  memcpy(wav, "RIFF", 4);
  putLE(wav + 4, 36 + data, 4);
  memcpy(wav + 8, "WAVEfmt ", 8);
  putLE(wav + 16, 16, 4);
  putLE(wav + 20, 1, 2);  // PCM
  putLE(wav + 22, 1, 2);  // mono
  putLE(wav + 24, MIC_RATE, 4);
  putLE(wav + 28, MIC_RATE * 2, 4);
  putLE(wav + 32, 2, 2);
  putLE(wav + 34, 16, 2);
  memcpy(wav + 36, "data", 4);
  putLE(wav + 40, data, 4);
  memcpy(wav + 44, talkBuf, data);
  *size = 44 + data;
  return wav;
}
