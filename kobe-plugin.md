---
name: atrus
visibility: public
version: 0.5.0
description: "Transcrição de URLs de mídia. Aceita YouTube/Vimeo/Spotify/podcast (via Firecrawl), Google Drive (via gdown — baixa o arquivo direto), e links diretos pra .mp3/.mp4/.m4a/.wav etc. (urllib). Caminho sem speakers: Groq Whisper (com fallback automático pra AssemblyAI). Caminho com speakers: AssemblyAI (transcrição + diarização). Toda execução detached via kobe-dispatch — operador recebe progresso em background e segue conversando enquanto roda. Seis formatos: TXT ou HTML (com ou sem speakers), legenda .srt (SubRip), e legenda .srt traduzida pra pt-br. TXT tem timestamp por frase; HTML tem 1 timestamp por parágrafo; o .srt tem timestamps de bloco HH:MM:SS,mmm por frase. Saída em `user-data/artifacts/transcricoes/`. Múltiplas URLs sempre rodam em paralelo. IMPORTANTE — delegue direto sem perguntar formato; o subagente é quem pergunta `[1]..[6]` quando o formato não veio no slash."
triggers:
  - "operador manda link de YouTube, Vimeo, Spotify, podcast ou pede 'transcreve esse vídeo/link'"
  - "comando `/transcrever <url1> <url2> ...` (sem formato — subagente pergunta `[1]..[6]`)"
  - "comando `/transcrever_txt <url1> <url2> ...` (TXT sem speakers)"
  - "comando `/transcrever_leitura <url1> <url2> ...` (HTML sem speakers)"
  - "comando `/transcrever_txt_speakers <url1> <url2> ...` (TXT + speakers via AssemblyAI)"
  - "comando `/transcrever_leitura_speakers <url1> <url2> ...` (HTML + speakers via AssemblyAI)"
  - "comando `/transcrever_legenda <url1> <url2> ...` (legenda .srt SubRip)"
  - "comando `/transcrever_legenda_traduzida <url1> <url2> ...` (legenda .srt traduzida pra pt-br)"
  - "também aceita as variantes com hífen (`/transcrever-txt`, `/transcrever-legenda`, etc.) por retrocompat — o subagente normaliza"
  - "URL solta sem slash → subagente pergunta o formato `[1]..[6]` na primeira mensagem"
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
  - name: transcrever_legenda
    description: "Transcrever URL em legenda .srt (SubRip)"
  - name: transcrever_legenda_traduzida
    description: "Transcrever URL em legenda .srt traduzida pra pt-br"
agent_definition: claude/agents/atrus.md
dependencies:
  python:
    - firecrawl-py  # scrape de URLs de página (YouTube/Vimeo/Spotify/podcast)
    - assemblyai    # transcrição + diarização (caminho com speakers, fallback)
    - gdown         # download de arquivos do Google Drive (bypass Firecrawl)
    - openai        # tradução da legenda pt-br (engine default; ver ATRUS_TRANSLATE_ENGINE)
  system:
    - ffmpeg
env:
  required:
    - FIRECRAWL_API_KEY
    - GROQ_API_KEY        # Whisper (sem speakers) + engine 'groq' de tradução
  optional:
    - ASSEMBLYAI_API_KEY  # obrigatório p/ /transcrever_txt_speakers e /transcrever_leitura_speakers
    - OPENAI_API_KEY      # tradução da legenda pt-br quando ATRUS_TRANSLATE_ENGINE=openai (default)
    - ATRUS_TRANSLATE_ENGINE  # 'openai' (default) ou 'groq' — engine de tradução do /transcrever_legenda_traduzida
---

# Atrus — transcritor de URLs

Plugin público do Kobe pra transcrição de URLs de mídia. Resolve o problema clássico de VPS bloqueado pelo YouTube/Vimeo: **Firecrawl** atua como proxy residencial (pago via API) e devolve URL assinada do MP3.

Caminhos, conforme o formato escolhido:

- **Sem speakers** (`/transcrever_txt`, `/transcrever_leitura`, `/transcrever_legenda`): **Groq Whisper-large-v3** (PT-BR, temperature=0). Pipeline rápido.
- **Com speakers** (`/transcrever_txt_speakers`, `/transcrever_leitura_speakers`): **AssemblyAI** com `speaker_labels=True` (transcrição + diarização numa única chamada à nuvem deles). Múltiplas URLs correm em paralelo.
- **Legenda traduzida** (`/transcrever_legenda_traduzida`): Whisper com **auto-detecção** do idioma de origem + tradução dos blocos pra pt-br via LLM (`ATRUS_TRANSLATE_ENGINE`: openai/groq). Toda execução é detached via `kobe-dispatch`.

## Seis formatos de saída

| Slash | Formato base | Speakers? | Engine | Saída |
|---|---|---|---|---|
| `/transcrever <urls>` | — | — | — | Sem qualifier — subagente pergunta `[1]..[6]` antes de processar |
| `/transcrever_txt <urls>` | analysis | não | Groq Whisper (com fallback) | `.txt` estilo TurboScribe — parágrafos de ~3 frases, cada frase prefixada por `(M:SS)` |
| `/transcrever_leitura <urls>` | reading | não | Groq Whisper (com fallback) | `.html` standalone com 1 timestamp discreto no início de cada parágrafo |
| `/transcrever_txt_speakers <urls>` | analysis | sim | AssemblyAI | `.txt` com blocos `Speaker 1`, `Speaker 2`… cada bloco contendo os parágrafos daquele falante |
| `/transcrever_leitura_speakers <urls>` | reading | sim | AssemblyAI | `.html` com `<section class="speaker">` por falante, cabeçalho discreto + parágrafos |
| `/transcrever_legenda <urls>` | srt | não | Groq Whisper (com fallback) | `.srt` SubRip — 1 bloco por frase, timestamps `HH:MM:SS,mmm --> HH:MM:SS,mmm` |
| `/transcrever_legenda_traduzida <urls>` | srt_ptbr | não | Whisper (auto-lang) + LLM tradutor | `.srt` SubRip traduzido pra pt-br, **mesmos timestamps** do original |

> Os 7 comandos aparecem no menu auto-complete do Telegram (`/`). As variantes com hífen (`/transcrever-txt`, `/transcrever-legenda`, etc.) continuam funcionando se você digitar manualmente — o subagente normaliza ambas as formas.

Sem speakers: derivados dos `segments[]` do Whisper-large-v3 (`verbose_json`), agrupados em frases por pontuação (`.!?`) ou por limite de palavras quando Whisper não pontua (28 palavras = força quebra). Parágrafos têm 3 frases.

Com speakers: derivados das `utterances` do AssemblyAI (cada utterance já tem speaker label + texto + timestamps); cada utterance é quebrada em frases por `.!?` com timestamps interpolados proporcionalmente ao texto. O resto do render reutiliza o mesmo pipeline.

Legenda `.srt`: cada **frase** (mesma quebra do TXT/HTML — timing por segmento do ASR, nunca agregação por parágrafo) vira um bloco SubRip. Guarda de timing: duração mínima de 1,2s e clamp contra sobreposição. O `.srt` **reaproveita o cache** do caminho sem-speakers (re-pedir uma URL já transcrita em TXT/HTML vira cache hit).

Legenda traduzida: transcreve com auto-detecção do idioma, depois traduz **só o texto** de cada bloco (batch numerado por índice, fallback por bloco ao original) e reencaixa nos timestamps originais — o sincronismo é preservado por construção. Engine via `ATRUS_TRANSLATE_ENGINE` (`openai` gpt-4o-mini default, ou `groq` llama-3.3-70b).

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
- Tradução da legenda pt-br: ~$0.01–0.02 por hora de áudio (gpt-4o-mini ou llama-3.3-70b) — desprezível
- **Total sem speakers: < $0.20 por hora**. **Com speakers: < $0.50 por hora.** **Legenda traduzida: ~igual ao sem speakers + centavos.**

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
- `GROQ_API_KEY` — já existe no Kobe-base, reusada pelo plugin (obrigatório p/ caminho sem speakers; também é a engine `groq` de tradução)
- `ASSEMBLYAI_API_KEY` — https://www.assemblyai.com/app/account (obrigatório p/ caminho com speakers)
- `OPENAI_API_KEY` — https://platform.openai.com (tradução da legenda pt-br quando `ATRUS_TRANSLATE_ENGINE=openai`, o default)
- `ATRUS_TRANSLATE_ENGINE` — `openai` (default) ou `groq`. Escolhe a engine de tradução do `/transcrever_legenda_traduzida`. Use `groq` pra reusar a chave do Whisper sem depender de billing da OpenAI.

## Requisitos do Kobe-base

- **v0.7.0+** pro plugin discovery automático.
- **v0.8.0+** pros helpers `kobe-notify` e `kobe-attach`.
- **vNEXT** (Fase 1b/1c) pros helpers `kobe-dispatch` e `kobe-heartbeat-run` (necessários no caminho com speakers).

## Limites conhecidos

- Vídeos privados / removidos / region-locked falham no Firecrawl.
- Chunking Whisper de 10min mantém timestamps coerentes (offset somado) — só no caminho sem speakers.
- Caminho com speakers (AssemblyAI) não faz chunking local — manda o arquivo inteiro pra eles processarem.
- Idioma de origem: `pt` fixo em TXT/HTML/legenda `.srt`; **auto-detectado** só no `/transcrever_legenda_traduzida` (que depois traduz pra pt-br). Pra transcrever direto em outro idioma sem traduzir, edite o script.
- Legenda `.srt`: 1 bloco por frase; frases longas (fallback de 28 palavras) viram blocos longos na tela — aceitável pra legenda de bloco, sem split por caractere.
- Legenda traduzida: se a engine de tradução falhar (ex: OpenAI sem quota), o fallback por bloco devolve o **texto original** naquele bloco — a legenda sai válida e sincronizada, mas sem tradução nesses trechos. Troque a engine via `ATRUS_TRANSLATE_ENGINE=groq` se a OpenAI não tiver billing.
