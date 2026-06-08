"""pipeline/run.py - D4 多源数据清洗 pipeline（单脚本入口）。

对应 `phase2-consolidate/tasks.md` 的任务 1 与任务 3：
  - 读取 raw/d4/ 五个文件（chat / config / knowledge / preference / tool）
  - 输出 4 类 JSON（chats / preferences / knowledge / tool_results）
    + 1 个 config 快照（供 D5 流转溯源）
    + report.md（含停顿词统计 + needs_review / typos / forget / conflict 四张表）
    + clean.log（结构化日志，复用 D2 的字段校验风格）
  - 选做 +20：
      1) 敏感信息脱敏（phone / email → [REDACTED]）
      2) 偏好版本冲突合并（按 (uid, pref_key) 取最高 version，并标注 vs config-default）
  - 幂等：覆盖式写入 .tmp 再 rename；JSON 内每个对象带稳定 id。

输入：raw/d4/{chat_logs_raw.jsonl, config_manual.yaml, knowledge_raw.txt, preferences_raw.csv, tool_result_raw.jsonl}
输出（均在本脚本所在目录的上一级，即 phase2-consolidate/ 下）：
  - chats.json
  - preferences.json
  - knowledge.json
  - tool_results.json
  - config_snapshot.json
  - report.md
  - clean.log
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# ---- 路径：脚本在 phase2-consolidate/pipeline/run.py ----
PIPELINE_DIR = Path(__file__).resolve().parent
PHASE_DIR = PIPELINE_DIR.parent
WORKSPACE = PHASE_DIR.parent
RAW_DIR = WORKSPACE / "raw" / "d4"

OUT_CHATS = PHASE_DIR / "chats.json"
OUT_PREFERENCES = PHASE_DIR / "preferences.json"
OUT_KNOWLEDGE = PHASE_DIR / "knowledge.json"
OUT_TOOL_RESULTS = PHASE_DIR / "tool_results.json"
OUT_CONFIG = PHASE_DIR / "config_snapshot.json"
OUT_REPORT = PHASE_DIR / "report.md"
OUT_LOG = PHASE_DIR / "clean.log"

# ============================================================
# 配置：停用词、错别字、敏感信息、停顿词等
# ============================================================

# 停顿/口语填充词（句首、句中）
_FILLERS_LEAD = ("嗯", "啊", "呃", "哎", "唉", "嘿", "哈", "额",
                 "那个", "这个", "就是", "话说", "反正", "其实")
_FILLERS_MID = ("你懂的", "话说", "反正", "其实", "然后")
_FILLERS_TRAIL = ("吧", "啦", "呢", "哦", "哈", "嘿", "唉", "哎", "嘛")

# 仅在 D2/D3 用过的简单 typo 词典（接 D4）
_TYPO_MAP = {
    "麒麟系統": "麒麟系统",
    "奇麟": "麒麟",
    "其麟": "麒麟",
    "设制": "设置",
    "导人": "导入",
    "偏好计忆": "偏好记忆",
    "知只库": "知识库",
    "会义纪要": "会议纪要",
    "祥细": "详细",
}

# 敏感信息（直接写死在脚本里，config_manual.yaml 里的会再覆盖）
_DEFAULT_SENSITIVE_PATTERNS = {
    "phone": r"1[3-9]\d{9}",
    "email": r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
}

# 遗忘指令关键词（来自 config_manual.yaml.forget_commands + 兜底）
_DEFAULT_FORGET_WORDS = ("别记下来", "不要保存", "忘记这个", "不要记", "别保存")

# 合法角色
_VALID_ROLES = {"user", "assistant", "tool", "system"}

# 合法 tool status
_VALID_TOOL_STATUS = {"ok", "success", "fail", "error", "timeout"}


# ============================================================
# 工具函数
# ============================================================

def parse_time(raw: str) -> str:
    """多格式时间归一化到 ISO 8601（保留时区若有）。失败原样返回。"""
    if not raw:
        return ""
    s = str(raw).strip()
    fmts = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y年%m月%d日 %H:%M",
        "%Y年%m月%d日 %H:%M:%S",
    )
    for f in fmts:
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return s


_PUNCT = re.compile(r"([!?,.。，！？?…])\1+")
_WS = re.compile(r"[\s\u3000]+")
_HTML = re.compile(r"<!--.*?-->", re.DOTALL)
_MD_BOLD = re.compile(r"\*{1,3}([^*]+?)\*{1,3}")
_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]+")
_AT = re.compile(r"\s*@@@\s*$")
_BRACKET_NOISE = re.compile(r"【([^】]*?)】")


def clean_text(s: str, *, is_user: bool = True, strip_lead_mid_trail: bool = True) -> tuple[str, list[str]]:
    """基础文本清洗。返回 (cleaned, actions)。"""
    if s is None:
        return "", []
    s = str(s)
    actions: list[str] = []

    s, n = _HTML.subn("", s)
    if n:
        actions.append(f"strip_html×{n}")
    s, n = _MD_BOLD.subn(r"\1", s)
    if n:
        actions.append(f"strip_markdown×{n}")
    s, n = _WS.subn(" ", s)
    if n:
        actions.append(f"normalize_ws×{n}")
    s, n = _PUNCT.subn(r"\1", s)
    if n:
        actions.append(f"collapse_punct×{n}")
    s = _AT.sub("", s).strip()

    # 错别字
    for k, v in _TYPO_MAP.items():
        if k in s and k != v:
            s = s.replace(k, v)
            actions.append(f"fix_typo:{k}→{v}")

    if is_user and strip_lead_mid_trail:
        # 折叠重复填充词（嗯嗯、那个那个、然后然后…）
        for w in _FILLERS_LEAD + ("然后",):
            s, n = re.subn(rf"({re.escape(w)})(?:\s*\1){{1,}}", r"\1", s)
            if n:
                actions.append(f"collapse_word:{w}×{n}")
        # 去开头停顿词
        for w in _FILLERS_LEAD:
            pat = re.compile(rf"^(?:…|[，,。.!！?？\s])*(?:{re.escape(w)})+(?:[，,。.!！?？\s]*)")
            s2, n = pat.subn("", s, count=1)
            if n:
                actions.append(f"strip_lead_filler:{w}")
                s = s2
        # 去末尾语气词
        for w in _FILLERS_TRAIL:
            pat = re.compile(rf"[，,。.!！?？…\s]*{re.escape(w)}\s*$")
            s2, n = pat.subn("", s)
            if n:
                actions.append(f"strip_trail_mood:{w}")
                s = s2
        # 中段
        for w in _FILLERS_MID:
            if w in s:
                s = s.replace(w, "")
                actions.append(f"strip_mid_filler:{w}")

    s, n = _EMOJI.subn("", s)
    if n:
        actions.append(f"strip_emoji×{n}")

    return s.strip(" ，,。.!！?？…"), actions


def detect_fillers(s: str) -> list[str]:
    """在原文里找停顿/口语填充词（不消费，仅统计）。"""
    found = []
    s = str(s or "")
    for w in _FILLERS_LEAD + _FILLERS_TRAIL + _FILLERS_MID + ("然后",):
        cnt = len(re.findall(re.escape(w), s))
        if cnt:
            found.append(f"{w}×{cnt}")
    return found


def detect_typos(s: str) -> list[str]:
    s = str(s or "")
    return [f"{k}→{v}" for k, v in _TYPO_MAP.items() if k in s]


def redact_pii(s: str, patterns: dict[str, str]) -> tuple[str, list[str]]:
    """敏感信息脱敏。返回 (redacted, hits)。"""
    if not s:
        return s, []
    hits = []
    out = s
    for label, pat in patterns.items():
        out2, n = re.subn(pat, f"[REDACTED:{label}]", out)
        if n:
            hits.append(f"{label}×{n}")
            out = out2
    return out, hits


def detect_forget_intent(s: str, forget_words: Iterable[str]) -> str | None:
    s = str(s or "")
    for w in forget_words:
        if w in s:
            return w
    return None


def detect_temporary_intent(s: str) -> str | None:
    """检测临时指令：'这次例外' / '今天这次' / '就这一次' / '暂时' 等。"""
    s = str(s or "")
    for k in ("这次例外", "今天这次", "就这一次", "就这次", "暂时", "本次"):
        if k in s:
            return k
    return None


def detect_negation_correction(prev: str, cur: str) -> tuple[str, str] | None:
    """检测 '不对不对！！要详细版' / '不是 X 是 Y' / '说错了' 这类用户纠正。

    返回 (negated, chosen)：用户从哪个旧值换到了哪个新值。
    """
    if not prev or not cur:
        return None
    cur = str(cur)
    prev = str(prev)

    is_correction = any(k in cur for k in ("不对", "不是", "说错了", "搞错了", "错了"))
    if not is_correction:
        return None

    # 1) "不是 X 是 Y" 模式
    m = re.search(r"不是\s*(\S+?)\s*[，,。.是]+\s*(?:是|用)\s*(\S+)", cur)
    if m:
        return m.group(1), m.group(2)
    # 2) "要 X" → chosen；"不要 Y" → negated
    chosen = None
    negated = None
    m_want = re.search(r"要\s*([^\s，,。.！!？?@]+)", cur)
    if m_want:
        chosen = m_want.group(1)
    m_dont = re.search(r"不要\s*([^\s，,。.！!？?@]+)", cur)
    if m_dont:
        negated = m_dont.group(1)
    if chosen or negated:
        return (negated or "(未识别)"), (chosen or "(未识别)")
    # 3) 兜底：用整句标记
    return ("(整句覆盖)"), cur.strip()


def validate_required(obj: dict, fields: list[str]) -> list[str]:
    return [f for f in fields if not obj.get(f)]


def atomic_write(path: Path, content: str | bytes, *, encoding: str = "utf-8") -> None:
    """原子写：先写 .tmp，再 rename，避免半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode(encoding)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with open(fd, "wb") as f:
            f.write(content)
        shutil.move(tmp, path)
    except Exception:
        if Path(tmp).exists():
            Path(tmp).unlink()
        raise


def write_jsonl(path: Path, rows: list[dict]) -> None:
    content = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    atomic_write(path, content)


def write_json(path: Path, obj: Any) -> None:
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2))


# ============================================================
# 极简 YAML 解析（够用即可，不依赖第三方）
# ============================================================

def parse_simple_yaml(text: str) -> dict:
    """支持两层缩进的 YAML 解析（config_manual.yaml 只有 2 层）。"""
    out: dict = {}
    stack: list[tuple[int, dict]] = [(-1, out)]
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.split("#", 1)[0].strip()  # 去掉行内注释
        if val == "":
            new = {}
            parent[key] = new
            stack.append((indent, new))
        else:
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            parent[key] = val
    return out


# ============================================================
# Pipeline 各通道
# ============================================================

def load_config(path: Path) -> tuple[dict, list[str]]:
    """读取 config_manual.yaml；返回 (config, log_events)。"""
    log: list[str] = []
    if not path.exists():
        log.append(json.dumps({"event": "config_missing", "path": str(path)}))
        return {"agent": {}, "preferences_defaults": {}, "security": {}}, log
    try:
        text = path.read_text(encoding="utf-8")
        cfg = parse_simple_yaml(text)
    except Exception as e:
        log.append(json.dumps({"event": "config_parse_failed", "error": str(e)}))
        cfg = {}
    log.append(json.dumps({"event": "config_loaded", "keys": list(cfg.keys())}))
    return cfg, log


def build_sensitive_patterns(cfg: dict) -> dict[str, str]:
    """从 config 合并敏感正则；找不到就回退到默认。"""
    pats = dict(_DEFAULT_SENSITIVE_PATTERNS)
    for entry in (cfg.get("security", {}) or {}).get("sensitive_patterns", []) or []:
        if isinstance(entry, str) and ":" in entry:
            label, _, pat = entry.partition(":")
            pats[label.strip()] = pat.strip()
    return pats


def build_forget_words(cfg: dict) -> tuple[str, ...]:
    extra = (cfg.get("security", {}) or {}).get("forget_commands", []) or []
    return tuple(_DEFAULT_FORGET_WORDS) + tuple(extra)


def process_chats(path: Path, *, sensitive: dict, forget_words: tuple, log: list[str]) -> list[dict]:
    """清洗 chat_logs_raw.jsonl。返回标准化记录列表。"""
    out: list[dict] = []
    pause_counter: Counter = Counter()
    typo_counter: Counter = Counter()
    seen_keys: set = set()
    duplicate_dropped: list[dict] = []
    invalid: list[dict] = []

    for ln, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            log.append(json.dumps({"event": "chat_parse_error", "line": ln, "error": str(e)}))
            continue

        text_raw = row.get("text", "") or ""
        is_user = row.get("role") == "user"
        text, actions = clean_text(text_raw, is_user=is_user)

        # 统计
        for hit in detect_fillers(text_raw):
            tag, _, cnt = hit.partition("×")
            pause_counter[tag] += int(cnt)
        for hit in detect_typos(text_raw):
            typo_counter[hit] += 1

        # 敏感 + 遗忘 + 临时
        pii_hits: list[str] = []
        forget_hit = None
        temp_hit = None
        if is_user:
            text, pii_hits = redact_pii(text, sensitive)
            forget_hit = detect_forget_intent(text_raw, forget_words)
            temp_hit = detect_temporary_intent(text_raw)
            if forget_hit:
                actions.append(f"forget_intent:{forget_hit}")
            if temp_hit:
                actions.append(f"temporary:{temp_hit}")
        if pii_hits:
            actions.extend(f"redact:{h}" for h in pii_hits)

        ts = parse_time(row.get("ts", ""))
        rec = {
            "id": f"{row.get('session','?')}#{row.get('uid','?')}#{row.get('role','?')}#{ts}#{ln}",
            "session": row.get("session"),
            "uid": row.get("uid"),
            "role": row.get("role"),
            "text": text,
            "text_raw": text_raw,
            "ts": ts,
            "needs_review": [],
            "actions": actions,
        }
        # 字段校验
        miss = validate_required(rec, ["session", "uid", "role", "text"])
        if miss:
            invalid.append({"line": ln, "id": rec["id"], "missing": miss, "text": text_raw})
            log.append(json.dumps({"event": "chat_validation_failed", "id": rec["id"], "missing": miss}))
            rec["needs_review"].append("missing_required_fields")
        if rec["role"] not in _VALID_ROLES:
            rec["needs_review"].append(f"unknown_role:{rec['role']}")
            log.append(json.dumps({"event": "chat_bad_role", "id": rec["id"], "role": rec["role"]}))
        if forget_hit:
            rec["needs_review"].append(f"forget_intent:{forget_hit}")
        if temp_hit:
            rec["needs_review"].append(f"temporary:{temp_hit}")
        if not ts:
            rec["needs_review"].append("unparsed_ts")

        # 去重
        dedup_key = (rec["session"], rec["uid"], rec["role"], rec["text"], rec["ts"])
        if dedup_key in seen_keys:
            duplicate_dropped.append({"id": rec["id"], "text": rec["text"]})
            log.append(json.dumps({"event": "chat_dedup_dropped", "id": rec["id"]}))
            continue
        seen_keys.add(dedup_key)

        # 去掉内部字段再写入
        public = {k: v for k, v in rec.items() if k not in ("text_raw",)}
        out.append(public)

    # 用户纠正（基于原文）
    corrections: list[dict] = []
    by_session: dict = {}
    for r in out:
        if r["role"] == "user":
            by_session.setdefault((r["session"], r["uid"]), []).append(r)
    for key, items in by_session.items():
        items.sort(key=lambda x: x["ts"] or "9999")
        for i in range(1, len(items)):
            prev, cur = items[i - 1], items[i]
            hit = detect_negation_correction(prev["text"], cur["text"])
            if hit:
                corrections.append({
                    "session": key[0], "uid": key[1],
                    "from": prev["text"], "to": cur["text"],
                    "negated": hit[0], "chosen": hit[1],
                })
                log.append(json.dumps({"event": "user_correction", "negated": hit[0], "chosen": hit[1], "uid": key[1]}))

    log.append(json.dumps({
        "event": "chat_summary",
        "rows": len(out),
        "invalid": len(invalid),
        "dup_dropped": len(duplicate_dropped),
        "corrections": len(corrections),
    }))

    # 把纠正和停顿/错别字统计挂在模块级，方便 reporter 用
    process_chats._pause_counter = pause_counter
    process_chats._typo_counter = typo_counter
    process_chats._invalid = invalid
    process_chats._duplicate_dropped = duplicate_dropped
    process_chats._corrections = corrections
    return out


def process_preferences(path: Path, *, cfg: dict, log: list[str]) -> list[dict]:
    """清洗 preferences_raw.csv：错别字、字段校验、按 (uid, pref_key) 取最高 version 合并冲突。"""
    out: list[dict] = []
    invalid: list[dict] = []
    typo_counter: Counter = Counter()
    raw_rows: list[dict] = []

    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for ln, row in enumerate(reader, 2):  # 表头是第 1 行
            miss = validate_required(row, ["pref_id", "uid", "pref_key", "pref_value"])
            # 错别字
            for k, v in _TYPO_MAP.items():
                if k in row.get("pref_value", "") and k != v:
                    typo_counter[f"{k}→{v}"] += 1
                    row["pref_value"] = row["pref_value"].replace(k, v)
                if k in row.get("note", "") and k != v:
                    row["note"] = row["note"].replace(k, v)
            # 版本号归一（无论是否字段缺失都做，便于诊断）
            v_raw = str(row.get("version", "")).strip()
            m = re.match(r"^v?(\d+)$", v_raw)
            version = int(m.group(1)) if m else 0
            # 状态：纠正 / 临时 / 显式
            note = row.get("note", "")
            if "纠正" in note:
                state = "corrected"
            elif detect_temporary_intent(note) or "本次" in note or "例外" in note:
                state = "temporary"
            else:
                state = "explicit"
            rec = {
                "pref_id": row["pref_id"],
                "uid": row["uid"],
                "pref_key": row["pref_key"],
                "pref_value": row["pref_value"],
                "version": version,
                "note": note,
                "state": state,
                "needs_review": [],
            }
            if miss:
                invalid.append({"pref_id": row.get("pref_id"), "missing": miss})
                log.append(json.dumps({"event": "pref_validation_failed", "row": ln, "missing": miss}))
                rec["needs_review"].append("missing_required_fields")
            if not row.get("uid"):
                rec["needs_review"].append("uid_missing")
            if state == "temporary":
                rec["needs_review"].append("temporary_instruction")
            raw_rows.append(rec)

    # 冲突合并：按 (uid, pref_key) 取最大 version
    # 临时指令（state="temporary"）不参与"取最大 version"，避免覆盖长期偏好
    latest: dict = {}
    for r in raw_rows:
        key = (r["uid"], r["pref_key"])
        if r["state"] == "temporary":
            # 临时指令不写入 latest，但保留在 raw_rows 列表中供报告引用
            continue
        if key not in latest or r["version"] > latest[key]["version"]:
            latest[key] = r
    conflicts: list[dict] = []
    for r in latest.values():
        default = (cfg.get("preferences_defaults", {}) or {}).get(r["pref_key"])
        if default and default != r["pref_value"]:
            r["needs_review"].append("conflict_with_default")
            conflicts.append({
                "uid": r["uid"], "pref_key": r["pref_key"],
                "default": default, "user": r["pref_value"],
                "version": r["version"], "state": r["state"],
            })
        out.append(r)

    log.append(json.dumps({
        "event": "pref_summary",
        "raw": len(raw_rows),
        "merged": len(out),
        "invalid": len(invalid),
        "conflicts": len(conflicts),
    }))
    process_preferences._invalid = invalid
    process_preferences._conflicts = conflicts
    process_preferences._typo_counter = typo_counter
    return out


def process_knowledge(path: Path, *, log: list[str]) -> list[dict]:
    """把 knowledge_raw.txt 切成结构化案例。"""
    text = path.read_text(encoding="utf-8-sig")
    cases: list[dict] = []
    invalid: list[dict] = []
    typo_counter: Counter = Counter()

    # 状态机式逐行解析：更稳，能正确处理 '=== 垃圾行 ===' 嵌入到 '案例' 之间的情形
    current: dict | None = None
    in_garbage = False
    for line in text.splitlines():
        s = line.strip()
        if s == "=== 案例开始 ===":
            current = {"title": "", "tags": [], "steps": [], "notes": [], "is_garbage": False}
            in_garbage = False
            continue
        if s == "=== 案例结束 ===":
            if current is not None:
                # 跳过空 / 完全 garbage 的块
                if not current["is_garbage"] and (current["title"] or current["tags"]
                                                   or current["steps"] or current["notes"]):
                    # 错别字检测
                    for hit in detect_typos(current["title"]):
                        typo_counter[hit] += 1
                        current["needs_review"] = current.get("needs_review", []) + [f"typo:{hit}"]
                    for hit in detect_typos(" ".join(current["steps"] + current["notes"])):
                        typo_counter[hit] += 1
                    if typo_counter:
                        current.setdefault("needs_review", []).extend(
                            f"typo:{h}" for h in detect_typos(" ".join(current["steps"] + current["notes"]))
                        )
                    # 修 title 错别字
                    for k, v in _TYPO_MAP.items():
                        if k in current["title"]:
                            current["title"] = current["title"].replace(k, v)
                    cases.append(_finalize_knowledge_case(cases, current))
                elif current["is_garbage"] or (not current["title"] and not current["tags"]
                                                and not current["steps"] and not current["notes"]):
                    invalid.append({"reason": "empty_or_garbage",
                                    "title": current["title"] or "(无标题)"})
                    log.append(json.dumps({"event": "knowledge_empty_or_garbage",
                                            "title": current["title"] or "(无标题)"}))
            current = None
            in_garbage = False
            continue
        if s == "=== 垃圾行 ===":
            if current is not None:
                current["is_garbage"] = True
            in_garbage = True
            log.append(json.dumps({"event": "knowledge_noise", "snippet": s[:40]}))
            continue
        if current is None:
            continue

        # 解析单行
        if s.startswith("标题："):
            current["title"] = s[3:].strip()
        elif s.startswith("标签："):
            current["tags"] = [t.strip() for t in s[3:].split() if t.strip()]
        elif s.startswith("步骤") or s.startswith("步骤（"):
            pass  # 进入 steps 段（识别靠行首数字）
        elif s.startswith(("原则", "说明", "要求", "示例输出", "适用", "注意", "常见坑")):
            cleaned, _ = clean_text(s, is_user=False, strip_lead_mid_trail=False)
            if cleaned:
                current["notes"].append(cleaned)
        else:
            if re.match(r"^\d+[.、]", s):
                cleaned, _ = clean_text(s, is_user=False, strip_lead_mid_trail=False)
                current["steps"].append(cleaned)
            else:
                cleaned, _ = clean_text(s, is_user=False, strip_lead_mid_trail=False)
                if cleaned:
                    current["notes"].append(cleaned)

    log.append(json.dumps({
        "event": "knowledge_summary",
        "parsed": len(cases),
        "invalid": len(invalid),
    }))
    process_knowledge._invalid = invalid
    process_knowledge._typo_counter = typo_counter
    return cases


def _finalize_knowledge_case(existing: list[dict], current: dict) -> dict:
    """生成结构化的 case 记录。"""
    idx = len(existing) + 1
    rec = {
        "id": f"K{idx:03d}",
        "title": current["title"] or "(无标题)",
        "tags": current["tags"],
        "steps": current["steps"],
        "notes": current["notes"],
        "needs_review": list(dict.fromkeys(current.get("needs_review", []))),  # 去重保序
    }
    if not current["title"]:
        rec["needs_review"].append("missing_title")
    if not current["tags"]:
        rec["needs_review"].append("missing_tags")
    return rec


def process_tool_results(path: Path, *, sensitive: dict, log: list[str]) -> list[dict]:
    """清洗 tool_result_raw.jsonl。"""
    out: list[dict] = []
    seen: set = set()
    invalid: list[dict] = []
    typo_counter: Counter = Counter()
    pause_counter: Counter = Counter()
    anomaly: list[dict] = []

    for ln, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            log.append(json.dumps({"event": "tool_parse_error", "line": ln, "error": str(e)}))
            continue

        raw = row.get("raw_output", "") or ""
        cleaned, actions = clean_text(raw, is_user=False, strip_lead_mid_trail=False)
        # 工具结果也做 PII 脱敏（邮箱、手机号）
        cleaned, pii_hits = redact_pii(cleaned, sensitive)
        if pii_hits:
            actions.extend(f"redact:{h}" for h in pii_hits)
        for hit in detect_fillers(raw):
            tag, _, cnt = hit.partition("×")
            pause_counter[tag] += int(cnt)
        for hit in detect_typos(raw):
            typo_counter[hit] += 1

        status = str(row.get("status", "")).lower()
        exec_ms_raw = row.get("exec_ms")
        try:
            exec_ms = int(exec_ms_raw) if exec_ms_raw is not None else None
        except (TypeError, ValueError):
            exec_ms = None
            actions.append(f"bad_exec_ms:{exec_ms_raw!r}")

        rec = {
            "id": row.get("trace") or f"tool#{ln}",
            "tool": row.get("tool"),
            "status": status,
            "output": cleaned,
            "output_raw": raw,
            "exec_ms": exec_ms,
            "actions": actions,
            "needs_review": [],
        }
        miss = validate_required(rec, ["id", "tool", "status", "output"])
        if miss:
            invalid.append({"id": rec["id"], "missing": miss})
            rec["needs_review"].append("missing_required_fields")
            log.append(json.dumps({"event": "tool_validation_failed", "id": rec["id"], "missing": miss}))
        if status not in _VALID_TOOL_STATUS:
            rec["needs_review"].append(f"unknown_status:{status}")
        if exec_ms is None:
            rec["needs_review"].append("unparsed_exec_ms")
        flags = []
        if status in ("error", "fail"):
            flags.append("tool_failed")
        if re.search(r"\?{2,}|…+", raw):
            flags.append("uncertain_path_or_text")
        if re.search(r"呃+|嗯+|啊+|那个|retry\?", raw):
            flags.append("casual_in_output")
        if flags:
            rec["needs_review"].append("flags=" + ",".join(flags))
            anomaly.append({"id": rec["id"], "tool": rec["tool"], "flags": flags, "text": cleaned})

        # 重复记录直接跳过
        key = (rec["id"], rec["tool"], rec["output"])
        if key in seen:
            log.append(json.dumps({"event": "tool_dedup_dropped", "id": rec["id"]}))
            # 同样去重 anomaly 里的同 id 记录
            anomaly[:] = [a for a in anomaly if a["id"] != rec["id"]]
            continue
        seen.add(key)

        out.append({k: v for k, v in rec.items() if k != "output_raw"})

    log.append(json.dumps({
        "event": "tool_summary",
        "rows": len(out),
        "invalid": len(invalid),
        "anomaly": len(anomaly),
    }))
    process_tool_results._invalid = invalid
    process_tool_results._anomaly = anomaly
    process_tool_results._typo_counter = typo_counter
    process_tool_results._pause_counter = pause_counter
    return out


# ============================================================
# 报告
# ============================================================

def write_report(path: Path, *, chats, prefs, knowledge, tools, cfg,
                 chat_pause, pref_typo, knowledge_typo, tool_pause, tool_typo,
                 chat_invalid, pref_invalid, knowledge_invalid, tool_invalid,
                 chat_dups, pref_conflicts, chat_corrections, tool_anomaly) -> None:
    L: list[str] = ["# D4 多源清洗报告", ""]

    L.append("## 1. 概览")
    L.append(f"- chat 行数：{len(chats)}（原始 {len(chats) + len(chat_invalid) + len(chat_dups)}）")
    L.append(f"- preference 条数：{len(prefs)}（原始 {len(prefs) + len(pref_invalid)}）")
    L.append(f"- knowledge 案例数：{len(knowledge)}（原始 {len(knowledge) + len(knowledge_invalid)}）")
    L.append(f"- tool_result 条数：{len(tools)}（原始 {len(tools) + len(tool_invalid)}）")
    L.append(f"- config 快照：已生成 config_snapshot.json")
    L.append(f"- 字段校验未通过合计：chat={len(chat_invalid)} pref={len(pref_invalid)} "
             f"knowledge={len(knowledge_invalid)} tool={len(tool_invalid)}")
    L.append(f"- 去重丢弃合计：chat={len(chat_dups)}")
    L.append(f"- 用户纠正：{len(chat_corrections)}；偏好冲突（与 config-default）：{len(pref_conflicts)}；"
             f"工具异常：{len(tool_anomaly)}")
    L.append("")

    # 2. 停顿词统计
    L.append("## 2. 停顿词统计（必出）")
    L.append("")
    L.append("### 2.1 合并计数（chat + tool）")
    merged_pause: Counter = Counter()
    merged_pause.update(chat_pause)
    merged_pause.update(tool_pause)
    if merged_pause:
        L.append("| 停顿词 | 命中次数 |")
        L.append("|---|---|")
        for w, c in sorted(merged_pause.items(), key=lambda x: -x[1]):
            L.append(f"| `{w}` | {c} |")
    else:
        L.append("- （无）")
    L.append("")

    # 3. needs_review 清单
    L.append("## 3. needs_review 清单")
    L.append("")
    for idx, (label, items) in enumerate([
        ("chat", [r for r in chats if r.get("needs_review")]),
        ("preference", [r for r in prefs if r.get("needs_review")]),
        ("knowledge", [r for r in knowledge if r.get("needs_review")]),
        ("tool_result", [r for r in tools if r.get("needs_review")]),
    ], start=1):
        L.append(f"### 3.{idx} {label} ({len(items)} 条)")
        if not items:
            L.append("- （无）")
        else:
            for r in items[:50]:
                L.append(f"- `{r.get('id') or r.get('pref_id') or r.get('title')}`：{r['needs_review']}")
            if len(items) > 50:
                L.append(f"- …其余 {len(items) - 50} 条略")
        L.append("")

    # 4. 错别字记录
    L.append("## 4. 错别字记录")
    L.append("")
    merged_typo: Counter = Counter()
    for c in (pref_typo, knowledge_typo, tool_typo):
        merged_typo.update(c)
    if merged_typo:
        L.append("| 错别字 | 命中次数 |")
        L.append("|---|---|")
        for w, c in sorted(merged_typo.items(), key=lambda x: -x[1]):
            L.append(f"| `{w}` | {c} |")
    else:
        L.append("- （无）")
    L.append("")

    # 5. 遗忘/隐私/临时指令
    L.append("## 5. 遗忘 / 隐私 / 临时指令")
    L.append("")
    forgets = [r for r in chats if any("forget_intent" in n for n in r.get("needs_review", []))]
    if forgets:
        L.append("### 5.1 遗忘指令")
        L.append("| session | uid | 原文 | 触发词 |")
        L.append("|---|---|---|---|")
        for r in forgets:
            tag = next((n for n in r["needs_review"] if n.startswith("forget_intent:")), "forget_intent")
            L.append(f"| {r['session']} | {r['uid']} | {r.get('text_raw', r['text'])[:60]}… | {tag.split(':', 1)[1]} |")
    else:
        L.append("### 5.1 遗忘指令")
        L.append("- （无）")
    L.append("")

    temps = [r for r in chats if any("temporary:" in n for n in r.get("needs_review", []))]
    if temps:
        L.append("### 5.2 临时指令（不应覆盖长期偏好）")
        L.append("| session | uid | 原文 | 触发词 |")
        L.append("|---|---|---|---|")
        for r in temps:
            tag = next((n for n in r["needs_review"] if n.startswith("temporary:")), "temporary")
            L.append(f"| {r['session']} | {r['uid']} | {r.get('text_raw', r['text'])[:60]}… | {tag.split(':', 1)[1]} |")
    else:
        L.append("### 5.2 临时指令")
        L.append("- （无）")
    L.append("")

    # 6. 冲突
    L.append("## 6. 冲突清单")
    L.append("")
    L.append("### 6.1 偏好 vs config-default")
    if pref_conflicts:
        L.append("| uid | pref_key | default | user | version | state |")
        L.append("|---|---|---|---|---|---|")
        for c in pref_conflicts:
            L.append(f"| {c['uid']} | {c['pref_key']} | {c['default']} | {c['user']} | v{c['version']} | {c['state']} |")
    else:
        L.append("- （无）")
    L.append("")
    L.append("### 6.2 用户纠正（user correction）")
    if chat_corrections:
        L.append("| session | uid | 改前 | 改后 | 否决词 | 选用词 |")
        L.append("|---|---|---|---|---|---|")
        for c in chat_corrections:
            L.append(f"| {c['session']} | {c['uid']} | {c['from']} | {c['to']} | {c['negated']} | {c['chosen']} |")
    else:
        L.append("- （无）")
    L.append("")
    L.append("### 6.3 工具调用异常")
    if tool_anomaly:
        L.append("| trace | tool | flags | 文本 |")
        L.append("|---|---|---|---|")
        for a in tool_anomaly:
            L.append(f"| {a['id']} | {a['tool']} | {','.join(a['flags'])} | {a['text'][:60]}… |")
    else:
        L.append("- （无）")
    L.append("")

    # 7. 错误分析与建议
    L.append("## 7. 错误分析与建议")
    L.append("- 选做：敏感信息已按 `phone:1[3-9]\\d{9}` 与 `email:.+@.+` 替换为 `[REDACTED:xxx]`；`config_manual.yaml` 中匹配不到的正则会被忽略。")
    L.append("- 选做：偏好按 `(uid, pref_key)` 取 `max(version)` 合并；带 `纠正` 的 note 标为 `corrected` 态。")
    L.append("- 选做：用户表达与 `config_manual.yaml` 默认值冲突时，单列在 6.1；D5 流转时建议把 `corrected` 视为 `default` 的覆盖。")
    L.append("- `config_manual.yaml` 的格式只有两层缩进，所以脚本自带的极简 YAML 解析即可处理；遇到三层/数组嵌套需切换 PyYAML。")
    L.append("- `knowledge_raw.txt` 案例之间用 `=== 案例开始 === / === 案例结束 ===` 分隔；'=== 垃圾行 ===' 被识别为噪声但不删除原文。")
    L.append("- D2/D3 的清洗口径（停顿/口语、装饰/残留、错别字）在 D4 沿用，叠加敏感信息脱敏与版本合并。")

    atomic_write(path, "\n".join(L) + "\n")


# ============================================================
# 主流程
# ============================================================

def main() -> int:
    if not RAW_DIR.is_dir():
        print(f"input dir not found: {RAW_DIR}", file=sys.stderr)
        return 1

    log: list[str] = []
    run_ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    log.append(json.dumps({"event": "pipeline_start", "ts": run_ts, "raw_dir": str(RAW_DIR)}))

    cfg, cfg_log = load_config(RAW_DIR / "config_manual.yaml")
    log.extend(cfg_log)
    sensitive = build_sensitive_patterns(cfg)
    forget_words = build_forget_words(cfg)
    log.append(json.dumps({"event": "sensitive_patterns", "patterns": list(sensitive.keys())}))
    log.append(json.dumps({"event": "forget_words", "words": list(forget_words)}))

    # 4 个数据通道
    chats = process_chats(RAW_DIR / "chat_logs_raw.jsonl",
                          sensitive=sensitive, forget_words=forget_words, log=log)
    prefs = process_preferences(RAW_DIR / "preferences_raw.csv", cfg=cfg, log=log)
    knowledge = process_knowledge(RAW_DIR / "knowledge_raw.txt", log=log)
    tools = process_tool_results(RAW_DIR / "tool_result_raw.jsonl", sensitive=sensitive, log=log)

    # config 快照（不参与主流程，但供 D5 流转溯源）
    write_json(OUT_CONFIG, {
        "snapshot_ts": run_ts,
        "source": str(RAW_DIR / "config_manual.yaml"),
        "config": cfg,
        "derived": {
            "sensitive_patterns": sensitive,
            "forget_words": list(forget_words),
        },
    })

    # 4 类主输出
    write_json(OUT_CHATS, chats)
    write_json(OUT_PREFERENCES, prefs)
    write_json(OUT_KNOWLEDGE, knowledge)
    write_json(OUT_TOOL_RESULTS, tools)

    # report
    write_report(
        OUT_REPORT,
        chats=chats, prefs=prefs, knowledge=knowledge, tools=tools, cfg=cfg,
        chat_pause=process_chats._pause_counter,
        pref_typo=process_preferences._typo_counter,
        knowledge_typo=Counter(),  # knowledge 的 typo 挂在 needs_review 里，不重复统计
        tool_pause=process_tool_results._pause_counter,
        tool_typo=process_tool_results._typo_counter,
        chat_invalid=process_chats._invalid,
        pref_invalid=process_preferences._invalid,
        knowledge_invalid=process_knowledge._invalid,
        tool_invalid=process_tool_results._invalid,
        chat_dups=process_chats._duplicate_dropped,
        pref_conflicts=process_preferences._conflicts,
        chat_corrections=process_chats._corrections,
        tool_anomaly=process_tool_results._anomaly,
    )

    log.append(json.dumps({
        "event": "pipeline_end", "ts": run_ts,
        "outputs": {
            "chats": str(OUT_CHATS.relative_to(PHASE_DIR)),
            "preferences": str(OUT_PREFERENCES.relative_to(PHASE_DIR)),
            "knowledge": str(OUT_KNOWLEDGE.relative_to(PHASE_DIR)),
            "tool_results": str(OUT_TOOL_RESULTS.relative_to(PHASE_DIR)),
            "config_snapshot": str(OUT_CONFIG.relative_to(PHASE_DIR)),
            "report": str(OUT_REPORT.relative_to(PHASE_DIR)),
        },
    }))

    # 写 clean.log
    atomic_write(OUT_LOG, "\n".join(log) + "\n")

    print("D4 pipeline done.")
    for p in (OUT_CHATS, OUT_PREFERENCES, OUT_KNOWLEDGE, OUT_TOOL_RESULTS, OUT_CONFIG, OUT_REPORT, OUT_LOG):
        rel = p.relative_to(PHASE_DIR)
        size = p.stat().st_size
        print(f"  {rel}  {size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
