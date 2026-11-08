"""Speech for the desk device: what a person says to it, as text, and what it says back, as audio.

Both go through Vultr Serverless Inference, with the same key as the reader (tally.vultr), and only the desk worker
(tally.desk) calls them; the web app never does. Speech in is the person's own words; speech out is text written by
Tally's code (tally.desk), never by a model, so a model here only turns sound into text and text into sound.

Models, all under licences that allow this use, tried in order:
- speech to text: IBM Granite Speech 5.0 2B (Apache-2.0), then Whisper Large V3 Turbo (MIT), then Qwen3-ASR 1.7B
  (Apache-2.0). Granite writes numbers as digits, which matters for "invoice 12".
- text to speech: Qwen3-TTS 12Hz 1.7B (Apache-2.0), then Chatterbox (MIT). Both return 24 kHz mono WAV, which is
  levelled and sent to the device at 12 kHz (see DEVICE_RATE).
"""
from __future__ import annotations

import array
import io
import json
import math
import time
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass, field

from tally.vultr import BASE_URL, InferenceError, _key

LISTEN_MODELS = ["granite-speech-5.0-2b", "whisper-large-v3-turbo", "qwen3-asr-1.7b"]
SPEAK_MODELS = [("qwen3-tts-12hz-1.7b", "ryan"), ("chatterbox", "default")]
# Vultr's published prices: per second of audio heard, and per second of audio spoken.
USD_PER_SECOND_HEARD = {"granite-speech-5.0-2b": 0.00000555, "whisper-large-v3-turbo": 0.00001111, "qwen3-asr-1.7b": 0.0000075}
USD_PER_SECOND_SPOKEN = 0.00028
MAX_SPOKEN_CHARS = 1500


@dataclass
class Heard:
    text: str
    model: str
    seconds: float          # how long the call took
    audio_seconds: float    # how long the recording was
    attempts: list[str] = field(default_factory=list)

    @property
    def usd(self) -> float:
        return self.audio_seconds * USD_PER_SECOND_HEARD.get(self.model, 0.0)


@dataclass
class Spoken:
    wav: bytes              # 16-bit mono PCM WAV with a plain 44-byte header
    model: str
    voice: str
    seconds: float
    audio_seconds: float
    attempts: list[str] = field(default_factory=list)

    @property
    def usd(self) -> float:
        return self.audio_seconds * USD_PER_SECOND_SPOKEN


def wav_info(data: bytes) -> tuple[int, int, int, float]:
    """(sample rate, channels, bits, seconds) of a WAV file, or ValueError if it is not a readable PCM WAV."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            rate, channels, width, frames = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
    except (wave.Error, EOFError) as e:
        raise ValueError(f"not a PCM WAV file: {e}") from None
    if not rate:
        raise ValueError("not a PCM WAV file: no sample rate")
    return rate, channels, 8 * width, frames / rate


TARGET_RMS_DBFS = -10.0   # speech this loud is clear on the desk device's small speaker
HIGHPASS_HZ = 400.0       # below this the board's 0.25 W speaker plays nothing; removing it leaves room for the voice
MAX_GAIN_DB = 18.0
KNEE = 0.5                # above this share of full scale, peaks are rounded off rather than clipped


def highpass(frames: bytes, rate: int, cutoff: float = HIGHPASS_HZ) -> bytes:
    """16-bit mono audio without what lies below `cutoff` (a first-order filter). The board's small speaker cannot
    play it, and on a voice it holds much of the energy, so taking it out lets the rest be made louder."""
    x = array.array("h", frames)
    if not x:
        return frames
    rc = 1 / (2 * math.pi * cutoff)
    a = rc / (rc + 1 / rate)
    out = array.array("h", bytes(len(frames)))
    y, prev = 0.0, x[0]
    for i, v in enumerate(x):
        y = a * (y + v - prev)
        prev = v
        out[i] = max(-32768, min(32767, int(round(y))))
    return out.tobytes()


PRESENCE_HZ, PRESENCE_DB = 2000.0, 6.0   # consonants (s, t, k) live here; small speakers lose them
COMPRESS_RATIO, COMPRESS_FROM = 5.0, 0.35   # 5:1 above 0.35 of the voice's average level
MAKEUP_MAX_DB = 30.0


def _onepole_highpass(x: list[float], rate: int, cutoff: float) -> list[float]:
    rc = 1 / (2 * math.pi * cutoff)
    a = rc / (rc + 1 / rate)
    out, y, prev = [], 0.0, x[0] if x else 0.0
    for v in x:
        y = a * (y + v - prev)
        prev = v
        out.append(y)
    return out


def for_speaker(frames: bytes, rate: int) -> bytes:
    """16-bit mono speech shaped for the desk device's small speaker, as a phone or PA system shapes a voice: the low
    end it cannot play taken out (HIGHPASS_HZ), consonants lifted (PRESENCE_DB above PRESENCE_HZ), quiet syllables
    brought up towards loud ones (a COMPRESS_RATIO compressor on the voice's envelope, 3 ms attack, 60 ms release),
    then levelled to TARGET_RMS_DBFS with a soft limiter, so nothing clips."""
    x = [float(v) for v in array.array("h", frames)]
    if not x:
        return frames
    x = _onepole_highpass(x, rate, HIGHPASS_HZ)
    lift = 10 ** (PRESENCE_DB / 20) - 1
    x = [v + lift * h for v, h in zip(x, _onepole_highpass(x, rate, PRESENCE_HZ))]
    rms = math.sqrt(sum(v * v for v in x) / len(x)) or 1.0
    attack, release = math.exp(-1 / (0.003 * rate)), math.exp(-1 / (0.06 * rate))
    env, squeezed = 0.0, []
    for v in x:
        level = abs(v)
        k = attack if level > env else release
        env = k * env + (1 - k) * level
        gain = (env / (COMPRESS_FROM * rms)) ** (1 / COMPRESS_RATIO - 1) if env > COMPRESS_FROM * rms else 1.0
        squeezed.append(v * gain)
    rms = math.sqrt(sum(v * v for v in squeezed) / len(squeezed)) / 32768 or 1e-9
    gain = min(10 ** ((TARGET_RMS_DBFS - 20 * math.log10(rms)) / 20), 10 ** (MAKEUP_MAX_DB / 20))
    out = array.array("h")
    for v in squeezed:
        y = v * gain / 32768
        m = abs(y)
        if m > KNEE:
            m = KNEE + (1 - KNEE) * math.tanh((m - KNEE) / (1 - KNEE))
        out.append(int(round(math.copysign(min(m, 1.0), y) * 32767)))
    return out.tobytes()


def louder(frames: bytes) -> bytes:
    """16-bit mono speech brought to TARGET_RMS_DBFS (raising it by at most MAX_GAIN_DB, never lowering it), with a
    soft limiter, so no sample clips. The voices came back with an average level near -23.5 dBFS; played on the
    board, that was barely audible."""
    samples = array.array("h", frames)
    if not samples:
        return frames
    rms = math.sqrt(sum(x * x for x in samples) / len(samples)) / 32768
    if rms <= 0:
        return frames
    gain = min(10 ** ((TARGET_RMS_DBFS - 20 * math.log10(rms)) / 20), 10 ** (MAX_GAIN_DB / 20))
    if gain <= 1:
        return frames
    out = array.array("h", bytes(len(frames)))
    for i, x in enumerate(samples):
        y = x * gain / 32768
        m = abs(y)
        if m > KNEE:
            m = KNEE + (1 - KNEE) * math.tanh((m - KNEE) / (1 - KNEE))
        out[i] = int(round(math.copysign(m, y) * 32767))
    return out.tobytes()


# The device downloads every answer over TLS. Its TCP window is about 5.7 KB, so from a desk in India to the host in
# New Jersey it reads about 17 KB a second: a 13-second announcement at 24 kHz (614 KB) took 36 s to arrive. At 12 kHz
# it is half the size, and speech stays clear on a small speaker.
DEVICE_RATE = 24000   # 12 kHz cut off the s and sh sounds; on a local network the size does not matter


def resample(frames: bytes, rate: int, to: int = DEVICE_RATE) -> bytes:
    """16-bit mono audio at `rate` as `to`, smoothed first so nothing above the new limit folds back in."""
    if rate <= to:
        return frames
    x = array.array("h", frames)
    if not x:
        return frames
    width = max(1, round(rate / to))           # a moving average about one output sample wide
    smooth, total = [], 0
    for i, v in enumerate(x):
        total += v
        if i >= width:
            total -= x[i - width]
        smooth.append(total / min(i + 1, width))
    shift = (width - 1) / 2                    # the average lags by half its width
    out = array.array("h")
    step = rate / to
    for n in range(int(len(x) / step)):
        pos = min(n * step + shift, len(x) - 1)
        i = int(pos)
        frac = pos - i
        nxt = smooth[min(i + 1, len(x) - 1)]
        out.append(int(round(smooth[i] + (nxt - smooth[i]) * frac)))
    return out.tobytes()


# What the device downloads: 8 kHz G.711 mu-law, 8 KB per second of speech, a sixth of 24 kHz 16-bit. The device's
# TCP window is 5,760 bytes (compiled into its Arduino core), so from India to New Jersey (277 ms) it reads at most
# about 20 KB/s, and often much less; it plays this while it downloads.
DEVICE_ULAW_RATE = 8000
_ULAW = bytes(
    (lambda s: ~((0x80 if s < 0 else 0) | (e << 4) | ((m >> (e + 3)) & 0x0F)) & 0xFF)(s)
    for s in range(-32768, 32768)
    for m in [min(abs(s), 32635) + 0x84]
    for e in [max([k for k in range(8) if m & (0x80 << k)] or [0])]
)


def ulaw_decode(u: int) -> int:
    """One G.711 mu-law byte as a 16-bit sample (as the device decodes it)."""
    u = ~u & 0xFF
    magnitude = ((((u & 0x0F) << 3) + 0x84) << ((u >> 4) & 7)) - 0x84
    return -magnitude if u & 0x80 else magnitude


def for_device(wav: bytes) -> bytes:
    """16-bit mono speech (as speak returns it) as the WAV the device downloads: 8 kHz, 8-bit mu-law, a plain
    44-byte header."""
    rate, channels, bits, _ = wav_info(wav)
    if bits != 16 or channels != 1:
        raise ValueError(f"expected 16-bit mono speech, got {bits}-bit, {channels} channel(s)")
    with wave.open(io.BytesIO(wav)) as w:
        frames = resample(w.readframes(w.getnframes()), rate, DEVICE_ULAW_RATE)
    data = bytes(_ULAW[x + 32768] for x in array.array("h", frames))
    header = (b"RIFF" + (36 + len(data)).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little")
              + (7).to_bytes(2, "little") + (1).to_bytes(2, "little") + DEVICE_ULAW_RATE.to_bytes(4, "little")
              + DEVICE_ULAW_RATE.to_bytes(4, "little") + (1).to_bytes(2, "little") + (8).to_bytes(2, "little")
              + b"data" + len(data).to_bytes(4, "little"))
    return header + data


def _plain_wav(data: bytes, for_device: bool = False) -> tuple[bytes, float]:
    """The same sound as 16-bit mono with a plain header (some encoders add chunks a small device would trip on).
    With for_device=True it is also brought to a level the device's speaker can be heard at, at DEVICE_RATE."""
    rate, channels, bits, seconds = wav_info(data)
    if bits != 16 or channels != 1:
        raise ValueError(f"expected 16-bit mono speech, got {bits}-bit, {channels} channel(s)")
    with wave.open(io.BytesIO(data)) as w:
        frames = w.readframes(w.getnframes())
    if for_device:
        frames = resample(for_speaker(frames, rate), rate)
        rate = min(rate, DEVICE_RATE)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return out.getvalue(), seconds


def _post(path: str, body: bytes, content_type: str, timeout: float) -> bytes:
    req = urllib.request.Request(BASE_URL + path, data=body, headers={"Authorization": f"Bearer {_key()}", "Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _multipart(fields: dict[str, str], name: str, data: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = b"".join(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode() for k, v in fields.items())
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def _tries(models, tries_per_model: int, call):
    """Run `call(model)` on each model in turn, a couple of times each, until one gives a usable answer."""
    attempts: list[str] = []
    for model in models:
        label = model[0] if isinstance(model, tuple) else model
        for attempt in range(tries_per_model):
            started = time.monotonic()
            try:
                result = call(model)
            except urllib.error.HTTPError as e:
                attempts.append(f"{label}: HTTP {e.code}")
                if e.code in (401, 403):
                    raise InferenceError("the inference key was refused") from None
                time.sleep(1.0 * (attempt + 1))
                continue
            except (urllib.error.URLError, TimeoutError) as e:
                attempts.append(f"{label}: {type(e).__name__}")
                continue
            except ValueError as e:
                attempts.append(f"{label}: {e}")
                continue
            attempts.append(f"{label}: ok")
            return result, label, time.monotonic() - started, attempts
    raise InferenceError("no speech model gave a usable answer: " + "; ".join(attempts))


def transcribe(wav: bytes, models: list[str] | None = None, *, timeout: float = 30.0) -> Heard:
    """What was said in a WAV recording, as text. An empty transcript is an answer, not an error."""
    _, _, _, audio_seconds = wav_info(wav)

    def call(model):
        body, ctype = _multipart({"model": model, "response_format": "json", "language": "en"}, "question.wav", wav, "audio/wav")
        d = json.loads(_post("/audio/transcriptions", body, ctype, timeout))
        if not isinstance(d, dict) or not isinstance(d.get("text"), str):
            raise ValueError("no text in the reply")
        return d["text"].strip()

    text, model, seconds, attempts = _tries(models or LISTEN_MODELS, 2, call)
    return Heard(text, model, seconds, audio_seconds, attempts)


def speak(text: str, models: list[tuple[str, str]] | None = None, *, timeout: float = 60.0) -> Spoken:
    """Text as speech, a WAV file the device can play as it is."""
    text = " ".join(text.split())[:MAX_SPOKEN_CHARS]
    if not text:
        raise ValueError("nothing to say")

    def call(model_voice):
        model, voice = model_voice
        data = _post("/audio/speech", json.dumps({"model": model, "voice": voice, "input": text, "response_format": "wav"}).encode(),
                     "application/json", timeout)
        return _plain_wav(data, for_device=True), voice

    ((wav, audio_seconds), voice), model, seconds, attempts = _tries(models or SPEAK_MODELS, 2, call)
    return Spoken(wav, model, voice, seconds, audio_seconds, attempts)
