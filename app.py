"""PDF to Audiobook: desktop app.

Choose a PDF, turn it into speech, listen to it right in the app and save the MP3.
Reading the PDF and text-to-speech come from main.py.
"""

import os
import queue
import shutil
import tempfile
import threading
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from pypdf import PdfReader

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"   # must be set before pygame is imported
import pygame  # noqa: E402

from main import NETWORK_ERRORS, Cancelled, convert  # noqa: E402

# ---------- Theme ----------
# (light, dark) pairs: CustomTkinter switches them automatically with the appearance mode
BG = ("#f7f5f0", "#141619")
SURFACE = ("#ffffff", "#1d2024")
BORDER = ("#e7e3da", "#2a2e34")
CHIP = ("#efece6", "#25292e")
CHIP_HOVER = ("#e4e0d7", "#30353b")
TEXT = ("#1f1f1f", "#e8e6e1")
MUTED = ("#8d8a83", "#7b8089")
ACCENT = "#5b4bdb"
ACCENT_HOVER = "#4a3bc4"
GOOD = "#2f9e5b"
ERROR = "#e5484d"
WHITE = "#ffffff"

UI_FONT = "Avenir Next"
SERIF_FONT = "Charter"

VOICES = {
    "English (US) · Aria": "en-US-AriaNeural",
    "English (US) · Guy": "en-US-GuyNeural",
    "English (UK) · Sonia": "en-GB-SoniaNeural",
    "Українська · Поліна": "uk-UA-PolinaNeural",
    "Українська · Остап": "uk-UA-OstapNeural",
    "Deutsch · Katja": "de-DE-KatjaNeural",
    "Deutsch · Conrad": "de-DE-ConradNeural",
}
SPEEDS = (("-25%", "0.75×"), ("+0%", "1×"), ("+25%", "1.25×"), ("+50%", "1.5×"))
BYTES_PER_SECOND = 48_000 / 8   # edge-tts sends 48 kbit/s MP3, so file size tells the duration
TICK_MS = 200


def font(size, weight="normal", family=UI_FONT):
    return ctk.CTkFont(family=family, size=size, weight=weight)


def format_time(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


class Segmented(ctk.CTkFrame):
    """A pill-shaped segmented control; the selected option is filled with the accent color."""

    def __init__(self, master, options, selected, button_width=72):
        super().__init__(master, fg_color=CHIP, corner_radius=12)
        self.buttons = {}
        self.value = None
        for value, label in options:
            button = ctk.CTkButton(self, text=label, width=button_width, height=32, corner_radius=9,
                                   font=font(14), command=lambda v=value: self.select(v))
            button.pack(side="left", padx=3, pady=3)
            self.buttons[value] = button
        self.select(selected)

    def select(self, value):
        self.value = value
        for option, button in self.buttons.items():
            if option == value:
                button.configure(fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=WHITE,
                                 font=font(14, "bold"))
            else:
                button.configure(fg_color="transparent", hover_color=CHIP_HOVER, text_color=TEXT,
                                 font=font(14))


class AudiobookApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PDF to Audiobook")
        self.root.geometry("600x800")
        self.root.minsize(560, 760)
        self.root.configure(fg_color=BG)
        pygame.mixer.init()

        self.pdf_path = None
        self.audio_path = None      # the latest converted MP3, in a temp folder until saved
        self.audio_name = None      # suggested file name when saving
        self.duration = 0.0
        self.offset = 0.0           # second of the audio where the current play() started
        self.state = "stopped"      # "stopped", "playing" or "paused"
        self.dragging = False       # the user is moving the position slider
        self.saved = True           # False while there is converted audio that hasn't been saved
        self.conversions = 0
        self.cancel_event = None    # set while converting; .set() asks the conversion to stop
        self.events = queue.Queue()  # messages from the conversion thread
        self.temp_dir = Path(tempfile.mkdtemp(prefix="audiobook-"))

        self.build()
        self.set_player_enabled(False)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.createcommand("::tk::mac::Quit", self.quit)    # Cmd+Q on macOS
        self.tick()

    # ---------- Layout ----------
    def build(self):
        page = ctk.CTkFrame(self.root, fg_color="transparent")
        page.pack(fill="both", expand=True, padx=28, pady=24)

        header = ctk.CTkFrame(page, fg_color="transparent")
        header.pack(fill="x")
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left")
        ctk.CTkLabel(titles, text="PDF to Audiobook", font=font(30, "bold", SERIF_FONT),
                     text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(titles, text="Too tired to read? Let a natural voice read it to you.", font=font(14),
                     text_color=MUTED).pack(anchor="w")
        self.theme_button = ctk.CTkButton(header, text=self.theme_icon(), width=40, height=40, corner_radius=20,
                                          font=font(18), fg_color=CHIP, hover_color=CHIP_HOVER, text_color=TEXT,
                                          command=self.toggle_theme)
        self.theme_button.pack(side="right", anchor="n")

        # 1 · Book
        book = self.card(page, "1  ·  BOOK")
        row = ctk.CTkFrame(book, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkButton(row, text="Choose PDF…", width=140, height=40, corner_radius=20, font=font(14, "bold"),
                      fg_color=CHIP, hover_color=CHIP_HOVER, text_color=TEXT,
                      command=self.choose_pdf).pack(side="left")
        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True, padx=(14, 0))
        self.file_label = ctk.CTkLabel(info, text="No file chosen", font=font(15, "bold"), text_color=TEXT,
                                       anchor="w")
        self.file_label.pack(fill="x")
        self.pages_label = ctk.CTkLabel(info, text="Scanned books work too, their text is recognized first",
                                        font=font(12), text_color=MUTED, anchor="w")
        self.pages_label.pack(fill="x")

        # 2 · Voice
        voice = self.card(page, "2  ·  VOICE")
        self.voice_menu = ctk.CTkOptionMenu(voice, values=list(VOICES), height=38, corner_radius=10,
                                            font=font(14), dropdown_font=font(14), fg_color=CHIP,
                                            button_color=CHIP, button_hover_color=CHIP_HOVER, text_color=TEXT,
                                            dropdown_fg_color=SURFACE, dropdown_text_color=TEXT,
                                            dropdown_hover_color=CHIP_HOVER)
        self.voice_menu.pack(fill="x")
        settings = ctk.CTkFrame(voice, fg_color="transparent")
        settings.pack(fill="x", pady=(14, 0))
        speed_box = ctk.CTkFrame(settings, fg_color="transparent")
        speed_box.pack(side="left")
        ctk.CTkLabel(speed_box, text="Speed", font=font(13), text_color=MUTED).pack(anchor="w")
        self.speed = Segmented(speed_box, SPEEDS, "+0%", button_width=56)
        self.speed.pack(anchor="w")
        pages_box = ctk.CTkFrame(settings, fg_color="transparent")
        pages_box.pack(side="right")
        ctk.CTkLabel(pages_box, text="Pages", font=font(13), text_color=MUTED).pack(anchor="w")
        self.pages_entry = ctk.CTkEntry(pages_box, placeholder_text="all, or 1-5, 8", width=150, height=38,
                                        corner_radius=10, border_width=0, fg_color=CHIP, text_color=TEXT,
                                        placeholder_text_color=MUTED, font=font(14))
        self.pages_entry.pack(anchor="w")

        # Convert
        self.convert_button = ctk.CTkButton(page, text="Convert to audio", height=50, corner_radius=25,
                                            font=font(16, "bold"), fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                            text_color=WHITE, command=self.convert_or_cancel)
        self.convert_button.pack(fill="x", pady=(20, 0))
        self.progress = ctk.CTkProgressBar(page, height=6, corner_radius=3, fg_color=BORDER,
                                           progress_color=ACCENT)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=(14, 0))
        self.status_label = ctk.CTkLabel(page, text="Choose a PDF to begin", font=font(13), text_color=MUTED)
        self.status_label.pack(pady=(4, 0))

        # 3 · Listen
        player = self.card(page, "3  ·  LISTEN")
        controls = ctk.CTkFrame(player, fg_color="transparent")
        controls.pack(fill="x")
        self.play_button = ctk.CTkButton(controls, text="▶", width=52, height=52, corner_radius=26, font=font(20),
                                         fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=WHITE,
                                         command=self.toggle_play)
        self.play_button.pack(side="left")
        self.stop_button = ctk.CTkButton(controls, text="■", width=40, height=40, corner_radius=20, font=font(14),
                                         fg_color=CHIP, hover_color=CHIP_HOVER, text_color=TEXT,
                                         command=self.stop)
        self.stop_button.pack(side="left", padx=(10, 0))
        track = ctk.CTkFrame(controls, fg_color="transparent")
        track.pack(side="left", fill="x", expand=True, padx=(16, 0))
        self.slider = ctk.CTkSlider(track, from_=0, to=1, height=16, fg_color=BORDER, progress_color=ACCENT,
                                    button_color=ACCENT, button_hover_color=ACCENT_HOVER, command=self.on_slide)
        self.slider.set(0)
        self.slider.pack(fill="x", pady=(8, 0))
        self.slider.bind("<ButtonRelease-1>", self.on_slide_release, add="+")
        self.time_label = ctk.CTkLabel(track, text="0:00 / 0:00", font=font(12), text_color=MUTED)
        self.time_label.pack(anchor="e")
        self.save_button = ctk.CTkButton(player, text="Save MP3…", height=40, corner_radius=20,
                                         font=font(14, "bold"), fg_color=CHIP, hover_color=CHIP_HOVER,
                                         text_color=TEXT, command=self.save_mp3)
        self.save_button.pack(fill="x", pady=(16, 0))

    def card(self, parent, title):
        card = ctk.CTkFrame(parent, fg_color=SURFACE, border_color=BORDER, border_width=1, corner_radius=18)
        card.pack(fill="x", pady=(20, 0))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=18)
        ctk.CTkLabel(inner, text=title, font=font(11, "bold"), text_color=MUTED).pack(anchor="w", pady=(0, 8))
        return inner

    def theme_icon(self):
        return "☾" if ctk.get_appearance_mode() == "Light" else "☀"

    def toggle_theme(self):
        ctk.set_appearance_mode("light" if ctk.get_appearance_mode() == "Dark" else "dark")
        self.theme_button.configure(text=self.theme_icon())

    def set_status(self, text, color=MUTED):
        self.status_label.configure(text=text, text_color=color)

    # ---------- PDF & conversion ----------
    def choose_pdf(self):
        path = filedialog.askopenfilename(title="Choose a PDF", filetypes=[("PDF files", "*.pdf")])
        if not path:
            return
        try:
            count = len(PdfReader(path).pages)
        except Exception as error:  # broken or password-protected PDFs raise many kinds of errors
            messagebox.showerror("Can't open this PDF", str(error))
            return
        self.pdf_path = Path(path)
        self.file_label.configure(text=self.pdf_path.name)
        self.pages_label.configure(text=f"{count} page{'' if count == 1 else 's'}")
        self.set_status("Ready to convert")

    def convert_or_cancel(self):
        if self.cancel_event:
            self.cancel_event.set()
            self.convert_button.configure(state="disabled", text="Stopping…")
        else:
            self.convert()

    def convert(self):
        if not self.saved and not self.confirm_discard("Convert again?"):
            return
        if not self.pdf_path:
            self.choose_pdf()
            if not self.pdf_path:
                return
        self.unload_audio()
        pages = self.pages_entry.get().strip()
        if pages.lower() == "all":
            pages = ""
        self.conversions += 1
        output = self.temp_dir / f"audio-{self.conversions}.mp3"
        self.audio_name = f"{self.pdf_path.stem}.mp3"

        self.cancel_event = threading.Event()
        self.convert_button.configure(text="Cancel", fg_color=CHIP, hover_color=CHIP_HOVER, text_color=TEXT)
        self.progress.set(0)
        self.set_status("Reading the PDF…")
        threading.Thread(target=self.run_conversion, daemon=True,
                         args=(self.pdf_path, pages, VOICES[self.voice_menu.get()], self.speed.value, output,
                               self.cancel_event)).start()

    def run_conversion(self, pdf_path, pages, voice, rate, output, cancel):
        """Runs in a background thread so the window stays responsive; reports back through self.events.

        Every outcome ends with exactly one "done", "cancelled" or "error" event,
        so the window never stays stuck on "Converting…".
        """
        def report(done, total, number, stage):
            self.events.put(("progress", done, total, number, stage))

        try:
            convert(pdf_path, pages, voice, rate, output, on_progress=report, cancel=cancel)
        except Cancelled:
            self.events.put(("cancelled",))
        except ValueError as error:
            self.events.put(("error", str(error)))
        except NETWORK_ERRORS as error:
            self.events.put(("error", "Text-to-speech failed. Are you online?\n\n"
                                      "Finished pages are saved: press Convert again to continue "
                                      f"where it stopped.\n\n({error})"))
        except Exception as error:  # anything unexpected: show it instead of failing silently
            traceback.print_exc()
            self.events.put(("error", f"Something went wrong: {type(error).__name__}: {error}\n\n"
                                      "Finished pages are saved: press Convert again to continue."))
        else:
            self.events.put(("done", output))

    def handle_events(self):
        while True:
            try:
                kind, *data = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "progress":
                done, total, number, stage = data
                action = {"ocr": "Recognizing scanned page", "speak": "Speaking page",
                          "saved": "Already done: page"}[stage]
                self.progress.set(done / total)
                self.set_status(f"{action} {number}  ·  {done + 1} of {total}")
            elif kind == "done":
                self.conversion_done(data[0])
            elif kind == "cancelled":
                self.conversion_cancelled()
            else:
                self.conversion_failed(data[0])

    def conversion_finished(self):
        self.cancel_event = None
        self.convert_button.configure(state="normal", text="Convert to audio", fg_color=ACCENT,
                                      hover_color=ACCENT_HOVER, text_color=WHITE)

    def conversion_done(self, output):
        self.conversion_finished()
        self.progress.set(1)
        self.audio_path = output
        self.saved = False
        self.duration = output.stat().st_size / BYTES_PER_SECOND
        pygame.mixer.music.load(output)
        self.set_player_enabled(True)
        self.set_status(f"Done! {format_time(self.duration)} of audio, press ▶ to listen", GOOD)

    def conversion_cancelled(self):
        self.conversion_finished()
        self.progress.set(0)
        self.set_status("Cancelled. Finished pages are kept, Convert continues from there")

    def conversion_failed(self, message):
        self.conversion_finished()
        self.progress.set(0)
        self.set_status("Conversion failed", ERROR)
        messagebox.showerror("Conversion failed", message)

    def save_mp3(self):
        target = filedialog.asksaveasfilename(title="Save audiobook", initialfile=self.audio_name,
                                              defaultextension=".mp3", filetypes=[("MP3 audio", "*.mp3")])
        if not target:
            return False
        try:
            shutil.copyfile(self.audio_path, target)
        except OSError as error:
            messagebox.showerror("Couldn't save the MP3", str(error))
            return False
        self.saved = True
        self.set_status(f"Saved to {target}", GOOD)
        return True

    def confirm_discard(self, title):
        """Ask before throwing away audio that hasn't been saved. True = go ahead."""
        answer = messagebox.askyesnocancel(title, "Your audiobook isn't saved yet. Save it first?")
        if answer is None:
            return False
        return self.save_mp3() if answer else True

    # ---------- Player ----------
    def position(self):
        if self.state == "stopped":
            return self.offset
        return min(self.offset + max(pygame.mixer.music.get_pos(), 0) / 1000, self.duration)

    def toggle_play(self):
        if self.state == "playing":
            pygame.mixer.music.pause()
            self.state = "paused"
        elif self.state == "paused":
            pygame.mixer.music.unpause()
            self.state = "playing"
        else:
            self.play_from(0 if self.offset >= self.duration else self.offset)
        self.update_player()

    def play_from(self, seconds):
        pygame.mixer.music.play(start=seconds)
        self.offset = seconds
        self.state = "playing"

    def stop(self):
        pygame.mixer.music.stop()
        self.state = "stopped"
        self.offset = 0
        self.update_player()

    def seek(self, seconds):
        if self.state == "playing":
            self.play_from(seconds)
        else:
            # "stopped" with an offset means: the next ▶ starts from here
            pygame.mixer.music.stop()
            self.state = "stopped"
            self.offset = seconds
        self.update_player()

    def on_slide(self, value):
        self.dragging = True
        self.time_label.configure(text=f"{format_time(value * self.duration)} / {format_time(self.duration)}")

    def on_slide_release(self, event):
        if self.dragging:
            self.dragging = False
            self.seek(self.slider.get() * self.duration)

    def update_player(self):
        if self.state == "playing" and not pygame.mixer.music.get_busy():
            self.state = "stopped"     # reached the end
            self.offset = 0
        self.play_button.configure(text="❚❚" if self.state == "playing" else "▶")
        if self.dragging or not self.duration:
            return
        position = self.position()
        self.slider.set(position / self.duration)
        self.time_label.configure(text=f"{format_time(position)} / {format_time(self.duration)}")

    def set_player_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for widget in (self.play_button, self.stop_button, self.slider, self.save_button):
            widget.configure(state=state)

    def unload_audio(self):
        pygame.mixer.music.stop()
        pygame.mixer.music.unload()
        self.state = "stopped"
        self.offset = 0
        self.duration = 0.0
        self.audio_path = None
        self.saved = True
        self.slider.set(0)
        self.time_label.configure(text="0:00 / 0:00")
        self.play_button.configure(text="▶")
        self.set_player_enabled(False)

    # ---------- Loop ----------
    def tick(self):
        self.handle_events()
        self.update_player()
        self.root.after(TICK_MS, self.tick)

    def quit(self):
        if self.cancel_event:
            if not messagebox.askyesno("Quit while converting?", "The conversion is still running. Quit anyway?\n\n"
                                       "Finished pages are kept, so next time Convert continues from there."):
                return
            self.cancel_event.set()
        elif not self.saved and not self.confirm_discard("Quit?"):
            return
        pygame.mixer.quit()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.root.destroy()


def run():
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    AudiobookApp(root)
    root.mainloop()


if __name__ == "__main__":
    run()
