# Generated Large Training Pack

- seed: `20260607`
- target_records_per_day: `10000`
- total_records: `50000`

## Files

| file | records |
|------|---------|
| `d02/tool_result.jsonl` | 3000 |
| `d02/user_behavior.jsonl` | 7000 |
| `d03/chat_sessions_dirty.csv` | 10000 |
| `d04/chat_logs_raw.jsonl` | 4000 |
| `d04/knowledge_raw.jsonl` | 1500 |
| `d04/preferences_raw.csv` | 2500 |
| `d04/tool_result_raw.jsonl` | 2000 |
| `d05/conflict_candidates_raw.csv` | 1000 |
| `d05/memory_events_raw.jsonl` | 6000 |
| `d05/user_memory_snapshots_raw.csv` | 3000 |
| `d06/eval_prompts_dirty.csv` | 5000 |
| `d06/tool_eval_trace_raw.jsonl` | 5000 |

## Generation Rules

- D02-D06 use broad randomized scenarios that are intentionally richer than the small demos.
- The small demos are for orientation only and are not a complete cleaning dictionary.
- Students should infer rules from evidence, report uncertain cases, and explain cleaning decisions.
- Teachers can regenerate with another `--seed` to create a fresh validation set.
