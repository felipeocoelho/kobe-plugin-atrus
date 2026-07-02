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
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


GROQ_MAX_BYTES = 25 * 1024 * 1024
WHISPER_MODEL = "whisper-large-v3"
CHUNK_SECONDS = 600  # 10 min — limite prático pra chunking + Whisper

# Fallback pra Whisper não pontuar: se o buffer da sentence atual passar
# disso sem ver `.!?`, fechamos manualmente (adicionando `.`) pra evitar
# parágrafos gigantescos.
MAX_WORDS_PER_SENTENCE = 28


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
  .ts {{
    color: var(--meta);
    font-size: 0.78em;
    font-family: "SF Mono", Consolas, Menlo, monospace;
    margin-right: 0.2em;
  }}
  section.speaker {{
    margin: 0 0 1.8em;
  }}
  section.speaker h2 {{
    font-size: 0.9em;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--accent);
    font-weight: 600;
    margin: 0 0 0.6em;
    padding: 0;
    border: none;
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

# Quantas frases por parágrafo no rendering final. TurboScribe usa ~3-4;
# 3 fica visualmente confortável tanto no TXT quanto no HTML.
SENTENCES_PER_PARAGRAPH = 3


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

def firecrawl_scrape(url: str, api_key: str) -> tuple[str, str | None]:
    """Chama o Firecrawl scrape solicitando formato 'audio'.

    Retorna `(audio_url, title_or_none)`. O título vem do
    `metadata.title` (`ogTitle`/`twitterTitle` como fallback) — útil pro
    cabeçalho do HTML; pode ser None se a página não declarar.

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

    # Páginas pesadas (ex: youtube.com/live, com player + chat ao vivo) podem
    # estourar o timeout default do Firecrawl. FIRECRAWL_TIMEOUT_MS permite
    # subir o orçamento sem mexer no código. Sem a env, mantém o default do
    # SDK (compat total com o comportamento anterior).
    timeout_ms_raw = os.environ.get("FIRECRAWL_TIMEOUT_MS")
    extra: dict = {}
    if timeout_ms_raw:
        try:
            extra["timeout"] = int(timeout_ms_raw)
        except ValueError:
            log(f"FIRECRAWL_TIMEOUT_MS inválido ({timeout_ms_raw!r}); ignorando")

    try:
        result = scrape_fn(url, formats=["audio"], **extra)
    except TypeError:
        try:
            params = {"formats": ["audio"]}
            if "timeout" in extra:
                params["timeout"] = extra["timeout"]
            result = scrape_fn(url, params=params)
        except Exception as exc:  # noqa: BLE001
            die(f"Firecrawl falhou (params fallback): {exc}")
    except Exception as exc:  # noqa: BLE001
        die(f"Firecrawl falhou: {exc}")

    audio_url = _extract_audio_url(result)
    if not audio_url:
        die(f"Firecrawl não retornou audio. Resposta: {_brief(result)}")
    title = _extract_title(result)
    return audio_url, title


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


def _extract_title(result: Any) -> str | None:
    """Tenta extrair título humano do scrape Firecrawl.

    Ordem de tentativa: `metadata.title` → `metadata.ogTitle` →
    `metadata.twitterTitle`. Aceita tanto dict quanto objeto com
    atributos (compat entre versões do SDK).
    """
    metadata = None
    if isinstance(result, dict):
        metadata = result.get("metadata") or (
            result.get("data", {}).get("metadata") if isinstance(result.get("data"), dict) else None
        )
    else:
        metadata = getattr(result, "metadata", None)
        if metadata is None:
            data = getattr(result, "data", None)
            if data is not None:
                metadata = getattr(data, "metadata", None)
    if metadata is None:
        return None
    for key in ("title", "ogTitle", "twitterTitle"):
        val = metadata.get(key) if isinstance(metadata, dict) else getattr(metadata, key, None)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _brief(obj: Any) -> str:
    s = repr(obj)
    return s if len(s) < 500 else s[:500] + "…"


# ───────────────────────── áudio: download / compressão / chunk ─────────

def download(url: str, dest: Path) -> None:
    log(f"baixando MP3 → {dest}")
    urllib.request.urlretrieve(url, str(dest))


# ──────────────── source dispatcher (Drive / link direto / Firecrawl) ────

# Extensões que indicam link direto pra arquivo de áudio/vídeo. Quando a
# URL termina em uma destas, pulamos Firecrawl e baixamos via urllib.
_DIRECT_AV_SUFFIXES = (
    ".mp3", ".m4a", ".wav", ".ogg", ".flac",
    ".mp4", ".webm", ".mov", ".mkv",
)


def _is_google_drive(url: str) -> bool:
    return bool(re.search(r"drive\.google\.com/file/d/[\w-]+", url))


def _is_direct_av_link(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    return any(path.endswith(ext) for ext in _DIRECT_AV_SUFFIXES)


def acquire_audio(
    url: str, fc_key: Optional[str], workdir: Path
) -> tuple[Path, Optional[str]]:
    """Resolve URL → arquivo local (mp3/mp4/etc). Retorna (path, title_or_none).

    Roteia conforme o tipo da URL:
    - Google Drive (`drive.google.com/file/d/<ID>/...`) → gdown direto.
      Título vem do nome do arquivo no Drive (sem extensão).
    - Link direto (`.../arquivo.mp3` ou `.mp4` etc.) → urllib download.
      Título vem do nome do arquivo na URL (sem extensão).
    - Outras URLs (YouTube/Vimeo/podcast/etc.) → Firecrawl scrape + download.
      Título vem do `metadata.title` do Firecrawl.

    `fc_key` só é usado no caminho Firecrawl. Caller pode passar None se
    soubermos que vai por outro caminho.
    """
    if _is_google_drive(url):
        return _acquire_drive(url, workdir)
    if _is_direct_av_link(url):
        return _acquire_direct(url, workdir)
    if not fc_key:
        die(
            "URL não é Google Drive nem link direto, mas FIRECRAWL_API_KEY "
            "não está configurada — sem caminho pra obter o áudio."
        )
    audio_url, scraped_title = firecrawl_scrape(url, fc_key)  # type: ignore[arg-type]
    dest = workdir / "input.mp3"
    download(audio_url, dest)
    return dest, scraped_title


def _filename_to_title(filename: str) -> Optional[str]:
    """Converte um filename em título humano-legível.

    Estratégia: tira extensão, troca _ e - por espaço, capitaliza primeira
    letra. "minha_reuniao_com_cliente_X.mp4" → "Minha reuniao com cliente X".

    Filtra só:
    - filename vazio.
    - hash hexadecimal puro (`[a-f0-9]{16,}`) — raro, mas não vira título.
    Nomes timestamped tipo Zoom recording (`GMT20260427-...Recording_1920x1080`)
    passam normalmente — não são hash hex puro.
    """
    stem = Path(filename).stem.strip()
    if not stem:
        return None
    if re.fullmatch(r"[a-f0-9]{16,}", stem.lower()):
        return None  # hash hex puro, não é título útil
    cleaned = stem.replace("_", " ").replace("-", " ").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return None
    return cleaned[:1].upper() + cleaned[1:]


def _acquire_drive(url: str, workdir: Path) -> tuple[Path, Optional[str]]:
    """Baixa arquivo do Google Drive via gdown. Retorna (path, title).

    Título vem do nome do arquivo no Drive (gdown preserva). Passamos
    `output=<workdir>/` (com `/` final) pra que o gdown grave usando
    o filename remoto em vez de renomear pra um nome fixo.

    `gdown` lida com a página de confirmação que o Drive força em arquivos
    grandes (>100MB) — pra arquivos pequenos vira HTTP direto.

    Em gdown 6+, `fuzzy` foi removido. Extraímos o `file_id` do regex
    manualmente e passamos como `id=`. Funciona pra qualquer variante de
    URL do Drive (.../view, .../view?usp=sharing, .../edit, etc.).
    """
    try:
        import gdown  # type: ignore
    except ImportError:
        die(
            "gdown não instalado. Roda: "
            "$KOBE_CLAUDE_CWD/.venv/bin/pip install gdown"
        )
    m = re.search(r"drive\.google\.com/file/d/([\w-]+)", url)
    if not m:
        die(f"não consegui extrair file_id da URL do Drive: {url}")
    file_id = m.group(1)
    log(f"baixando do Google Drive (id={file_id[:8]}…)…")
    # Passar diretório como output (com / no final) faz o gdown usar o
    # nome original do arquivo, em vez de renomear pra "drive-input".
    output_dir = str(workdir) + os.sep
    try:
        result_path = gdown.download(id=file_id, output=output_dir, quiet=False)
    except Exception as exc:  # noqa: BLE001
        die(f"gdown falhou baixando do Drive: {exc}")
    if not result_path:
        die("gdown não retornou path — Drive recusou o download (arquivo privado?)")
    path = Path(result_path)
    if not path.is_file() or path.stat().st_size == 0:
        die(f"gdown retornou path inválido ou vazio: {path}")
    log(f"baixado: {path.name} ({path.stat().st_size // 1024 // 1024}MB)")
    title = _filename_to_title(path.name)
    return path, title


def _acquire_direct(url: str, workdir: Path) -> tuple[Path, Optional[str]]:
    """Baixa link direto (.mp3/.mp4/...) via urllib. Retorna (path, title).

    Título vem do nome do arquivo na URL (sem extensão), quando útil.
    """
    parsed = urllib.parse.urlparse(url)
    filename = Path(parsed.path).name or "direct-input"
    suffix = Path(filename).suffix.lower() or ".bin"
    # Garante extensão coerente (alguns servers podem servir sem ela no path).
    if not Path(filename).suffix:
        filename = filename + suffix
    dest = workdir / filename
    log(f"baixando link direto → {dest.name}")
    try:
        urllib.request.urlretrieve(url, str(dest))
    except (urllib.error.URLError, OSError) as exc:
        die(f"download de link direto falhou: {exc}")
    if not dest.is_file() or dest.stat().st_size == 0:
        die(f"download retornou arquivo vazio: {dest}")
    log(f"baixado: {dest.name} ({dest.stat().st_size // 1024 // 1024}MB)")
    title = _filename_to_title(filename)
    return dest, title


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

class WhisperFailure(RuntimeError):
    """Erro recuperável do Whisper — main captura e tenta fallback pra AssemblyAI."""


def whisper_segments(path: Path, api_key: str) -> list[dict]:
    """Chama Whisper com verbose_json. Devolve lista de segments dict.

    Cada segment tem ao menos `start` (s), `end` (s) e `text`.

    Levanta `WhisperFailure` em qualquer erro do SDK/API — o caller pode
    capturar pra tentar fallback. Erros não-recuperáveis (SDK ausente)
    seguem chamando `die()` direto.
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
        raise WhisperFailure(f"Groq Whisper falhou: {exc}") from exc

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


# ───────────────────────── speaker attribution ──────────────────────

def _attribute_and_group_by_speaker(
    sentences: list[tuple[float, float, str]],
    turns: list[tuple[float, float, str]],
) -> list[tuple[str, list[list[tuple[float, float, str]]]]]:
    """Atribui speaker a cada sentence e agrupa em blocos contíguos do mesmo speaker.

    Pra cada sentence (start, end, text), busca o turn com MAIOR overlap
    de tempo. Empate ou nenhum overlap → fica com o último speaker
    conhecido (fala curta no meio normalmente é continuação).

    Renomeia `SPEAKER_00` → `Speaker 1`, `SPEAKER_01` → `Speaker 2`, etc.,
    preservando a ordem de aparição (não a ordem alfabética do pyannote).

    Devolve `[(speaker_label, [paragraph, paragraph, ...]), ...]` onde
    paragraph é uma lista de até `SENTENCES_PER_PARAGRAPH` sentences.
    """
    label_map: dict[str, str] = {}
    def _humanize(raw: str) -> str:
        if raw not in label_map:
            label_map[raw] = f"Speaker {len(label_map) + 1}"
        return label_map[raw]

    last_speaker: str | None = None
    annotated: list[tuple[float, float, str, str]] = []
    for s_start, s_end, text in sentences:
        best_overlap = 0.0
        best_raw = None
        for t_start, t_end, t_label in turns:
            overlap = max(0.0, min(s_end, t_end) - max(s_start, t_start))
            if overlap > best_overlap:
                best_overlap = overlap
                best_raw = t_label
        if best_raw is None:
            # nenhum overlap (sentence em gap de silêncio) — mantém anterior
            speaker = last_speaker or _humanize("SPEAKER_00")
        else:
            speaker = _humanize(best_raw)
            last_speaker = speaker
        annotated.append((s_start, s_end, text, speaker))

    # Agrupa em blocos contíguos por speaker
    grouped: list[tuple[str, list[tuple[float, float, str]]]] = []
    for s_start, s_end, text, speaker in annotated:
        if grouped and grouped[-1][0] == speaker:
            grouped[-1][1].append((s_start, s_end, text))
        else:
            grouped.append((speaker, [(s_start, s_end, text)]))

    # Quebra cada bloco em parágrafos
    return [
        (speaker, _group_into_paragraphs(block_sentences))
        for speaker, block_sentences in grouped
    ]


# ───────────────────────── Whisper ───────────────────────────────────

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

# Pontuação que fecha sentença. Aspas/parênteses depois disso ainda
# fecham — usamos `rstrip` antes pra normalizar.
_SENTENCE_END = (".", "!", "?")
_CLOSERS = ' "”’\')]'


def _format_timestamp(seconds: float) -> str:
    """Formato curto estilo TurboScribe: `(M:SS)` se < 1h, `(H:MM:SS)` caso contrário.

    Sem zero-padding nos minutos quando < 1h — assim `(0:03)`, `(2:30)`.
    """
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# Duração mínima de um bloco SRT. Evita cue de duração ~0 (frase muito
# curta com start≈end), que players ignoram ou fazem "piscar" na tela.
SRT_MIN_CUE_SECONDS = 1.2


def _format_srt_timestamp(seconds: float) -> str:
    """Converte segundos → `HH:MM:SS,mmm` (padrão SubRip, vírgula decimal).

    Diferente de `_format_timestamp` (que faz `M:SS` pra leitura humana):
    aqui é o formato estrito do SubRip, com horas zero-padded e
    milissegundos separados por vírgula — o que players/editores esperam.
    """
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _segments_to_sentences(
    segments: list[dict],
) -> list[tuple[float, float, str]]:
    """Acumula `segments[].text` até fechar uma sentença (`.!?` no fim).

    Whisper fragmenta em 2-5s por pausa curta (vírgula). Cada segment já
    carrega a pontuação correta — quase sempre. Quando NÃO carrega
    (acontece em trechos de fala corrida em PT-BR), o acúmulo pode crescer
    indefinidamente. Por isso temos um fallback duro: ao passar de
    `MAX_WORDS_PER_SENTENCE` sem ver `.!?`, fechamos manualmente com `.`
    pra evitar parágrafos gigantescos no render.

    Retorna lista de `(start, end, text)`. O `end` é útil pra cruzar com
    a saída do pyannote (diarization) e atribuir speaker à frase.
    """
    sentences: list[tuple[float, float, str]] = []
    buf: list[str] = []
    start: float | None = None
    end: float = 0.0
    word_count = 0
    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue
        if start is None:
            start = seg["start"]
        end = seg["end"]
        buf.append(text)
        word_count += len(text.split())
        closed = text.rstrip(_CLOSERS).endswith(_SENTENCE_END)
        if closed or word_count >= MAX_WORDS_PER_SENTENCE:
            joined = " ".join(buf)
            if not closed:
                # Whisper não fechou e estouramos o limite — força `.`
                joined = joined.rstrip(' "”’\')]').rstrip(",;:") + "."
            sentences.append((start, end, joined))
            buf = []
            start = None
            word_count = 0
    if buf:
        joined = " ".join(buf)
        if not joined.rstrip(_CLOSERS).endswith(_SENTENCE_END):
            joined = joined.rstrip(' "”’\')]').rstrip(",;:") + "."
        sentences.append((start or 0.0, end, joined))
    return sentences


def _group_into_paragraphs(
    sentences: list[tuple[float, float, str]],
    per_paragraph: int = SENTENCES_PER_PARAGRAPH,
) -> list[list[tuple[float, float, str]]]:
    """Agrupa N sentenças por parágrafo, formato TurboScribe."""
    return [
        sentences[i : i + per_paragraph]
        for i in range(0, len(sentences), per_paragraph)
    ]


def render_analysis(
    segments: list[dict], speakers: list[tuple[float, float, str]] | None = None
) -> str:
    """TXT estilo TurboScribe.

    Sem speakers: parágrafos com `(M:SS) frase. (M:SS) frase. …` (timestamp
    por frase).

    Com speakers: blocos por falante, cabeçalho `Speaker N` + parágrafos
    como acima. A troca de speaker força quebra de bloco mesmo que o
    parágrafo anterior ainda não tenha 3 frases.
    """
    sentences = _segments_to_sentences(segments)
    if not speakers:
        paragraphs = _group_into_paragraphs(sentences)
        blocks = [
            " ".join(f"({_format_timestamp(ts)}) {text}" for ts, _, text in para)
            for para in paragraphs
        ]
        return "\n\n".join(blocks) + "\n"

    grouped = _attribute_and_group_by_speaker(sentences, speakers)
    out: list[str] = []
    for speaker_label, blocks in grouped:
        out.append(f"{speaker_label}")
        for para in blocks:
            parts = [f"({_format_timestamp(ts)}) {text}" for ts, _, text in para]
            out.append(" ".join(parts))
        out.append("")  # linha em branco entre speakers
    return "\n".join(out).rstrip() + "\n"


def render_reading(
    segments: list[dict],
    title: str,
    url: str,
    speakers: list[tuple[float, float, str]] | None = None,
) -> str:
    """HTML standalone — `<p>` estilizados com 1 timestamp discreto por parágrafo.

    Sem speakers: cada `<p>` começa com `<span class="ts">(M:SS)</span>` e
    contém as frases do parágrafo emendadas.

    Com speakers: cada bloco de falante vira `<section class="speaker">`
    com `<h2>Speaker N</h2>` + parágrafos do mesmo padrão.
    """
    sentences = _segments_to_sentences(segments)
    body_parts: list[str] = []

    if not speakers:
        paragraphs = _group_into_paragraphs(sentences)
        for para in paragraphs:
            ts = _format_timestamp(para[0][0])
            text = " ".join(html.escape(t) for _, _, t in para)
            body_parts.append(
                f'    <p><span class="ts">({ts})</span> {text}</p>'
            )
    else:
        grouped = _attribute_and_group_by_speaker(sentences, speakers)
        for speaker_label, blocks in grouped:
            body_parts.append('    <section class="speaker">')
            body_parts.append(f"      <h2>{html.escape(speaker_label)}</h2>")
            for para in blocks:
                ts = _format_timestamp(para[0][0])
                text = " ".join(html.escape(t) for _, _, t in para)
                body_parts.append(
                    f'      <p><span class="ts">({ts})</span> {text}</p>'
                )
            body_parts.append("    </section>")

    return HTML_TEMPLATE.format(
        title=html.escape(title or "Transcrição"),
        url=html.escape(url),
        date=datetime.now().strftime("%d/%m/%Y"),
        body="\n".join(body_parts),
    )


def _srt_cue_times(
    sentences: list[tuple[float, float, str]],
) -> list[tuple[float, float, str]]:
    """Normaliza os tempos das frases pra blocos SRT válidos.

    - Garante `end > start` (duração mínima `SRT_MIN_CUE_SECONDS`) pra não
      emitir cue de duração ~0.
    - Clampa `end` ao `start` do próximo bloco quando há sobreposição, pra
      duas legendas não aparecerem ao mesmo tempo na tela.

    Não altera o texto nem reordena — só sanitiza os tempos.
    """
    n = len(sentences)
    out: list[tuple[float, float, str]] = []
    for i, (start, end, text) in enumerate(sentences):
        start = max(0.0, start)
        end = max(end, start + SRT_MIN_CUE_SECONDS)
        if i + 1 < n:
            next_start = max(0.0, sentences[i + 1][0])
            # Só clampa se o próximo começa depois deste start (senão manter
            # a duração mínima é mais seguro que gerar end < start).
            if next_start > start and end > next_start:
                end = next_start
        out.append((start, end, text))
    return out


def render_srt(
    segments: list[dict],
    texts: Optional[list[str]] = None,
) -> str:
    """Renderiza legenda SubRip (.srt).

    Cada FRASE (via `_segments_to_sentences` — timing por segmento do ASR,
    granularidade de frase) vira um bloco numerado no formato
    `HH:MM:SS,mmm --> HH:MM:SS,mmm`. NUNCA agrega por parágrafo: a unidade
    de legenda é a frase, com os timestamps que o ASR já produziu.

    `texts` (opcional): quando fornecido, substitui o texto de cada bloco
    (mesma ordem e contagem das frases originais) — é o hook do formato
    traduzido, que troca só o texto e **preserva os timestamps intactos**.
    Levanta ValueError se a contagem não bater (segurança de sincronismo).
    """
    sentences = _segments_to_sentences(segments)
    if texts is not None:
        if len(texts) != len(sentences):
            raise ValueError(
                f"render_srt: len(texts)={len(texts)} != "
                f"len(sentences)={len(sentences)} — sincronismo quebraria"
            )
        sentences = [
            (start, end, texts[i])
            for i, (start, end, _old) in enumerate(sentences)
        ]

    cues = _srt_cue_times(sentences)
    blocks: list[str] = []
    for idx, (start, end, text) in enumerate(cues, start=1):
        blocks.append(
            f"{idx}\n"
            f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}\n"
            f"{text}"
        )
    return "\n\n".join(blocks) + "\n"


def srt_sentence_texts(segments: list[dict]) -> list[str]:
    """Extrai só o texto de cada frase, na mesma ordem/contagem que
    `render_srt` usa. Serve pra alimentar a tradução (formato traduzido)
    e casar 1:1 com o `texts=` do `render_srt`."""
    return [text for _s, _e, text in _segments_to_sentences(segments)]


# ───────────────────────── utilidades ────────────────────────────────

def _slug_from_url(url: str) -> str:
    """Slug curto pro nome do arquivo final.

    YouTube: usa o video ID (11 chars). Outras URLs: SHA-1 truncado.
    """
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([\w-]{11})", url)
    if m:
        return m.group(1)
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _is_youtube(url: str) -> bool:
    return bool(re.search(r"(?:youtube\.com|youtu\.be)", url, re.I))


def youtube_oembed_title(url: str) -> str | None:
    """Pega o título canônico do vídeo via oEmbed do YouTube.

    Razão: o Firecrawl extrai `<title>` da HTML servida, que o YouTube
    pode traduzir baseado em Accept-Language/geo-IP do servidor que
    fez o scrape — então um vídeo em PT-BR pode chegar com título em
    inglês. O endpoint `/oembed` sempre devolve o título publicado pelo
    canal, sem tradução automática.

    Retorna None silenciosamente em qualquer erro (rede, 404, JSON
    inválido) — o caller deve cair pro `metadata.title` do Firecrawl.
    """
    if not _is_youtube(url):
        return None
    endpoint = "https://www.youtube.com/oembed?" + urllib.parse.urlencode(
        {"url": url, "format": "json"}
    )
    try:
        with urllib.request.urlopen(endpoint, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None
    title = data.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


# ──────────────── cache leve de intermediários ──────────────────────

# Cache só guarda os "intermediates" do pipeline (segments do
# Whisper/AssemblyAI + speakers turns) — NÃO o mp3/mp4 bruto. Mp3 de 30MB
# x dezenas de transcrições viraria GBs em disco; segments JSON pesam
# ~100KB cada. ROI: re-pedir a mesma URL em formato diferente (TXT↔HTML)
# pula download + engine inteiros, só re-renderiza.
#
# Layout: $KOBE_HOME/.local/atrus-cache/<sha1-url-16>/{meta,segments,speakers}.json
# TTL: 7 dias por mtime, gerenciado pelo cleanup loop do bot/cleanup.py.

_CACHE_DIR_NAME = "atrus-cache"


def _cache_key(url: str, diarize: bool) -> str:
    """Hash determinístico por (URL, --diarize). Diarize muda engine →
    output incompatível, então faz parte da chave."""
    raw = url + ("|diarize" if diarize else "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _cache_dir(url: str, diarize: bool) -> Path:
    kobe_home = os.environ.get("KOBE_HOME") or str(Path.home() / "kobe")
    return Path(kobe_home) / ".local" / _CACHE_DIR_NAME / _cache_key(url, diarize)


def _cache_load(
    url: str, diarize: bool
) -> Optional[tuple[list[dict], Optional[list[tuple[float, float, str]]], str]]:
    """Lê cache pra `(url, diarize)`. Retorna (segments, speakers, engine_used)
    ou None se ausente/inválido. Toca mtime pra reset do TTL."""
    cdir = _cache_dir(url, diarize)
    segments_file = cdir / "segments.json"
    meta_file = cdir / "meta.json"
    if not segments_file.is_file() or not meta_file.is_file():
        return None
    try:
        segments = json.loads(segments_file.read_text())
        meta = json.loads(meta_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(segments, list) or not segments:
        return None
    speakers: Optional[list[tuple[float, float, str]]] = None
    if diarize:
        speakers_file = cdir / "speakers.json"
        if not speakers_file.is_file():
            return None  # cache incompleto pra modo speakers
        try:
            raw = json.loads(speakers_file.read_text())
            speakers = [tuple(t) for t in raw]
        except (OSError, json.JSONDecodeError):
            return None
    # Touch: bumpa mtime pra cache em uso resetar o TTL.
    try:
        cdir.touch()
    except OSError:
        pass
    engine_used = meta.get("engine_used", "cache")
    return segments, speakers, engine_used


def _cache_save(
    url: str,
    diarize: bool,
    segments: list[dict],
    speakers: Optional[list[tuple[float, float, str]]],
    engine_used: str,
    title: Optional[str],
) -> None:
    """Grava cache (best-effort — erro é só logado, não derruba o pipeline)."""
    cdir = _cache_dir(url, diarize)
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "segments.json").write_text(json.dumps(segments, ensure_ascii=False))
        if speakers is not None:
            (cdir / "speakers.json").write_text(
                json.dumps([list(t) for t in speakers], ensure_ascii=False)
            )
        meta = {
            "url": url,
            "diarize": diarize,
            "created_at": datetime.now().isoformat(),
            "engine_used": engine_used,
            "title": title or "",
        }
        (cdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        log(f"cache: salvo em {cdir.name}")
    except OSError as exc:
        log(f"cache: falha gravando (segue sem cachear): {exc}")


def _default_output_dir() -> Path:
    """Onde gravar a transcrição final.

    Mora dentro de `user-data/artifacts/transcricoes/` porque transcrição é
    dado do operador (output sob demanda dele) — não pertence à árvore de
    `projetos/`, que é pra projetos de software que o operador conduz.
    `user-data/` é .gitignored no repo público do Kobe-base, então nada
    vaza pra fora da instância.
    """
    kobe_home = os.environ.get("KOBE_HOME") or str(Path.home() / "kobe")
    return Path(kobe_home) / "user-data" / "artifacts" / "transcricoes"


# ───────────────────────── main ──────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="atrus — transcreve URL de mídia via Firecrawl + Groq Whisper"
    )
    parser.add_argument("url")
    parser.add_argument(
        "--format", choices=("analysis", "reading"), default="analysis",
        help="formato de saída: analysis (txt com timestamps por frase) ou reading (html, 1 timestamp por parágrafo)",
    )
    parser.add_argument(
        "--diarize", action="store_true",
        help="identifica speakers via AssemblyAI (requer ASSEMBLYAI_API_KEY no env)",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="diretório de saída (default: $KOBE_HOME/user-data/artifacts/transcricoes)",
    )
    parser.add_argument(
        "--title", default=None,
        help="título humano da transcrição (usado no HTML; default: o que vier do Firecrawl, ou slug da URL)",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="ignora cache de segments (re-baixa e re-transcreve mesmo se o cache existir)",
    )
    args = parser.parse_args()

    # Firecrawl é só pra URLs de página (YouTube/Vimeo/podcast/etc).
    # Drive e link direto pulam Firecrawl, então só exigimos a key quando
    # a URL realmente precisa dela.
    need_firecrawl = not (_is_google_drive(args.url) or _is_direct_av_link(args.url))
    fc_key: Optional[str] = require_env("FIRECRAWL_API_KEY") if need_firecrawl else None

    # Caminho sem speakers continua via Groq Whisper (qualidade superior em
    # PT-BR e mais barato pra áudios curtos). Caminho com speakers vai pelo
    # AssemblyAI (transcrição + diarização em chamada única — substitui
    # pyannote + Whisper de uma vez, sem CPU local saturada).
    if args.diarize:
        aai_key = require_env("ASSEMBLYAI_API_KEY")
        groq_key = None
    else:
        aai_key = None
        groq_key = require_env("GROQ_API_KEY")

    output_dir = Path(args.output_dir) if args.output_dir else _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    workdir = Path(tempfile.mkdtemp(prefix="atrus-"))
    # `engine_used` é gravado na 2ª linha do stdout pra o worker poder
    # avisar o operador quando rolou fallback. Valores: "groq-whisper",
    # "assemblyai", "assemblyai-fallback", "cache".
    engine_used = "unknown"
    segments: Optional[list[dict]] = None
    speakers: Optional[list[tuple[float, float, str]]] = None
    # Tenta cache ANTES de criar workdir/baixar/transcrever. Se hit, pula
    # tudo isso e vai direto pro render — economiza minutos e $$.
    title: Optional[str] = None
    if not args.no_cache:
        cached = _cache_load(args.url, args.diarize)
        if cached is not None:
            segments, speakers, prev_engine = cached
            engine_used = "cache"  # comunica reuso pro worker/operador
            log(
                f"cache HIT: reusando {len(segments)} segmentos "
                f"(engine original: {prev_engine})"
            )

    try:
        if segments is None:
            # Cache miss — pipeline completo (download + compress + engine).
            # acquire_audio decide entre Drive (gdown), link direto (urllib)
            # ou Firecrawl conforme a URL. Retorna path local + título (se
            # vier do scrape; None nos outros casos).
            audio_path, scraped_title = acquire_audio(args.url, fc_key, workdir)
            # Prioridade do título: --title (override) > oEmbed YouTube (canônico)
            # > metadata.title do Firecrawl (pode vir traduzido) > slug da URL.
            oembed_title = youtube_oembed_title(args.url) if not args.title else None
            title = args.title or oembed_title or scraped_title or _slug_from_url(args.url)
            if oembed_title:
                log(f"título via YouTube oEmbed: {oembed_title!r}")
            mp3 = compress_if_needed(audio_path)

            if args.diarize:
                # AssemblyAI faz transcrição + diarização numa única chamada;
                # devolve segments (já quebrados por frase via timestamps
                # interpolados das utterances) + speakers_turns compatíveis com
                # `_attribute_and_group_by_speaker`.
                from assemblyai_engine import transcribe_with_speakers
                log(f"transcrevendo via AssemblyAI no formato '{args.format}' com speakers…")
                try:
                    segments, speakers = transcribe_with_speakers(mp3, aai_key)  # type: ignore[arg-type]
                    engine_used = "assemblyai"
                except RuntimeError as exc:
                    die(str(exc))
            else:
                log(f"transcrevendo via Groq Whisper no formato '{args.format}'…")
                try:
                    segments = transcribe_all(mp3, groq_key)  # type: ignore[arg-type]
                    engine_used = "groq-whisper"
                except WhisperFailure as exc:
                    # Fallback automático pra AssemblyAI quando ASSEMBLYAI_API_KEY
                    # disponível — cobre 429 de rate limit e erros de rede do
                    # Groq. Sem a key, morre com o erro original.
                    aai_fallback_key = os.environ.get("ASSEMBLYAI_API_KEY")
                    if not aai_fallback_key:
                        die(f"Whisper falhou e não há ASSEMBLYAI_API_KEY pra fallback: {exc}")
                    log(f"Whisper falhou ({exc}). Caindo pra AssemblyAI sem speakers…")
                    from assemblyai_engine import transcribe_without_speakers
                    try:
                        segments = transcribe_without_speakers(mp3, aai_fallback_key)
                        engine_used = "assemblyai-fallback"
                    except RuntimeError as exc2:
                        die(f"Whisper falhou e AssemblyAI fallback também: {exc2}")

            # Cache save: só quando o pipeline rodou de fato (cache miss),
            # com sucesso. Em cache hit não salvamos (já está lá). Falha
            # de cache é silenciosa — não derruba a transcrição.
            if segments and not args.no_cache:
                _cache_save(args.url, args.diarize, segments, speakers, engine_used, title)
        else:
            # Cache hit: já temos segments. Só precisamos do título pra render.
            # Sem chamada de rede aqui — usa oEmbed cache local se for YouTube;
            # outros casos caem no slug da URL.
            oembed_title = youtube_oembed_title(args.url) if not args.title else None
            title = args.title or oembed_title or _slug_from_url(args.url)

        if not segments:
            die("transcrição vazia")

        slug = _slug_from_url(args.url)
        suffix_speakers = "-speakers" if args.diarize else ""
        # Header indicando a engine usada — operador pode comparar
        # transcrições depois sem precisar consultar logs.
        engine_label = {
            "groq-whisper": "Groq Whisper-large-v3",
            "assemblyai": "AssemblyAI (transcrição + speakers)",
            "assemblyai-fallback": "AssemblyAI (FALLBACK — Whisper indisponível)",
            "cache": "cache (transcrição reaproveitada do cache local)",
        }.get(engine_used, engine_used)
        if args.format == "analysis":
            txt_header = f"# Transcrito via: {engine_label}\n# Fonte: {args.url}\n\n"
            content = txt_header + render_analysis(segments, speakers=speakers)
            out_path = output_dir / f"{slug}-analysis{suffix_speakers}.txt"
        else:
            content = render_reading(
                segments, title, args.url, speakers=speakers,
            )
            # Injeta um comentário HTML antes de <!DOCTYPE> indicando engine.
            content = f"<!-- Transcrito via: {engine_label} -->\n{content}"
            out_path = output_dir / f"{slug}-reading{suffix_speakers}.html"

        out_path.write_text(content, encoding="utf-8")
        # stdout — duas linhas:
        #   1: path do arquivo final (compat com versões antigas do worker)
        #   2: engine=<id>  (worker novo lê pra avisar fallback ao operador)
        print(str(out_path))
        print(f"engine={engine_used}")
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
