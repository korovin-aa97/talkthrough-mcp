"""Deterministic word-level speaker splitting and fallback boundaries."""

from __future__ import annotations

import logging

import pytest

from talkthrough_mcp.core.diarize import Turn, attribute_segments_with_words
from talkthrough_mcp.core.stt import SttSegment, SttWord


def _segment(text: str = " Hello world.") -> SttSegment:
    return SttSegment(seq=7, t0_ms=0, t1_ms=2000, text=text)


def test_boundary_inside_a_word_assigns_the_whole_token_by_max_overlap() -> None:
    words = [SttWord(0, 1000, " Hello")]
    turns = [Turn(0, 400, "S1"), Turn(400, 1000, "S2")]
    split = attribute_segments_with_words([_segment("Hello")], words, turns)
    assert [(item.text, item.speaker, item.t0_ms, item.t1_ms) for item in split] == [
        ("Hello", "S2", 0, 1000)
    ]


def test_boundary_between_words_splits_without_loss_or_duplication() -> None:
    words = [SttWord(0, 700, " Hello"), SttWord(700, 1400, " world.")]
    turns = [Turn(0, 700, "S1"), Turn(700, 1400, "S2")]
    split = attribute_segments_with_words([_segment()], words, turns)
    assert [(item.seq, item.text, item.speaker) for item in split] == [
        (1, "Hello", "S1"),
        (2, "world.", "S2"),
    ]
    assert " ".join(item.text for item in split) == "Hello world."
    assert [item.source_seq for item in split] == [7, 7]


def test_repeated_amend_can_merge_a_previous_word_boundary() -> None:
    words = [SttWord(0, 700, " Hello"), SttWord(700, 1400, " world.")]
    first = attribute_segments_with_words(
        [_segment()],
        words,
        [Turn(0, 700, "S1"), Turn(700, 1400, "S2")],
    )
    assert [(item.text, item.speaker) for item in first] == [
        ("Hello", "S1"),
        ("world.", "S2"),
    ]

    amended = attribute_segments_with_words(first, words, [Turn(0, 1400, "S1")])
    assert [(item.text, item.speaker, item.source_seq) for item in amended] == [
        ("Hello world.", "S1", 7)
    ]


def test_exact_ties_prefer_earlier_turn_then_lower_numeric_label() -> None:
    word = SttWord(0, 1000, " Tie")
    earlier = attribute_segments_with_words(
        [_segment("Tie")], [word], [Turn(500, 1000, "S1"), Turn(0, 500, "S2")]
    )
    assert earlier[0].speaker == "S2"
    lower_label = attribute_segments_with_words(
        [_segment("Tie")], [word], [Turn(0, 1000, "S2"), Turn(0, 1000, "S1")]
    )
    assert lower_label[0].speaker == "S1"


def test_overlapping_turns_choose_max_overlap_and_gaps_stay_unknown() -> None:
    words = [
        SttWord(200, 900, " overlap"),
        SttWord(1200, 1500, " gap"),
    ]
    turns = [Turn(0, 500, "S1"), Turn(300, 1000, "S2")]
    split = attribute_segments_with_words([_segment("overlap gap")], words, turns)
    assert [(item.text, item.speaker) for item in split] == [
        ("overlap", "S2"),
        ("gap", None),
    ]
    no_turns = attribute_segments_with_words([_segment("overlap gap")], words, [])
    assert [(item.text, item.speaker) for item in no_turns] == [("overlap gap", None)]


def test_segment_without_words_uses_whole_segment_fallback() -> None:
    segments = [
        SttSegment(1, 0, 1000, "No words here"),
        SttSegment(2, 1000, 2000, " Has words"),
    ]
    split = attribute_segments_with_words(
        segments,
        [SttWord(1000, 1800, " Has words")],
        [Turn(0, 900, "S1"), Turn(1000, 2000, "S2")],
    )
    assert [(item.text, item.speaker) for item in split] == [
        ("No words here", "S1"),
        ("Has words", "S2"),
    ]


def test_raw_word_concatenation_preserves_internal_spacing_and_punctuation() -> None:
    words = [
        SttWord(0, 200, "  Hello"),
        SttWord(200, 300, ","),
        SttWord(300, 700, "   world"),
        SttWord(700, 800, "!  "),
    ]
    split = attribute_segments_with_words(
        [_segment("Hello,   world!")], words, [Turn(0, 1000, "S1")]
    )
    assert split[0].text == "Hello,   world!"


def test_invalid_word_timestamp_falls_back_for_the_transcript(
    caplog: pytest.LogCaptureFixture,
) -> None:
    words = [SttWord(0, 500, " partial"), SttWord(800, 700, " broken")]
    with caplog.at_level(logging.WARNING):
        split = attribute_segments_with_words(
            [_segment("Full intact segment")], words, [Turn(0, 2000, "S1")]
        )
    assert [(item.text, item.speaker) for item in split] == [("Full intact segment", "S1")]
    assert "invalid word timestamp" in caplog.text


def test_word_outside_every_source_segment_falls_back_without_text_loss(
    caplog: pytest.LogCaptureFixture,
) -> None:
    words = [SttWord(0, 500, " partial"), SttWord(2500, 2800, " stray")]
    with caplog.at_level(logging.WARNING):
        split = attribute_segments_with_words(
            [_segment("Full intact segment")], words, [Turn(0, 2000, "S1")]
        )
    assert [(item.text, item.speaker) for item in split] == [("Full intact segment", "S1")]
    assert "outside every source segment" in caplog.text
