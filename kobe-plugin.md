---
name: atrus
visibility: public
version: 0.4.0
description: "Transcrição de URLs de mídia. Aceita YouTube/Vimeo/Spotify/podcast (via Firecrawl), Google Drive (via gdown — baixa o arquivo direto), e links diretos pra .mp3/.mp4/.m4a/.wav etc. (urllib). Caminho sem speakers: Groq Whisper (com fallback automático pra AssemblyAI). Caminho com speakers: AssemblyAI (transcrição + diarização). Toda execução detached via kobe-dispatch — operador recebe progresso em background e segue conversando enquanto roda. Quatro formatos: TXT ou HTML, com ou sem speakers. TXT tem timestamp por frase; HTML tem 1 timestamp por parágrafo. Saída em `user-data/artifacts/transcricoes/`. Múltiplas URLs sempre rodam em paralelo. IMPORTANTE — delegue direto sem perguntar formato; o subagente é quem pergunta `[1]/[2]/[3]/[4]` quando o formato não veio no slash."
triggers:
  - "operador manda link de YouTube, Vimeo, Spotify, podcast ou pede 'transcreve esse vídeo/link'"
  - "comando `/transcrever <url1> <url2> ...` (sem formato — subagente pergunta `[1] TXT/[2] HTML/[3] TXT+speakers/[4] HTML+speakers`)"
  - "comando `/transcrever_txt <url1> <url2> ...` (TXT sem speakers)"
  - "comando `/transcrever_leitura <url1> <url2> ...` (HTML sem speakers)"
  - "comando `/transcrever_txt_speakers <url1> <url2> ...` (TXT + speakers via AssemblyAI)"
  - "comando `/transcrever_leitura_speakers <url1> <url2> ...` (HTML + speakers via AssemblyAI)"
  - "também aceita as variantes com hífen (`/transcrever-txt`, `/transcrever-leitura`, etc.) por retrocompat — o subagente normaliza"
  - "URL solta sem slash → subagente pergunta o formato `[1]/[2]/[3]/[4]` na primeira mensagem"
slash_commands:
  - name: transcrever
    description: "Transcrever URL (subagente pergunta o formato)"
  - name: transcrever_txt
    description: "Transcrever URL em TXT sem speakers"
  - name: transcrever_leitura
    description: "Transcrever URL em HTML pra leitura, sem speakers"
  - name: transcrever_txt_speakers
    description: "Transcrever URL em TXT com identificação de speakers"
  - name: transcrever_leitura_speakers
    description: "Transcrever URL em HTML pra leitura com speakers"
agent_definition: claude/agents/atrus.md
dependencies:
  python:
    - firecrawl-py  # scrape de URLs de página (YouTube/Vimeo/Spotify/podcast)
    - assemblyai    # transcrição + diarização (caminho com speakers, fallback)
    - gdown         # download de arquivos do Google Drive (bypass Firecrawl)
  system:
    - ffmpeg
env:
  required:
    - FIRECRAWL_API_KEY
    - GROQ_API_KEY        # usado no caminho sem speakers (Whisper)
  optional:
    - ASSEMBLYAI_API_KEY  # obrigatório se quiser usar /transcrever-speakers ou /transcrever-leitura-speakers
---

# Atrus — transcritor de URLs

Plugin público do Kobe pra transcrição de URLs de mídia. Resolve o problema clássico de VPS bloqueado pelo YouTube/Vimeo: **Firecrawl** atua como proxy residencial (pago via API) e devolve URL assinada do MP3.

Dois caminhos, conforme o formato escolhido:

- **Sem speakers** (`/transcrever`, `/transcrever-leitura`): **Groq Whisper-large-v3** (PT-BR, temperature=0). Pipeline síncrono, rápido, no turno do agente.
- **Com speakers** (`/transcrever-speakers`, `/transcrever-leitura-speakers`): **AssemblyAI** com `speaker_labels=True` (transcrição + diarização numa única chamada à nuvem deles). Pipeline detached via `kobe-dispatch`: o subagente dispara e volta o turno em ~1s; o worker em background entrega o resultado pelo Telegram. Múltiplas URLs correm em paralelo.

## Quatro formatos de saída

| Slash | Formato base | Speakers? | Engine | Saída |
|---|---|---|---|---|
| `/transcrever <urls>` | — | — | — | Sem qualifier — subagente pergunta `[1]/[2]/[3]/[4]` antes de processar |
| `/transcrever_txt <urls>` | analysis | não | Groq Whisper (com fallback) | `.txt` estilo TurboScribe — parágrafos de ~3 frases, cada frase prefixada por `(M:SS)` |
| `/transcrever_leitura <urls>` | reading | não | Groq Whisper (com fallback) | `.html` standalone com 1 timestamp discreto no início de cada parágrafo |
| `/transcrever_txt_speakers <urls>` | analysis | sim | AssemblyAI | `.txt` com blocos `Speaker 1`, `Speaker 2`… cada bloco contendo os parágrafos daquele falante |
| `/transcrever_leitura_speakers <urls>` | reading | sim | AssemblyAI | `.html` com `<section class="speaker">` por falante, cabeçalho discreto + parágrafos |

> Os 5 comandos aparecem no menu auto-complete do Telegram (`/`). As variantes com hífen (`/transcrever-txt`, `/transcrever-leitura`, etc.) continuam funcionando se você digitar manualmente — o subagente normaliza ambas as formas.

Sem speakers: derivados dos `segments[]` do Whisper-large-v3 (`verbose_json`), agrupados em frases por pontuação (`.!?`) ou por limite de palavras quando Whisper não pontua (28 palavras = força quebra). Parágrafos têm 3 frases.

Com speakers: derivados das `utterances` do AssemblyAI (cada utterance já tem speaker label + texto + timestamps); cada utterance é quebrada em frases por `.!?` com timestamps interpolados proporcionalmente ao texto. O resto do render reutiliza o mesmo pipeline.

Se a URL chegar sem slash, o subagente pergunta o formato em texto: `[1] TXT / [2] HTML / [3] TXT+speakers / [4] HTML+speakers` e processa após a resposta.

## Múltiplas URLs

- **Sem speakers**: processadas **em série**, com notify/attach a cada uma terminando. Pipeline é rápido, série não dói.
- **Com speakers**: processadas **em paralelo** via `kobe-dispatch`. Subagente dispara todas em sequência (cada dispatch retorna em ~1s) e cada worker corre independente. Tempo total ≈ URL mais demorada, não soma.

## Como funciona

```
Operador → Telegram → Kobe (agente principal)
                    → reconhece slash / URL / intenção
                    → invoca Agent(subagent_type="atrus", ...)
                       → SEM speakers (caminho A, síncrono):
                         pra cada URL:
                           kobe-notify "[N/M] Transcrevendo..."
                           python transcribe_url.py <url> --format <X>
                             → Firecrawl scrape → MP3 signed
                             → ffmpeg mono 16kbps se >25MB
                             → chunking Whisper 10min se ainda passar
                             → Whisper-large-v3 verbose_json (Groq)
                             → render → salva arquivo → stdout = path
                           kobe-attach "$path"
                         → resumo final

                       → COM speakers (caminho B, detached, paralelo):
                         pra cada URL:
                           kobe-dispatch -- kobe-heartbeat-run --interval 600 \
                                          -- python transcribe_url_worker.py <url> --diarize
                           → retorna {job_id, state_file, pid} em ~1s
                         → resumo final lista os job_ids
                         (em background, cada worker:)
                           kobe-notify "▶️ iniciando..."
                           python transcribe_url.py <url> --format <X> --diarize
                             → Firecrawl scrape → MP3 signed
                             → assemblyai_engine.transcribe_with_speakers (upload + nuvem)
                             → render com speakers → salva arquivo
                           kobe-notify "✅ pronto em Xs"
                           kobe-attach "$path"
```

## Onde os arquivos vão parar

`$KOBE_HOME/user-data/artifacts/transcricoes/` — fora da árvore de `projetos/` (que é pra projetos de software). Transcrição é dado do operador, fica em `user-data/` (gitignored).

## Custos típicos

- Firecrawl: ~$0.01–0.05 por scrape de áudio
- Groq Whisper-large-v3: ~$0.11 por hora de áudio (caminho sem speakers)
- AssemblyAI: ~$0.37 por hora de áudio com speakers (Universal model + speaker_labels)
- **Total sem speakers: < $0.20 por hora**. **Com speakers: < $0.50 por hora.**

Antes (pyannote local) era custo zero por uso, mas com saturação de CPU da VPS por horas — esse trade-off não escala.

## Instalação no Kobe

```bash
bash $KOBE_HOME/infra/install-plugin.sh https://github.com/felipeocoelho/kobe-plugin-atrus.git
$KOBE_HOME/.venv/bin/pip install -r $KOBE_HOME/plugins/public/atrus/requirements.txt
echo "FIRECRAWL_API_KEY=fc-..." >> $KOBE_HOME/.env
echo "ASSEMBLYAI_API_KEY=..." >> $KOBE_HOME/.env   # se quer formatos com speakers
systemctl --user restart kobe
```

## Variáveis de ambiente

- `FIRECRAWL_API_KEY` — https://www.firecrawl.dev (obrigatório)
- `GROQ_API_KEY` — já existe no Kobe-base, reusada pelo plugin (obrigatório p/ caminho sem speakers)
- `ASSEMBLYAI_API_KEY` — https://www.assemblyai.com/app/account (obrigatório p/ caminho com speakers)

## Requisitos do Kobe-base

- **v0.7.0+** pro plugin discovery automático.
- **v0.8.0+** pros helpers `kobe-notify` e `kobe-attach`.
- **vNEXT** (Fase 1b/1c) pros helpers `kobe-dispatch` e `kobe-heartbeat-run` (necessários no caminho com speakers).

## Limites conhecidos

- Vídeos privados / removidos / region-locked falham no Firecrawl.
- Chunking Whisper de 10min mantém timestamps coerentes (offset somado) — só no caminho sem speakers.
- Caminho com speakers (AssemblyAI) não faz chunking local — manda o arquivo inteiro pra eles processarem.
- `language="pt"` hardcoded em ambos os engines — pra outros idiomas, edite no script.
