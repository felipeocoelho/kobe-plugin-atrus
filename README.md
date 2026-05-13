# kobe-plugin-atrus

> Plugin público do [Kobe](https://github.com/felipeocoelho/kobe) — transcreve URLs de mídia (YouTube, podcast, vídeo embed) usando **Firecrawl** (proxy residencial pago) + **Groq Whisper-large-v3** (transcrição) + **pyannote.audio** (diarização opcional de speakers, roda local).

Resolve o problema clássico de IPs de datacenter sendo bloqueados pelo YouTube/Vimeo. O Firecrawl serve como camada de extração que devolve uma URL assinada do MP3; o Kobe baixa e manda pra Groq. Quando o operador pede speakers, pyannote roda local na CPU antes do Whisper pra identificar quem falou em cada trecho.

## Stack

| Componente | Função |
|---|---|
| Firecrawl `scrape(formats=["audio"])` | Resolve a URL de origem e devolve URL assinada do MP3 (~1h) + `metadata.title` |
| `urllib.request` | Download direto do MP3 (a URL signed do CDN não cai no bloqueio) |
| `ffmpeg` (mono 16kbps) | Compressão pra caber no limite de 25MB do Groq Whisper |
| `ffmpeg segment` | Chunking de 10min se ainda passar do limite |
| Groq Whisper-large-v3 (`language="pt"`, `temperature=0`) | Transcrição determinística |
| pyannote.audio (`speaker-diarization-3.1`) | Diarização local opcional — só quando `--diarize` |

## Custo típico

- Firecrawl: ~$0.01–0.05 por scrape
- Groq Whisper: ~$0.11 por hora de áudio
- pyannote: **gratuito** (CPU local)
- **Total: < $0.20 por hora transcrita**

(Compare com $10/mês fixo do TurboScribe — e sem identificação de speakers.)

## Instalação no Kobe

```bash
# 1. Instala o plugin
bash ~/kobe/infra/install-plugin.sh https://github.com/felipeocoelho/kobe-plugin-atrus.git

# 2. Adiciona a dependência Python base no venv do Kobe
~/kobe/.venv/bin/pip install firecrawl-py

# 3. Adiciona a credencial no .env
echo "FIRECRAWL_API_KEY=fc-sua-key-aqui" >> ~/kobe/.env

# 4. Reinicia o bot pra carregar
systemctl --user restart kobe
```

A partir daí, qualquer URL de vídeo/podcast que você mandar no Telegram com intenção de transcrição dispara o subagente `atrus`.

**Pra habilitar speakers (`/transcrever-speakers` e `/transcrever-leitura-speakers`):** veja [`docs/runbooks/pyannote-setup.md`](./docs/runbooks/pyannote-setup.md).

## Variáveis de ambiente

| Var | Quando | Onde obter |
|---|---|---|
| `FIRECRAWL_API_KEY` | sempre | https://www.firecrawl.dev |
| `GROQ_API_KEY` | sempre | https://console.groq.com (já configurada no Kobe-base) |
| `HF_TOKEN` | só pra speakers | https://huggingface.co/settings/tokens — veja runbook |

## Quatro formatos de saída

| Comando textual | Formato | Speakers? | Saída |
|---|---|---|---|
| `/transcrever <urls>` | analysis | não | `.txt` estilo TurboScribe — parágrafos de ~3 frases, cada frase prefixada por `(M:SS)` |
| `/transcrever-leitura <urls>` | reading | não | `.html` standalone com 1 timestamp discreto por parágrafo |
| `/transcrever-speakers <urls>` | analysis | sim | `.txt` com blocos `Speaker 1`, `Speaker 2`… |
| `/transcrever-leitura-speakers <urls>` | reading | sim | `.html` com `<section class="speaker">` por falante |

URL solta sem slash → o subagente pergunta o formato antes de processar (`[1]/[2]/[3]/[4]`).

Múltiplas URLs num só comando: processadas em série, com progresso em tempo real via `kobe-notify` / `kobe-attach` — cada anexo chega conforme fica pronto.

Arquivos vão pra `$KOBE_HOME/user-data/artifacts/transcricoes/`.

## Uso direto (sem Kobe)

O script é standalone — funciona via CLI:

```bash
export FIRECRAWL_API_KEY=fc-...
export GROQ_API_KEY=gsk-...
python scripts/transcribe_url.py "https://www.youtube.com/watch?v=..." \
       --format analysis \
       --output-dir /tmp

# Com diarização (precisa de HF_TOKEN + pyannote instalado):
export HF_TOKEN=hf_...
python scripts/transcribe_url.py "<url>" --format reading --diarize --output-dir /tmp
```

Imprime o **path do arquivo gerado** em stdout, progresso em stderr.

## Limites conhecidos

- Vídeos **privados / removidos / region-locked** falham na etapa Firecrawl. Sem retry automático.
- Áudio **muito longo** com Whisper usa chunking de 10min (timestamps absolutos preservados via offset).
- Diarização (pyannote) roda no áudio inteiro **antes** do chunking Whisper — vídeos >2h podem usar muita RAM.
- Quando o Whisper não pontua um trecho longo, forçamos quebra de frase a cada 28 palavras (insere `.` no fim) — evita parágrafos gigantescos no render.
- **Idioma**: força `language="pt"`. Pra outros idiomas, edite no script.

## Licença

MIT — veja [`LICENSE`](./LICENSE) (ou siga a do Kobe-base).
