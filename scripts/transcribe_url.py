#!/usr/bin/env python3
"""atrus — transcreve uma URL de mídia via Firecrawl + Groq Whisper.

Uso:
    python transcribe_url.py <URL>

Imprime a transcrição em stdout. Mensagens de progresso e erro em stderr.

Envs obrigatórias:
    FIRECRAWL_API_KEY — https://www.firecrawl.dev
    GROQ_API_KEY      — https://console.groq.com (já existe no Kobe-base)

Dependências:
    firecrawl-py (`pip install firecrawl-py` no venv do Kobe)
    groq         (já vem com o Kobe-base)
    ffmpeg       (binário do sistema, instalado pelo install.sh do Kobe)

Estratégia:
    1. Firecrawl scrape com formats=["audio"] → URL assinada de MP3 (válida ~1h).
    2. Download via HTTP direto.
    3. Se > 25MB (limite do Groq Whisper), comprime pra mono 16kbps via ffmpeg.
       Vídeos de 2h ficam em ~14MB depois disso.
    4. Se ainda passar, divide em chunks de 10min via ffmpeg segment, transcreve
       cada um, concatena com quebra dupla pra preservar fronteiras.
    5. Groq Whisper-large-v3, language="pt", temperature=0 (determinístico,
       anti-alucinação). Não usa turbo nem distil — qualidade > velocidade.

Decisões intencionais:
    - Sem retry automático: erro de mídia indisponível é definitivo, retry só
      gasta cota. O subagente do plugin decide se vale tentar de novo.
    - Limpa o /tmp ao final (try/finally), mesmo em erro.
    - Não traduz nem resuma — devolve o texto literal do Whisper.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


GROQ_MAX_BYTES = 25 * 1024 * 1024  # limite do endpoint Whisper da Groq
WHISPER_MODEL = "whisper-large-v3"
CHUNK_SECONDS = 600  # 10 minutos


def log(msg: str) -> None:
    """Mensagem de progresso pra stderr (não polui o stdout que é a transcrição)."""
    print(f"[atrus] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"[atrus] ERRO: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        die(f"Missing {name}", code=2)
    return val


def firecrawl_get_audio_url(url: str, api_key: str) -> str:
    """Chama o Firecrawl scrape solicitando o formato 'audio'. Retorna URL do MP3.

    A API do Firecrawl varia entre versões — tentamos os dois shapes de
    resposta conhecidos (dict aninhado em `data` e atributo direto).
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
    try:
        result = app.scrape_url(url, formats=["audio"])
    except TypeError:
        # SDK mais antigo usava 'params={"formats": ...}'
        result = app.scrape_url(url, params={"formats": ["audio"]})
    except Exception as exc:  # noqa: BLE001
        die(f"Firecrawl falhou: {exc}")

    audio_url = _extract_audio_url(result)
    if not audio_url:
        die(f"Firecrawl não retornou audio. Resposta: {_brief(result)}")
    return audio_url


def _extract_audio_url(result) -> str | None:
    """Tenta extrair a URL do MP3 da resposta do Firecrawl em vários shapes."""
    if result is None:
        return None
    # Shape 1: dict aninhado em data
    if isinstance(result, dict):
        data = result.get("data") or result
        for key in ("audio", "audioUrl", "audio_url"):
            val = data.get(key) if isinstance(data, dict) else None
            if isinstance(val, str):
                return val
    # Shape 2: objeto com atributos
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


def _brief(obj) -> str:
    s = repr(obj)
    return s if len(s) < 500 else s[:500] + "…"


def download(url: str, dest: Path) -> None:
    log(f"baixando MP3 → {dest}")
    urllib.request.urlretrieve(url, str(dest))


def compress_if_needed(mp3: Path) -> Path:
    """Se > 25MB, recodifica pra mono 16kbps via ffmpeg. Retorna o path final."""
    size = mp3.stat().st_size
    if size <= GROQ_MAX_BYTES:
        return mp3
    log(f"arquivo {size // 1024 // 1024}MB > 25MB — comprimindo (mono 16kbps)…")
    compressed = mp3.with_name(mp3.stem + "-mono16k.mp3")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(mp3),
                "-ac", "1", "-ar", "16000", "-b:a", "16k",
                str(compressed),
            ],
            check=True, capture_output=True,
        )
    except FileNotFoundError:
        die("ffmpeg: command not found. Instala com: sudo apt install ffmpeg")
    except subprocess.CalledProcessError as exc:
        die(f"ffmpeg falhou: {exc.stderr.decode('utf-8', errors='replace')[:500]}")
    mp3.unlink(missing_ok=True)
    return compressed


def split_chunks(mp3: Path, seconds: int) -> list[Path]:
    """Divide o MP3 em pedaços de ~`seconds`s via ffmpeg segment."""
    pattern = mp3.parent / f"{mp3.stem}-chunk-%03d.mp3"
    log(f"dividindo em chunks de {seconds}s…")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(mp3),
                "-f", "segment", "-segment_time", str(seconds),
                "-c", "copy", str(pattern),
            ],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        die(f"ffmpeg segment falhou: {exc.stderr.decode('utf-8', errors='replace')[:500]}")
    return sorted(mp3.parent.glob(f"{mp3.stem}-chunk-*.mp3"))


def whisper_transcribe(path: Path, api_key: str) -> str:
    """Chama o Groq Whisper-large-v3 com language=pt e temperature=0."""
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
            response_format="text",
        )
    except Exception as exc:  # noqa: BLE001
        die(f"Groq Whisper falhou: {exc}")
    return res if isinstance(res, str) else getattr(res, "text", "")


def transcribe(mp3: Path, groq_key: str) -> str:
    """Transcreve, chunking se necessário. Devolve texto completo."""
    if mp3.stat().st_size <= GROQ_MAX_BYTES:
        log("transcrevendo (peça única)…")
        return whisper_transcribe(mp3, groq_key).strip()

    chunks = split_chunks(mp3, CHUNK_SECONDS)
    if not chunks:
        die("split em chunks retornou zero arquivos")
    pieces: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        log(f"transcrevendo chunk {i}/{len(chunks)}…")
        pieces.append(whisper_transcribe(chunk, groq_key).strip())
        chunk.unlink(missing_ok=True)
    return "\n\n".join(p for p in pieces if p)


def main(url: str) -> None:
    fc_key = require_env("FIRECRAWL_API_KEY")
    groq_key = require_env("GROQ_API_KEY")

    audio_url = firecrawl_get_audio_url(url, fc_key)

    # Workdir temporário só pra este job; limpo no finally.
    workdir = Path(tempfile.mkdtemp(prefix="atrus-"))
    mp3 = workdir / "input.mp3"
    try:
        download(audio_url, mp3)
        mp3 = compress_if_needed(mp3)
        transcript = transcribe(mp3, groq_key)
        if not transcript:
            die("transcrição vazia")
        # stdout = resultado final.
        print(transcript)
    finally:
        # Limpa tudo dentro do workdir mesmo em erro.
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
    if len(sys.argv) != 2:
        print("uso: python transcribe_url.py <URL>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
