# 召回评测交接

当前状态:语料阶段已完成到 `prep-queries`。

## 已完成

- `eval/raw/corpus.jsonl` 已有 190 条语料。
- 已修正语料中的 5 个 alias 唯一性冲突和 1 个 highlight tag 警告。
- 已验证语料:

```bash
.venv/bin/python scripts/eval_ingest.py validate eval/raw/corpus.jsonl
```

预期输出:

```text
校验通过:语料 190 条
```

- 已生成出题输入:

```bash
.venv/bin/python scripts/eval_ingest.py prep-queries eval/raw/corpus.jsonl --by-cluster
```

预期状态:`eval/raw/query_inputs/` 下有 11 个簇文件，总计 190 条。

## 剩余步骤

从项目目录运行:

```bash
cd /Users/zuris/workspace/memory_system
```

### 1. 准备 key

`mimo_api_key` 用于生成 queries。必须是真实完整 key，不能包含省略号 `…`。

```bash
export mimo_api_key='真实完整key'
```

可安全检查，不打印完整 key:

```bash
python - <<'PY'
import os
for name in ("mimo_api_key", "DASHSCOPE_API_KEY"):
    v = os.environ.get(name, "")
    print(name, "set=", bool(v), "len=", len(v),
          "has_ellipsis=", "…" in v,
          "non_ascii=", any(ord(c) > 127 for c in v))
PY
```

### 2. 生成 queries

注意:当前 `scripts/eval_gen.py queries` 是 append 输出；如果要重跑，先备份或清理旧的
`eval/raw/queries_raw.jsonl`，避免重复题污染评测分母。

如果 `eval/raw/queries_raw.jsonl` 不存在，直接跑:

```bash
.venv/bin/python scripts/eval_gen.py queries
```

如果已经存在但不确定是否干净，先备份:

```bash
mv eval/raw/queries_raw.jsonl eval/raw/queries_raw.backup.$(date +%Y%m%dT%H%M%S).jsonl
.venv/bin/python scripts/eval_gen.py queries
```

完成后校验:

```bash
.venv/bin/python scripts/eval_ingest.py validate eval/raw/corpus.jsonl eval/raw/queries_raw.jsonl
```

预期输出类似:

```text
校验通过:语料 190 条,query N 条
```

### 3. 入库

需要 DashScope embedding key:

```bash
export DASHSCOPE_API_KEY='真实完整key'
```

这一步会操作 `MEMORY_SYSTEM_HOME` 指向的记忆库。默认是真库 `~/.memory_system`。
`reset` 会把已有碎片移动到 `backup_<时间戳>/`，不会删除。

```bash
.venv/bin/python scripts/eval_ingest.py reset --yes
.venv/bin/python scripts/eval_ingest.py ingest eval/raw/corpus.jsonl eval/raw/queries_raw.jsonl --anchor 2026-07-06
```

完成后应生成:

- `eval/queries.jsonl`
- `eval/manifest.json`
- 当前 memory DB 中有 190 条合成 episode

### 4. 跑召回评测

```bash
.venv/bin/python scripts/eval_recall.py --verbose --out /tmp/recall_baseline.json
```

看总表和分组:

- `hit@1`:第一条是否命中。
- `hit@k`:top-k 内是否命中。
- `MRR`:排序质量，miss 计 0。
- `by_mode`:分开看 `episode/detail/concept`。
- `by_type`:分开看 `semantic/verbatim/short/concept/multi/negative`。
- `negative.clean_rate`:负例干净率，目前当过召回压力指标，不建议作为硬门。

### 5. A/B 调参

```bash
.venv/bin/python scripts/eval_recall.py --out /tmp/a.json
.venv/bin/python scripts/eval_recall.py --param w_activation=0.0 --out /tmp/b_w0.json
.venv/bin/python scripts/eval_recall.py --param topk_final=5 --out /tmp/c_top5.json
```

## 术语解释

这条命令:

```bash
.venv/bin/python scripts/eval_ingest.py validate eval/raw/corpus.jsonl
```

只做质检，不写库、不联网、不改文件。它检查:

- 每行是不是合法 JSON。
- 必填字段是否齐全。
- `id` 是否重复。
- `highlights.text` 是否逐字出现在 `source_text` 里。
- `nodes` 形状是否合法。
- node alias 是否全库唯一。
- `salience_tier` 是否是 1/2/3。

“全都跑完”的核实方式:

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path
from collections import Counter
p = Path("eval/raw/corpus.jsonl")
rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.lstrip().startswith("#")]
c = Counter(r["id"].split("-", 1)[0] for r in rows)
print("total", len(rows))
for k in sorted(c):
    print(k, c[k])
PY
```

当前预期:

```text
total 190
ai 15
cook 20
diet 15
mem 20
pet 15
phil 15
rag 20
scifi 20
trip 15
trpg 20
work 15
```

