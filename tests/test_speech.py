"""Speech is made loud enough for the desk device's small speaker, without clipping."""
import array
import math

from tally.speech import MAX_GAIN_DB, TARGET_RMS_DBFS, louder


def _dbfs(samples) -> float:
    return 20 * math.log10(math.sqrt(sum(x * x for x in samples) / len(samples)) / 32768)


def _quiet_speech(seconds: float = 2.0, rate: int = 24000) -> array.array:
    """Bursts of tone with silence between and short loud consonants, about -24 dBFS on average but peaking near
    -6 dBFS, like the voices."""
    out = array.array("h")
    for i in range(int(seconds * rate)):
        t = i / rate
        envelope = 1.0 if (t * 4) % 1 < 0.35 else 0.0
        spike = 2.6 if (t * 4) % 1 < 0.004 else 1.0
        out.append(int(6000 * spike * envelope * math.sin(2 * math.pi * 220 * t) * (0.35 + 0.65 * abs(math.sin(2 * math.pi * 3 * t)))))
    return out


def test_quiet_speech_is_brought_up_to_the_target_without_clipping():
    quiet = _quiet_speech()
    loud = array.array("h", louder(quiet.tobytes()))
    assert _dbfs(quiet) < TARGET_RMS_DBFS - 4
    assert abs(_dbfs(loud) - TARGET_RMS_DBFS) < 2.0
    assert max(abs(x) for x in loud) <= 32767
    assert sum(1 for x in loud if abs(x) >= 32767) == 0   # rounded off by the limiter, never flat-topped


def test_loud_speech_is_left_alone_and_the_gain_is_capped():
    loud = array.array("h", (int(30000 * math.sin(i / 7)) for i in range(24000)))
    assert louder(loud.tobytes()) == loud.tobytes()
    whisper = array.array("h", (int(20 * math.sin(i / 7)) for i in range(24000)))
    raised = array.array("h", louder(whisper.tobytes()))
    assert _dbfs(raised) - _dbfs(whisper) <= MAX_GAIN_DB + 0.1
    assert louder(b"") == b""


def test_speech_for_the_device_is_resampled_to_half_the_size():
    from tally.speech import DEVICE_RATE, resample
    rate = 24000
    tone = array.array("h", (int(12000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate)))
    half = array.array("h", resample(tone.tobytes(), rate))
    assert abs(len(half) - DEVICE_RATE) <= 1                              # one second at the device's rate
    assert abs(_dbfs(half) - _dbfs(tone)) < 1.0                           # a 440 Hz voice keeps its level
    hiss = array.array("h", (12000 if i % 2 else -12000 for i in range(rate)))   # 12 kHz: above the new limit
    assert _dbfs(array.array("h", resample(hiss.tobytes(), rate))) < _dbfs(hiss) - 20
    assert resample(tone.tobytes(), DEVICE_RATE) == tone.tobytes()


def test_the_device_gets_8_khz_mu_law_that_decodes_back_to_the_speech():
    import io
    import wave
    from tally.speech import DEVICE_ULAW_RATE, for_device, ulaw_decode
    rate = 12000
    voice = array.array("h", (int(9000 * math.sin(2 * math.pi * 300 * i / rate) + 3000 * math.sin(2 * math.pi * 900 * i / rate))
                              for i in range(rate * 2)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate), w.writeframes(voice.tobytes())
    out = for_device(buf.getvalue())
    assert out[:4] == b"RIFF" and out[8:16] == b"WAVEfmt " and out[36:40] == b"data" and len(out) == 44 + 2 * DEVICE_ULAW_RATE
    assert int.from_bytes(out[20:22], "little") == 7 and int.from_bytes(out[24:28], "little") == DEVICE_ULAW_RATE
    assert int.from_bytes(out[34:36], "little") == 8
    decoded = [ulaw_decode(b) for b in out[44:]]
    assert max(abs(x) for x in decoded) > 8000                       # loud enough to hear, not squashed
    assert all(ulaw_decode(b) == -ulaw_decode(b ^ 0x80) or b in (0x7F, 0xFF) for b in range(256))
    assert abs(ulaw_decode(0xFF)) <= 8 and abs(ulaw_decode(0x80)) >= 32000   # silence and full scale
