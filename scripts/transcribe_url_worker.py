#!/usr/bin/env python3
"""atrus.transcribe_url_worker — wrapper detached pra transcribe_url.py.

Roda dentro de `kobe-dispatch` (e opcionalmente envelopado por
`kobe-heartbeat-run`) pra que a transcrição inteira aconteça em background.
Emite `kobe-notify` no início e no fim, e `kobe-attach` com o arquivo
produzido quando dá certo.

Uso (geralmente chamado pelo subagente atrus via kobe-dispatch):
    transcribe_url_worker.py <URL> --format <analysis|reading> [--diarize]
                             [--label "<texto pra notify>"]

Envs herdadas (vêm do `claude -p` parent + .env do Kobe):
    KOBE_HOME, KOBE_TELEGRAM_BOT_TOKEN, KOBE_CHAT_ID, KOBE_THREAD_ID,
    FIRECRAWL_API_KEY, GROQ_API_KEY, ASSEMBLYAI_API_KEY.

Sai com 0 em sucesso, exit code do transcribe_url.py em erro.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


_SCRIPT_DIR = Path(__file__).resolve().parent
_TRANSCRIBE_SCRIPT = _SCRIPT_DIR / "transcribe_url.py"


def _kobe_home() -> Path:
    return Path(os.environ.get("KOBE_HOME") or os.environ.get("KOBE_CLAUDE_CWD")
                or str(Path.home() / "kobe"))


def _venv_python() -> str:
    venv_py = _kobe_home() / ".venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable  # fallback se não rodando do venv do Kobe


def _bin(name: str) -> Path:
    return _kobe_home() / "bot" / "bin" / name


def _notify(text: str) -> None:
    try:
        subprocess.run(
            [str(_bin("kobe-notify")), text],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except Exception:  # noqa: BLE001 — notify é nice-to-have
        pass


def _attach(path: Path) -> None:
    try:
        subprocess.run(
            [str(_bin("kobe-attach")), str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass


def _short(text: str, n: int = 80) -> str:
    text = text.strip()
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def _fmt_elapsed(secs: float) -> str:
    s = int(secs)
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{ss:02d}s"
    return f"{ss}s"


def main() -> int:
    parser = argparse.ArgumentParser(prog="transcribe_url_worker")
    parser.add_argument("url")
    parser.add_argument(
        "--format", choices=("analysis", "reading", "srt", "srt_ptbr"),
        default="analysis",
    )
    parser.add_argument("--diarize", action="store_true")
    parser.add_argument("--label", default="", help="texto pra mostrar no kobe-notify (default: URL truncada)")
    args = parser.parse_args()

    label = args.label or _short(args.url)
    fmt_name = {
        "analysis": "TXT",
        "reading": "HTML",
        "srt": "SRT",
        "srt_ptbr": "SRT-PTBR",
    }.get(args.format, args.format)
    speakers_tag = " + speakers" if args.diarize else ""
    _notify(f"▶️ atrus: iniciando ({fmt_name}{speakers_tag})\n{label}")

    cmd = [
        _venv_python(),
        str(_TRANSCRIBE_SCRIPT),
        args.url,
        f"--format={args.format}",
    ]
    if args.diarize:
        cmd.append("--diarize")

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        _notify(f"❌ atrus: falha lançando subprocess: {exc}\n{label}")
        return 1
    elapsed = time.monotonic() - started

    if proc.returncode != 0:
        # Repassa as últimas 3 linhas de stderr do transcribe_url.py
        err_lines = [l for l in (proc.stderr or "").splitlines() if l.strip()][-3:]
        err_text = "\n".join(err_lines) or "(sem detalhe no stderr)"
        _notify(
            f"❌ atrus: falhou após {_fmt_elapsed(elapsed)}\n{label}\n```\n{err_text}\n```"
        )
        return proc.returncode

    # transcribe_url.py emite duas linhas no stdout:
    #   1: path do arquivo final
    #   2: engine=<id>  (groq-whisper | assemblyai | assemblyai-fallback)
    stdout_lines = [l for l in (proc.stdout or "").splitlines() if l.strip()]
    if not stdout_lines:
        _notify(f"❌ atrus: terminou sem stdout ({_fmt_elapsed(elapsed)})\n{label}")
        return 1

    out_path = Path(stdout_lines[0])
    engine_used = ""
    for line in stdout_lines:
        if line.startswith("engine="):
            engine_used = line.split("=", 1)[1].strip()
            break
    if not out_path.is_file():
        _notify(f"❌ atrus: path retornado não existe: {out_path}\n{label}")
        return 1

    # Aviso de fallback: operador precisa saber quando AssemblyAI foi
    # usado por necessidade (e não por escolha explícita de speakers).
    engine_tag = ""
    if engine_used == "assemblyai-fallback":
        engine_tag = " (via AssemblyAI fallback — Whisper indisponível)"
    _notify(f"✅ atrus: pronto em {_fmt_elapsed(elapsed)}{engine_tag}\n{label}")
    _attach(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
