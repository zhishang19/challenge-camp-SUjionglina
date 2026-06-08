# D4 多源清洗报告

## 1. 概览
- chat 行数：15（原始 16）
- preference 条数：8（原始 9）
- knowledge 案例数：5（原始 6）
- tool_result 条数：7（原始 7）
- config 快照：已生成 config_snapshot.json
- 字段校验未通过合计：chat=0 pref=1 knowledge=1 tool=0
- 去重丢弃合计：chat=1
- 用户纠正：1；偏好冲突（与 config-default）：3；工具异常：2

## 2. 停顿词统计（必出）

### 2.1 合并计数（chat + tool）
| 停顿词 | 命中次数 |
|---|---|
| `嗯` | 6 |
| `然后` | 4 |
| `那个` | 3 |
| `啊` | 2 |
| `这个` | 2 |
| `呃` | 1 |
| `就是` | 1 |
| `啦` | 1 |

## 3. needs_review 清单

### 3.1 chat (3 条)
- `S103#u004#user#2026-06-04T16:00:00#10`：['forget_intent:别记下来']
- `S104#U005#user#2026-06-04T17:22:00#13`：['temporary:这次例外']
- `S105#u006#user#2026-06-04 18:10#14`：['forget_intent:别保存']

### 3.2 preference (4 条)
- `P2`：['conflict_with_default']
- `P3`：['conflict_with_default']
- `P6`：['missing_required_fields', 'uid_missing']
- `P9`：['conflict_with_default']

### 3.3 knowledge (1 条)
- `K005`：['typo:偏好计忆→偏好记忆', 'typo:会义纪要→会议纪要']

### 3.4 tool_result (3 条)
- `T-502`：['flags=casual_in_output']
- `T-503`：['flags=tool_failed,uncertain_path_or_text,casual_in_output']
- `T-506`：['flags=tool_failed,casual_in_output']

## 4. 错别字记录

| 错别字 | 命中次数 |
|---|---|
| `奇麟→麒麟` | 1 |

## 5. 遗忘 / 隐私 / 临时指令

### 5.1 遗忘指令
| session | uid | 原文 | 触发词 |
|---|---|---|---|
| S103 | u004 | 我的邮箱是 [REDACTED:email] 别记下来啊… | 别记下来 |
| S105 | u006 | 密码重置流程：先在控制中心找账户，再点忘记密码。电话 [REDACTED:phone] 是我测试号，别保存… | 别保存 |

### 5.2 临时指令（不应覆盖长期偏好）
| session | uid | 原文 | 触发词 |
|---|---|---|---|
| S104 | U005 | 今天这次例外，给我 bullet 列表，不代表以后都这样… | 这次例外 |

## 6. 冲突清单

### 6.1 偏好 vs config-default
| uid | pref_key | default | user | version | state |
|---|---|---|---|---|---|
| u001 | output_style | 简洁 | 详细、带数据表格 | v2 | corrected |
| U002 | emoji_policy | 允许 | 禁用 | v1 | explicit |
| u002 | emoji_policy | 允许 | 允许少量 emoji | v0 | explicit |

### 6.2 用户纠正（user correction）
| session | uid | 改前 | 改后 | 否决词 | 选用词 |
|---|---|---|---|---|---|
| S100 | u001 | 帮我把月报导出成PDF，要【简洁版】 | 不对不对！要详细版 @@@ 不要简洁 | 简洁 | 详细版 |

### 6.3 工具调用异常
| trace | tool | flags | 文本 |
|---|---|---|---|
| T-502 | file_export | casual_in_output | exported: /home/u001/月报_详细版.pdf 嗯嗯完成啦… |
| T-503 | web_search | tool_failed,uncertain_path_or_text,casual_in_output | timeout after 3000ms … 网络那个不稳定… |

## 7. 错误分析与建议
- 选做：敏感信息已按 `phone:1[3-9]\d{9}` 与 `email:.+@.+` 替换为 `[REDACTED:xxx]`；`config_manual.yaml` 中匹配不到的正则会被忽略。
- 选做：偏好按 `(uid, pref_key)` 取 `max(version)` 合并；带 `纠正` 的 note 标为 `corrected` 态。
- 选做：用户表达与 `config_manual.yaml` 默认值冲突时，单列在 6.1；D5 流转时建议把 `corrected` 视为 `default` 的覆盖。
- `config_manual.yaml` 的格式只有两层缩进，所以脚本自带的极简 YAML 解析即可处理；遇到三层/数组嵌套需切换 PyYAML。
- `knowledge_raw.txt` 案例之间用 `=== 案例开始 === / === 案例结束 ===` 分隔；'=== 垃圾行 ===' 被识别为噪声但不删除原文。
- D2/D3 的清洗口径（停顿/口语、装饰/残留、错别字）在 D4 沿用，叠加敏感信息脱敏与版本合并。
