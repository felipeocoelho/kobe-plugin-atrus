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

# Pyannote speaker-diarization pipeline. Carregado lazy só quando --diarize.
PYANNOTE_PIPELINE = "pyannote/speaker-diarization-3.1"

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


# ───────────────────────── pyannote (speaker diarization) ───────────

def pyannote_diarize(mp3: Path, hf_token: str) -> list[tuple[float, float, str]]:
    """Roda pyannote speaker-diarization-3.1 e devolve turns `[(start, end, label)]`.

    Importação lazy: só chama quando `--diarize` foi passado, evita
    forçar o pip de pyannote.audio (+torch ~500MB) em usuários que só
    querem transcrição limpa.

    Modelo precisa ser baixado uma vez (~500MB → `~/.cache/huggingface/`)
    e exige aceite manual dos termos em
    https://huggingface.co/pyannote/speaker-diarization-3.1 com o mesmo
    HF token.
    """
    try:
        from pyannote.audio import Pipeline  # type: ignore
    except ImportError:
        die(
            "pyannote.audio não instalado. Roda: "
            "$KOBE_CLAUDE_CWD/.venv/bin/pip install pyannote.audio "
            "(~500MB com torch). Veja o runbook em docs/runbooks/."
        )
    log("carregando pipeline pyannote/speaker-diarization-3.1…")
    try:
        pipeline = Pipeline.from_pretrained(
            PYANNOTE_PIPELINE, use_auth_token=hf_token
        )
    except Exception as exc:  # noqa: BLE001
        die(
            f"pyannote: falha carregando pipeline ({exc}). "
            "Verifica se o HF_TOKEN é válido e se você aceitou os termos "
            "em https://huggingface.co/pyannote/speaker-diarization-3.1"
        )
    log("rodando diarization (pode levar 5-10min por hora de áudio na CPU)…")
    try:
        annotation = pipeline(str(mp3))
    except Exception as exc:  # noqa: BLE001
        die(f"pyannote falhou processando áudio: {exc}")

    turns: list[tuple[float, float, str]] = []
    for turn, _, label in annotation.itertracks(yield_label=True):
        turns.append((float(turn.start), float(turn.end), str(label)))
    if not turns:
        log("pyannote não encontrou turns — voltando pra speaker único")
    return turns


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
        help="identifica speakers via pyannote local (requer HF_TOKEN no env + termos aceitos)",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="diretório de saída (default: $KOBE_HOME/user-data/artifacts/transcricoes)",
    )
    parser.add_argument(
        "--title", default=None,
        help="título humano da transcrição (usado no HTML; default: o que vier do Firecrawl, ou slug da URL)",
    )
    args = parser.parse_args()

    fc_key = require_env("FIRECRAWL_API_KEY")
    groq_key = require_env("GROQ_API_KEY")
    hf_token = os.environ.get("HF_TOKEN") if args.diarize else None
    if args.diarize and not hf_token:
        die("--diarize requer HF_TOKEN no env. Veja docs/runbooks/pyannote-setup.md.")

    output_dir = Path(args.output_dir) if args.output_dir else _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    audio_url, scraped_title = firecrawl_scrape(args.url, fc_key)
    title = args.title or scraped_title or _slug_from_url(args.url)

    workdir = Path(tempfile.mkdtemp(prefix="atrus-"))
    mp3 = workdir / "input.mp3"
    try:
        download(audio_url, mp3)
        mp3 = compress_if_needed(mp3)

        speakers: list[tuple[float, float, str]] | None = None
        if args.diarize:
            # pyannote precisa do áudio inteiro (sem chunking) pra manter
            # labels coerentes — roda ANTES do Whisper, no mesmo arquivo
            # já comprimido (mono 16kbps já basta pra diarization).
            speakers = pyannote_diarize(mp3, hf_token)  # type: ignore[arg-type]

        log(f"transcrevendo no formato '{args.format}'{' com speakers' if args.diarize else ''}…")
        segments = transcribe_all(mp3, groq_key)
        if not segments:
            die("transcrição vazia")

        slug = _slug_from_url(args.url)
        suffix_speakers = "-speakers" if args.diarize else ""
        if args.format == "analysis":
            content = render_analysis(segments, speakers=speakers)
            out_path = output_dir / f"{slug}-analysis{suffix_speakers}.txt"
        else:
            content = render_reading(segments, title, args.url, speakers=speakers)
            out_path = output_dir / f"{slug}-reading{suffix_speakers}.html"

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
