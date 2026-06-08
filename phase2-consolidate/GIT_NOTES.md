# phase2 Git 使用笔记

D4 阶段按赛题要求（任务 2：Git 工程规范 20 分）整理。
本笔记只记录本阶段实际用到的命令与踩坑，不抄通用教程。

## 1. 仓库约定

- 远端：`git@github.com:zhishang19/challenge-camp-SUjionglina.git`（已迁移到该地址）
- 本阶段只读 `raw/d4/` 五个文件，全部输出物（4 个 JSON + report.md + clean.log）放到 `phase2-consolidate/` 下
- 分支策略：每个阶段对应一个分支（`main` / `phase1-basics` / `phase2-consolidate` / `phase3-advance`），互不污染

## 2. 本阶段实操记录

### 2.1 初始化 / 拉取（沿用 phase1 已建的本地仓库）

```bash
cd "d:\新建文件夹 (13)"
git status                         # 确认在 main，分支是否领先/落后
git fetch --all                    # 同步远端四个分支的引用
```

### 2.2 提交 D4 输出

```bash
git add phase2-consolidate/pipeline/run.py
git add phase2-consolidate/chats.json
git add phase2-consolidate/preferences.json
git add phase2-consolidate/knowledge.json
git add phase2-consolidate/tool_results.json
git add phase2-consolidate/config_snapshot.json
git add phase2-consolidate/report.md
git add phase2-consolidate/clean.log
git add phase2-consolidate/requirements.txt
git add phase2-consolidate/.gitignore
git add phase2-consolidate/GIT_NOTES.md
git commit -m "phase2: D4 multi-source cleaning pipeline"
git push origin phase2-consolidate
```

### 2.3 幂等性

`pipeline/run.py` 写文件用 `tempfile + shutil.move` 原子替换，重复运行：
- 不会产生半截 JSON
- JSON 内每条记录带稳定 `id`（`session#uid#role#ts#line`），便于 D5 流转时关联
- 配置 / 偏好 / 知识的去重与版本合并逻辑是确定性的，重跑结果一致

## 3. 踩过的坑

| 现象 | 根因 | 处理 |
|---|---|---|
| `non-fast-forward` 拒绝 | 远端 `phase1-basics` 与本地 `main` 历史互不相关 | `git push origin main:phase1-basics --force`（已与用户确认） |
| PowerShell 不支持 `&&` | 旧版语法 | 改用 `;` 分隔，或拆成多条命令 |
| 静默 `fatal: ambiguous argument` | 输出被 stdout buffer 吞掉 | 把 `git xxx 2>&1` 写到 `*.log` 再 `Get-Content` 看 |
| 强推后 `phase1-basics` 看起来"空" | 实际是和 main 同步，提交日志只显示一次 | 用 `git log --oneline origin/phase1-basics -3` 二次确认 |

## 4. 后续（D5 / D6）

- D5 流转：直接消费 `phase2-consolidate/*.json`，按 `id` / `pref_id` 关联；冲突项以 `report.md` 第 6 节为种子清单
- D6 评测：`report.md` 的停顿词统计、needs_review 计数、conflict 计数都可以直接喂评测脚本
- 提交建议：每个阶段的 `pipeline/run.py` + 6 个产物文件一起 commit；`clean.log` 不大（KB 级），建议每次都提交以便回溯
