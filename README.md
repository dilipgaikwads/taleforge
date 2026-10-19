# TaleForge

> Autonomous AI story-video studio: from a topic to a fact-checked, narrated, captioned YouTube Short, powered by free and open-source tools. (Internal name: VideoFactory.)

An autonomous AI short-video factory: topic → research → fact-check → script → scenes →
pictures → narration → captions → 1080×1920 video → quality check → YouTube upload,
controlled from your phone. Built to run at **$0** with free and open-source tools.

## Quick start

**Mac** (Terminal, inside this folder):

    bash setup/mac-setup.sh

**Windows** (PowerShell, inside this folder; not yet tested on a real Windows PC):

    powershell -ExecutionPolicy Bypass -File setup\windows-setup.ps1

The script installs Python 3.13 and FFmpeg if needed, creates the private toolbox,
asks for your free Gemini key and a dashboard password, and starts the factory
automatically at every login. It is safe to run again: finished steps are skipped.

Then open **http://127.0.0.1:8000** on the computer and log in.
Full beginner instructions, YouTube setup, phone setup and troubleshooting:
**docs/VideoFactory-Setup-Guide.pdf** (or .docx).

## Everyday commands (Mac)

| What | Command |
|---|---|
| Status and phone links | `bash factory.sh status` |
| Restart after an update | `bash factory.sh restart` |
| Stop / start | `bash factory.sh stop` · `bash factory.sh start` |
| Recent activity | `bash factory.sh logs` |

## What is never in this repository

`.env` (keys, password hash), `secrets/` (YouTube permission), `data/` (database,
videos, logs), `venv/` and `bin/` (installed tools). `.gitignore` keeps them out.
Never commit them, even to a private repository.

## Folders

| Path | Contents |
|---|---|
| `app/` | The application (Flask dashboard, production line, providers) |
| `setup/` | One-command setup scripts for Mac and Windows |
| `docs/` | Complete setup guide (PDF and Word) |
| `tools/storybook-animation-test/` | Experimental walking-and-talking storybook prototype |
| `factory.sh`, `start.sh` | Mac control and start scripts |
| `start-windows.bat` | Windows start script |

## Status

Checkpoints 1–13 working and tested on an Intel MacBook Pro (macOS Tahoe).
YouTube connection and private upload built (checkpoints 14–15).
Next: scheduler, animated story mode, long videos.
