# kobe-plugin-atrus

> Plugin público do [Kobe](https://github.com/felipeocoelho/kobe) — transcreve URLs de mídia (YouTube, podcast, vídeo embed) usando **Firecrawl** (proxy residencial pago) + **Groq Whisper-large-v3** (transcrição).

Resolve o problema clássico de IPs de datacenter sendo bloqueados pelo YouTube/Vimeo. O Firecrawl serve como camada de extração que devolve uma URL assinada do MP3; o Kobe baixa e manda pra Groq.

## Stack

| Componente | Função |
|---|---|
| Firecrawl `scrape(formats=["audio"])` | Resolve a URL de origem e devolve uma URL assinada do MP3 (válida ~1h) |
| `urllib.request` | Download direto do MP3 (a URL signed do CDN não cai no bloqueio) |
| `ffmpeg` (mono 16kbps) | Compressão pra caber no limite de 25MB do Groq Whisper |
| `ffmpeg segment` | Chunking de 10min se ainda passar do limite |
| Groq Whisper-large-v3 (`language="pt"`, `temperature=0`) | Transcrição determinística |

## Custo típico

- Firecrawl: ~$0.01–0.05 por scrape
- Groq Whisper: ~$0.11 por hora de áudio
- **Total: < $0.20 por hora transcrita**

(Compare com $10/mês fixo do TurboScribe.)

## Instalação no Kobe

```bash
# 1. Instala o plugin
bash ~/kobe/infra/install-plugin.sh https://github.com/felipeocoelho/kobe-plugin-atrus.git

# 2. Adiciona a dependência Python no venv do Kobe
~/kobe/.venv/bin/pip install firecrawl-py

# 3. Adiciona a credencial no .env
echo "FIRECRAWL_API_KEY=fc-sua-key-aqui" >> ~/kobe/.env

# 4. Reinicia o bot pra carregar
systemctl --user restart kobe
```

A partir daí, qualquer URL de vídeo/podcast que você mandar no Telegram com intenção de transcrição dispara o subagente `atrus`.

## Variáveis de ambiente

| Var | Onde obter |
|---|---|
| `FIRECRAWL_API_KEY` | https://www.firecrawl.dev |
| `GROQ_API_KEY` | https://console.groq.com (já configurada no Kobe-base) |

## Dois formatos de saída

| Comando textual | Formato | Saída | Uso |
|---|---|---|---|
| `/transcrever <urls>` | `analysis` | `.txt` com `[HH:MM:SS] texto` | Pra alimentar análise downstream |
| `/transcrever-leitura <urls>` | `reading` | `.html` standalone com CSS estilo livro | Pra ler no celular/navegador |

URL solta sem slash → o subagente pergunta o formato antes de processar (`[1]` / `[2]`).

Múltiplas URLs num só comando: processadas em série, com progresso em tempo real via `kobe-notify` / `kobe-attach` — cada anexo chega conforme fica pronto.

## Uso direto (sem Kobe)

O script é standalone — funciona via CLI:

```bash
export FIRECRAWL_API_KEY=fc-...
export GROQ_API_KEY=gsk-...
python scripts/transcribe_url.py "https://www.youtube.com/watch?v=..." \
       --format analysis \
       --output-dir /tmp
```

Imprime o **path do arquivo gerado** em stdout, progresso em stderr.

## Limites conhecidos

- Vídeos **privados / removidos / region-locked** falham na etapa Firecrawl. Sem retry automático.
- Áudio **muito longo** (várias horas) usa chunking de 10min. Texto vem concatenado com quebra dupla; timestamps absolutos por chunk não são reconstruídos.
- **Idioma**: força `language="pt"`. Pra outros idiomas, edite a variável `whisper_transcribe` no script.

## Licença

MIT — veja [`LICENSE`](./LICENSE) (ou siga a do Kobe-base).
