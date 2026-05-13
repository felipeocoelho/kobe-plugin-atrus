#!/usr/bin/env python3
"""atrus — transcreve uma URL de mídia via Firecrawl + Groq Whisper.

Uso:
    python transcribe_url.py <URL> [--format=analysis|reading]
                                   [--output-dir=<path>]
                                   [--title=<título humano>]

Imprime o **path do arquivo final** em stdout (pra o subagente capturar
e passar pro `kobe-attach`). Progresso e erros vão pro stderr.

Formatos:
    analysis  — texto cru com `[HH:MM:SS] texto` por segmento Whisper.
                Pensado pra alimentar skills/prompts que precisam de
                quando-foi-dito-cada-coisa. Default.
    reading   — HTML standalone com CSS embarcado (fundo creme, serif,
                line-height generoso, parágrafos curtos). Pra consumo
                humano direto no celular.

Envs obrigatórias:
    FIRECRAWL_API_KEY — https://www.firecrawl.dev
    GROQ_API_KEY      — https://console.groq.com (já no Kobe-base)

Dependências:
    firecrawl-py (>=2)  — instalada no venv do Kobe
    groq                 — já vem com o Kobe-base
    ffmpeg               — binário do sistema

Estratégia:
    1. Firecrawl scrape(formats=["audio"]) → URL assinada de MP3 (1h).
    2. Download HTTP direto.
    3. Se > 25MB (limite do Whisper), comprime mono 16kbps via ffmpeg.
    4. Se ainda passar, chunking de 10min via ffmpeg segment.
    5. Whisper-large-v3 com `language="pt"`, `temperature=0`,
       `response_format="verbose_json"` (sempre — pegamos timestamps
       mesmo no formato reading porque servem pra agrupar parágrafos).
    6. Renderiza no formato escolhido e salva em `output-dir`.

Decisões intencionais:
    - Sem retry automático: erro de mídia indisponível é definitivo,
      retry só gasta cota.
    - Limpeza do workdir mesmo em erro (try/finally).
    - Para chunking, ajusta os timestamps de cada chunk somando o
      offset (chunk N começa em N * CHUNK_SECONDS) — assim o formato
      analysis tem timestamps absolutos coerentes.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


GROQ_MAX_BYTES = 25 * 1024 * 1024
WHISPER_MODEL = "whisper-large-v3"
CHUNK_SECONDS = 600  # 10 min — limite prático pra chunking + Whisper


# HTML standalone pra formato "reading". CSS inline pra abrir em
# qualquer navegador sem dependência externa.
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg: #fdf6e3;
    --ink: #3a2e1f;
    --accent: #6b4423;
    --rule: #d4c4a0;
    --meta: #8a7045;
  }}
  body {{
    background: var(--bg);
    color: var(--ink);
    font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
    font-size: 18px;
    line-height: 1.75;
    margin: 0;
    padding: 2em 1em 4em;
  }}
  main {{
    max-width: 65ch;
    margin: 0 auto;
  }}
  h1 {{
    font-size: 1.4em;
    margin: 0 0 0.4em;
    color: var(--accent);
    border-bottom: 1px solid var(--rule);
    padding-bottom: 0.3em;
    font-weight: 600;
  }}
  .meta {{
    color: var(--meta);
    font-size: 0.85em;
    margin-bottom: 2em;
  }}
  .meta a {{
    color: var(--accent);
    word-break: break-all;
  }}
  p {{
    margin: 0 0 1.2em 0;
    text-align: justify;
    hyphens: auto;
  }}
  @media (max-width: 480px) {{
    body {{ font-size: 17px; padding: 1.2em 1em 3em; }}
    p {{ text-align: left; }}
  }}
</style>
</head>
<body>
  <main>
    <h1>{title}</h1>
    <div class="meta">Fonte: <a href="{url}">{url}</a> &middot; Transcrito em {date}</div>
{body}
  </main>
</body>
</html>
"""


def log(msg: str) -> None:
    print(f"[atrus] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"[atrus] ERRO: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        die(f"Missing {name}", code=2)
    return val


# ───────────────────────── Firecrawl ─────────────────────────────────

def firecrawl_get_audio_url(url: str, api_key: str) -> str:
    """Chama o Firecrawl scrape solicitando formato 'audio'. Retorna URL do MP3.

    Compat de SDK: firecrawl-py v1 expunha `scrape_url(...)`; v2+ renomeou
    pra `scrape(...)`. Pegamos qualquer um via `getattr`. Tentamos também
    ambos os shapes de chamada (kwargs `formats=` direto e o legado
    `params={"formats": ...}`).
    """
    try:
        from firecrawl import FirecrawlApp
    except ImportError:
        die(
            "firecrawl-py não instalado. Roda: "
            "$KOBE_CLAUDE_CWD/.venv/bin/pip install firecrawl-py"
        )
    log("scrape via Firecrawl (formats=audio)…")
    app = FirecrawlApp(api_key=api_key)

    scrape_fn = getattr(app, "scrape", None) or getattr(app, "scrape_url", None)
    if scrape_fn is None:
        die(
            "firecrawl-py: nenhum método scrape/scrape_url no SDK instalado "
            f"(versão incompatível). dir={[a for a in dir(app) if not a.startswith('_')][:20]}"
        )

    try:
        result = scrape_fn(url, formats=["audio"])
    except TypeError:
        try:
            result = scrape_fn(url, params={"formats": ["audio"]})
        except Exception as exc:  # noqa: BLE001
            die(f"Firecrawl falhou (params fallback): {exc}")
    except Exception as exc:  # noqa: BLE001
        die(f"Firecrawl falhou: {exc}")

    audio_url = _extract_audio_url(result)
    if not audio_url:
        die(f"Firecrawl não retornou audio. Resposta: {_brief(result)}")
    return audio_url


def _extract_audio_url(result: Any) -> str | None:
    if result is None:
        return None
    if isinstance(result, dict):
        data = result.get("data") or result
        for key in ("audio", "audioUrl", "audio_url"):
            val = data.get(key) if isinstance(data, dict) else None
            if isinstance(val, str):
                return val
    for key in ("audio", "audioUrl", "audio_url"):
        val = getattr(result, key, None)
        if isinstance(val, str):
            return val
        data = getattr(result, "data", None)
        if data is not None:
            val = getattr(data, key, None)
            if isinstance(val, str):
                return val
    return None


def _brief(obj: Any) -> str:
    s = repr(obj)
    return s if len(s) < 500 else s[:500] + "…"


# ───────────────────────── áudio: download / compressão / chunk ─────────

def download(url: str, dest: Path) -> None:
    log(f"baixando MP3 → {dest}")
    urllib.request.urlretrieve(url, str(dest))


def compress_if_needed(mp3: Path) -> Path:
    size = mp3.stat().st_size
    if size <= GROQ_MAX_BYTES:
        return mp3
    log(f"arquivo {size // 1024 // 1024}MB > 25MB — comprimindo (mono 16kbps)…")
    compressed = mp3.with_name(mp3.stem + "-mono16k.mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3), "-ac", "1", "-ar", "16000",
             "-b:a", "16k", str(compressed)],
            check=True, capture_output=True,
        )
    except FileNotFoundError:
        die("ffmpeg: command not found. Instala com: sudo apt install ffmpeg")
    except subprocess.CalledProcessError as exc:
        die(f"ffmpeg falhou: {exc.stderr.decode('utf-8', errors='replace')[:500]}")
    mp3.unlink(missing_ok=True)
    return compressed


def split_chunks(mp3: Path, seconds: int) -> list[Path]:
    pattern = mp3.parent / f"{mp3.stem}-chunk-%03d.mp3"
    log(f"dividindo em chunks de {seconds}s…")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3), "-f", "segment",
             "-segment_time", str(seconds), "-c", "copy", str(pattern)],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        die(f"ffmpeg segment falhou: {exc.stderr.decode('utf-8', errors='replace')[:500]}")
    return sorted(mp3.parent.glob(f"{mp3.stem}-chunk-*.mp3"))


# ───────────────────────── Whisper ───────────────────────────────────

def whisper_segments(path: Path, api_key: str) -> list[dict]:
    """Chama Whisper com verbose_json. Devolve lista de segments dict.

    Cada segment tem ao menos `start` (s), `end` (s) e `text`.
    """
    try:
        from groq import Groq
    except ImportError:
        die("groq SDK não instalado. (Deveria estar no Kobe-base.)")
    client = Groq(api_key=api_key)
    with path.open("rb") as fh:
        audio_bytes = fh.read()
    try:
        res = client.audio.transcriptions.create(
            file=(path.name, audio_bytes),
            model=WHISPER_MODEL,
            language="pt",
            temperature=0,
            response_format="verbose_json",
        )
    except Exception as exc:  # noqa: BLE001
        die(f"Groq Whisper falhou: {exc}")

    if isinstance(res, dict):
        segments = res.get("segments") or []
    else:
        segments = getattr(res, "segments", None) or []
    out: list[dict] = []
    for s in segments:
        out.append({
            "start": float(_get(s, "start", 0.0) or 0.0),
            "end": float(_get(s, "end", 0.0) or 0.0),
            "text": str(_get(s, "text", "") or "").strip(),
        })
    return out


def _get(obj: Any, key: str, default: Any) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def transcribe_all(mp3: Path, groq_key: str) -> list[dict]:
    """Devolve lista plana de segments com timestamps absolutos. Faz
    chunking se necessário e ajusta o offset de cada chunk."""
    if mp3.stat().st_size <= GROQ_MAX_BYTES:
        log("transcrevendo (peça única)…")
        return whisper_segments(mp3, groq_key)

    chunks = split_chunks(mp3, CHUNK_SECONDS)
    if not chunks:
        die("split em chunks retornou zero arquivos")
    all_segs: list[dict] = []
    for i, chunk in enumerate(chunks):
        offset = i * CHUNK_SECONDS
        log(f"transcrevendo chunk {i + 1}/{len(chunks)}…")
        for seg in whisper_segments(chunk, groq_key):
            all_segs.append({
                "start": seg["start"] + offset,
                "end": seg["end"] + offset,
                "text": seg["text"],
            })
        chunk.unlink(missing_ok=True)
    return all_segs


# ───────────────────────── renderers ─────────────────────────────────

def _format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def render_analysis(segments: list[dict]) -> str:
    """Texto cru com timestamps absolutos, um segmento por linha."""
    lines: list[str] = []
    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue
        lines.append(f"[{_format_timestamp(seg['start'])}] {text}")
    return "\n".join(lines) + "\n"


def _group_paragraphs(
    segments: list[dict], max_segs: int = 4, max_gap: float = 2.0
) -> list[str]:
    """Agrupa segments em parágrafos.

    Quebra parágrafo quando:
    - atingiu `max_segs` segments seguidos, OU
    - gap entre fim do segment anterior e início do atual > `max_gap`s
      (heurística de pausa, marca uma nova ideia).
    """
    paragraphs: list[str] = []
    current: list[str] = []
    last_end: float | None = None
    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue
        gap_too_big = last_end is not None and (seg["start"] - last_end) > max_gap
        if current and (len(current) >= max_segs or gap_too_big):
            paragraphs.append(" ".join(current))
            current = []
        current.append(text)
        last_end = seg["end"]
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def render_reading(segments: list[dict], title: str, url: str) -> str:
    """HTML standalone com CSS embarcado."""
    paragraphs = _group_paragraphs(segments)
    body = "\n".join(f"    <p>{html.escape(p)}</p>" for p in paragraphs)
    return HTML_TEMPLATE.format(
        title=html.escape(title or "Transcrição"),
        url=html.escape(url),
        date=datetime.now().strftime("%d/%m/%Y"),
        body=body,
    )


# ───────────────────────── utilidades ────────────────────────────────

def _slug_from_url(url: str) -> str:
    """Slug curto pro nome do arquivo final.

    YouTube: usa o video ID (11 chars). Outras URLs: SHA-1 truncado.
    """
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([\w-]{11})", url)
    if m:
        return m.group(1)
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _default_output_dir() -> Path:
    kobe_home = os.environ.get("KOBE_HOME") or str(Path.home() / "kobe")
    return Path(kobe_home) / "projetos" / "transcricoes"


# ───────────────────────── main ──────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="atrus — transcreve URL de mídia via Firecrawl + Groq Whisper"
    )
    parser.add_argument("url")
    parser.add_argument(
        "--format", choices=("analysis", "reading"), default="analysis",
        help="formato de saída: analysis (txt com timestamps) ou reading (html estilizado)",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="diretório de saída (default: $KOBE_HOME/projetos/transcricoes)",
    )
    parser.add_argument(
        "--title", default=None,
        help="título humano da transcrição (usado no HTML; default: slug da URL)",
    )
    args = parser.parse_args()

    fc_key = require_env("FIRECRAWL_API_KEY")
    groq_key = require_env("GROQ_API_KEY")

    output_dir = Path(args.output_dir) if args.output_dir else _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    audio_url = firecrawl_get_audio_url(args.url, fc_key)

    workdir = Path(tempfile.mkdtemp(prefix="atrus-"))
    mp3 = workdir / "input.mp3"
    try:
        download(audio_url, mp3)
        mp3 = compress_if_needed(mp3)
        log(f"transcrevendo no formato '{args.format}'…")
        segments = transcribe_all(mp3, groq_key)
        if not segments:
            die("transcrição vazia")

        slug = _slug_from_url(args.url)
        if args.format == "analysis":
            content = render_analysis(segments)
            out_path = output_dir / f"{slug}-analysis.txt"
        else:
            content = render_reading(segments, args.title or slug, args.url)
            out_path = output_dir / f"{slug}-reading.html"

        out_path.write_text(content, encoding="utf-8")
        # stdout = path do arquivo final (pra o subagente capturar)
        print(str(out_path))
    finally:
        for f in workdir.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            workdir.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    main()
