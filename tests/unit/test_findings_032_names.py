"""0.3.2 external findings F3/F5/F6: the name-candidate chrome filter is a
vocabulary, not a list of reported strings; hidden pending labels are listed;
the force refusal names what it protects."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import make_manifest

from talkthrough_mcp.core import pipeline
from talkthrough_mcp.core.diarize import Diarization, Turn, speaker_roster
from talkthrough_mcp.core.errors import ValidationError
from talkthrough_mcp.core.pipeline import _name_candidate_rejection_reason

# Real name plates across scripts, particles, hyphens, apostrophes, roles and
# pronoun suffixes. Every one of them must survive the filter.
NAME_PLATES = [
    "Vera Smith",
    "MARTIN DUBOIS",
    "Ирина Петрова",
    "Élodie Martin",
    "张伟",
    "محمد علي",
    "Vera Smith (she/her)",
    "Ludwig van der Beek",
    "Jean d'Arc",
    "Иван Петров (организатор)",
    "Prof. Dr. Hans-Peter von der Linden",
    "Ana María López de la Torre",
    "vera smith",
    "María de la Cruz",
    "Ahmed bin Rashid Al Maktoum",
    "O'Brien",
    "Пётр Ильич Чайковский",
    "山田 太郎",
    "Anne-Sophie Mutter",
    "Dr. Vera Smith (Product, Berlin)",
    "Grace Hall",
    "Mark Reed",
    "Will Hand",
    "Emily Post",
    "Thomas More",
    "Sharon Stone",
    "Bill Gates",
    "Rose Byrne",
    "Faith Hill",
    "Chase Williams",
    "Reed Richards",
    "Vera Smith Product Manager",
    "Kim Min-jun",
    "Nguyễn Văn An",
    "Björk Guðmundsdóttir",
    "José García-Márquez",
    "Søren Kierkegaard",
    "Łukasz Nowak",
    "Ольга Смирнова",
    "Айгүл Нұрланқызы",
    "Παναγιώτης Παπαδόπουλος",
    "David Ben-Gurion",
    "Aisha Al-Sayed",
    "Priya Ramaswamy",
    "Wang Fang",
    "Chen Ling",
    "Li Ming",
    "Jean-Luc Picard",
    "Mary-Kate Olsen",
    "Sam O'Neil",
    "Ana de Armas",
    "Leonardo da Vinci",
    "Charles de Gaulle",
    "Vincent van Gogh",
    "Abu Bakr",
    "Ibn Sina",
    "Le Corbusier",
    "Мария Кузнецова",
    "Дмитрий Ильин",
    "V. Smith",
]

# UI chrome the way meeting apps, recorders, IDEs, browsers and dashboards
# actually render it — including the nine strings an external review found
# accepted in 0.3.2 and their Russian / DE / ES / FR equivalents.
UI_CHROME = [
    "File Edit View Navigate Code Refactor Run Tools VCS Window Help",
    "Participants Chat Share Leave",
    "10:42",
    "https://example.com/alex",
    r"C:\\Users\\alex\\project.py",
    "this is a long slide sentence explaining the quarterly product roadmap",
    "A" * 81,
    "PROPHET",
    "Terminal",
    "Loading",
    "Meeting Chat",
    "Zoom Meeting",
    "Waiting Room",
    "Everyone",
    "Unknown Caller",
    "Copy Paste Delete",
    "Stop Recording",
    "参会者",
    "Продажи Отчет",
    # the nine 0.3.2 review specimens
    "Screen Sharing",
    "Breakout Rooms",
    "Raise Hand",
    "Mute All",
    "Participants Panel",
    "Gallery View",
    "Live Captions",
    "Speaker Notes",
    "Recording Stopped",
    # meeting apps
    "Share Screen",
    "Stop Share",
    "You are presenting",
    "Recording in progress",
    "Leave Meeting",
    "End Meeting",
    "Join Audio",
    "Start Video",
    "Stop Video",
    "Unmute",
    "Reactions",
    "Whiteboard",
    "More Options",
    "Host",
    "Guest",
    "Co-host",
    "Presenter",
    "Presenting",
    "Waiting for host",
    "Connecting…",
    "Reconnecting",
    "Recording",
    "Rec",
    "Meeting ID",
    "Everyone in Meeting",
    "Turn on captions",
    "Show captions",
    "Hide self view",
    "Pin video",
    "Spotlight for everyone",
    "Ask to unmute",
    "Lower hand",
    "Admit all",
    "Google Meet",
    "Microsoft Teams",
    # IDE / OS / browser
    "Build succeeded",
    "Tests passed",
    "Pull request",
    "Search Everywhere",
    "Recent Files",
    "Save As",
    "New Tab",
    "Close Tab",
    "Untitled",
    "Sign in",
    "Log out",
    "Forgot password",
    "Remember me",
    "Continue with Google",
    "Something went wrong",
    "Try again",
    "Please wait",
    "Welcome back",
    "Good morning",
    # dashboards and roles
    "Sales Report",
    "Total Revenue",
    "Profit and Loss",
    "Budget vs Actual",
    "Team Members",
    "Product Manager",
    "Senior Software Engineer",
    "Head of Sales",
    "Chief Executive Officer",
    "Marketing Director",
    # Russian
    "Демонстрация экрана",
    "Поделиться экраном",
    "Участники",
    "Покинуть встречу",
    "Включить микрофон",
    "Выключить камеру",
    "Идет запись",
    "Остановить запись",
    "Зал ожидания",
    "Сессионные залы",
    "Поднять руку",
    "Настройки",
    "Загрузка",
    "Файл Правка Вид",
    "Отчет по продажам",
    "Все участники",
    "Неизвестный абонент",
    "Подключение…",
    "Добро пожаловать",
    # DE / ES / FR
    "Bildschirm freigeben",
    "Teilnehmer",
    "Compartir pantalla",
    "Participantes",
    "Partager l'écran",
    "Quitter",
]


@pytest.mark.parametrize("line", NAME_PLATES)
def test_name_plates_survive_the_chrome_filter(line: str) -> None:
    assert _name_candidate_rejection_reason(line) is None


@pytest.mark.parametrize("line", UI_CHROME)
def test_ui_chrome_is_rejected_by_vocabulary_not_by_exact_string(line: str) -> None:
    assert _name_candidate_rejection_reason(line) is not None


def test_chrome_filter_precision_and_recall_on_the_whole_corpus() -> None:
    """One number to watch when the vocabulary is tuned again."""
    rejected_names = [line for line in NAME_PLATES if _name_candidate_rejection_reason(line)]
    accepted_chrome = [line for line in UI_CHROME if not _name_candidate_rejection_reason(line)]
    assert rejected_names == []
    assert accepted_chrome == []


def test_a_role_beside_a_name_keeps_the_name_but_a_bare_role_does_not() -> None:
    assert _name_candidate_rejection_reason("Vera Smith Product Manager") is None
    assert _name_candidate_rejection_reason("Product Manager") == "ui_chrome"
    assert _name_candidate_rejection_reason("You are presenting") == "ui_chrome"


def test_chrome_vocabulary_never_contains_common_given_names() -> None:
    for name in ("grace", "mark", "will", "hope", "bill", "rose", "faith", "joy", "chase",
                 "sharon", "reed", "post", "вера", "роман", "надежда", "любовь"):
        assert name not in pipeline._UI_CHROME_WORDS


# --- F5: hidden pending labels are listed ---------------------------------------


def _pending_heavy_diarization() -> Diarization:
    turns = [Turn(0, 5000, "S1"), Turn(5000, 8000, "S2")]
    diarization = Diarization(
        available=True, reason="", detected_num_speakers=2, speakers=speaker_roster(turns),
        turns=turns,
    )
    diarization.speaker_names_pending_review = {
        f"S{index}": f"Name {index}" for index in range(1, pipeline.SUMMARY_ROSTER_CAP + 5)
    }
    diarization.speaker_names_pending_review["S99"] = "Stale name"
    return diarization


def test_hidden_pending_labels_are_listed_so_they_can_be_removed() -> None:
    payload = pipeline.pending_review_payload(_pending_heavy_diarization())
    assert payload["speaker_names_pending_review_truncated"] == 5
    assert payload["speaker_names_pending_review_hidden_labels"] == [
        "S13", "S14", "S15", "S16", "S99"
    ]
    assert set(payload["speaker_names_pending_review_hidden_labels"]).isdisjoint(
        payload["speaker_names_pending_review"]
    )


def test_hidden_pending_labels_list_is_itself_capped() -> None:
    diarization = _pending_heavy_diarization()
    diarization.speaker_names_pending_review = {
        f"S{index}": "x" for index in range(1, pipeline.PENDING_HIDDEN_LABELS_CAP + 40)
    }
    payload = pipeline.pending_review_payload(diarization)
    hidden = payload["speaker_names_pending_review_hidden_labels"]
    assert len(hidden) == pipeline.PENDING_HIDDEN_LABELS_CAP
    assert payload["speaker_names_pending_review_truncated"] > len(hidden)


def test_no_hidden_labels_key_when_everything_fits() -> None:
    diarization = _pending_heavy_diarization()
    diarization.speaker_names_pending_review = {"S1": "Alice"}
    payload = pipeline.pending_review_payload(diarization)
    assert "speaker_names_pending_review_hidden_labels" not in payload
    assert "speaker_names_pending_review_truncated" not in payload


# --- F6: the force refusal names what it protects ------------------------------


def _manifest_with(names: dict[str, str] | None, pending: dict[str, str] | None):  # type: ignore[no-untyped-def]
    manifest = make_manifest()
    turns = [Turn(0, 5000, "S1"), Turn(5000, 8000, "S2")]
    manifest.transcript.diarization = Diarization(
        available=True, reason="", detected_num_speakers=2, speakers=speaker_roster(turns),
        turns=turns, speaker_names=names, speaker_names_pending_review=pending,
    )
    return manifest


def test_force_refusal_counts_saved_and_pending_identities_separately() -> None:
    both = pipeline._identity_force_refusal(_manifest_with({"S1": "Alice"}, {"S3": "Carol"}))
    assert "1 saved and 1 pending-review speaker identities" in both
    assert "force=true, diarize=true" in both

    pending_only = pipeline._identity_force_refusal(_manifest_with(None, {"S3": "Carol"}))
    assert "1 pending-review speaker identity requires" in pending_only
    assert "saved speaker identities" not in pending_only
    assert 'label_speakers(labels={"Sx": null})' in pending_only

    saved_only = pipeline._identity_force_refusal(
        _manifest_with({"S1": "Alice", "S2": "Bob"}, None)
    )
    assert "2 saved speaker identities" in saved_only
    assert "pending" not in saved_only.split("requires")[0]


def test_force_refusal_on_a_pending_only_job_reaches_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_pipeline_config import _stored_force_identity_job, engine

    from talkthrough_mcp.core import jobs
    from talkthrough_mcp.core.manifest import save_manifest

    media, stored = _stored_force_identity_job(tmp_path, monkeypatch)
    diarization = stored.transcript.diarization
    assert diarization is not None
    diarization.speaker_names = None
    diarization.speaker_name_evidence = None
    save_manifest(stored, jobs.job_dir(stored.job_id))
    engine(monkeypatch, available=True)
    monkeypatch.delenv("TALKTHROUGH_DIARIZE", raising=False)
    with pytest.raises(ValidationError, match=r"1 pending-review speaker identity requires"):
        pipeline.process_media(str(media), force=True)
