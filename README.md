# 挑战杯选拔集训 - 多源记忆数据包

## 目录说明

| 目录 | 用途 | 分发对象 |
|------|------|----------|
| `raw/d1/` | D1 观察 demo，小量样例 | 学生 |
| `raw/d2/` | D2 合并练习脏数据 | 学生 |
| `raw/d3/` | D3 基础验收脏数据 | 学生 |
| `raw/d4/` | D4 多源清洗实战脏数据 | 学生 |
| `raw/d5/` | D5 记忆抽取、冲突与流转训练数据 | 学生 |
| `raw/d6/` | D6 评测与端到端验收训练数据 | 学生 |
| `raw/generated/` | D2-D6 批量生成数据，每天约 1 万条 | 学生 |
| `standard/` | 参考答案与 rubric | **仅教师**（勿发学生） |

## 使用方式

- **D1**：观察 `raw/d1/` 小样例，写出清洗假设
- **D2**：编写 `merge_day2.py`，读取 `raw/d2/`，输出 `merged.jsonl`
- **D3**：编写 `clean_basic.py`，读取 `raw/d3/chat_sessions_dirty.csv`，输出干净 CSV
- **D4**：读取 `raw/d4/` 五个文件，输出 `standard/` 目录下四类 JSON + `report.md`
- **D5**：从多轮交互中抽取偏好、知识、工具执行与遗忘指令，完成冲突合并
- **D6**：用清洗后的记忆结果支持检索、回答与评测，形成可解释的质量报告

详细任务说明见 `docs/daily/` 每日任务单。

学生清洗提示见 `raw/README.md`。该说明只给出预期清理效果，不提前说明数据形式。

## 批量数据生成

默认按 D2-D6 每天约 1 万条生成：

```bash
py scripts/generate_memory_training_data.py --records-per-day 10000
```

输出目录为 `camp-data/raw/generated/`，可用 `--seed` 固定或调整生成结果。生成脚本使用比 demo 更宽的随机场景和噪声组合，demo 不等于答案词典。
