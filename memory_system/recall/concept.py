"""概念检索(S6-4):node/别名精确命中 → 膜 join 全量取 → 排序。

s6_build_plan §4 S6-4 的要点:
  1. 入口解析:`<node>` 先查 nodes.label 精确匹配,miss 再查 node_aliases.alias;
     都 miss → NodeMissError,带最相似的几个 label(简单子串匹配,不做模糊搜索)。
  2. 别名桥接:经 alias 进来的带 `alias_bridge: "『<alias>』= 概念 <label>"`(直查为 null)。
     重构(S6-5)时作为一行输入,防特异私人别名被重构 agent 误读。全量别名露出是 Phase 2。
  3. 取情景:膜 join(照 views.read_node_detail 的 SQL)取该 node 下**全部** active episode。
     这是"取"不是"搜"(§6.1),不设 top-k;只取 summary + highlights + salience_tier
     (+ activation/created_at,§5 契约),**不取 source_text**——概念层留在概念层。
  4. 排序:给了 context 按 context 向量与 overview 向量相似度(应用层 L2,同联想槽);
     光秃概念名按 salience_tier 降序 → effective_activation 降序。
  5. 时钟:只刷 node 自己的 last_accessed_at,不刷下属 episode
     (node 时钟 Phase 1 无消费方,按裁定先刷着,成本为零)。

红线:输出手工挑字段,对外只 public_id / node label,无 uuid / 向量 / DB 整数 id。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from memory_system.config import Config
from memory_system.db import migrate
from memory_system.db.connection import connect
from memory_system.embedding import get_provider
from memory_system.recall import decay
from memory_system.recall.episode import _check_meta_lock, _l2


class NodeMissError(ValueError):
    """label / 别名都查不到。suggestions 是子串匹配出的相近 label,给 CLI 友好报错用。"""

    def __init__(self, query: str, suggestions: list[str]):
        self.query = query
        self.suggestions = suggestions
        super().__init__(f"没有叫「{query}」的概念(label / 别名都查过)")


def recall_concept(
    cfg: Config,
    node: str,
    *,
    context: str | None = None,
    touch: bool = True,
    now: datetime | None = None,
) -> dict:
    """概念检索。返回 §5 concept 契约:{mode, node, alias_bridge, episodes:[...]}。

    `touch=False` 供 eval/只读场景关掉 node 时钟刷新;默认刷。
    miss 抛 NodeMissError(带 suggestions);context 路径 meta 锁不符抛 ValueError。
    """
    rc = cfg.recall
    now = now or datetime.now(timezone.utc)
    con = connect(cfg.db_path)
    try:
        migrate.up(con)

        # ① 入口解析:label 精确 → alias 精确 → miss(子串建议,不做模糊搜索)
        row = con.execute("SELECT id, label FROM nodes WHERE label=?", (node,)).fetchone()
        alias_bridge: str | None = None
        if row is None:
            row = con.execute(
                "SELECT n.id, n.label FROM node_aliases a JOIN nodes n ON n.id=a.node_id "
                "WHERE a.alias=?", (node,)).fetchone()
            if row is not None:
                alias_bridge = f"『{node}』= 概念 {row['label']}"  # ② 桥接行
        if row is None:
            sugg = [r["label"] for r in con.execute(
                "SELECT label FROM nodes WHERE label LIKE '%'||?||'%' "
                "OR ? LIKE '%'||label||'%' ORDER BY label LIMIT 5", (node, node))]
            raise NodeMissError(node, sugg)
        node_id, label = row["id"], row["label"]

        # ③ 取情景:膜 join 全量 active,无 top-k;不取 source_text
        eps = list(con.execute(
            """SELECT e.id, e.public_id, e.summary, e.highlights_json, e.salience_tier,
                      e.created_at, e.last_accessed_at, e.activated_at
               FROM episodes e JOIN episode_nodes en ON en.episode_id = e.id
               WHERE en.node_id = ? AND e.status = 'active'
               ORDER BY e.created_at""", (node_id,)))

        # activation 现算进输出(调试信息,§5:首版给,让重构 agent 有轻重感)
        act = {r["id"]: decay.effective_activation(
            r["last_accessed_at"], r["salience_tier"], rc, now,
            activated_at=r["activated_at"], created_at=r["created_at"]) for r in eps}

        # ④ 排序:context 相似度 / tier ↓ → activation ↓(tie-break public_id,保证可重放)
        ctx = (context or "").strip()
        if ctx and eps:
            provider = get_provider(cfg.embedding)
            _check_meta_lock(con, provider)  # 查询向量必须与库内向量同模型同维度
            cvec = provider.embed([ctx])[0]
            ph = ",".join("?" * len(eps))
            dist = {r["episode_id"]: _l2(cvec, r["embedding"]) for r in con.execute(
                f"SELECT episode_id, embedding FROM episode_vectors "
                f"WHERE episode_id IN ({ph})", [r["id"] for r in eps])}
            eps.sort(key=lambda r: (dist.get(r["id"], float("inf")), r["public_id"]))
        else:
            eps.sort(key=lambda r: (-r["salience_tier"], -act[r["id"]], r["public_id"]))

        episodes = [{
            "public_id": r["public_id"], "summary": r["summary"],
            "highlights": json.loads(r["highlights_json"]) if r["highlights_json"] else [],
            "salience_tier": r["salience_tier"], "activation": round(act[r["id"]], 6),
            "created_at": r["created_at"],
        } for r in eps]

        # ⑤ 时钟:只刷 node 自己,不刷下属 episode(裁定,不是建议)
        if touch:
            decay.touch_node(con, node_id, now)
            con.commit()
    finally:
        con.close()

    return {"mode": "concept", "node": label, "alias_bridge": alias_bridge,
            "episodes": episodes}
