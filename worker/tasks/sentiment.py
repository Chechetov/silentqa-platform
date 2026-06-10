"""
Sentiment analysis на русском языке.
Использует rubert-tiny2-russian-sentiment — модель, обученную на русских текстах.
Классы: neutral, positive, negative
"""
import logging

from transformers import pipeline

logger = logging.getLogger(__name__)

_classifier = None


def get_classifier():
    """Lazy-загрузка sentiment модели."""
    global _classifier
    if _classifier is None:
        logger.info("Loading Russian sentiment model (rubert-tiny2-russian-sentiment)...")
        _classifier = pipeline(
            "text-classification",
            model="seara/rubert-tiny2-russian-sentiment",
            tokenizer="seara/rubert-tiny2-russian-sentiment",
            device=-1,  # CPU
            max_length=512,
            truncation=True,
        )
        logger.info("Sentiment model loaded!")

    return _classifier


def analyze_sentiment(transcript: list[dict]) -> list[dict]:
    """
    Анализирует тональность каждого сегмента транскрипта.

    Args:
        transcript: Список сегментов с полями start, end, text, speaker

    Returns:
        Список результатов: [{
            "start": 0.0,
            "end": 2.5,
            "speaker": "SPEAKER_00",
            "text": "Привет, как дела?",
            "sentiment": "positive",
            "sentiment_score": 0.95
        }, ...]
    """
    classifier = get_classifier()

    # Собираем тексты для батч-обработки
    texts = [s["text"] for s in transcript if s.get("text", "").strip()]

    if not texts:
        return []

    # Батч-классификация — быстрее чем по одному
    logger.info(f"Analyzing sentiment for {len(texts)} segments...")
    predictions = classifier(texts, batch_size=32)

    results = []
    pred_idx = 0
    for segment in transcript:
        text = segment.get("text", "").strip()
        if not text:
            continue

        pred = predictions[pred_idx]
        pred_idx += 1

        results.append({
            "start": segment["start"],
            "end": segment["end"],
            "speaker": segment.get("speaker", "UNKNOWN"),
            "text": text,
            "sentiment": pred["label"].lower(),
            "sentiment_score": round(pred["score"], 3),
        })

    # Статистика
    sentiment_counts = {}
    for r in results:
        s = r["sentiment"]
        sentiment_counts[s] = sentiment_counts.get(s, 0) + 1

    logger.info(f"Sentiment analysis complete: {sentiment_counts}")

    return results


def get_speaker_sentiment_summary(sentiment_results: list[dict]) -> dict:
    """
    Сводка по тональности для каждого спикера.

    Returns:
        {
            "SPEAKER_00": {"positive": 5, "negative": 2, "neutral": 10, "avg_score": 0.72},
            "SPEAKER_01": {...}
        }
    """
    speakers = {}
    for r in sentiment_results:
        speaker = r["speaker"]
        if speaker not in speakers:
            speakers[speaker] = {"positive": 0, "negative": 0, "neutral": 0, "scores": []}

        sentiment = r["sentiment"]
        if sentiment in speakers[speaker]:
            speakers[speaker][sentiment] += 1
        speakers[speaker]["scores"].append(r["sentiment_score"])

    # Средний score
    for speaker, data in speakers.items():
        scores = data.pop("scores")
        data["avg_score"] = round(sum(scores) / len(scores), 3) if scores else 0
        data["total_segments"] = sum(v for k, v in data.items() if k != "avg_score")

    return speakers
