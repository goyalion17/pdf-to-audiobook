"""PDF to Audiobook: extracts the text from a PDF file and turns it into an MP3.

Text-to-speech is done by Microsoft Edge's online neural voices (via edge-tts):
free, no API key and no account needed. Scanned pages (images without a text layer)
are read with Apple Vision OCR, which is built into macOS.
"""

import argparse
import asyncio
import contextlib
import hashlib
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import aiohttp
import edge_tts
from pypdf import PdfReader

try:
    import pypdfium2 as pdfium
    from ocrmac import ocrmac
except ImportError:  # OCR uses Apple Vision, so it is only available on macOS
    ocrmac = None

DEFAULT_VOICE = "en-US-AriaNeural"
RETRIES = 3                 # attempts per page before giving up
STALL_SECONDS = 30          # no audio for this long = the connection is stuck, try again
PARALLEL_PAGES = 3          # pages spoken at the same time; more may get us rate-limited by the free service
CACHE_DIR = Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache") / "pdf-to-audiobook"
CACHE_DAYS = 30             # unfinished conversions older than this are deleted
PAGE_NUMBER = re.compile(r"^\W*\d{1,4}\W*$")   # "9", "- 12 -", "8;"
MARGIN = 0.15               # top/bottom share of a scanned page where page numbers live
NETWORK_ERRORS = (edge_tts.exceptions.EdgeTTSException, aiohttp.ClientError, asyncio.TimeoutError)


class Cancelled(Exception):
    """The user stopped the conversion."""


def parse_pages(spec, total):
    """Turn a spec like "1-3,7" into zero-based page indexes."""
    if not spec:
        return list(range(total))
    pages = []
    for part in spec.split(","):
        start, _, end = part.strip().partition("-")
        try:
            first, last = int(start), int(end or start)
        except ValueError:
            raise ValueError(f"'{part.strip()}' is not a page number or range like 1-5") from None
        if not 1 <= first <= last <= total:
            raise ValueError(f"page range '{part}' is outside 1-{total}")
        pages.extend(range(first - 1, last))
    return pages


def drop_page_numbers(text):
    """Remove a page number standing alone on the first or last line of a page."""
    lines = text.strip().split("\n")
    while lines and PAGE_NUMBER.match(lines[0]):
        lines.pop(0)
    while lines and PAGE_NUMBER.match(lines[-1]):
        lines.pop()
    return "\n".join(lines)


def clean_text(text):
    """Make PDF text read naturally: re-join hyphenated words and broken lines."""
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)   # "audio-\nbook" -> "audiobook"
    text = re.sub(r"\n{2,}", "<PARAGRAPH>", text)   # keep real paragraph breaks
    text = re.sub(r"\s+", " ", text)                # single line breaks -> spaces
    return text.replace("<PARAGRAPH>", "\n\n").strip()


def voice_language(voice):
    """"uk-UA-PolinaNeural" -> "uk-UA", so OCR expects the same language the voice speaks."""
    return "-".join(voice.split("-")[:2])


def ocr_text(lines):
    """Join OCR lines into text, skipping page numbers near the top or bottom edge.

    Each line is (text, confidence, (x, y, width, height)) with coordinates from 0 to 1
    and y measured from the bottom. Position matters: on a scanned two-page spread the
    left page's number would otherwise end up in the middle of the text.
    """
    kept = [text for text, _, (x, y, width, height) in lines
            if not (PAGE_NUMBER.match(text) and (y < MARGIN or y + height > 1 - MARGIN))]
    return "\n".join(kept)


def ocr_page(document, index, language):
    """Read the text of a scanned page with Apple Vision."""
    image = document[index].render(scale=2).to_pil()
    return ocr_text(ocrmac.OCR(image, language_preference=[language], recognition_level="accurate").recognize())


def work_dir_for(pdf_path, voice, rate):
    """A cache folder for one book + voice + speed. Finished pages are kept there until the MP3 is built."""
    stat = pdf_path.stat()
    key = f"{pdf_path.resolve()}|{stat.st_size}|{stat.st_mtime}|{voice}|{rate}"
    return CACHE_DIR / hashlib.sha1(key.encode()).hexdigest()[:16]


def prune_cache():
    """Forget conversions that were stopped long ago and never finished."""
    if not CACHE_DIR.exists():
        return
    cutoff = time.time() - CACHE_DAYS * 86400
    for folder in CACHE_DIR.iterdir():
        if folder.is_dir() and folder.stat().st_mtime < cutoff:
            shutil.rmtree(folder, ignore_errors=True)


async def speak_page(text, voice, rate, path, cancel=None):
    """Speak one page into its own MP3, retrying when the connection hiccups."""
    part = path.with_suffix(".part")    # renamed to .mp3 only when complete
    for attempt in range(1, RETRIES + 1):
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            loop = asyncio.get_running_loop()
            # aclosing() closes the connection properly even when we stop halfway (cancel or stall)
            async with contextlib.aclosing(communicate.stream()) as stream:
                # the service sometimes goes silent without closing the connection: the deadline
                # moves forward with every chunk, so only a real stall raises TimeoutError
                async with asyncio.timeout(STALL_SECONDS) as deadline:
                    with open(part, "wb") as file:
                        async for chunk in stream:
                            deadline.reschedule(loop.time() + STALL_SECONDS)
                            if cancel and cancel.is_set():
                                raise Cancelled
                            if chunk["type"] == "audio":
                                file.write(chunk["data"])
            part.replace(path)
            return True
        except edge_tts.exceptions.NoAudioReceived:
            return False                # nothing speakable on this page, e.g. only "* * *"
        except NETWORK_ERRORS:
            if attempt == RETRIES:
                raise
            await asyncio.sleep(2 ** attempt)   # wait 2 s, then 4 s


def print_progress(done, total, number, stage):
    """Called when work on a page starts; done = pages finished so far, stage = "ocr", "speak" or "saved"."""
    action = {"ocr": "recognizing scanned page", "speak": "speaking page", "saved": "already done: page"}[stage]
    print(f"  [{done}/{total} done] {action} {number}")


def convert(pdf_path, page_spec, voice, rate, output, on_progress=print_progress, cancel=None):
    """Turn the chosen pages of a PDF into one MP3 file.

    Every page is first spoken into its own file in a cache folder. If the conversion stops
    (no internet, cancelled, app closed), running it again with the same book, voice and speed
    only does the pages that are still missing.
    """
    pdf_path = Path(pdf_path)
    reader = PdfReader(pdf_path)
    indexes = parse_pages(page_spec, len(reader.pages))
    prune_cache()
    work_dir = work_dir_for(pdf_path, voice, rate)
    work_dir.mkdir(parents=True, exist_ok=True)
    work_dir.touch()                    # mark as recently used for prune_cache
    scan = None

    def read_page(i, done):
        nonlocal scan
        text = drop_page_numbers(reader.pages[i].extract_text() or "")
        if not text.strip() and ocrmac:
            on_progress(done, len(indexes), i + 1, "ocr")
            scan = scan or pdfium.PdfDocument(pdf_path)
            text = ocr_page(scan, i, voice_language(voice))
        return clean_text(text)

    spoken = asyncio.run(speak_pages(indexes, read_page, work_dir, voice, rate, on_progress, cancel))
    if not spoken:
        raise ValueError("No text found on these pages."
                         + ("" if ocrmac else " Scanned pages need OCR, which only works on macOS."))
    with open(output, "wb") as book:    # MP3 frames can simply be put one after another
        for audio in spoken:
            with open(audio, "rb") as page:
                shutil.copyfileobj(page, book)
    if not page_spec:
        shutil.rmtree(work_dir, ignore_errors=True)     # the whole book is done, no need to keep pages
    return output


async def speak_pages(indexes, read_page, work_dir, voice, rate, on_progress, cancel):
    """Speak up to PARALLEL_PAGES pages at once; return the finished page files in page order.

    Pages are read (and OCR'd) one at a time on a single helper thread, because pdfium
    must not be used from several threads at once. A page is only read when a speaking
    slot is free, so reading never runs far ahead of speaking.
    """
    loop = asyncio.get_running_loop()
    slots = asyncio.Semaphore(PARALLEL_PAGES)
    finished = {}                       # page number -> audio file, or None if there was nothing to read
    total = len(indexes)

    async def speak(number, text, audio, silent):
        try:
            if text and await speak_page(text, voice, rate, audio, cancel):
                finished[number] = audio
            else:
                silent.touch()
                finished[number] = None
        finally:
            slots.release()

    try:
        with ThreadPoolExecutor(max_workers=1) as reader_thread:
            async with asyncio.TaskGroup() as group:
                for i in indexes:
                    number = i + 1
                    audio = work_dir / f"page-{number:04d}.mp3"
                    silent = work_dir / f"page-{number:04d}.silent"     # remembers pages with nothing to read
                    if audio.exists():
                        on_progress(len(finished), total, number, "saved")
                        finished[number] = audio
                        continue
                    if silent.exists():
                        finished[number] = None
                        continue

                    await slots.acquire()
                    if cancel and cancel.is_set():
                        slots.release()
                        raise Cancelled
                    try:
                        text = await loop.run_in_executor(reader_thread, read_page, i, len(finished))
                    except BaseException:
                        slots.release()
                        raise
                    on_progress(len(finished), total, number, "speak")
                    group.create_task(speak(number, text, audio, silent))
    except ExceptionGroup as errors:
        raise errors.exceptions[0] from None    # report the first real error, e.g. Cancelled or a network error
    return [audio for number, audio in sorted(finished.items()) if audio]


async def list_voices(language):
    voices = await edge_tts.list_voices()
    for voice in sorted(voices, key=lambda v: v["ShortName"]):
        if voice["Locale"].lower().startswith(language.lower()):
            print(f"{voice['ShortName']:<35} {voice['Gender']}")


def main():
    parser = argparse.ArgumentParser(description="Convert a PDF file into an MP3 audiobook.")
    parser.add_argument("pdf", nargs="?", help="path to the PDF file")
    parser.add_argument("-o", "--output", help="MP3 file to write (default: <pdf name>.mp3)")
    parser.add_argument("-v", "--voice", default=DEFAULT_VOICE, help=f"voice name (default: {DEFAULT_VOICE})")
    parser.add_argument("-p", "--pages", help='pages to read, e.g. "1-3,7" (default: all)')
    parser.add_argument("-r", "--rate", default="+0%", help='speaking speed, e.g. "+20%%" or "-10%%"')
    parser.add_argument("--list-voices", metavar="LANG", help='show voices for a language, e.g. "en", "uk", "de"')
    args = parser.parse_args()

    if args.list_voices:
        asyncio.run(list_voices(args.list_voices))
        return
    if not args.pdf:
        # no PDF given (e.g. started with the Run button): open the desktop app instead
        import app
        app.run()
        return

    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        sys.exit(f"File not found: {pdf_path}")
    output = Path(args.output) if args.output else pdf_path.with_suffix(".mp3")

    print(f"Reading {pdf_path.name} with voice {args.voice}...")
    try:
        convert(pdf_path, args.pages, args.voice, args.rate, output)
    except ValueError as error:
        sys.exit(f"Error: {error}")
    except NETWORK_ERRORS as error:
        sys.exit(f"Text-to-speech failed (are you online?): {error}\n"
                 "Finished pages are saved: run the same command again to continue.")
    except KeyboardInterrupt:
        sys.exit("\nStopped. Finished pages are saved: run the same command again to continue.")
    print(f"Done! Audiobook saved to {output}")


if __name__ == "__main__":
    main()
