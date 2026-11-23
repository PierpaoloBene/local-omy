# Fase 0: misure sul MacBook Pro M2 Pro 16 GB (2026-11-23)

5 segmenti reali da 2,3k a 5,2k token, schema JSON vincolato, temperatura 0,1, contesto 16k, `-fa on -ngl all --no-mmproj`.

## Organizzatore: Qwen3.8-27B (ISTA-DASLab GSQ-RCO IQ2_XS-mtp, 8,77 GB)

| Configurazione (llama.cpp) | Prefill tok/s | Decode tok/s | Draft accettati | Tempo 5 segmenti | Picco RSS | JSON/schema/refs |
|---|---|---|---|---|---|---|
| base | 74 | 8,8 | – | 855 s | 10,2 GB | 100% |
| MTP n=2 | 72 | 12,3 | 88% | 644 s | 10,7 GB | 100% |
| MTP n=3 | 72 | 14,9 | 83% | 614 s | 10,5 GB | 100% |
| **MTP n=4** | 72 | **16,6** | 77% | **565 s** | 10,3–10,7 GB | 100% |
| MTP n=5 | 72 | 17,6 | 69% | 554 s | 10,9 GB | 100% |
| ngram-mod | 74 | 8,4 | – | – | – | 100% |
| ngram-map-k | 74 | 8,3 | 8% | 844 s | 10,6 GB | 100% |
| MTP n=3 + ngram-mod | 72 | 13,8 | 72% | 616 s | 10,7 GB | 100% |
| unsloth UD-IQ2_XS base (7,63 GB, senza MTP) | 74 | 8,6 | – | 814 s | 8,0 GB | 100% |

- Il decode delle IQ2 è limitato dal calcolo (dequantizzazione a codebook), non dalla banda: 8,8 tok/s ≈ 70 GB/s effettivi.
- Prefix cache: con system prompt identico vengono riusati 613 token su ~657 (context checkpoint del modello ibrido).
- Logprob con speculazione: a build 10360 valgono sempre 1,0 (bug llama.cpp #27972); corretto dalla build 12359 (confidenza media 0,908 con MTP n=4 contro 0,915 senza).

## Modello piccolo per "Chiedi" (~2,5–3k token di contesto, risposta ≤200 token)

| Modello / runtime | Prefill tok/s | Decode tok/s | Primo token | RSS |
|---|---|---|---|---|
| Qwen3.5-4B Q4_0, llama.cpp | 500–520 | 48–50 | 3,5–5,1 s | 3,7 GB |
| idem + MTP (sidecar Q4_0) | 485–490 | 40–47 | 3,7–5,3 s | 4,6 GB |
| Qwen3.8-4B-Distill Q4_K_M, llama.cpp | 460–475 | 43–46 | 3,8–5,6 s | 3,8 GB |
| Qwen3.5-4B 4-bit, mlx-lm 0.32 | n/d | 56–58 | 4,7–6,7 s | 2,7 GB |

- `-b/-ub 2048` non cambia il prefill. MTP non aiuta il 4B.
- Per stare sotto 5 s end-to-end il contesto RAG deve restare intorno a 1–1,5k token.
