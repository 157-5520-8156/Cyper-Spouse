# Five-turn experience evaluation

The experience evaluator compares human-reviewed five-turn conversation variants. Its numeric
dimensions are annotations, not an automatic claim that the companion is human-like. Facts must
remain identical within and across variants so the comparison measures experience rather than a
different fictional history.

Each input JSON object has `schema_version: 1`, a unique `variant_id`, and exactly five `turns`.
Every turn records the reply, speech act, stance, empathy, persona continuity, grounding, agency,
observable action consequence, a manual review note, and the factual invariants used in that turn.
Scores range from 1 to 5. `action_consequence` is one of `none`, `planned`, `delivered`, `failed`,
`cancelled`, `expired`, or `unknown`.

Record one variant in the append-only JSONL evaluation ledger:

```shell
uv run companion-eval-experience record candidate-a.json --ledger var/evaluation/experience.jsonl
```

Repeat the command for each baseline or candidate, then compare all recorded variants:

```shell
uv run companion-eval-experience compare var/evaluation/experience.jsonl \
  --report var/evaluation/experience-report.json
```

The comparison reports mean reviewer scores, surface diversity, and whether every turn has a
manual note. `human_like` remains `null`: the diagnostic report deliberately does not replace the
human reviewer's judgment.
