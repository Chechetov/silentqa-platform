"""
Транскрипция аудио.
Движки — упорядоченная цепочка фолбэков [ASR_ENGINE] + [elevenlabs, assemblyai, whisper]:
  - elevenlabs: облачный Scribe v2, встроенная диаризация — основной в проде
  - assemblyai: облачный API, word_boost
  - whisper: локальный faster-whisper (CPU) — только последний фолбэк,
    заметно хуже по качеству; фактический прогон через whisper — красный флаг
Выбор primary — env ASR_ENGINE (код-дефолт whisper обманчив: деплои ставят облачный).
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)


# ── Whisper (local) ─────────────────────────────────────────────

_model = None


def _get_whisper_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        model_size = os.getenv("WHISPER_MODEL", "large-v3")
        compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
        device = os.getenv("WHISPER_DEVICE", "cpu")
        cpu_threads = int(os.getenv("WHISPER_THREADS", "8"))

        logger.info(f"Loading faster-whisper model: {model_size} ({compute_type}) on {device}...")
        _model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
        )
        logger.info("Model loaded successfully!")
    return _model


def _transcribe_whisper(audio_path: str) -> list[dict]:
    model = _get_whisper_model()
    language = os.getenv("WHISPER_LANGUAGE", "ru")

    segments, info = model.transcribe(
        audio_path,
        language=language,
        beam_size=5,
        best_of=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
        word_timestamps=True,
        condition_on_previous_text=True,
    )

    logger.info(f"Detected language: {info.language} (prob: {info.language_probability:.2f})")
    logger.info(f"Audio duration: {info.duration:.1f}s")

    result = []
    for segment in segments:
        result.append({
            "start": round(segment.start, 2),
            "end": round(segment.end, 2),
            "text": segment.text.strip(),
            "words": [
                {
                    "start": round(w.start, 2),
                    "end": round(w.end, 2),
                    "word": w.word.strip(),
                    "probability": round(w.probability, 3),
                }
                for w in (segment.words or [])
            ],
        })

    logger.info(f"Transcribed {len(result)} segments, {sum(len(s['text'].split()) for s in result)} words")
    return result


# ── AssemblyAI (cloud) ──────────────────────────────────────────

def _transcribe_assemblyai(audio_path: str, word_boost: list[str] | None = None) -> list[dict]:
    import assemblyai as aai

    api_key = os.getenv("ASSEMBLYAI_API_KEY")
    if not api_key:
        raise RuntimeError("ASSEMBLYAI_API_KEY not set")

    aai.settings.api_key = api_key

    config_kwargs = {
        "speech_models": ["universal-3-pro", "universal-2"],
        "language_detection": True,
        "speaker_labels": True,
    }
    if word_boost:
        config_kwargs["word_boost"] = word_boost
        config_kwargs["boost_param"] = "high"
        logger.info(f"AssemblyAI word_boost: {len(word_boost)} terms")

    config = aai.TranscriptionConfig(**config_kwargs)

    logger.info(f"Sending {audio_path} to AssemblyAI...")
    transcript = aai.Transcriber(config=config).transcribe(audio_path)

    if transcript.status == "error":
        raise RuntimeError(f"AssemblyAI transcription failed: {transcript.error}")

    logger.info(f"AssemblyAI done. Words: {len(transcript.words or [])}, Utterances: {len(transcript.utterances or [])}")

    # Convert to our standard segment format
    result = []
    for utt in (transcript.utterances or []):
        result.append({
            "start": round(utt.start / 1000, 2),
            "end": round(utt.end / 1000, 2),
            "text": utt.text.strip(),
            "speaker": utt.speaker,
            "words": [
                {
                    "start": round(w.start / 1000, 2),
                    "end": round(w.end / 1000, 2),
                    "word": w.text.strip(),
                    "confidence": round(w.confidence, 3),
                }
                for w in (utt.words or [])
            ],
        })

    # Fallback: if no utterances, use sentences
    if not result and transcript.text:
        result.append({
            "start": 0.0,
            "end": (transcript.audio_duration or 0) / 1000,
            "text": transcript.text,
            "words": [],
        })

    logger.info(f"AssemblyAI: {len(result)} segments")
    return result


# ── ElevenLabs Scribe v2 (cloud, diarization built-in) ──────────

def _transcribe_elevenlabs(audio_path: str) -> list[dict]:
    """ElevenLabs Scribe v2: ru + speaker diarization. Returns assemblyai-shaped
    segments {speaker,start,end,text} (so the pipeline skips diarization)."""
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY not set")
    model = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2")
    language = os.getenv("ELEVENLABS_LANGUAGE", "ru")
    logger.info(f"Sending {audio_path} to ElevenLabs ({model}, {language})...")
    with open(audio_path, "rb") as fh:
        files = {
            "file": (os.path.basename(audio_path), fh, "application/octet-stream"),
            "model_id": (None, model),
            "language_code": (None, language),
            "diarize": (None, "true"),
        }
        with httpx.Client(timeout=900) as client:
            resp = client.post("https://api.elevenlabs.io/v1/speech-to-text",
                               headers={"xi-api-key": api_key}, files=files)
    if resp.status_code != 200:
        raise RuntimeError(f"ElevenLabs STT failed: HTTP {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    segments: list[dict] = []
    for w in (data.get("words") or []):
        text = w.get("text", "")
        if w.get("type", "word") != "word":          # spacing / audio_event -> append
            if segments:
                segments[-1]["text"] += text
            continue
        spk = w.get("speaker_id") or "speaker_0"
        start, end = round(w.get("start", 0.0), 2), round(w.get("end", 0.0), 2)
        if segments and segments[-1]["speaker"] == spk:
            segments[-1]["text"] += text
            segments[-1]["end"] = end
        else:
            segments.append({"speaker": spk, "start": start, "end": end, "text": text})
    for s in segments:
        s["text"] = " ".join(s["text"].split())
    segments = [s for s in segments if s["text"]]
    if not segments and data.get("text"):
        segments = [{"speaker": "speaker_0", "start": 0.0,
                     "end": round(data.get("audio_duration_secs", 0.0) or 0.0, 2),
                     "text": data["text"].strip()}]
    logger.info(f"ElevenLabs: {len(segments)} segments")
    return segments


# ── Public API ──────────────────────────────────────────────────

def transcribe_audio(audio_path: str, word_boost: list[str] | None = None, engine_override: str | None = None, fallback: bool = True) -> list[dict]:
    """
    Транскрибирует аудиофайл выбранным движком.
    При ошибке основного движка пробует fallback (если fallback=True).

    Args:
        audio_path: Путь к аудиофайлу
        word_boost: Список слов/фраз для улучшения распознавания (AssemblyAI)
        engine_override: Принудительный выбор движка (иначе из env ASR_ENGINE)
        fallback: Пробовать ли другие движки при ошибке. Для честного сравнения
            движков (ASR-тюнинг) ставится False — нужен ровно запрошенный движок.
    """
    engine = engine_override or os.getenv("ASR_ENGINE", "whisper")

    def _run(name: str) -> list[dict]:
        if name == "elevenlabs":
            return _transcribe_elevenlabs(audio_path)
        if name == "assemblyai":
            return _transcribe_assemblyai(audio_path, word_boost=word_boost)
        return _transcribe_whisper(audio_path)

    if not fallback:
        return _run(engine)

    default_chain = ["elevenlabs", "assemblyai", "whisper"]
    chain = [engine] + [e for e in default_chain if e != engine]
    last_err: Exception | None = None
    for i, name in enumerate(chain):
        try:
            return _run(name)
        except Exception as e:
            last_err = e
            nxt = chain[i + 1] if i + 1 < len(chain) else None
            logger.warning(f"ASR engine '{name}' failed: {e}." +
                           (f" Trying '{nxt}'..." if nxt else " No more engines."))
    raise RuntimeError(f"All ASR engines failed. Last error: {last_err}") from last_err
