import numpy as np

from relayiq.voice.audio import Downsampler24kTo8k, frames, mulaw_to_pcm16, pcm16_to_mulaw
from relayiq.voice.chunker import SentenceChunker


def test_mulaw_roundtrip_small_error():
    x = (8000 * np.sin(np.linspace(0, 50, 4000))).astype(np.int16)
    back = mulaw_to_pcm16(pcm16_to_mulaw(x))
    assert np.max(np.abs(back.astype(int) - x.astype(int))) < 300


def test_streaming_downsampler_length_and_tone():
    t = np.arange(24000) / 24000
    sig = (8000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16).tobytes()
    d = Downsampler24kTo8k()
    out = np.concatenate([d.process(sig[i:i + 4801]) for i in range(0, len(sig), 4801)])
    assert abs(len(out) - 8000) <= 2
    assert out[200:].max() > 7000  # 440 Hz passes the 3.6 kHz low-pass


def test_frames_are_20ms():
    assert [len(f) for f in frames(b"\x00" * 400)] == [160, 160, 80]


def test_chunker_sends_a_short_first_sentence_immediately_and_skips_abbrev():
    c = SentenceChunker()
    out = []
    for t in ["Got it. ", "You're with Dr. ", "Chen at 9 a.m. tomorrow. Anything ", "else?"]:
        out += c.push(t)
    out += c.flush()
    assert out == ["Got it.", "You're with Dr. Chen at 9 a.m. tomorrow.", "Anything else?"]


def test_chunker_first_chunk_can_be_a_clause():
    c = SentenceChunker()
    assert c.push("Sure, let me pull up the schedule, ") == ["Sure, let me pull up the schedule,"]
    assert c.push("and check Dr. Chen's mornings") == []  # later chunks wait for a full sentence


def test_chunker_does_not_emit_tiny_fragments_first():
    c = SentenceChunker()
    assert c.push("Ok. ") == []  # under 6 chars: wait for more
