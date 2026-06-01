# Runbook — habilitar diarização de speakers no atrus (AssemblyAI)

> **Quando rodar isso:** apenas se você quer usar os formatos `/transcrever-speakers` ou `/transcrever-leitura-speakers`. Os formatos sem speakers (`/transcrever` e `/transcrever-leitura`) funcionam sem nada disso — usam Groq Whisper, que já está configurado no Kobe-base.

A diarização de speakers no atrus usa **AssemblyAI** (transcrição + speaker labels numa única chamada à nuvem deles). Substituiu a implementação anterior em **pyannote local** porque pyannote saturava a CPU da VPS durante horas; AssemblyAI processa em paralelo na nuvem e libera a VPS.

## Passos

### 1. Criar conta na AssemblyAI

Vá em https://www.assemblyai.com/app/account e crie conta. Free tier oferece créditos iniciais (~$50 ao registrar). Depois é pay-as-you-go: ~$0.37/h de áudio com speakers.

### 2. Gerar API key

Em **Settings → API Keys**, copie a chave (formato `xxx...xxx`, 32 chars).

### 3. Adicionar ao .env do Kobe

```bash
echo "ASSEMBLYAI_API_KEY=<sua-chave>" >> $KOBE_HOME/.env
```

Não comite — `.env` já está no `.gitignore` por padrão.

### 4. Instalar SDK no venv do Kobe

```bash
$KOBE_HOME/.venv/bin/pip install assemblyai
```

(Já está em `requirements.txt` do plugin — se você usou `pip install -r requirements.txt` na instalação, está feito.)

### 5. Reiniciar o bot do Kobe

```bash
systemctl --user restart kobe
```

(Necessário pra o `load_dotenv` ler a chave nova.)

## Validar

No Telegram, mande:

```
/transcrever-speakers https://www.youtube.com/watch?v=ID-CURTO
```

O subagente atrus vai disparar o pipeline em background (via `kobe-dispatch`) e retornar em segundos algo como:

```
Disparei 1 transcrição em paralelo, em background:
• https://www.youtube.com/... — job abc123def456
Cada uma te avisa quando começar e quando terminar.
```

Em alguns segundos vem `▶️ atrus: iniciando (TXT + speakers)`, depois em minutos vem `✅ atrus: pronto em Xs` + o arquivo anexado.

## Quanto custa monitorar

`https://www.assemblyai.com/app/usage` mostra créditos consumidos. Para áudios típicos (entrevistas de 30-90min), cada transcrição custa $0.20-$0.55.

## Troubleshooting

| Sintoma | Diagnóstico |
|---|---|
| `AssemblyAI falhou: Authentication failed` | Chave inválida ou expirada — regenere em Settings → API Keys. |
| `Missing ASSEMBLYAI_API_KEY` | Variável não chegou no env do bot — confira `.env` e reinicie o bot. |
| `assemblyai SDK não instalado` | Rode `$KOBE_HOME/.venv/bin/pip install assemblyai`. |
| Vídeo não tem `utterances` | Áudio muito curto, sem fala detectável, ou idioma errado — confira o `language_code` (default `pt`). |

## Histórico

A versão anterior do atrus (≤ v0.3.x) usava `pyannote/speaker-diarization-3.1` local. Foi substituído na v0.4.0 por motivos de **footprint operacional**: pyannote rodando em CPU virtualizada da VPS Hostinger consumia 99% CPU por 2-3h em vídeos de 1h — alarmou rate limits da hospedagem e travava outras tarefas do bot. AssemblyAI custa $0.37/h mas libera completamente a VPS.
