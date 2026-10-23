"""Tests for main.py. Network and OCR are replaced with fakes, so they run offline in a second."""

import threading
from pathlib import Path

import aiohttp
import pytest
from pypdf import PdfReader, PdfWriter

import main as M

SAMPLE = Path(__file__).parent.parent / "sample.pdf"


# ---------- parse_pages ----------
def test_parse_pages_all_when_empty():
    assert M.parse_pages("", 3) == [0, 1, 2]
    assert M.parse_pages(None, 2) == [0, 1]


def test_parse_pages_ranges_and_single_pages():
    assert M.parse_pages("1-3, 7", 10) == [0, 1, 2, 6]


@pytest.mark.parametrize("spec", ["0", "5-11", "4-2", "abc", "1-x"])
def test_parse_pages_rejects_bad_input(spec):
    with pytest.raises(ValueError):
        M.parse_pages(spec, 10)


# ---------- text cleanup ----------
def test_clean_text_joins_hyphenated_words_and_lines():
    assert M.clean_text("free audio-\nbook for\nyou") == "free audiobook for you"


def test_clean_text_keeps_paragraphs():
    assert M.clean_text("First.\n\nSecond.") == "First.\n\nSecond."


def test_clean_text_works_with_ukrainian():
    assert M.clean_text("Хру-\nщова") == "Хрущова"


def test_drop_page_numbers_only_at_the_edges():
    assert M.drop_page_numbers("12\nText about 1958\n- 13 -") == "Text about 1958"


def test_ocr_text_skips_page_numbers_near_edges_only():
    lines = [
        ("Text on the left page", 0.9, (0.07, 0.80, 0.37, 0.02)),
        ("8;", 0.9, (0.07, 0.10, 0.01, 0.01)),      # left page number, bottom edge
        ("1958", 0.9, (0.56, 0.50, 0.05, 0.02)),     # a year in the middle of the page
        ("9", 0.9, (0.92, 0.10, 0.01, 0.01)),       # right page number
    ]
    assert M.ocr_text(lines) == "Text on the left page\n1958"


def test_voice_language():
    assert M.voice_language("uk-UA-PolinaNeural") == "uk-UA"


# ---------- speak_page: retries ----------
def fake_communicate(failures):
    """A stand-in for edge_tts.Communicate whose first `failures` attempts lose the connection."""
    calls = {"n": 0}

    class FakeCommunicate:
        def __init__(self, text, voice, rate):
            pass

        async def stream(self):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise aiohttp.ClientConnectionError("Wi-Fi dropped")
            yield {"type": "audio", "data": b"mp3"}

    return FakeCommunicate, calls


@pytest.fixture
def no_waiting(monkeypatch):
    async def instant(seconds):
        pass
    monkeypatch.setattr(M.asyncio, "sleep", instant)


def test_speak_page_retries_after_network_errors(tmp_path, monkeypatch, no_waiting):
    fake, calls = fake_communicate(failures=2)
    monkeypatch.setattr(M.edge_tts, "Communicate", fake)
    path = tmp_path / "page.mp3"
    assert M.asyncio.run(M.speak_page("Hi", "en-US-AriaNeural", "+0%", path))
    assert calls["n"] == 3
    assert path.read_bytes() == b"mp3"


def test_speak_page_gives_up_and_leaves_no_finished_file(tmp_path, monkeypatch, no_waiting):
    fake, calls = fake_communicate(failures=99)
    monkeypatch.setattr(M.edge_tts, "Communicate", fake)
    path = tmp_path / "page.mp3"
    with pytest.raises(aiohttp.ClientConnectionError):
        M.asyncio.run(M.speak_page("Hi", "en-US-AriaNeural", "+0%", path))
    assert calls["n"] == M.RETRIES
    assert not path.exists()


# ---------- convert: cancel and resume ----------
@pytest.fixture
def book(tmp_path, monkeypatch):
    """A 3-page text PDF, an empty cache folder and a fake speak_page that records which pages it spoke."""
    writer = PdfWriter()
    for _ in range(3):
        writer.add_page(PdfReader(SAMPLE).pages[0])
    pdf = tmp_path / "book.pdf"
    writer.write(pdf)
    monkeypatch.setattr(M, "CACHE_DIR", tmp_path / "cache")

    spoken = []

    async def fake_speak(text, voice, rate, path, cancel=None):
        if cancel and cancel.is_set():      # like the real one, which checks while streaming
            raise M.Cancelled
        spoken.append(int(path.stem.split("-")[1]))
        path.write_bytes(f"<page {spoken[-1]}>".encode())
        return True

    monkeypatch.setattr(M, "speak_page", fake_speak)
    return pdf, spoken


def quiet(*args):
    pass


def test_convert_joins_pages_in_order(book, tmp_path):
    pdf, spoken = book
    output = M.convert(pdf, "", "en-US-AriaNeural", "+0%", tmp_path / "out.mp3", on_progress=quiet)
    assert output.read_bytes() == b"<page 1><page 2><page 3>"
    assert not M.work_dir_for(pdf, "en-US-AriaNeural", "+0%").exists()   # cleaned up after a whole book


def test_convert_resumes_after_cancel(book, tmp_path):
    pdf, spoken = book
    cancel = threading.Event()

    def stop_before_page_2(done, total, number, stage):
        if number == 2:
            cancel.set()

    with pytest.raises(M.Cancelled):
        M.convert(pdf, "", "en-US-AriaNeural", "+0%", tmp_path / "out.mp3", stop_before_page_2, cancel)
    assert spoken == [1]

    stages = []
    M.convert(pdf, "", "en-US-AriaNeural", "+0%", tmp_path / "out.mp3", lambda *a: stages.append(a[2:]))
    assert spoken == [1, 2, 3]                  # page 1 was not spoken twice
    assert stages[0] == (1, "saved")
    assert (tmp_path / "out.mp3").read_bytes() == b"<page 1><page 2><page 3>"


def test_convert_with_no_text_raises(tmp_path, monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    pdf = tmp_path / "blank.pdf"
    writer.write(pdf)
    monkeypatch.setattr(M, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(M, "ocr_page", lambda *a: "")
    with pytest.raises(ValueError, match="No text"):
        M.convert(pdf, "", "en-US-AriaNeural", "+0%", tmp_path / "out.mp3", on_progress=quiet)
