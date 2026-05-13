# Runbook — habilitar diarização de speakers no atrus

> **Quando rodar isso:** apenas se você quer usar os formatos `/transcrever-speakers` ou `/transcrever-leitura-speakers`. Os formatos sem speakers (`/transcrever` e `/transcrever-leitura`) funcionam sem nada disso.

A diarização de speakers no atrus usa **pyannote.audio** (modelo `speaker-diarization-3.1`) rodando **local** no servidor do Kobe — sem custo por uso, sem dependência de API externa, mas exige 5-10 min de CPU por hora de áudio.

Pra habilitar, são quatro coisas:

1. Criar token no Hugging Face
2. Aceitar os termos do modelo `speaker-diarization-3.1` no site do HF
3. Aceitar os termos do modelo `segmentation-3.0` (dependência do pipeline)
4. Instalar `pyannote.audio` no venv do Kobe e colocar `HF_TOKEN` no `.env`

Estimativa: 10-15 min se você seguir na ordem.

---

## 1. Criar token no Hugging Face

1. Abra https://huggingface.co/settings/tokens
2. Se não tiver conta, crie (https://huggingface.co/join). É gratuito.
3. Clique em **Create new token**:
   - Nome: `kobe-atrus` (qualquer nome serve)
   - Tipo: **Read** (não precisa de Write/Fine-grained)
4. Copie o token (formato `hf_...`). **Salva em algum lugar agora** — depois de fechar a página, não dá pra ver de novo, só regerar.

---

## 2. Aceitar os termos do modelo `speaker-diarization-3.1`

1. Abra https://huggingface.co/pyannote/speaker-diarization-3.1
2. Faça login no HF (mesmo usuário do token).
3. Procure o aviso **"You need to agree to share your contact information to access this model"** e clique em **Agree and access repository**.
4. Confirme que apareceu **"Gated model — You have been granted access"** no topo da página.

> Esse passo **não dá pra automatizar** — o Hugging Face exige aceite por humano via browser.

---

## 3. Aceitar os termos do modelo `segmentation-3.0`

O pipeline `speaker-diarization-3.1` usa internamente um modelo de segmentação que tem aceite separado.

1. Abra https://huggingface.co/pyannote/segmentation-3.0
2. Mesma coisa: **Agree and access repository**.
3. Confirme acesso.

> Se você pular esse passo, o atrus vai falhar com mensagem tipo `Cannot load model 'pyannote/segmentation-3.0'`. Aí volte aqui.

---

## 4. Instalar pyannote no venv do Kobe + adicionar HF_TOKEN no `.env`

Conectado na VPS, com o Kobe instalado em `~/kobe`:

```bash
# 1. Instala pyannote.audio no venv do Kobe (~500MB com torch, leva 2-5min)
~/kobe/.venv/bin/pip install pyannote.audio

# 2. Adiciona o token no .env (substitua hf_... pelo seu token real)
echo "HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxx" >> ~/kobe/.env

# 3. Reinicia o bot pra ele carregar a env nova
systemctl --user restart kobe

# 4. Verifica que subiu OK
systemctl --user status kobe --no-pager | head -20
```

> O modelo em si (~500MB) é baixado **na primeira execução** com `--diarize`, não na instalação. Vai pra `~/.cache/huggingface/`. As próximas execuções reusam o cache.

---

## 5. Teste

No Telegram, manda pro HAL:

```
/transcrever-speakers https://www.youtube.com/watch?v=URL_CURTA_DE_UNS_2_MIN
```

Esperado:
1. HAL invoca o subagente atrus
2. Notify: `[1/1] Transcrevendo: youtube.com/...`
3. Stderr (na primeira execução só): baixa o modelo pyannote (~500MB)
4. Stderr: `rodando diarization (pode levar 5-10min por hora de áudio na CPU)…`
5. Stderr: `transcrevendo no formato 'analysis' com speakers…`
6. Anexo: `<slug>-analysis-speakers.txt` com blocos `Speaker 1`, `Speaker 2`…

---

## Troubleshooting

| Sintoma | Causa provável | Correção |
|---|---|---|
| `--diarize requer HF_TOKEN no env` | Falta `HF_TOKEN` no `~/kobe/.env` ou bot não foi reiniciado | Confere `cat ~/kobe/.env \| grep HF_TOKEN` e roda `systemctl --user restart kobe` |
| `pyannote.audio não instalado` | `pip install` não foi feito (ou foi em venv errado) | Garantir que rodou `~/kobe/.venv/bin/pip install pyannote.audio`, não só `pip install` |
| `falha carregando pipeline … 401 Unauthorized` | Token HF inválido | Gera token novo em https://huggingface.co/settings/tokens e substitui no `.env` |
| `falha carregando pipeline … 403 Forbidden` ou `gated repo` | Você não aceitou os termos de um dos dois modelos | Volta nos passos 2 e 3 — precisa aceitar **ambos** (speaker-diarization-3.1 **e** segmentation-3.0) |
| Diarização demora muito (>30min pra 1h de áudio) | CPU fraca ou áudio muito longo | Aceitável até umas 2h de áudio. Acima disso, pondere se vale a pena ou use sem speakers |
| RAM cheia / OOM | Áudio muito longo (>2-3h) | Por enquanto não tem chunking pyannote — divida o áudio antes ou rode sem speakers |

---

## Por que não algo mais simples?

| Alternativa | Por que não | 
|---|---|
| **Deepgram Nova-2** | Diarização nativa, qualidade boa, ~$0.26/h. Descartada porque cria dependência paga adicional |
| **AssemblyAI** | Diarização nativa, ~$0.37/h. Mesmo motivo |
| **WhisperX (sobre Groq)** | Wrapper que faz Whisper + diarization. Requer GPU pra ser rápido; sem GPU é mais lento que pyannote direto |
| **Groq Whisper só** | Não faz diarization nativamente. Sem caminho |

A escolha de pyannote local é coerente com a filosofia do Kobe: **dado fica na VPS, custo previsível, sem nova credencial paga**.
