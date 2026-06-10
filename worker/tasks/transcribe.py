"""
Транскрипция аудио.
Движки:
  - whisper (default): локальный faster-whisper, бесплатно
  - assemblyai: облачный API, платный, fallback
Выбор через env ASR_ENGINE=whisper|assemblyai
"""
import logging
import os

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


# ── Public API ──────────────────────────────────────────────────

def transcribe_audio(audio_path: str, word_boost: list[str] | None = None, engine_override: str | None = None) -> list[dict]:
    """
    Транскрибирует аудиофайл выбранным движком.
    При ошибке основного движка пробует fallback.

    Args:
        audio_path: Путь к аудиофайлу
        word_boost: Список слов/фраз для улучшения распознавания (AssemblyAI)
        engine_override: Принудительный выбор движка (иначе из env ASR_ENGINE)
    """
    engine = engine_override or os.getenv("ASR_ENGINE", "whisper")

    def _primary(path):
        if engine == "assemblyai":
            return _transcribe_assemblyai(path, word_boost=word_boost)
        return _transcribe_whisper(path)

    def _fallback(path):
        if engine == "assemblyai":
            return _transcribe_whisper(path)
        return _transcribe_assemblyai(path, word_boost=word_boost)

    try:
        return _primary(audio_path)
    except Exception as e:
        fallback_name = "whisper" if engine == "assemblyai" else "assemblyai"
        logger.warning(f"Primary ASR ({engine}) failed: {e}. Trying fallback ({fallback_name})...")
        try:
            return _fallback(audio_path)
        except Exception as e2:
            logger.error(f"Fallback ASR ({fallback_name}) also failed: {e2}")
            raise RuntimeError(f"Both ASR engines failed. Primary: {e}, Fallback: {e2}") from e2
