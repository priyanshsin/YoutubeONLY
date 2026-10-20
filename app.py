"""Independent local MusicalBUNKER web publisher. Run with Start-Web.bat."""
from __future__ import annotations
import argparse
import base64
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import itertools
import json
import os
from pathlib import Path
import random
import secrets
import sys
import threading
import time
import uuid
import webbrowser
from zoneinfo import ZoneInfo

BASE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
os.chdir(BASE)
from dotenv import load_dotenv
load_dotenv(BASE / ".env", override=True)
# This app never imports or reads the parent Channel app or its settings.
os.environ["SONG_WORKSPACE"] = str(BASE)
os.environ.pop("SONG_CREDENTIALS_FILE", None)
import pipeline
import requests
from flask import Flask, jsonify, render_template, request, send_file, abort

INTERVAL = 24 * 3600
PREPARE_AHEAD = 3600
LANGUAGES = ["Hindi", "Hindi", "English", "English", "Hindi-English mix (Hinglish)"]
IST = ZoneInfo("Asia/Kolkata")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def stamp(value):
    return datetime.fromtimestamp(value, IST).strftime("%d %b %Y, %I:%M:%S %p IST")


def plans():
    choices = random.sample(list(itertools.product(pipeline.GENRES, pipeline.THEMES)), 5)
    return [{"index": i + 1, "language": language, "genre": genre, "theme": theme,
             "status": "queued", "title": f"Song {i + 1}", "url": None}
            for i, (language, (genre, theme)) in enumerate(zip(LANGUAGES, choices))]


class Stream:
    def __init__(self, manager):
        self.manager = manager

    def write(self, text):
        if text.strip():
            self.manager.log(text.strip())
        return len(text)

    def flush(self):
        pass


class Manager:
    def __init__(self, root=BASE / "data"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.shutdown = threading.Event()
        self.running = False
        self.preview = False
        self.logs = []
        self.thread = None
        path = self.root / "state.json"
        self.state = pipeline.read_json(path) if path.exists() else {
            "enabled": True, "next_release": time.time() + INTERVAL, "batches": [], "current": None}
        # Never silently repeat external actions after an interrupted process.
        if self.state.get("current"):
            batch = self.batch(self.state["current"])
            if batch["status"] not in ("complete", "failed"):
                batch["status"] = "interrupted"
                batch["error"] = "The app closed during this batch. Review it, then use Retry batch."
                self.write_batch(batch)
                self.state["enabled"] = False
        self.persist()

    def persist(self):
        with self.lock:
            pipeline.save_json(self.root / "state.json", self.state)

    def folder(self, batch_id):
        if not isinstance(batch_id, str) or not batch_id or any(c not in "0123456789abcdef-" for c in batch_id):
            raise ValueError("Invalid batch ID.")
        return self.root / batch_id

    def batch(self, batch_id):
        with self.lock:
            return pipeline.read_json(self.folder(batch_id) / "batch.json")

    def write_batch(self, batch):
        with self.lock:
            folder = self.folder(batch["id"])
            folder.mkdir(exist_ok=True)
            pipeline.save_json(folder / "batch.json", batch)

    def log(self, message):
        for key in ("GEMINI_API_KEY", "RESEND_API_KEY"):
            value = os.getenv(key, "")
            if value:
                message = message.replace(value, "[REDACTED]")
        with self.lock:
            self.logs.append({"at": now_iso(), "message": message[:1800]})
            self.logs = self.logs[-100:]

    def snapshot(self):
        with self.lock:
            return {
                "enabled": self.state["enabled"], "running": self.running,
                "next_release": self.state["next_release"], "prepare_ahead": PREPARE_AHEAD,
                "preview": self.preview, "now": time.time(), "recipient": os.getenv("REPORT_EMAIL"),
                "batches": [self.batch(i) for i in self.state["batches"][-20:]][::-1],
                "logs": self.logs[-30:],
            }

    def start(self, release=None, retry=None):
        with self.lock:
            if self.running:
                raise ValueError("A batch is already running.")
            current = self.state.get("current")
            if current and not retry:
                raise ValueError("Finish or retry the current batch before starting another.")
            if retry:
                if retry != current:
                    raise ValueError("Only the current incomplete batch can be retried.")
                batch = self.batch(retry)
                batch["error"] = None
            else:
                batch = {"id": str(uuid.uuid4()), "created_at": now_iso(),
                         "release_at": release or time.time(), "status": "generating",
                         "songs": plans(), "email": {"status": "pending"}, "error": None}
                self.state["batches"].append(batch["id"])
            batch["status"] = "generating"
            self.write_batch(batch)
            self.state["current"] = batch["id"]
            self.state["enabled"] = True
            self.persist()
            self.running = True
            self.thread = threading.Thread(target=self.run_batch, args=(batch["id"],), daemon=True)
            self.thread.start()
            return batch["id"]

    def gate(self):
        while not self.shutdown.is_set():
            with self.lock:
                if self.state["enabled"]:
                    return
            self.shutdown.wait(1)
        raise RuntimeError("App is stopping. Resume this batch on the next launch.")

    def set_enabled(self, enabled):
        with self.lock:
            self.state["enabled"] = enabled
            self.persist()
        self.log("Schedule resumed." if enabled else "Schedule paused. No further stages will start.")

    def verify_publication(self, folder, song):
        receipt = pipeline.read_json(folder / "upload.json")
        service = pipeline.youtube()
        for _ in range(120):  # Up to 20 minutes, resumable by Retry batch.
            self.gate()
            data = service.videos().list(part="status,snippet", id=receipt["video_id"]).execute()
            if not data.get("items"):
                raise RuntimeError("Uploaded video could not be found. Check YouTube Studio.")
            item = data["items"][0]
            status = item["status"]
            if status.get("uploadStatus") in ("failed", "rejected", "deleted"):
                raise RuntimeError("YouTube did not accept this video. Check YouTube Studio.")
            if status.get("privacyStatus") != "public":
                raise RuntimeError("YouTube has not made this upload public. Check YouTube Studio.")
            if status.get("uploadStatus") == "processed":
                song.update(url=receipt["url"], status="published",
                            published_at=item["snippet"]["publishedAt"])
                return
            if self.shutdown.wait(10):
                self.gate()
        raise RuntimeError("YouTube is still processing. Retry this batch later; it will reuse the upload.")

    def attachment(self, folder):
        manifest = pipeline.read_json(folder / "song.json")
        audio = folder / manifest["audio_file"]
        target = folder / "email-song.mp3"
        if not target.exists():
            temporary = folder / "email-song.pending.mp3"
            pipeline.run_process([pipeline.ffmpeg(), "-hide_banner", "-loglevel", "error",
                                  "-y", "-i", str(audio), "-vn", "-c:a", "libmp3lame",
                                  "-b:a", "96k", str(temporary)], check=True)
            temporary.replace(target)
        return target

    def send_report(self, batch):
        if batch["email"]["status"] == "sent":
            return
        folder = self.folder(batch["id"])
        payload_file = folder / "email-payload.json"
        if payload_file.exists():
            payload = pipeline.read_json(payload_file)
        else:
            lines = ["Your five MusicalBUNKER songs are now public.", ""]
            attachments = []
            for song in batch["songs"]:
                if song["status"] != "published":
                    raise RuntimeError("Email report requires five verified public songs.")
                published = datetime.fromisoformat(song["published_at"].replace("Z", "+00:00"))
                lines.extend([f"{song['index']}. {song['title']} ({song['language']}; {song['genre']})",
                              song["url"], "Published: " + published.astimezone(IST).strftime(
                                  "%d %b %Y, %I:%M:%S %p IST"), ""])
                audio = self.attachment(folder / f"song-{song['index']:02d}")
                attachments.append({"filename": f"{song['index']:02d}-{song['language'].split()[0]}-song.mp3",
                                    "content": base64.b64encode(audio.read_bytes()).decode("ascii")})
            lines.append("All five complete songs are attached as MP3 files. Created with AI tools.")
            payload = {"from": os.getenv("RESEND_FROM_EMAIL", "MusicalBUNKER <onboarding@resend.dev>"),
                       "to": [os.getenv("REPORT_EMAIL", "priyanshsingh.indian@gmail.com")],
                       "subject": "MusicalBUNKER â€” 5 new songs are public",
                       "text": "\n".join(lines), "attachments": attachments}
            if len(json.dumps(payload).encode("utf-8")) > 23_000_000:
                raise RuntimeError("Email exceeds the attachment budget. Files are saved; reduce MP3 bitrate.")
            pipeline.save_json(payload_file, payload)
        pending = batch["email"].get("attempted_at")
        if pending and time.time() - pending > 23 * 3600:
            raise RuntimeError("Email delivery is uncertain and the safe retry window expired. "
                               "Check Resend before sending again; videos will not be reuploaded.")
        batch["email"].update(status="sending", attempted_at=pending or time.time())
        self.write_batch(batch)
        response = requests.post("https://api.resend.com/emails",
                                 headers={"Authorization": "Bearer " + pipeline.required_env("RESEND_API_KEY"),
                                          "Idempotency-Key": "musicalbunker/" + batch["id"]},
                                 json=payload, timeout=(30, 120))
        if not response.ok:
            raise RuntimeError(f"Email provider returned HTTP {response.status_code}. "
                               "Retry the batch to retry only unfinished work.")
        batch["email"].update(status="sent", provider_id=response.json()["id"], sent_at=now_iso())
        self.write_batch(batch)

    def run_batch(self, batch_id):
        batch = self.batch(batch_id)
        try:
            with redirect_stdout(Stream(self)):
                for song in batch["songs"]:
                    self.gate()
                    folder = self.folder(batch_id) / f"song-{song['index']:02d}"
                    if not (folder / "song.mp4").exists():
                        song["status"] = "generating"
                        self.write_batch(batch)
                        args = argparse.Namespace(
                            topic=song["theme"], language=song["language"],
                            style=f"{song['genre']}, expressive original vocals. "
                                  + ("Mix Hindi and English naturally in the same song."
                                     if "mix" in song["language"] else ""),
                            seconds=240, out=folder, upload=False, privacy="public",
                            made_for_kids=False, cover_style="Cinematic album art")
                        pipeline.build(args)
                    song["title"] = pipeline.read_json(folder / "metadata.json")["title"]
                    if song["status"] != "published":
                        song["status"] = "ready"
                    self.write_batch(batch)
                batch["status"] = "waiting"
                self.write_batch(batch)
                while time.time() < batch["release_at"]:
                    self.gate()
                    self.shutdown.wait(min(5, max(0, batch["release_at"] - time.time())))
                batch["status"] = "publishing"
                self.write_batch(batch)
                for song in batch["songs"]:
                    self.gate()
                    if song["status"] == "published":
                        continue
                    folder = self.folder(batch_id) / f"song-{song['index']:02d}"
                    song["status"] = "publishing"
                    self.write_batch(batch)
                    pipeline.upload(folder, "public", False)
                    self.verify_publication(folder, song)
                    self.write_batch(batch)
                self.gate()
                batch["status"] = "emailing"
                self.write_batch(batch)
                self.send_report(batch)
                batch["status"] = "complete"
                batch["completed_at"] = now_iso()
                self.write_batch(batch)
                with self.lock:
                    self.state["current"] = None
                    next_release = batch["release_at"] + INTERVAL
                    while next_release <= time.time():
                        next_release += INTERVAL
                    self.state["next_release"] = next_release
                    self.persist()
                self.log("Five songs published. Resend accepted the report with five MP3 attachments.")
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            for name in ("GEMINI_API_KEY", "RESEND_API_KEY"):
                if os.getenv(name):
                    message = message.replace(os.environ[name], "[REDACTED]")
            batch.update(status="failed", error=message)
            self.write_batch(batch)
            self.set_enabled(False)
            self.log("Batch stopped: " + message)
        finally:
            with self.lock:
                self.running = False

    def scheduler(self):
        while not self.shutdown.wait(2):
            with self.lock:
                due = (self.state["enabled"] and not self.running and not self.state["current"]
                       and time.time() >= self.state["next_release"] - PREPARE_AHEAD)
                release = self.state["next_release"]
            if due:
                try:
                    self.start(release)
                except Exception as exc:
                    self.log("Scheduler could not start: " + type(exc).__name__)
                    self.set_enabled(False)


def create_app(manager):
    app = Flask(__name__, template_folder=str(BASE / "templates"), static_folder=str(BASE / "static"))
    csrf = secrets.token_urlsafe(32)
    app.config["MAX_CONTENT_LENGTH"] = 8192

    @app.before_request
    def local_only():
        if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
            abort(403)
        if request.method == "POST":
            if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), csrf):
                abort(403)
            if request.headers.get("Origin") != request.host_url.rstrip("/"):
                abort(403)

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; media-src 'self'; "
            "script-src 'self'; style-src 'self'; frame-ancestors 'none'")
        return response

    @app.get("/")
    def home():
        return render_template("index.html", csrf=csrf)

    @app.get("/releases")
    def releases():
        return render_template("releases.html")

    @app.get("/api/releases")
    def all_releases():
        with manager.lock:
            songs = [dict(song, batchId=batch_id)
                     for batch_id in reversed(manager.state["batches"])
                     for song in manager.batch(batch_id)["songs"]
                     if song.get("status") == "published"]
        return jsonify(songs)

    @app.get("/api/status")
    def status():
        return jsonify(manager.snapshot())

    @app.post("/api/action")
    def action():
        if manager.preview:
            return jsonify(error="Preview mode: launch Start-Web.bat to enable actions."), 409
        data = request.get_json(silent=True) or {}
        try:
            if data.get("action") == "pause":
                manager.set_enabled(False)
            elif data.get("action") == "resume":
                manager.set_enabled(True)
            elif data.get("action") == "run":
                manager.start()
            elif data.get("action") == "retry":
                manager.start(retry=data.get("id"))
            else:
                raise ValueError("Unknown action.")
            return jsonify(ok=True)
        except ValueError as exc:
            return jsonify(error=str(exc)), 409

    @app.get("/media/<batch_id>/<int:index>/<kind>")
    def media(batch_id, index, kind):
        if index not in range(1, 6) or kind not in ("video", "cover", "audio"):
            abort(404)
        try:
            folder = manager.folder(batch_id) / f"song-{index:02d}"
        except ValueError:
            abort(404)
        filename = {"video": "song.mp4", "cover": "thumbnail.jpg", "audio": "email-song.mp3"}[kind]
        path = folder / filename
        if not path.is_file():
            abort(404)
        return send_file(path, conditional=True)

    return app


def instance_lock(path):
    handle = open(path, "a+b")
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        if path.stat().st_size == 0:
            handle.write(b"1")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true", help="No scheduler, generation, uploads or email")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    (BASE / "data").mkdir(exist_ok=True)
    try:
        process_lock = instance_lock(BASE / "data" / "app.lock")
    except OSError:
        print("MusicalBUNKER Web is already running. Open http://127.0.0.1:8765")
        return
    manager = Manager()
    manager.preview = args.preview
    app = create_app(manager)
    if not args.preview:
        threading.Thread(target=manager.scheduler, daemon=True).start()
    url = f"http://127.0.0.1:{args.port}"
    print(f"MusicalBUNKER Web: {url}", flush=True)
    print("Preview mode: no external actions." if args.preview else
          "Schedule: five public songs every 24 hours; preparation starts one hour early.", flush=True)
    if not args.no_browser:
        threading.Timer(1, lambda: webbrowser.open(url)).start()
    from waitress import serve
    try:
        serve(app, host="127.0.0.1", port=args.port, threads=8)
    finally:
        manager.shutdown.set()
        process_lock.close()


if __name__ == "__main__":
    main()
