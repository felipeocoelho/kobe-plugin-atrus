"""atrus.translate — tradução de blocos de legenda pra pt-br via OpenAI.

Traduz uma lista de textos (as frases/cues de uma legenda) preservando a
correspondência 1:1 com os timestamps: a tradução **nunca toca no tempo**,
só troca o texto. O caller (transcribe_url.render_srt) reencaixa cada
tradução no cue de origem pela posição na lista.

Robustez de sincronismo (o requisito duro):
  - Batches numerados por índice (`<n>||texto`).
  - Fallback por bloco: se a tradução de um índice sumir ou o parsing
    falhar, aquele bloco volta com o **texto original** — nunca perde
    legenda nem desalinha o mapeamento bloco↔timestamp.
  - Os timestamps jamais entram no prompt → o modelo não tem como bagunçá-los.

Engine selecionável por `ATRUS_TRANSLATE_ENGINE` (default `openai`):
  - `openai` → `gpt-4o-mini` (chave `OPENAI_API_KEY`). Boa qualidade pt-br.
  - `groq`   → `llama-3.3-70b-versatile` (chave `GROQ_API_KEY`, já no Kobe).
    Muito rápido/barato; usa a mesma interface `chat.completions`.
Modelo sobrescrevível via `ATRUS_TRANSLATE_MODEL`. Import lazy do SDK —
mesma disciplina do `assemblyai_engine.py`.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Optional


# Registro das engines suportadas. Ambas expõem a mesma interface
# `client.chat.completions.create(...)` (o SDK do Groq é compatível com o
# shape do OpenAI), então só muda a construção do client e os defaults.
ENGINES: dict[str, dict[str, str]] = {
    "openai": {"key_env": "OPENAI_API_KEY", "model": "gpt-4o-mini"},
    "groq": {"key_env": "GROQ_API_KEY", "model": "llama-3.3-70b-versatile"},
}
DEFAULT_ENGINE = "openai"

# Nº de blocos por chamada. 40 mantém o prompt curto o bastante pra o modelo
# não "resumir"/mesclar linhas, e poucas chamadas mesmo em áudios longos.
BATCH_SIZE = 40


def resolve_engine(engine: Optional[str] = None) -> str:
    """Resolve a engine efetiva: arg explícito > env > default."""
    name = (engine or os.environ.get("ATRUS_TRANSLATE_ENGINE") or DEFAULT_ENGINE).lower()
    if name not in ENGINES:
        raise RuntimeError(
            f"ATRUS_TRANSLATE_ENGINE={name!r} inválido — use {list(ENGINES)}"
        )
    return name


def key_env_for(engine: str) -> str:
    """Nome da env var de chave exigida pela engine (pra checagem antecipada)."""
    return ENGINES[resolve_engine(engine)]["key_env"]


def _build_client(engine: str, key: str):
    """Constrói o client do SDK correspondente (import lazy)."""
    if engine == "groq":
        try:
            from groq import Groq  # type: ignore
        except ImportError as exc:
            raise RuntimeError("groq SDK não instalado.") from exc
        return Groq(api_key=key)
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "openai SDK não instalado. Roda: "
            "$KOBE_CLAUDE_CWD/.venv/bin/pip install openai"
        ) from exc
    return OpenAI(api_key=key)

_LINE_RE = re.compile(r"^\s*(\d+)\s*\|\|\s?(.*)$")

_SYSTEM_PROMPT = (
    "Você é um tradutor profissional de legendas. Recebe linhas numeradas e "
    "traduz cada uma para português do Brasil (pt-BR).\n"
    "Regras rígidas:\n"
    "1. Devolva EXATAMENTE uma linha de saída para cada linha de entrada, na "
    "mesma numeração e na mesma ordem.\n"
    "2. Formato de cada linha de saída: `<n>||<tradução>` (o número, duas "
    "barras verticais, e a tradução).\n"
    "3. NUNCA junte, divida, reordene ou omita linhas. Uma entrada = uma saída.\n"
    "4. Traduza de forma natural e idiomática, adequada a legenda (concisa).\n"
    "5. Preserve nomes próprios, siglas e termos técnicos.\n"
    "6. Se uma linha já estiver em português, devolva-a praticamente igual.\n"
    "7. Não adicione comentários, cabeçalhos nem texto fora do formato."
)


def _log(msg: str) -> None:
    print(f"[atrus/translate] {msg}", file=sys.stderr, flush=True)


def _build_user_prompt(batch: list[str]) -> str:
    return "\n".join(f"{i}||{text}" for i, text in enumerate(batch, start=1))


def _parse_response(raw: str, batch_len: int) -> dict[int, str]:
    """Extrai `{índice_1based: tradução}` da resposta. Índices fora de
    [1, batch_len] são ignorados. Linhas fora do formato são ignoradas
    (o fallback no caller cobre os índices ausentes)."""
    out: dict[int, str] = {}
    for line in raw.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        idx = int(m.group(1))
        if 1 <= idx <= batch_len:
            out[idx] = m.group(2).strip()
    return out


def _translate_batch(client, model: str, batch: list[str]) -> list[str]:
    """Traduz um batch. Devolve lista do mesmo tamanho; blocos sem tradução
    válida caem no texto original (fallback que preserva sincronismo)."""
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(batch)},
            ],
        )
        raw = resp.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001
        _log(f"batch falhou ({exc}); mantendo texto original desses blocos")
        return list(batch)

    mapped = _parse_response(raw, len(batch))
    missing = 0
    result: list[str] = []
    for i, original in enumerate(batch, start=1):
        translated = mapped.get(i)
        if translated:
            result.append(translated)
        else:
            missing += 1
            result.append(original)  # fallback → nunca perde legenda
    if missing:
        _log(f"{missing}/{len(batch)} blocos sem tradução — usando original neles")
    return result


def translate_cues(
    cues: list[str],
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    engine: Optional[str] = None,
) -> list[str]:
    """Traduz `cues` pra pt-br. Retorna lista do MESMO tamanho e ordem
    (garantia dura pra o sincronismo com os timestamps).

    `engine`: `openai` (default) ou `groq` — ver `ATRUS_TRANSLATE_ENGINE`.
    `api_key`: default lê a env da engine (`OPENAI_API_KEY`/`GROQ_API_KEY`).
    `model`: default `ATRUS_TRANSLATE_MODEL` ou o default da engine.

    Levanta RuntimeError só quando não há como nem começar (SDK ausente ou
    sem chave). Falhas por batch degradam pro texto original, não abortam.
    """
    if not cues:
        return []
    eng = resolve_engine(engine)
    spec = ENGINES[eng]
    key = api_key or os.environ.get(spec["key_env"])
    if not key:
        raise RuntimeError(
            f"{spec['key_env']} ausente — necessária pra --format=srt_ptbr "
            f"(engine={eng})"
        )
    resolved_model = model or os.environ.get("ATRUS_TRANSLATE_MODEL") or spec["model"]
    client = _build_client(eng, key)

    out: list[str] = []
    total_batches = (len(cues) + BATCH_SIZE - 1) // BATCH_SIZE
    for b in range(0, len(cues), BATCH_SIZE):
        batch = cues[b : b + BATCH_SIZE]
        _log(f"traduzindo batch {b // BATCH_SIZE + 1}/{total_batches} "
             f"({len(batch)} blocos) via {eng}/{resolved_model}…")
        out.extend(_translate_batch(client, resolved_model, batch))

    # Garantia dura: tamanho idêntico à entrada.
    if len(out) != len(cues):
        _log(f"ALERTA: saída {len(out)} != entrada {len(cues)}; corrigindo por padding")
        # Padding defensivo (não deve acontecer — _translate_batch já garante).
        while len(out) < len(cues):
            out.append(cues[len(out)])
        out = out[: len(cues)]
    return out
