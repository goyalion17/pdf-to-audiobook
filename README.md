# PDF to Audiobook

Too tired to read? This Python project takes a PDF file, extracts its text and converts it to speech, effectively creating a free audiobook. Use it as a desktop app or from the command line. Built as part of my 100 Days of Code journey.

Text-to-speech comes from Microsoft Edge's online neural voices via [edge-tts](https://github.com/rany2/edge-tts). They sound natural, cover 70+ languages (including Ukrainian and German) and need **no API key, no account and no credit card**.

## Features

- Desktop app: choose a PDF, convert it, listen right in the app and save the MP3 to your computer
- Extracts text from PDFs with [pypdf](https://github.com/py-pdf/pypdf)
- Scanned books work too: pages without a text layer are recognized with Apple Vision OCR (built into macOS, via [ocrmac](https://github.com/straussmaximilian/ocrmac)) in the language of the chosen voice; page numbers on the scan are skipped so they aren't read aloud
- Cleans the text so it reads naturally: re-joins words hyphenated across lines and merges broken lines
- Read the whole book or only selected pages, e.g. `1-3, 7`
- Choose a voice and speaking speed
- Shows progress page by page and saves everything to one MP3 file
- Built for long books: every page is retried up to 3 times if the connection drops, a conversion can be cancelled, and finished pages are kept (in `~/Library/Caches/pdf-to-audiobook`), so Convert continues where it stopped instead of starting over
- Asks before quitting or converting again while your audiobook isn't saved yet

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Desktop app

```bash
python3 app.py
```

1. **Book**: choose a PDF and see how many pages it has
2. **Voice**: pick a voice (English, Ukrainian, German), a speed (0.75×–1.5×) and optionally which pages to read
3. **Convert to audio**: a progress bar shows which page is being spoken, and the window stays responsive
4. **Listen**: play / pause / stop and drag the slider to jump anywhere, then **Save MP3…** to keep the audiobook

Light and dark theme, switchable with the ☀ / ☾ button.

## Command line

```bash
python3 main.py book.pdf                          # -> book.mp3
python3 main.py book.pdf -p 1-5 -o chapter1.mp3   # only pages 1-5
python3 main.py book.pdf -v uk-UA-PolinaNeural    # Ukrainian voice
python3 main.py book.pdf -r "+20%"                # read 20% faster
python3 main.py --list-voices de                  # list German voices
```

Try either one with the included `sample.pdf`. Scanned pages take about a second each to recognize before they are spoken; OCR needs macOS.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests replace the network and OCR with fakes, so they run offline in under a second. They cover page ranges, text cleanup, page-number filtering, retries after a dropped connection, and cancelling and resuming a conversion.

## Why edge-tts?

The assignment suggested three TTS APIs. I compared them before choosing:

| API | Free? | Setup |
|---|---|---|
| iSpeech | No real free tier, paid API key | Account + key |
| Google Cloud Text-to-Speech | Free monthly quota (millions of characters) | Google Cloud project, billing account with a credit card, service-account key |
| Amazon Polly | Free tier for new AWS accounts | AWS account with a credit card, IAM keys |
| **edge-tts** | **Free** | **None** |

Google and Amazon both work well, but need a credit card on file. edge-tts gives similar neural voice quality with zero setup.

## Project structure

```
├── app.py            # desktop app: choose a PDF, convert, listen, save (CustomTkinter + pygame)
├── main.py           # PDF text extraction, text cleanup, TTS and the command-line interface
├── tests/            # pytest tests for main.py
├── sample.pdf        # a short test document
├── requirements.txt  # pypdf, edge-tts, customtkinter, pygame-ce, pypdfium2, ocrmac
├── requirements-dev.txt  # + pytest
├── pytest.ini
└── README.md
```
