import tasks.transcribe as tr


class _FakeResp:
    status_code = 200
    text = ""
    def __init__(self, payload): self._p = payload
    def json(self): return self._p


class _FakeClient:
    def __init__(self, payload): self._p = payload
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, *a, **k): return _FakeResp(self._p)


def test_elevenlabs_merges_words_into_speaker_turns(monkeypatch, tmp_path):
    payload = {"text": "x", "audio_duration_secs": 1.2, "words": [
        {"type": "word", "text": "Здравствуйте", "start": 0.0, "end": 0.5, "speaker_id": "speaker_0"},
        {"type": "spacing", "text": " "},
        {"type": "word", "text": "доктор", "start": 0.5, "end": 0.9, "speaker_id": "speaker_0"},
        {"type": "word", "text": "Да", "start": 1.0, "end": 1.2, "speaker_id": "speaker_1"},
    ]}
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(tr.httpx, "Client", lambda *a, **k: _FakeClient(payload))
    audio = tmp_path / "a.wav"; audio.write_bytes(b"RIFF")
    segs = tr._transcribe_elevenlabs(str(audio))
    assert segs == [
        {"speaker": "speaker_0", "start": 0.0, "end": 0.9, "text": "Здравствуйте доктор"},
        {"speaker": "speaker_1", "start": 1.0, "end": 1.2, "text": "Да"},
    ]


def test_elevenlabs_requires_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    audio = tmp_path / "a.wav"; audio.write_bytes(b"x")
    import pytest
    with pytest.raises(RuntimeError):
        tr._transcribe_elevenlabs(str(audio))


def test_dispatch_falls_back_to_next_engine(monkeypatch):
    calls = []
    monkeypatch.setattr(tr, "_transcribe_elevenlabs", lambda p, **k: (_ for _ in ()).throw(RuntimeError("no key")))
    monkeypatch.setattr(tr, "_transcribe_assemblyai", lambda p, **k: calls.append("aai") or [{"speaker": "A", "start": 0, "end": 1, "text": "ok"}])
    out = tr.transcribe_audio("/x.wav", engine_override="elevenlabs")
    assert calls == ["aai"] and out[0]["text"] == "ok"
