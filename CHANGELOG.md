# Changelog — atrus

Formato: cada mudança é um bloco auditável (pedido → porquê → o que foi feito →
testes → commits → reversão). Datas em AAAA-MM-DD.

## [2026-07-02] — Formatos SRT: legenda `.srt` + legenda `.srt` traduzida pt-br (v0.5.0)

**Operador pediu:** adicionar 2 novos formatos de saída ao atrus — (1) legenda `.srt`
(SubRip) com timestamps de bloco `HH:MM:SS,mmm --> HH:MM:SS,mmm`; (2) legenda `.srt`
traduzida pra pt-br mantendo o sincronismo dos timestamps.

**Por quê:** os 4 formatos atuais (TXT/HTML × com/sem speakers) servem leitura e análise,
mas não geram trilha de legenda carregável em player/editor de vídeo. E vídeos em outra
língua precisavam de legenda em português sincronizada.

**Foi feito:**
- `_format_srt_timestamp()` — formata segundos no padrão SubRip (`HH:MM:SS,mmm`, vírgula
  decimal), distinto do `_format_timestamp` (leitura humana `M:SS`).
- `render_srt()` — cada frase de `_segments_to_sentences` (timing por segmento do ASR,
  granularidade de frase, **nunca** agregação por parágrafo) vira 1 bloco SRT numerado.
  Aceita `texts=` opcional pra trocar só o texto preservando os timestamps (hook da tradução).
- `_srt_cue_times()` — guarda de timing: duração mínima (1,2s) contra cue de duração ~0 e
  clamp ao próximo bloco contra sobreposição.
- `srt_sentence_texts()` — extrai os textos das frases na mesma ordem/contagem, pra alimentar
  a tradução casando 1:1 com `render_srt(texts=...)`.
- Tradução (formato 6): módulo `scripts/translate.py`, OpenAI `gpt-4o-mini`, batch numerado
  por índice com fallback por bloco ao texto original; auto-detecção do idioma de origem
  (Whisper sem `language` forçado) com chave de cache `|autolang` separada.
- `--format` aceita `srt` e `srt_ptbr` em `transcribe_url.py` e no worker; SRT sai sem
  header/comentário (senão quebraria o parsing do player).
- Manifest + agent def: 2 slash commands novos (`/transcrever_legenda`,
  `/transcrever_legenda_traduzida`), pergunta de formato vira `[1..6]`, bump v0.4.0 → v0.5.0.

**Testes (dev VPS):** núcleo SRT com segments sintéticos (`.local/test_srt.py`, 19 checagens,
custo zero) — timestamp SubRip, estrutura de blocos, guarda de duração-zero e sobreposição,
preservação idêntica dos timestamps entre original e traduzido, e ValueError em contagem
incompatível. Parser/batcher de tradução com tradutor fake (`.local/test_translate.py`, 12
checagens, sem API) — mapeamento por índice, fallback por bloco ao original, garantia de
tamanho, erro sem chave. Smoke de integração do `--format=srt` via cache hit sintético
(zero custo). Smoke REAL do `--format=srt_ptbr` via Groq: "Hello everyone, welcome to the
show." → "Olá a todos, bem-vindos ao programa.", com timestamps idênticos ao original (sync
provado). Observação: OpenAI (engine default) está sem quota neste ambiente — validação real
foi via Groq; o fallback deixou o SRT válido mesmo com a OpenAI em 429. Todos verdes.

**Descoberta durante a execução (engine de tradução):** o `OPENAI_API_KEY` do ambiente está
sem quota (429 `insufficient_quota`). Por isso `translate.py` virou **multi-engine**
selecionável por `ATRUS_TRANSLATE_ENGINE` — `openai` (default, escolha do operador) ou `groq`
(`llama-3.3-70b-versatile`, mesma chave do Whisper, com quota). O fallback por bloco garantiu
que mesmo com a engine sem quota o SRT saiu válido (texto original, timestamps intactos). A
escolha da engine de produção fica com o operador; a troca é um flip de env, sem mexer em código.

**Commits (progresso):**
- `06b5101` — núcleo SRT (render_srt, _format_srt_timestamp, guarda de timing) + testes
- commit 2 — `--format=srt` no transcribe_url (branch sem header) + worker; smoke de integração
  via cache hit sintético ok (SRT reaproveita o cache do caminho sem-speakers)
- `07cc1d2` — `translate.py` multi-engine + `--format=srt_ptbr` + auto-detecção de idioma
  (Whisper sem `language`; AssemblyAI `language_detection` no fallback) + cache key `|autolang`.
  Teste do parser/batcher com tradutor fake (12 checagens) + smoke REAL via Groq (tradução
  EN→pt-br correta, timestamps idênticos ao original — sync provado)
- commit 4 — manifest 6 formatos (2 slash novos, deps `openai`, envs `OPENAI_API_KEY`/
  `ATRUS_TRANSLATE_ENGINE`, bump v0.5.0) + agent def pergunta `[1..6]` + mapa comando→--format +
  exceção da regra "não traduza" p/ o formato traduzido + `requirements.txt`

**Reversão:** cada commit é atômico e revertível via `git revert <hash>`; nenhum arquivo
existente é removido, mudanças são aditivas (novos formatos, código atual intocado).
