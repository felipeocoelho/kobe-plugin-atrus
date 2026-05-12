---
name: atrus
visibility: public
version: 0.1.0
description: Transcrição de URLs de mídia (YouTube, podcast, vídeo embed) via Firecrawl + Groq Whisper. Resolve o caso clássico de IPs de datacenter bloqueados pelo YouTube — Firecrawl atua como proxy residencial pago via API e devolve URL assinada do MP3.
triggers:
  - "operador manda link de YouTube, Vimeo, Spotify, podcast ou pede 'transcreve esse vídeo/link'"
  - "mensagem com URL de mídia + intenção explícita de transcrição"
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

Plugin público do Kobe pra transcrição de URLs de mídia. Resolve o problema clássico de VPS sendo bloqueado por YouTube/Vimeo: o Firecrawl atua como proxy residencial (pago via API), retorna URL assinada do MP3, e o Groq Whisper (large-v3, language="pt", temperature=0) transcreve.

## Como funciona

```
Telegram → Kobe → detecta URL de mídia
              → subagente atrus
              → scripts/transcribe_url.py <url>
                  → Firecrawl scrape(formats=["audio"]) → URL MP3 (signed, 1h)
                  → download do MP3
                  → ffmpeg comprime mono 16kbps se > 25MB
                  → chunking de 10min se ainda passar
                  → Groq Whisper-large-v3 (pt, temp=0)
              → texto na resposta do subagente
              → Kobe devolve no Telegram (fatiando se > 4000 chars)
```

## Custos típicos

- Firecrawl: ~$0.01–0.05 por scrape de áudio
- Groq Whisper-large-v3: ~$0.11 por hora de áudio
- Total: < $0.20 por hora transcrita

## Instalação

```bash
bash $KOBE_HOME/infra/install-plugin.sh https://github.com/felipeocoelho/kobe-plugin-atrus.git
$KOBE_HOME/.venv/bin/pip install firecrawl-py
echo "FIRECRAWL_API_KEY=fc-..." >> $KOBE_HOME/.env
systemctl --user restart kobe
```

## Variáveis de ambiente

- `FIRECRAWL_API_KEY` — chave da API Firecrawl (https://www.firecrawl.dev)
- `GROQ_API_KEY` — já existe no Kobe-base, reusada pelo plugin

## Limites e ressalvas

- Vídeos privados / removidos / region-locked: Firecrawl falha; mensagem clara, sem retry.
- Áudio muito grande pós-compressão: chunking automático em pedaços de 10min (perde precisão de timestamps absolutos mas mantém qualidade da transcrição).
- Não tenta detectar idioma — força `language="pt"` (operador é PT-BR). Adapte se for o caso.
