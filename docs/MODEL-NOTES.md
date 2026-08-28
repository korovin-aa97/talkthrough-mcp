# Model compatibility notes — talkthrough-mcp v0.3.0

*Snapshot: August 2026. This is a reproducible local corpus cut, not a
leaderboard; provider models can drift after the snapshot.*

## Method

210 agent runs across 6 runner configurations and
35 logical cells. The full grid was scored by an isolated
Sonnet judge, then audited against saved raw outputs. Safety and v0.3 cells
were checked mechanically; every zero and every baseline drop required
explicit adjudication. Stores and media were isolated from user data.

## Task x model (mean score, n)

| Task | haiku (default) | sonnet (default) | opus (default) | gpt-5.5 (medium) | gpt-5.5 (high) | gpt-5.4-mini (low) |
|---|---|---|---|---|---|---|
| T0 — Naive «analyze this meeting» (zero hints) | **0.5** (2)<br>323s | **1.0** (2)<br>841s | **1.5** (2)<br>432s | **2.0** (2)<br>372s | **1.0** (2)<br>392s | **1.5** (2)<br>294s |
| T0s — Naive «triage this bug screencast» (inverse: must NOT diarize) | **0.0** (1)<br>41s | **2.0** (1)<br>93s | **2.0** (1)<br>93s | **2.0** (1)<br>41s | **2.0** (1)<br>62s | **0.0** (1)<br>33s |
| T1 — Ingest with who-said-what intent (parameter choice) | **1.0** (3)<br>23s | **1.0** (3)<br>784s | **0.7** (3)<br>845s | **1.3** (3)<br>347s | **1.3** (3)<br>54s | **1.0** (3)<br>28s |
| T2 — Point lookup: who said <known quote> + when | **0.7** (3)<br>18s | **0.3** (3)<br>21s | **1.3** (3)<br>36s | **0.7** (3)<br>17s | **0.7** (3)<br>23s | **1.7** (3)<br>26s |
| T3 — Map speaker labels to real names (evidence required) | **0.7** (3)<br>101s | **1.3** (3)<br>280s | **1.3** (3)<br>572s | **1.0** (3)<br>74s | **1.0** (3)<br>221s | **1.0** (3)<br>68s |
| T4 — Meeting minutes with owners (source language) | **0.0** (2)<br>54s | **1.0** (2)<br>246s | **1.0** (2)<br>253s | **0.5** (2)<br>65s | **0.5** (2)<br>91s | **1.0** (2)<br>46s |
| T5 — Find the key slide, return screenshot path | **0.5** (2)<br>32s | **2.0** (2)<br>37s | **1.0** (2)<br>105s | **0.0** (2)<br>40s | **1.5** (2)<br>40s | **1.0** (2)<br>27s |
| T6 — Bug triage to findings JSON (verbatim quotes) | **2.0** (1)<br>46s | **2.0** (1)<br>69s | **2.0** (1)<br>123s | **2.0** (1)<br>50s | **0.0** (1)<br>60s | **1.0** (1)<br>38s |

## Recording x model (mean score, n)

| Recording | haiku | sonnet | opus | gpt-5.5 | gpt-5.5 | gpt-5.4-mini |
|---|---|---|---|---|---|---|
| 73-min RU team meeting (1 dominant presenter + Q&A) | **0.5** (6)<br>39s | **1.5** (6)<br>128s | **1.2** (6)<br>192s | **0.7** (6)<br>53s | **0.7** (6)<br>63s | **1.3** (6)<br>38s |
| 26-min EN knowledge-transfer call | **0.0** (2)<br>226s | **0.5** (2)<br>219s | **1.0** (2)<br>75s | **0.5** (2)<br>62s | **0.5** (2)<br>204s | **1.0** (2)<br>40s |
| 43-min EN UX-research workshop (fast turn-taking) | **0.8** (6)<br>28s | **1.0** (6)<br>155s | **1.0** (6)<br>161s | **1.2** (6)<br>49s | **1.3** (6)<br>47s | **1.3** (6)<br>26s |
| 2-min RU narrated bug screencast | **1.0** (2)<br>44s | **2.0** (2)<br>81s | **2.0** (2)<br>108s | **2.0** (2)<br>46s | **1.0** (2)<br>61s | **0.5** (2)<br>36s |
| 30-sec EN two-voice synthetic fixture | **1.0** (1)<br>23s | **0.0** (1)<br>17s | **2.0** (1)<br>47s | **2.0** (1)<br>29s | **2.0** (1)<br>54s | **0.0** (1)<br>20s |

## v0.3 behavior cells

| Cell | Mechanical passes | Runs |
|---|---:|---:|
| TLABEL-READ | 6 | 6 |
| TLABEL-SAVE | 6 | 6 |
| TNAMECAND | 6 | 6 |
| TSPLIT-A360 | 6 | 6 |
| TSPLIT-MI | 6 | 6 |

## Regression gate

- True→False parity flips: 1
- Audited release-caused regressions: 0
- Gate: PASS

Raw outputs, mechanical rescoring, judge records, and adjudications are
kept under `talkthrough-qa/data/battery-*-v030*`.
