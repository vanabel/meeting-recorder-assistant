# Onefile build

This project can be packaged into a single Windows GUI executable with PyInstaller.

## Prerequisites

- Python 3.11 virtual environment at `.venv`
- PyInstaller installed into that virtual environment

Install the build dependency:

```powershell
.\.venv\Scripts\python.exe -m pip install .[build]
```

## Build

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_onefile.ps1
```

Build output:

- `dist\Meeting Recorder Assistant.exe`
- `dist\config.json` if the local project already has one
- `dist\config.example.json`
- `dist\README.md`

The packaged app reads `config.json` and writes `logs\meeting-recorder.log` next to the executable.
