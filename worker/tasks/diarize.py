"""
Speaker diarization через pyannote.audio.
Определяет кто и когда говорит в аудиозаписи.
"""
import logging
import os

import torch
from pyannote.audio import Pipeline

logger = logging.getLogger(__name__)

_pipeline = None


def get_pipeline() -> Pipeline:
    """Lazy-загрузка pyannote pipeline."""
    global _pipeline
    if _pipeline is None:
        hf_token = os.getenv("HF_TOKEN")
        if not hf_token:
            raise ValueError(
                "HF_TOKEN is required for pyannote. "
                "Get one at https://huggingface.co/settings/tokens "
                "and accept the license at https://huggingface.co/pyannote/speaker-diarization-3.1"
            )

        logger.info("Loading pyannote speaker diarization pipeline...")
        _pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=hf_token,
        )

        # CPU — без GPU
        _pipeline.to(torch.device("cpu"))
        logger.info("Pyannote pipeline loaded!")

    return _pipeline


def diarize_audio(
    audio_path: str,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
) -> list[dict]:
    """
    Дiarизация аудиофайла.

    Args:
        audio_path: Путь к WAV файлу (16kHz mono)
        min_speakers: Минимальное ожидаемое количество спикеров
        max_speakers: Максимальное ожидаемое количество спикеров

    Returns:
        Список сегментов: [{"start": 0.0, "end": 5.2, "speaker": "SPEAKER_00"}, ...]
    """
    pipeline = get_pipeline()

    # Параметры дiarизации
    kwargs = {}
    if min_speakers is not None:
        kwargs["min_speakers"] = min_speakers
    if max_speakers is not None:
        kwargs["max_speakers"] = max_speakers

    logger.info(f"Running diarization on {audio_path}...")
    diarization = pipeline(audio_path, **kwargs)

    result = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        result.append({
            "start": round(turn.start, 2),
            "end": round(turn.end, 2),
            "speaker": speaker,
        })

    speakers = set(s["speaker"] for s in result)
    logger.info(f"Diarization complete: {len(result)} segments, {len(speakers)} speakers: {speakers}")

    return result
