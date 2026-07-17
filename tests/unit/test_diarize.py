"""Attribution math, label determinism, roster/range queries, env, model cache."""

from __future__ import annotations

import hashlib
import sys
import tarfile
import wave
from pathlib import Path

import pytest

from talkthrough_mcp.core import diarize
from talkthrough_mcp.core.diarize import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_SEGMENTATION_MODEL,
    DEFAULT_THRESHOLD,
    EMBEDDING_MODELS,
    SEGMENTATION_MODELS,
    Diarization,
    ModelSpec,
    SpeakerStat,
    Turn,
    attribute_segments,
    clustering_threshold,
    create_diarizer,
    diarization_threads,
    diarize_default,
    ensure_model_file,
    load_wav_float32,
    models_root,
    relabel_turns,
    resolve_model,
    speaker_roster,
    speakers_in_range,
)
from talkthrough_mcp.core.errors import ToolFailureError, ValidationError
from talkthrough_mcp.core.stt import SttSegment


def seg(seq: int, t0_ms: int, t1_ms: int, speaker: str | None = None) -> SttSegment:
    return SttSegment(seq=seq, t0_ms=t0_ms, t1_ms=t1_ms, text=f"segment {seq}", speaker=speaker)


# --- relabel_turns ------------------------------------------------------------


def test_relabel_orders_labels_by_first_appearance_not_cluster_id() -> None:
    turns = relabel_turns([(0, 1000, 7), (1500, 2500, 3), (3000, 4000, 7)])
    assert turns == [
        Turn(0, 1000, "S1"),
        Turn(1500, 2500, "S2"),
        Turn(3000, 4000, "S1"),
    ]


def test_relabel_sorts_unordered_input_by_time() -> None:
    turns = relabel_turns([(5000, 6000, 1), (0, 1000, 2)])
    assert [turn.speaker for turn in turns] == ["S1", "S2"]
    assert turns[0].t0_ms == 0


def test_relabel_supports_double_digit_speakers() -> None:
    raw = [(i * 1000, i * 1000 + 500, i) for i in range(12)]
    turns = relabel_turns(raw)
    assert turns[9].speaker == "S10"
    assert turns[11].speaker == "S12"


# --- attribute_segments -------------------------------------------------------


def test_attribution_full_cover_single_speaker() -> None:
    turns = [Turn(0, 10_000, "S1")]
    (attributed,) = attribute_segments([seg(1, 1000, 3000)], turns)
    assert attributed.speaker == "S1"
    assert attributed.text == "segment 1"  # everything else untouched


def test_attribution_partial_overlap_picks_larger_share() -> None:
    turns = [Turn(0, 4000, "S1"), Turn(4000, 10_000, "S2")]
    (attributed,) = attribute_segments([seg(1, 3000, 8000)], turns)
    assert attributed.speaker == "S2"  # 1s of S1 vs 4s of S2


def test_attribution_sums_multiple_turns_of_same_speaker() -> None:
    # S1 speaks 0-3s and 7-10s (6s total) around S2's single 4s turn.
    turns = [Turn(0, 3000, "S1"), Turn(3000, 7000, "S2"), Turn(7000, 10_000, "S1")]
    (attributed,) = attribute_segments([seg(1, 0, 10_000)], turns)
    assert attributed.speaker == "S1"


def test_attribution_no_overlap_is_none() -> None:
    turns = [Turn(0, 1000, "S1")]
    (attributed,) = attribute_segments([seg(1, 5000, 6000)], turns)
    assert attributed.speaker is None


def test_attribution_exact_tie_goes_to_lower_label() -> None:
    turns = [Turn(0, 5000, "S2"), Turn(5000, 10_000, "S1")]
    (attributed,) = attribute_segments([seg(1, 0, 10_000)], turns)
    assert attributed.speaker == "S1"


def test_attribution_overwrites_stale_labels() -> None:
    turns = [Turn(0, 10_000, "S1")]
    (relabeled,) = attribute_segments([seg(1, 0, 2000, speaker="S9")], turns)
    assert relabeled.speaker == "S1"
    (cleared,) = attribute_segments([seg(1, 0, 2000, speaker="S9")], [])
    assert cleared.speaker is None


def test_attribution_zero_length_segment_is_none() -> None:
    turns = [Turn(0, 10_000, "S1")]
    (attributed,) = attribute_segments([seg(1, 500, 500)], turns)
    assert attributed.speaker is None


def test_attribution_keeps_segment_order_and_count() -> None:
    turns = [Turn(0, 2000, "S1"), Turn(2000, 4000, "S2")]
    segments = [seg(1, 0, 1500), seg(2, 2200, 3800), seg(3, 9000, 9500)]
    attributed = attribute_segments(segments, turns)
    assert [s.seq for s in attributed] == [1, 2, 3]
    assert [s.speaker for s in attributed] == ["S1", "S2", None]


# --- roster / ranges ----------------------------------------------------------


def test_roster_aggregates_and_orders_numerically() -> None:
    turns = relabel_turns([(i * 1000, i * 1000 + 500, i) for i in range(11)])
    turns.append(Turn(20_000, 21_000, "S1"))
    roster = speaker_roster(turns)
    assert [stat.label for stat in roster][:3] == ["S1", "S2", "S3"]
    assert roster[-1].label == "S11"  # numeric order, not lexicographic
    s1 = roster[0]
    assert s1 == SpeakerStat(
        label="S1", talk_time_ms=1500, turn_count=2, first_ms=0, last_ms=21_000
    )


def test_speakers_in_range_inclusive_bounds_like_slice_segments() -> None:
    turns = [Turn(0, 1000, "S1"), Turn(1000, 2000, "S2"), Turn(5000, 6000, "S3")]
    assert speakers_in_range(turns, 1000, 3000) == ["S1", "S2"]  # touching counts
    assert speakers_in_range(turns, 2500, 4999) == []
    assert speakers_in_range(turns, 0, 10_000) == ["S1", "S2", "S3"]


# --- Diarization serde --------------------------------------------------------


def make_diarization() -> Diarization:
    turns = [Turn(0, 5000, "S1"), Turn(5000, 8000, "S2")]
    return Diarization(
        available=True,
        reason="",
        engine="sherpa-onnx",
        engine_version="1.13.4",
        segmentation_model="pyannote-segmentation-3.0",
        embedding_model="wespeaker_en_voxceleb_resnet34_LM",
        requested_num_speakers=2,
        detected_num_speakers=2,
        threshold=0.5,
        speakers=speaker_roster(turns),
        turns=turns,
    )


def test_diarization_round_trip_with_compact_turn_triplets() -> None:
    diarization = make_diarization()
    payload = diarization.to_dict()
    assert payload["turns"] == [[0, 5000, "S1"], [5000, 8000, "S2"]]
    assert "speaker_names" not in payload  # None never serialized
    assert Diarization.from_dict(payload) == diarization


def test_diarization_serializes_speaker_names_when_present() -> None:
    diarization = make_diarization()
    diarization.speaker_names = {"S1": "Alice"}
    payload = diarization.to_dict()
    assert payload["speaker_names"] == {"S1": "Alice"}
    assert Diarization.from_dict(payload).speaker_names == {"S1": "Alice"}


def test_diarization_from_dict_ignores_unknown_and_malformed() -> None:
    payload = make_diarization().to_dict()
    payload["embedding_dim"] = 256  # field from a future version
    payload["speakers"][0]["confidence"] = 0.9
    payload["turns"].append([1, 2])  # malformed triplet is skipped
    rebuilt = Diarization.from_dict(payload)
    assert rebuilt.detected_num_speakers == 2
    assert len(rebuilt.turns) == 2
    assert rebuilt.speakers[0].label == "S1"


# --- env ------------------------------------------------------------------


def test_diarize_default_off_and_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALKTHROUGH_DIARIZE", raising=False)
    assert diarize_default() is False
    for value in ("on", "1", "true", " ON "):
        monkeypatch.setenv("TALKTHROUGH_DIARIZE", value)
        assert diarize_default() is True
    monkeypatch.setenv("TALKTHROUGH_DIARIZE", "off")
    assert diarize_default() is False


def test_threshold_default_override_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALKTHROUGH_DIARIZATION_THRESHOLD", raising=False)
    assert clustering_threshold() == DEFAULT_THRESHOLD
    monkeypatch.setenv("TALKTHROUGH_DIARIZATION_THRESHOLD", "0.72")
    assert clustering_threshold() == 0.72
    for junk in ("not-a-float", "0", "-1", "-0.5"):
        monkeypatch.setenv("TALKTHROUGH_DIARIZATION_THRESHOLD", junk)
        assert clustering_threshold() == DEFAULT_THRESHOLD


def test_threads_default_override_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALKTHROUGH_DIARIZATION_THREADS", raising=False)
    default = diarization_threads()
    assert 1 <= default <= 4
    monkeypatch.setenv("TALKTHROUGH_DIARIZATION_THREADS", "2")
    assert diarization_threads() == 2
    for bad in ("0", "-3", "many"):
        monkeypatch.setenv("TALKTHROUGH_DIARIZATION_THREADS", bad)
        assert diarization_threads() == default


# --- model cache ------------------------------------------------------------


def spec_for(payload: bytes, tmp_path: Path, *, archive_member: str | None = None) -> ModelSpec:
    return ModelSpec(
        name="test-model",
        url="https://example.invalid/assets/test-model.onnx",
        sha256=hashlib.sha256(payload).hexdigest(),
        license="MIT",
        archive_member=archive_member,
    )


def serve_bytes(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> list[str]:
    """Replace the downloader with one writing ``payload``; returns the URL log."""
    calls: list[str] = []

    def fake_download(url: str, dest: Path) -> None:
        calls.append(url)
        dest.write_bytes(payload)

    monkeypatch.setattr(diarize, "_download", fake_download)
    return calls


def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(url: str, dest: Path) -> None:
        raise AssertionError(f"network touched for {url}")

    monkeypatch.setattr(diarize, "_download", no_network)


def test_models_root_respects_talkthrough_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TALKTHROUGH_HOME", str(tmp_path))
    assert models_root() == tmp_path / "models" / "diarization"


def test_warm_cache_is_zero_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TALKTHROUGH_HOME", str(tmp_path))
    forbid_network(monkeypatch)
    spec = spec_for(b"weights", tmp_path)
    cached = tmp_path / "models" / "diarization" / spec.name / "model.onnx"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"weights")
    assert ensure_model_file(spec) == cached


def test_cold_download_verifies_sha_and_installs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TALKTHROUGH_HOME", str(tmp_path))
    payload = b"onnx-bytes"
    calls = serve_bytes(monkeypatch, payload)
    spec = spec_for(payload, tmp_path)
    target = ensure_model_file(spec)
    assert target.read_bytes() == payload
    assert calls == [spec.url]
    assert list(target.parent.glob("*.part")) == []
    # second call is a pure cache hit
    forbid_network(monkeypatch)
    assert ensure_model_file(spec) == target


def test_sha_mismatch_fails_and_leaves_no_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TALKTHROUGH_HOME", str(tmp_path))
    serve_bytes(monkeypatch, b"tampered-bytes")
    spec = spec_for(b"expected-bytes", tmp_path)
    with pytest.raises(ToolFailureError, match="sha256"):
        ensure_model_file(spec)
    model_dir = tmp_path / "models" / "diarization" / spec.name
    assert not (model_dir / "model.onnx").exists()
    assert list(model_dir.glob("*.part")) == []


def test_tar_asset_extracts_single_member(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TALKTHROUGH_HOME", str(tmp_path))
    inner = tmp_path / "model.onnx"
    inner.write_bytes(b"segmentation-weights")
    archive = tmp_path / "asset.tar.bz2"
    with tarfile.open(archive, "w:bz2") as tar:
        tar.add(inner, arcname="release-dir/model.onnx")
        tar.add(inner, arcname="release-dir/README.md")
    payload = archive.read_bytes()
    serve_bytes(monkeypatch, payload)
    spec = spec_for(payload, tmp_path, archive_member="release-dir/model.onnx")
    target = ensure_model_file(spec)
    assert target.read_bytes() == b"segmentation-weights"
    assert list(target.parent.glob("*.part")) == []
    assert list(target.parent.glob("*.tar.bz2")) == []


def test_tar_asset_missing_member_is_actionable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TALKTHROUGH_HOME", str(tmp_path))
    inner = tmp_path / "other.onnx"
    inner.write_bytes(b"x")
    archive = tmp_path / "asset.tar.bz2"
    with tarfile.open(archive, "w:bz2") as tar:
        tar.add(inner, arcname="release-dir/other.onnx")
    payload = archive.read_bytes()
    serve_bytes(monkeypatch, payload)
    spec = spec_for(payload, tmp_path, archive_member="release-dir/model.onnx")
    with pytest.raises(ToolFailureError, match="re-download"):
        ensure_model_file(spec)


def test_download_stall_retries_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TALKTHROUGH_HOME", str(tmp_path))
    payload = b"weights-after-retry"
    attempts: list[int] = []

    class StallingResponse:
        def __enter__(self) -> StallingResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            raise TimeoutError("The read operation timed out")

    class ServingResponse(StallingResponse):
        served = False

        def read(self, size: int = -1) -> bytes:
            if self.served:
                return b""
            self.served = True
            return payload

    def urlopen(request: object, timeout: float = 0) -> StallingResponse:
        attempts.append(1)
        return StallingResponse() if len(attempts) == 1 else ServingResponse()

    monkeypatch.setattr(diarize.urllib.request, "urlopen", urlopen)
    spec = spec_for(payload, tmp_path)
    target = ensure_model_file(spec)
    assert target.read_bytes() == payload
    assert len(attempts) == 2


def test_pinned_specs_are_wellformed() -> None:
    assert DEFAULT_SEGMENTATION_MODEL in SEGMENTATION_MODELS
    assert DEFAULT_EMBEDDING_MODEL in EMBEDDING_MODELS
    for spec in [*SEGMENTATION_MODELS.values(), *EMBEDDING_MODELS.values()]:
        assert spec.url.startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/")
        assert len(spec.sha256) == 64
        assert spec.license
    # reverb models are non-commercial — they must never enter the allowlist
    assert not any("reverb" in name for name in SEGMENTATION_MODELS)


# --- model resolution (env) ---------------------------------------------------


def test_resolve_model_default_and_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    resolved: list[ModelSpec] = []

    def fake_ensure(spec: ModelSpec) -> Path:
        resolved.append(spec)
        return tmp_path / spec.name / "model.onnx"

    monkeypatch.setattr(diarize, "ensure_model_file", fake_ensure)
    monkeypatch.delenv("TALKTHROUGH_DIARIZATION_EMB_MODEL", raising=False)
    label, _ = resolve_model(
        "TALKTHROUGH_DIARIZATION_EMB_MODEL", EMBEDDING_MODELS, DEFAULT_EMBEDDING_MODEL
    )
    assert label == DEFAULT_EMBEDDING_MODEL
    monkeypatch.setenv("TALKTHROUGH_DIARIZATION_EMB_MODEL", "nemo_en_titanet_small")
    label, _ = resolve_model(
        "TALKTHROUGH_DIARIZATION_EMB_MODEL", EMBEDDING_MODELS, DEFAULT_EMBEDDING_MODEL
    )
    assert label == "nemo_en_titanet_small"
    assert [spec.name for spec in resolved] == [DEFAULT_EMBEDDING_MODEL, "nemo_en_titanet_small"]


def test_resolve_model_local_path_is_offline_preseed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    forbid_network(monkeypatch)
    preseed = tmp_path / "custom.onnx"
    preseed.write_bytes(b"weights")
    monkeypatch.setenv("TALKTHROUGH_DIARIZATION_EMB_MODEL", str(preseed))
    label, path = resolve_model(
        "TALKTHROUGH_DIARIZATION_EMB_MODEL", EMBEDDING_MODELS, DEFAULT_EMBEDDING_MODEL
    )
    assert path == preseed
    assert label == str(preseed)


def test_resolve_model_unknown_name_lists_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALKTHROUGH_DIARIZATION_EMB_MODEL", "no-such-model")
    with pytest.raises(ValidationError, match="nemo_en_titanet_small"):
        resolve_model(
            "TALKTHROUGH_DIARIZATION_EMB_MODEL", EMBEDDING_MODELS, DEFAULT_EMBEDDING_MODEL
        )


# --- engine plumbing ----------------------------------------------------------


def test_create_diarizer_without_extra_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sherpa_onnx", None)
    assert create_diarizer() is None


def write_wav(path: Path, *, channels: int = 1, rate: int = 16_000, width: int = 2) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        frame = b"\x00\x40" * channels  # 16384 per channel
        handle.writeframes(frame * 4)


def test_load_wav_float32_scales_and_reports_rate(tmp_path: Path) -> None:
    wav = tmp_path / "mono.wav"
    write_wav(wav)
    samples, rate = load_wav_float32(wav)
    assert rate == 16_000
    assert len(samples) == 4
    assert abs(float(samples[0]) - 0.5) < 1e-4  # 16384 / 32768


def test_load_wav_float32_downmixes_stereo(tmp_path: Path) -> None:
    wav = tmp_path / "stereo.wav"
    write_wav(wav, channels=2)
    samples, _ = load_wav_float32(wav)
    assert len(samples) == 4
    assert abs(float(samples[0]) - 0.5) < 1e-4


def test_load_wav_float32_rejects_non_16bit(tmp_path: Path) -> None:
    wav = tmp_path / "eight.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(16_000)
        handle.writeframes(b"\x40" * 8)
    with pytest.raises(ToolFailureError, match="16-bit"):
        load_wav_float32(wav)
