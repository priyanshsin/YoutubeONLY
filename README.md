# MusicalBUNKER Web

Double-click **Start-Web.bat**. It starts the included MusicalBUNKER-Web.exe and opens http://127.0.0.1:8765. No Python installation, desktop EXE, parent Channel files, or Google Cloud CLI is needed to run this copy.

Keep the server running, your PC awake, and the internet connected. Closing the browser is fine; closing the server stops the scheduler. This is a local app, not an always-on hosted website. The saved countdown survives restarts.

The first release is scheduled 24 hours after the first launch. Preparation starts one hour before release. Each batch creates 2 Hindi songs, 2 English songs and 1 Hinglish song with random genres and themes, around 4 minutes each (strictly over 3 minutes). At the countdown deadline the app automatically uploads publicly to MusicalBUNKER, verifies YouTube processing, and emails priyanshsingh.indian@gmail.com with titles, links, publication times in IST, and all five full MP3 attachments. No publish confirmation is required for scheduled batches. Generation or processing delays can move actual publication later than the countdown.

The Release gallery button opens every automatically published song from this Web app, including previews, genre/language search, and YouTube links. The YouTube channel button opens MusicalBUNKER. Older songs created by the desktop app are not imported.

The desktop studio remains separate and requires your explicit Publish action.

## Independence and files

Keep the whole Web folder together. The executable includes its own Python and libraries. Runtime files are .env, secrets/, tools/, templates/, static/, and data/. Generation credentials, Google refresh credentials, YouTube token, email credentials, and FFmpeg/FFprobe are local copies. Runtime reads no parent Channel configuration or output files. Source, tests, build/ and .venv/ are development files and are not required by Start-Web.bat.

The .env and secrets/ files contain private credentials. Do not publish or share this folder as a public source repository. Internet services and Windows itself are still required.

## Recovery

Successful uploads have saved receipts and are reused on retries. If a provider fails, the dashboard shows the error and pauses the batch. Retry unfinished work resumes it. An ambiguous interrupted upload intentionally requires checking YouTube Studio to avoid duplicate public uploads. Email retries use Resend idempotency keys; accepted means accepted by the provider, not verified inbox delivery. If an uncertain email retry is older than 23 hours, check Resend before resending.

A missed release is processed when the app runs again; it does not publish a flood of missed daily batches. Google or YouTube can require renewed authorization if a token is revoked or expires. API usage and provider limits still apply.

## Validation

The automated tests exercise the language split, daily schedule, five-public-upload workflow, five email attachments, retry behavior, local request protection, and gallery. Test media and services are mocked; tests do not publish music or send email. The generation key was checked with a small real Gemini request and both local Cloud and YouTube authentication were verified. A full new live five-song batch and inbox delivery have not been tested.
