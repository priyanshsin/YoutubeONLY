"""Generate a song and cover through Gemini, render with FFmpeg, upload via OAuth."""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import random
from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess
import sys

import requests
from dotenv import load_dotenv

ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True, encoding="utf-8-sig")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
DISCLOSURE = "This song, vocals, lyrics, and artwork were created using AI tools."
GENRES = ["acoustic folk", "synth pop", "indie rock", "lo-fi soul", "cinematic pop",
          "funk", "reggae fusion", "electronic dance pop", "jazz pop", "ambient folk"]
THEMES = ["hope after a difficult night", "a monsoon reunion", "a journey to a new city",
          "childhood memories", "finding courage", "friendship across distance",
          "dancing under the stars", "a peaceful mountain morning", "new beginnings",
          "letting go and moving forward"]


def run_process(command, **kwargs):
    if os.name == "nt":
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    return subprocess.run(command, **kwargs)


def save_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} in .env or your environment.")
    return value


def generate(model, prompt, config=None):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise ValueError("Invalid model ID.")
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if config:
        body["generationConfig"] = config
    endpoint = os.getenv("GEMINI_ENDPOINT", "generativelanguage.googleapis.com")
    interaction = endpoint == "aiplatform.googleapis.com" and model.startswith("lyria-3")
    if endpoint == "aiplatform.googleapis.com":
        if interaction:
            project = required_env("GOOGLE_CLOUD_PROJECT")
            if not re.fullmatch(r"[a-zA-Z0-9-]+", project):
                raise ValueError("Invalid GOOGLE_CLOUD_PROJECT.")
            url = (f"https://aiplatform.googleapis.com/v1beta1/projects/{project}"
                   "/locations/global/interactions")
            body = {"model": model, "input": prompt, "store": False}
        else:
            url = f"https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:generateContent"
    elif endpoint == "generativelanguage.googleapis.com":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    else:
        raise ValueError("Unsupported GEMINI_ENDPOINT; use a supported Google API hostname.")
    headers = {"x-goog-api-key": required_env("GEMINI_API_KEY")}
    if interaction:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        credentials = Credentials.from_authorized_user_file(str(ROOT / "secrets" / "google-cloud.json"))
        credentials.refresh(Request())
        headers = {"Authorization": "Bearer " + credentials.token}
    try:
        response = requests.post(
            url,
            headers=headers,
            json=body, timeout=(30, 900),
        )
    except requests.RequestException:
        raise RuntimeError(
            "Gemini connection failed. The request may have been charged; inspect your "
            "Google API usage before retrying. Completed stages remain on disk."
        ) from None
    if not response.ok:
        try:
            detail = str(response.json().get("error", {}).get("message", ""))[:1500]
            detail = detail.replace(required_env("GEMINI_API_KEY"), "[REDACTED]")
            if "Authorization" in headers:
                detail = detail.replace(headers["Authorization"].removeprefix("Bearer "), "[REDACTED]")
        except (ValueError, AttributeError):
            detail = "No error detail available."
        raise RuntimeError(
            f"{endpoint} / {model}: HTTP {response.status_code}. {detail}"
        )
    data = response.json()
    if interaction:
        if data.get("status") != "completed":
            raise RuntimeError("Music interaction did not complete. Check Cloud API usage before retrying.")
        parts = []
        for output in data.get("outputs", []):
            if output.get("type") == "text":
                parts.append({"text": output.get("text", "")})
            elif output.get("type") == "audio" and output.get("data"):
                parts.append({"inlineData": {"mimeType": output.get("mime_type", "audio/mpeg"),
                                             "data": output["data"]}})
        return parts
    candidates = data.get("candidates", [])
    if not candidates or candidates[0].get("finishReason") not in (None, "STOP"):
        raise RuntimeError("Gemini returned no complete candidate (possibly blocked).")
    parts = candidates[0].get("content", {}).get("parts", [])
    return [p for p in parts if not p.get("thought")]


def text_of(parts):
    return "\n".join(p["text"] for p in parts if p.get("text"))


def binary_of(parts, mime_prefix):
    for part in parts:
        blob = part.get("inlineData", {})
        if blob.get("mimeType", "").startswith(mime_prefix) and blob.get("data"):
            return base64.b64decode(blob["data"], validate=True), blob["mimeType"]
    raise RuntimeError(f"Model returned no {mime_prefix} data.")


def validate_metadata(meta):
    for key in ("title", "description"):
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            raise ValueError(f"Metadata needs a nonempty {key}.")
    if len(meta["title"]) > 100 or any(c in meta["title"] for c in "<>"):
        raise ValueError("YouTube title must be <=100 characters and contain no < or >.")
    if DISCLOSURE not in meta["description"]:
        meta["description"] += "\n\n" + DISCLOSURE
    if len(meta["description"].encode("utf-8")) > 5000 or any(
        c in meta["description"] for c in "<>"
    ):
        raise ValueError("Description must be <=5000 UTF-8 bytes with no < or >.")
    tags = meta.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip() for t in tags):
        raise ValueError("Tags must be a list of nonempty strings.")
    if sum(len(t) + (2 if " " in t else 0) for t in tags) + max(len(tags) - 1, 0) > 500:
        raise ValueError("YouTube tags exceed 500 characters.")
    return meta


def ffmpeg():
    executable = os.getenv("FFMPEG", "ffmpeg")
    if not shutil.which(executable):
        raise ValueError("FFmpeg missing. Install it or set FFMPEG to its full executable path.")
    return executable


def duration(path):
    probe = os.getenv("FFPROBE", "ffprobe")
    if not shutil.which(probe):
        raise ValueError("FFprobe missing. Install it alongside FFmpeg or set FFPROBE.")
    result = run_process([probe, "-v", "error", "-show_entries", "format=duration",
                             "-of", "json", str(path)], capture_output=True, text=True, check=True)
    seconds = float(json.loads(result.stdout)["format"]["duration"])
    if not 0 < seconds < 86400:
        raise ValueError("Invalid media duration.")
    return seconds


def require_long_song(path):
    seconds = duration(path)
    if seconds <= 180:
        raise ValueError(f"Song is only {seconds:.1f}s. MusicalBUNKER songs must exceed 180s.")
    return seconds


def extend_song(folder, manifest, plan, target):
    """Add newly generated instrumental music, never silence or repeated audio."""
    base = folder / manifest.get("base_audio_file", manifest["audio_file"])
    base_seconds = duration(base)
    if base_seconds >= max(181, target - 15):
        return
    outro = folder / "instrumental-ending.mp3"
    if not outro.exists():
        needed = min(180, max(60, int(target - base_seconds + 5)))
        print(f"Generating an approximately {needed}s instrumental ending...", flush=True)
        parts = generate(os.getenv("GEMINI_MUSIC_MODEL", "lyria-3-pro-preview"),
                         f"Create an instrumental ending lasting {needed} seconds. No vocals. "
                         f"Match this song arrangement, tempo and key: {plan['music_prompt']}. "
                         "Build a musical reprise then resolve to a gentle ending.")
        data, mime = binary_of(parts, "audio/")
        if mime not in ("audio/mpeg", "audio/mp3"):
            raise ValueError("Instrumental ending must be returned as MP3.")
        outro.write_bytes(data)
    if base_seconds + duration(outro) - 3 <= 180:
        raise ValueError("Generated sections are too short to exceed three minutes. "
                         "No upload was attempted. Keep the base track and regenerate the ending.")
    target_file = folder / "music-extended.wav"
    run_process([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(base), "-i", str(outro), "-filter_complex",
                    "[0:a]aresample=44100,aformat=channel_layouts=stereo[a];"
                    "[1:a]aresample=44100,aformat=channel_layouts=stereo[b];"
                    "[a][b]acrossfade=d=3:c1=tri:c2=tri[out]",
                    "-map", "[out]", "-c:a", "pcm_s16le", str(target_file)], check=True)
    require_long_song(target_file)
    manifest["base_audio_file"] = base.name
    manifest["audio_file"] = target_file.name
    save_json(folder / "song.json", manifest)
    (folder / "song.mp4").unlink(missing_ok=True)


def render(folder):
    folder = Path(folder)
    manifest = read_json(folder / "song.json")
    audio = folder / manifest["audio_file"]
    cover = folder / "cover.png"
    target = folder / "song.mp4"
    temp = folder / "song.rendering.mp4"
    run_process([
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-loop", "1", "-framerate", "24", "-i", str(cover), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
               "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-c:v", "libx264", "-tune", "stillimage", "-preset", "medium",
        "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-t", str(duration(audio)), "-movflags", "+faststart", str(temp),
    ], check=True)
    temp.replace(target)
    print(f"Rendered: {target}")


def build(args):
    from PIL import Image
    ffmpeg()  # Fail before incurring generation charges.
    folder = args.out.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    request = {"topic": args.topic, "language": args.language,
               "style": args.style, "seconds": args.seconds}
    if getattr(args, "cover_style", ""):
        request["cover_style"] = args.cover_style
    request_file = folder / "request.json"
    if request_file.exists() and read_json(request_file) != request:
        raise ValueError("This folder belongs to a different song. Use a new --out folder.")
    save_json(request_file, request)
    plan_path = folder / "plan.json"
    if not plan_path.exists():
        print("Writing original lyrics and YouTube metadata...", flush=True)
        prompt = (
            "Create an original song plan for this brief: " + json.dumps(request) +
            '. Return JSON with string fields title, lyrics, music_prompt, cover_prompt, '
            'description, and a tags array of strings. Use verse/chorus section labels. '
            'Write lyrics in the requested language for a 170-second vocal section; '
            'a separately generated instrumental ending will extend the finished song. '
            'Use a title under 90 characters, description under 2000 characters and '
            '5 relevant tags totaling under 200 characters. Description should introduce '
            'the song, mood and genre and invite listeners to subscribe. Do not claim '
            'a human artist, ownership guarantees or copyright-free status. '
            'Do not imitate named artists. Cover prompt: cinematic widescreen song artwork '
            'with readable song title, strong composition and no logos. '
            'Music prompt: detailed instrumentation, explicit BPM and musical key, '
            'mood and vocal direction. Brand the description for MusicalBUNKER.'
        )
        parts = generate(os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash"), prompt,
                         {"responseMimeType": "application/json"})
        plan = json.loads(text_of(parts))
        validate_metadata(plan)
        for key in ("lyrics", "music_prompt", "cover_prompt"):
            if not isinstance(plan.get(key), str) or not plan[key].strip():
                raise ValueError(f"Generated plan is missing {key}. Retry in a new folder.")
        save_json(plan_path, plan)
    plan = read_json(plan_path)
    (folder / "lyrics.txt").write_text(plan["lyrics"], encoding="utf-8")
    if not (folder / "metadata.json").exists():
        save_json(folder / "metadata.json",
                  validate_metadata({k: plan[k] for k in ("title", "description", "tags")}))
    manifest_path = folder / "song.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {}
    if not manifest.get("audio_file") or not (folder / manifest["audio_file"]).exists():
        print("Generating sung music (this may take several minutes)...", flush=True)
        parts = generate(os.getenv("GEMINI_MUSIC_MODEL", "lyria-3-pro-preview"),
                         f"Create a complete song approximately {min(args.seconds, 170)} seconds long. "
                         f"{plan['music_prompt']}\nSing these lyrics:\n{plan['lyrics']}")
        audio, mime = binary_of(parts, "audio/")
        extensions = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3",
                      "audio/wav": ".wav", "audio/x-wav": ".wav"}
        if mime not in extensions:
            raise ValueError(f"Unsupported audio format: {mime}")
        manifest["audio_file"] = "music" + extensions[mime]
        (folder / manifest["audio_file"]).write_bytes(audio)
        (folder / "music_response.txt").write_text(text_of(parts), encoding="utf-8")
        save_json(manifest_path, manifest)
    if not manifest.get("base_audio_file") and duration(folder / manifest["audio_file"]) < max(181, args.seconds - 15):
        extend_song(folder, manifest, plan, args.seconds)
    require_long_song(folder / manifest["audio_file"])
    if not (folder / "cover.png").exists():
        print("Generating the cover image...", flush=True)
        parts = generate(os.getenv("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image"),
                         f"Create a 16:9 song cover. {plan['cover_prompt']}",
                         {"responseModalities": ["TEXT", "IMAGE"]})
        image_bytes, _ = binary_of(parts, "image/")
        with Image.open(io.BytesIO(image_bytes)) as picture:
            picture.convert("RGB").save(folder / "cover.png")
    with Image.open(folder / "cover.png") as picture:
        from PIL import ImageOps
        ImageOps.pad(picture.convert("RGB"), (1280, 720), color="black").save(
            folder / "thumbnail.jpg", quality=85)
    if not (folder / "song.mp4").exists():
        render(folder)
    print(f"Song ready in {folder}. Review song.mp4 and metadata.json.")
    if args.upload:
        upload(folder, args.privacy, args.made_for_kids)


def youtube():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build as api_build
    default_token = ROOT / "secrets" / "youtube-token.json"
    token = Path(os.getenv("YOUTUBE_TOKEN_FILE") or default_token)
    credentials = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        secret_file = Path(required_env("YOUTUBE_CLIENT_SECRETS"))
        flow = InstalledAppFlow.from_client_secrets_file(str(secret_file), SCOPES)
        credentials = flow.run_local_server(
            port=0, access_type="offline", prompt="consent select_account",
            timeout_seconds=300,
            login_hint=os.getenv("YOUTUBE_LOGIN_HINT", "priyanshsingh.indian@gmail.com"))
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(credentials.to_json(), encoding="utf-8")
    return api_build("youtube", "v3", credentials=credentials, cache_discovery=False)


def list_channels(service):
    return service.channels().list(part="id,snippet", mine=True).execute().get("items", [])


def upload(folder, privacy, made_for_kids):
    from googleapiclient.http import MediaFileUpload
    folder = Path(folder).resolve()
    receipt = folder / "upload.json"
    pending = folder / "upload.pending.json"
    if receipt.exists():
        print("Already uploaded: " + read_json(receipt)["url"])
        return
    if pending.exists():
        raise ValueError("An upload was previously started. Check YouTube Studio before "
                         "removing upload.pending.json; retrying could create a duplicate.")
    expected = required_env("YOUTUBE_CHANNEL_ID")
    meta = validate_metadata(read_json(folder / "metadata.json"))
    video = folder / "song.mp4"
    if not video.is_file() or video.stat().st_size == 0:
        raise ValueError("Render song.mp4 before uploading.")
    require_long_song(video)
    service = youtube()
    channels = list_channels(service)
    if len(channels) != 1 or channels[0]["id"] != expected:
        raise ValueError("Authorized YouTube channel does not match YOUTUBE_CHANNEL_ID. "
                         "Run channels to inspect it. Reauthorize with the correct account/channel.")
    print(f"Uploading to {channels[0]['snippet']['title']} ({expected}) as {privacy}.", flush=True)
    media = MediaFileUpload(str(video), mimetype="video/mp4",
                            chunksize=8 * 1024 * 1024, resumable=True)
    try:
        request = service.videos().insert(
            part="snippet,status",
            body={"snippet": {"title": meta["title"], "description": meta["description"],
                              "tags": meta.get("tags", []), "categoryId": "10"},
                  "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": made_for_kids,
                             "containsSyntheticMedia": True}},
            media_body=media)
        save_json(pending, {"channel_id": expected, "privacy": privacy})
        result = None
        while result is None:
            status, result = request.next_chunk(num_retries=3)
            if status:
                print(f"Upload {status.progress():.0%}", flush=True)
    finally:
        media.stream().close()
    info = {"video_id": result["id"], "channel_id": expected,
            "url": "https://www.youtube.com/watch?v=" + result["id"],
            "requested_privacy": privacy, "returned_status": result.get("status", {})}
    save_json(receipt, info)  # Record video ID before attempting optional thumbnail.
    pending.unlink()
    print("Uploaded: " + info["url"])
    thumbnail = folder / "thumbnail.jpg"
    if thumbnail.exists():
        thumbnail_media = MediaFileUpload(str(thumbnail), mimetype="image/jpeg")
        try:
            service.thumbnails().set(videoId=result["id"], media_body=thumbnail_media).execute()
        except Exception:
            print("Video uploaded; thumbnail could not be set. Add thumbnail.jpg in YouTube Studio.")
        finally:
            thumbnail_media.stream().close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("channels", help="Sign into YouTube and print the authorized channel ID")
    create = sub.add_parser("create", help="Generate and render; resume completed stages on rerun")
    create.add_argument("--topic", help="Omit for a random theme")
    create.add_argument("--language", default="Hindi")
    create.add_argument("--style", help="Omit for a random genre")
    create.add_argument("--seconds", type=int, default=240)
    create.add_argument("--out", type=Path)
    create.add_argument("--upload", action="store_true")
    for command in (create, sub.add_parser("upload")):
        if command is not create:
            command.add_argument("--out", type=Path, required=True)
        command.add_argument("--privacy", choices=["private", "unlisted", "public"],
                             default=os.getenv("YOUTUBE_PRIVACY", "public"))
        command.add_argument("--made-for-kids", action="store_true")
    rerender = sub.add_parser("render")
    rerender.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "channels":
        for channel in list_channels(youtube()):
            print(channel["id"] + "  " + channel["snippet"]["title"])
            if channel["snippet"]["title"].casefold() == os.getenv(
                "YOUTUBE_CHANNEL_NAME", "MusicalBUNKER").casefold():
                from dotenv import set_key
                set_key(ROOT / ".env", "YOUTUBE_CHANNEL_ID", channel["id"])
                print("Saved verified MusicalBUNKER channel ID.")
    elif args.command == "create":
        if not 181 <= args.seconds <= 300:
            parser.error("--seconds must be between 181 and 300 (duration is approximate).")
        args.out = args.out or ROOT / "output" / datetime.now().strftime("%Y%m%d-%H%M%S")
        prior = read_json(args.out / "request.json") if (args.out / "request.json").exists() else {}
        args.topic = args.topic or prior.get("topic") or random.choice(THEMES)
        args.style = args.style or prior.get("style") or random.choice(GENRES)
        print(f"Theme: {args.topic}. Genre: {args.style}. Language: {args.language}.")
        build(args)
    elif args.command == "render":
        render(args.out)
    elif args.command == "upload":
        upload(args.out, args.privacy, args.made_for_kids)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # OAuth exceptions can contain sensitive URLs: don't dump them or a traceback.
        if isinstance(error, (ValueError, RuntimeError)):
            message = str(error)
            key = os.getenv("GEMINI_API_KEY")
            print("Error: " + (message.replace(key, "[REDACTED]") if key else message), file=sys.stderr)
        else:
            print(f"Error ({type(error).__name__}). Check configuration, connectivity and "
                  "API access. Completed files were preserved.", file=sys.stderr)
        sys.exit(1)
