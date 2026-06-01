"""atrus.assemblyai_engine — transcrição + diarização via AssemblyAI.

Substitui o caminho pyannote+Whisper-Groq quando o operador pediu
identificação de speakers. Uma única chamada à API resolve transcrição
e diarização juntas (mais rápido, mais barato e melhor qualidade em
áudios longos do que pyannote local na CPU da VPS).

Importação lazy: o SDK `assemblyai` (~poucos KB, dependência leve) só
é carregado quando este módulo é usado. Caminho sem-speakers continua
intocado em `transcribe_url.py`.

Saída: `(segments, speakers_turns)` no mesmo formato que o caminho
pyannote+Whisper produzia, pra reaproveitar os renders existentes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional


def _log(msg: str) -> None:
    print(f"[atrus/assemblyai] {msg}", file=sys.stderr, flush=True)


# Quebra texto em frases, mantendo pontuação. Conservador: split em
# `.!?` seguido de espaço ou fim. Não tenta tratar abreviações
# (Sr., Dr.) — em PT-BR a frequência é baixa o suficiente pra não
# afetar visivelmente o resultado.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _split_into_sentence_segments(
    text: str, start_s: float, end_s: float
) -> list[dict]:
    """Quebra `text` em frases e distribui timestamps proporcionais.

    Útil pra alimentar `_segments_to_sentences` em transcribe_url.py: cada
    "segment" fica com texto que termina em `.!?` e timestamps adequados
    (start/end interpolados linearmente por proporção de chars).
    """
    text = text.strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p.strip()]
    if not parts:
        return []
    total = end_s - start_s
    total_chars = sum(len(p) for p in parts)
    if total <= 0 or total_chars <= 0:
        return [{"start": start_s, "end": end_s, "text": text}]
    out: list[dict] = []
    cursor = 0
    for p in parts:
        share = len(p) / total_chars
        seg_start = start_s + (cursor / total_chars) * total
        cursor += len(p)
        seg_end = start_s + (cursor / total_chars) * total
        out.append({"start": seg_start, "end": seg_end, "text": p})
    return out


def transcribe_with_speakers(
    audio_path: Path,
    api_key: str,
    language_code: str = "pt",
) -> tuple[list[dict], list[tuple[float, float, str]]]:
    """Transcreve com speaker labels usando AssemblyAI.

    Retorna:
      segments: list[{"start": float, "end": float, "text": str}] em segundos.
      speakers_turns: list[(start_s, end_s, label)] — label tipo "A"/"B".

    Ambos consumidos pelos renders existentes (`render_analysis`,
    `render_reading`) via o mesmo pipeline de `_segments_to_sentences`
    + `_attribute_and_group_by_speaker`.

    Levanta RuntimeError com mensagem útil em caso de falha.
    """
    try:
        import assemblyai as aai  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "assemblyai SDK não instalado. Roda: "
            "$KOBE_CLAUDE_CWD/.venv/bin/pip install assemblyai"
        ) from exc

    aai.settings.api_key = api_key
    # `speech_models` (plural) é obrigatório no backend atual da AssemblyAI
    # — exige lista não-vazia de modelos válidos. "universal-2" é o modelo
    # multilíngue padrão (suporta PT-BR com qualidade). Alternativa atual:
    # "universal-3-pro" (mais caro, ainda em GA limitada).
    config = aai.TranscriptionConfig(
        speaker_labels=True,
        language_code=language_code,
        punctuate=True,
        format_text=True,
        speech_models=["universal-2"],
    )

    _log(f"enviando áudio pro AssemblyAI ({audio_path.stat().st_size // 1024} KB)…")
    transcriber = aai.Transcriber(config=config)
    transcript = transcriber.transcribe(str(audio_path))

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI falhou: {transcript.error}")

    # Segments: usamos `utterances` quando temos speakers — cada utterance
    # tem speaker + texto + (start_ms, end_ms). Se não vier utterances (raro),
    # caímos em `words` agregando.
    segments: list[dict] = []
    speakers_turns: list[tuple[float, float, str]] = []

    utterances = getattr(transcript, "utterances", None) or []
    if utterances:
        for utt in utterances:
            start_s = float(utt.start) / 1000.0
            end_s = float(utt.end) / 1000.0
            text = (utt.text or "").strip()
            speaker_raw = str(getattr(utt, "speaker", "") or "")
            label = f"SPEAKER_{speaker_raw}" if speaker_raw else "SPEAKER_00"
            # Quebra em segmentos por frase pra reaproveitar o pipeline
            # de `_segments_to_sentences` em transcribe_url.py.
            segments.extend(_split_into_sentence_segments(text, start_s, end_s))
            speakers_turns.append((start_s, end_s, label))
    else:
        # Fallback: sem utterances, agrega `words` por silêncio (fora do escopo
        # do nosso caso normal). Gera segments mas speakers vão indefinidos.
        _log("AssemblyAI não retornou utterances — fallback pra words sem speakers")
        words = getattr(transcript, "words", None) or []
        if not words:
            raise RuntimeError("AssemblyAI retornou transcrição vazia")
        # Junta tudo num segment único pra não perder o texto
        text = " ".join((w.text or "").strip() for w in words if w.text)
        if text:
            segments.append({
                "start": float(words[0].start) / 1000.0,
                "end": float(words[-1].end) / 1000.0,
                "text": text,
            })

    if not segments:
        raise RuntimeError("AssemblyAI retornou zero segmentos utilizáveis")

    _log(
        f"sucesso: {len(segments)} segmentos, "
        f"{len({label for _, _, label in speakers_turns})} speakers únicos"
    )
    return segments, speakers_turns


def transcribe_without_speakers(
    audio_path: Path,
    api_key: str,
    language_code: str = "pt",
) -> list[dict]:
    """Transcreve SEM diarização — usado como fallback do Whisper Groq.

    Retorna `segments` no mesmo formato que `whisper_segments`:
    `[{"start": float, "end": float, "text": str}, ...]`.

    Diferenças vs `transcribe_with_speakers`:
    - `speaker_labels=False` (mais barato, ~$0.12/h vs ~$0.37/h)
    - Sem `utterances` → usa `words` agrupando em segments de ~3s
    """
    try:
        import assemblyai as aai  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "assemblyai SDK não instalado. Roda: "
            "$KOBE_CLAUDE_CWD/.venv/bin/pip install assemblyai"
        ) from exc

    aai.settings.api_key = api_key
    config = aai.TranscriptionConfig(
        speaker_labels=False,
        language_code=language_code,
        punctuate=True,
        format_text=True,
        speech_models=["universal-2"],
    )

    _log(f"enviando áudio pro AssemblyAI sem speakers ({audio_path.stat().st_size // 1024} KB)…")
    transcriber = aai.Transcriber(config=config)
    transcript = transcriber.transcribe(str(audio_path))

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI falhou: {transcript.error}")

    # Agrega palavras em segments de ~3s (parecido com a granularidade do
    # Whisper). Mantém pontuação que veio no texto.
    words = getattr(transcript, "words", None) or []
    if not words:
        # Fallback do fallback: texto inteiro num único segment.
        text = (getattr(transcript, "text", "") or "").strip()
        if not text:
            raise RuntimeError("AssemblyAI retornou transcrição vazia")
        return [{"start": 0.0, "end": 0.0, "text": text}]

    segments: list[dict] = []
    buf_words: list[str] = []
    buf_start: Optional[float] = None
    buf_end: float = 0.0
    target_seconds = 3.0
    for w in words:
        word_start = float(w.start) / 1000.0
        word_end = float(w.end) / 1000.0
        text = (w.text or "").strip()
        if not text:
            continue
        if buf_start is None:
            buf_start = word_start
        buf_end = word_end
        buf_words.append(text)
        if (buf_end - buf_start) >= target_seconds and text[-1:] in ".!?,;":
            segments.append({"start": buf_start, "end": buf_end, "text": " ".join(buf_words)})
            buf_words = []
            buf_start = None
    if buf_words:
        segments.append({"start": buf_start or 0.0, "end": buf_end, "text": " ".join(buf_words)})

    if not segments:
        raise RuntimeError("AssemblyAI retornou zero segmentos utilizáveis")

    _log(f"sucesso (sem speakers): {len(segments)} segmentos")
    return segments
