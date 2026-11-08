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
