"""trim_vocabulary_echo: drop initial_prompt echoes, never live speech.

The real case (73-min RU meeting, v0.2.1 battery): whisper replayed the
attendee vocabulary over the quiet opening seconds — two segments of
nothing but name repeats swallowed the actual first words. The guard the
plan demands: a live roll-call that HAPPENS to list the same names must
survive, because real speech carries verbs and prepositions.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from talkthrough_mcp.core.stt import (
    VOCAB_ECHO_WINDOW_MS,
    SttSegment,
    transcribe,
    trim_vocabulary_echo,
)

VOCABULARY = "Анастасия, Диана, Влад, Евгений, Александр"


def seg(seq: int, t0_ms: int, text: str, t1_ms: int | None = None) -> SttSegment:
    return SttSegment(seq=seq, t0_ms=t0_ms, t1_ms=t1_ms or (t0_ms + 2000), text=text)


def test_repeated_name_echo_is_trimmed() -> None:
    segments = [
        seg(1, 0, "Анастасия, Диана, Анастасия, Диана, Анастасия, Диана."),
        seg(2, 3000, "Евгений мне сообщил, что вроде как Клод вы получили доступ."),
    ]
    kept, trimmed = trim_vocabulary_echo(segments, VOCABULARY)
    assert [s.seq for s in kept] == [2]
    assert [s.seq for s in trimmed] == [1]


def test_verbatim_vocabulary_prefix_is_trimmed() -> None:
    segments = [
        seg(1, 500, "Анастасия, Диана, Влад, Евгений."),
        seg(2, 4000, "Начинаем встречу."),
    ]
    kept, trimmed = trim_vocabulary_echo(segments, VOCABULARY)
    assert [s.seq for s in kept] == [2]
    assert len(trimmed) == 1


def test_vocab_order_subsequence_echo_is_trimmed() -> None:
    """The REAL echo shape observed on the 73-min meeting re-run: one pass
    over the list with names dropped — no repeats, not a strict prefix, but
    the vocabulary's own order, which live speech has no reason to follow."""
    vocabulary = "Анастасия, Евгений, Владислав, Дмитрий, Алексей, Диана"
    segments = [
        seg(1, 1680, "Анастасия, Дмитрий, Алексей, Диана"),
        seg(2, 61680, "Дальше про строительные блоки."),
    ]
    kept, trimmed = trim_vocabulary_echo(segments, vocabulary)
    assert [s.seq for s in kept] == [2]
    assert [s.seq for s in trimmed] == [1]


def test_names_out_of_vocabulary_order_survive() -> None:
    # Live addressing lists people in the speaker's order, not the list's —
    # a reversed-order trio must NOT look like an echo.
    segments = [seg(1, 2000, "Влад, Диана, Анастасия?")]
    kept, trimmed = trim_vocabulary_echo(segments, VOCABULARY)
    assert kept == segments
    assert trimmed == []


def test_live_roll_call_with_connecting_words_survives() -> None:
    """The mandatory guard: real speech listing the same names is NOT echo —
    verbs/prepositions push the vocabulary fraction under the bar."""
    segments = [
        seg(1, 1000, "На встрече присутствуют Анастасия, Диана и Влад."),
        seg(2, 5000, "Переходим к повестке."),
    ]
    kept, trimmed = trim_vocabulary_echo(segments, VOCABULARY)
    assert [s.seq for s in kept] == [1, 2]
    assert trimmed == []


def test_echo_shaped_segment_after_the_window_survives() -> None:
    late = VOCAB_ECHO_WINDOW_MS + 5000
    segments = [seg(1, late, "Анастасия, Диана, Анастасия, Диана, Анастасия.")]
    kept, trimmed = trim_vocabulary_echo(segments, VOCABULARY)
    assert kept == segments
    assert trimmed == []


def test_short_pure_name_mention_survives() -> None:
    # One or two vocabulary tokens with no repeats: a real vocative
    # («Анастасия?»), not an echo — below the prefix minimum.
    segments = [seg(1, 2000, "Анастасия?"), seg(2, 6000, "Анастасия, Диана?")]
    kept, trimmed = trim_vocabulary_echo(segments, VOCABULARY)
    assert [s.seq for s in kept] == [1, 2]
    assert trimmed == []


def test_empty_vocabulary_trims_nothing() -> None:
    segments = [seg(1, 0, "Анастасия, Диана, Анастасия, Диана, Анастасия.")]
    assert trim_vocabulary_echo(segments, "  ,  ") == (segments, [])


def test_yo_normalization_applies_to_vocabulary_matching() -> None:
    segments = [seg(1, 0, "Артем, Семен, Артем, Семен, Артем, Семен.")]
    kept, trimmed = trim_vocabulary_echo(segments, "Артём, Семён")
    assert kept == []
    assert len(trimmed) == 1


def test_transcribe_trims_echo_words_with_their_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talkthrough_mcp.core import stt

    raw = [
        SimpleNamespace(
            start=0.0,
            end=2.0,
            text=" Анастасия, Диана, Анастасия, Диана, Анастасия.",
            words=[
                SimpleNamespace(start=0.0, end=0.5, word=" Анастасия"),
                SimpleNamespace(start=0.5, end=1.0, word=", Диана"),
            ],
        ),
        SimpleNamespace(
            start=3.0,
            end=5.0,
            text=" Начинаем встречу.",
            words=[
                SimpleNamespace(start=3.0, end=3.8, word=" Начинаем"),
                SimpleNamespace(start=3.8, end=5.0, word=" встречу."),
            ],
        ),
    ]

    class FakeModel:
        def transcribe(self, path: str, **kwargs: object):  # type: ignore[no-untyped-def]
            assert kwargs["word_timestamps"] is True
            return iter(raw), SimpleNamespace(language="ru", language_probability=0.99)

    monkeypatch.setattr(stt, "_load_model", lambda name: FakeModel())
    result = transcribe(
        Path("unused.wav"),
        model_name="tiny",
        vocabulary=VOCABULARY,
        word_timestamps=True,
    )
    assert [segment.text for segment in result.segments] == ["Начинаем встречу."]
    assert [word.text for word in result.words] == [" Начинаем", " встречу."]
    assert result.vocabulary_echo_trimmed == 1


def test_transcribe_does_not_request_or_store_words_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talkthrough_mcp.core import stt

    raw = [
        SimpleNamespace(
            start=0.0,
            end=1.0,
            text=" Plain transcript.",
            words=[SimpleNamespace(start=0.0, end=1.0, word=" Plain transcript.")],
        )
    ]

    class FakeModel:
        def transcribe(self, path: str, **kwargs: object):  # type: ignore[no-untyped-def]
            assert "word_timestamps" not in kwargs
            return iter(raw), SimpleNamespace(language="en", language_probability=0.9)

    monkeypatch.setattr(stt, "_load_model", lambda name: FakeModel())
    result = transcribe(Path("unused.wav"), model_name="tiny")
    assert result.words == ()
    assert [segment.text for segment in result.segments] == ["Plain transcript."]
