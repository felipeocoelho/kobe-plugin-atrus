---
name: atrus
visibility: public
version: 0.3.0
description: Transcrição de URLs de mídia (YouTube, podcast, vídeo embed) via Firecrawl + Groq Whisper, com diarização opcional de speakers via pyannote local. Quatro formatos — TXT analysis e HTML reading, cada um com ou sem speakers. TXT tem timestamp por frase (estilo TurboScribe); HTML tem 1 timestamp por parágrafo (leitura limpa). Saída em `user-data/artifacts/transcricoes/`. Aceita múltiplas URLs em série, progresso via kobe-notify/kobe-attach. IMPORTANTE: delegue direto sem perguntar formato — o subagente é quem pergunta `[1] TXT / [2] HTML / [3] TXT+speakers / [4] HTML+speakers` quando o formato não veio explícito no slash.
triggers:
  - "operador manda link de YouTube, Vimeo, Spotify, podcast ou pede 'transcreve esse vídeo/link'"
  - "comando textual `/transcrever <url1> <url2> ...` (TXT, sem speakers)"
  - "comando textual `/transcrever-leitura <url1> <url2> ...` (HTML, sem speakers)"
  - "comando textual `/transcrever-speakers <url1> <url2> ...` (TXT + identificação de speakers via pyannote)"
  - "comando textual `/transcrever-leitura-speakers <url1> <url2> ...` (HTML + speakers)"
  - "URL solta sem slash → DELEGA pro subagente direto; é ELE quem pergunta `[1]/[2]/[3]/[4]` na primeira mensagem. Agente principal NÃO pergunta o formato — só repassa."
agent_definition: claude/agents/atrus.md
dependencies:
  python:
    - firecrawl-py
    - pyannote.audio  # opcional; só carregado quando --diarize é usado
  system:
    - ffmpeg
env:
  required:
    - FIRECRAWL_API_KEY
    - GROQ_API_KEY
  optional:
    - HF_TOKEN  # obrigatório se quiser usar /transcrever-speakers ou /transcrever-leitura-speakers
---

# Atrus — transcritor de URLs

Plugin público do Kobe pra transcrição de URLs de mídia. Resolve o problema clássico de VPS bloqueado pelo YouTube/Vimeo: **Firecrawl** atua como proxy residencial (pago via API) e devolve URL assinada do MP3; **Groq Whisper-large-v3** (PT-BR, temperature=0) transcreve com qualidade. Quando o operador pede speakers, **pyannote.audio** local entra na frente do Whisper pra identificar quem falou quando (CPU pesada, custo zero por uso).

## Quatro formatos de saída

| Slash | Formato base | Speakers? | Saída |
|---|---|---|---|
| `/transcrever <urls>` | analysis | não | `.txt` estilo TurboScribe — parágrafos de ~3 frases, cada frase prefixada por `(M:SS)` |
| `/transcrever-leitura <urls>` | reading | não | `.html` standalone com 1 timestamp discreto no início de cada parágrafo |
| `/transcrever-speakers <urls>` | analysis | sim | `.txt` com blocos `Speaker 1`, `Speaker 2`… cada bloco contendo os parágrafos daquele falante |
| `/transcrever-leitura-speakers <urls>` | reading | sim | `.html` com `<section class="speaker">` por falante, cabeçalho discreto + parágrafos |

Todos os formatos derivam dos `segments[]` do Whisper-large-v3 (`verbose_json`), agrupados em frases por pontuação (`.!?`) ou por limite de palavras quando Whisper não pontua (28 palavras = força quebra). Parágrafos têm 3 frases.

Se a URL chegar sem slash, o subagente pergunta o formato em texto: `[1] TXT / [2] HTML / [3] TXT+speakers / [4] HTML+speakers` e processa após a resposta.

## Múltiplas URLs

`/transcrever url1 url2 url3` processa **em série** (uma por vez), enviando notificação de progresso e anexo de cada uma assim que fica pronta — operador não fica em silêncio esperando.

## Como funciona

```
Operador → Telegram → Kobe (agente principal)
                    → reconhece slash / URL / intenção
                    → invoca Agent(subagent_type="atrus", ...)
                       → pra cada URL:
                         kobe-notify "[N/M] Transcrevendo..."
                         python transcribe_url.py <url> --format <X> [--diarize]
                           → Firecrawl scrape(formats=["audio"]) → MP3 (signed 1h) + metadata.title
                           → download HTTP direto
                           → ffmpeg mono 16kbps se >25MB
                           → [se --diarize]: pyannote 3.1 (CPU, 5-10min/h) → turns [(start, end, speaker)]
                           → chunking Whisper 10min se ainda passar
                           → Whisper-large-v3 verbose_json
                           → [se --diarize]: merge por overlap temporal → sentence ganha speaker
                           → render analysis|reading → salva arquivo
                           → stdout = path
                         kobe-attach "$path"
                       → resumo final
```

## Onde os arquivos vão parar

`$KOBE_HOME/user-data/artifacts/transcricoes/` — fora da árvore de `projetos/` (que é pra projetos de software). Transcrição é dado do operador, fica em `user-data/` (gitignored).

## Custos típicos

- Firecrawl: ~$0.01–0.05 por scrape de áudio
- Groq Whisper-large-v3: ~$0.11 por hora de áudio
- pyannote: **gratuito** (roda local, CPU)
- **Total: < $0.20 por hora transcrita** (mesmo com speakers)

## Instalação no Kobe

```bash
# Base (sem speakers)
bash $KOBE_HOME/infra/install-plugin.sh https://github.com/felipeocoelho/kobe-plugin-atrus.git
$KOBE_HOME/.venv/bin/pip install firecrawl-py
echo "FIRECRAWL_API_KEY=fc-..." >> $KOBE_HOME/.env
systemctl --user restart kobe

# Pra habilitar speakers (formatos [3] e [4]): veja docs/runbooks/pyannote-setup.md
```

## Variáveis de ambiente

- `FIRECRAWL_API_KEY` — https://www.firecrawl.dev (obrigatório)
- `GROQ_API_KEY` — já existe no Kobe-base, reusada pelo plugin (obrigatório)
- `HF_TOKEN` — só pra speakers; veja `docs/runbooks/pyannote-setup.md`

## Requisitos do Kobe-base

- **v0.7.0+** pro plugin discovery automático.
- **v0.8.0+** pros helpers `kobe-notify` e `kobe-attach` (progresso em tempo real).

## Limites conhecidos

- Vídeos privados / removidos / region-locked falham no Firecrawl.
- Chunking Whisper de 10min mantém timestamps coerentes (offset somado).
- Diarização (pyannote) roda no áudio inteiro **antes** do chunking Whisper — pra vídeos muito longos (>2h) pode usar muita RAM. Considerar limite no futuro.
- `language="pt"` hardcoded — pra outros idiomas, edite no script.
