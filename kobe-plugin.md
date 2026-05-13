---
name: atrus
visibility: public
version: 0.2.2
description: Transcrição de URLs de mídia (YouTube, podcast, vídeo embed) via Firecrawl + Groq Whisper. Dois formatos de saída — análise (TXT) e leitura (HTML estilo livro), ambos estilo TurboScribe (parágrafos de ~3 frases, timestamp `(M:SS)` por frase). Aceita múltiplas URLs em série, progresso em tempo real via kobe-notify/kobe-attach. IMPORTANTE: delegue direto sem perguntar formato — o subagente é quem pergunta `[1] TXT / [2] HTML` quando o formato não veio explícito no slash command.
triggers:
  - "operador manda link de YouTube, Vimeo, Spotify, podcast ou pede 'transcreve esse vídeo/link'"
  - "comando textual `/transcrever <url1> <url2> ...` (formato análise, com timestamps)"
  - "comando textual `/transcrever-leitura <url1> <url2> ...` (formato leitura, HTML)"
  - "URL solta sem slash → DELEGA pro subagente direto; é ELE quem pergunta `[1] TXT / [2] HTML` na primeira mensagem. Agente principal NÃO pergunta o formato — só repassa."
agent_definition: claude/agents/atrus.md
dependencies:
  python:
    - firecrawl-py
  system:
    - ffmpeg
env:
  required:
    - FIRECRAWL_API_KEY
    - GROQ_API_KEY
---

# Atrus — transcritor de URLs

Plugin público do Kobe pra transcrição de URLs de mídia. Resolve o problema clássico de VPS bloqueado pelo YouTube/Vimeo: **Firecrawl** atua como proxy residencial (pago via API) e devolve URL assinada do MP3; **Groq Whisper-large-v3** (PT-BR, temperature=0) transcreve com qualidade.

## Dois formatos de saída

| Slash | Formato | Saída | Caso de uso |
|---|---|---|---|
| `/transcrever <urls>` | **analysis** | `.txt` estilo TurboScribe: parágrafos de ~3 frases, cada frase prefixada por `(M:SS)` | Texto bruto pra ser analisado por skill/prompt — frases completas e bem pontuadas, timestamp por frase |
| `/transcrever-leitura <urls>` | **reading** | `.html` standalone com mesmo conteúdo do analysis, dentro de `<p>` estilizados (timestamp em `<span class="ts">` discreto) | Consumo humano direto no celular/navegador |

Ambos os formatos derivam dos `segments[]` do Whisper-large-v3 (`verbose_json`), agrupados em frases por pontuação (`.!?`) e em parágrafos de 3 frases. Não tem speaker diarization ainda — todas as falas saem como um único bloco, mesmo em vídeos com múltiplos speakers.

Se a URL chegar sem slash, o subagente pergunta o formato em texto: "[1] TXT timestamps / [2] HTML leitura" e processa após a resposta.

## Múltiplas URLs

`/transcrever url1 url2 url3` processa **em série** (uma por vez), enviando notificação de progresso e anexo de cada uma assim que fica pronta — operador não fica em silêncio esperando 15min pelo último arquivo.

## Como funciona

```
Operador → Telegram → Kobe (agente principal)
                    → reconhece slash / URL / intenção
                    → invoca Agent(subagent_type="atrus", ...)
                       → pra cada URL:
                         kobe-notify "[N/M] Transcrevendo..."
                         python transcribe_url.py <url> --format <X>
                           → Firecrawl scrape(formats=["audio"]) → MP3 (signed 1h)
                           → download HTTP direto
                           → ffmpeg mono 16kbps se >25MB
                           → chunking 10min se ainda passar
                           → Whisper-large-v3 verbose_json
                           → render analysis|reading → salva arquivo
                           → stdout = path
                         kobe-attach "$path"
                       → resumo final
```

## Custos típicos

- Firecrawl: ~$0.01–0.05 por scrape de áudio
- Groq Whisper-large-v3: ~$0.11 por hora de áudio
- **Total: < $0.20 por hora transcrita**

## Instalação no Kobe

```bash
bash $KOBE_HOME/infra/install-plugin.sh https://github.com/felipeocoelho/kobe-plugin-atrus.git
$KOBE_HOME/.venv/bin/pip install firecrawl-py
echo "FIRECRAWL_API_KEY=fc-..." >> $KOBE_HOME/.env
systemctl --user restart kobe
```

## Variáveis de ambiente

- `FIRECRAWL_API_KEY` — https://www.firecrawl.dev
- `GROQ_API_KEY` — já existe no Kobe-base, reusada pelo plugin

## Requisitos do Kobe-base

- **v0.7.0+** pro plugin discovery automático.
- **v0.8.0+** pros helpers `kobe-notify` e `kobe-attach` (progresso em tempo real).

## Limites conhecidos

- Vídeos privados / removidos / region-locked falham no Firecrawl.
- Chunking de 10min mantém timestamps coerentes (offset somado) mas se uma palavra cair na fronteira ela pode aparecer em ambos os pedaços.
- `language="pt"` hardcoded — pra outros idiomas, edite no script.
