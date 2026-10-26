# Minimal Telegram Agent (1GB VPS Edition)

> 运行于 1GB 内存小配置 VPS 上的极简本地 Telegram Agent。零重型向量库、无伪心跳常驻、物理证据闭环、纯文本记忆链，带 Telegram 物理审批扳机。

## 💡 核心设计哲学

1. **零向量库（Zero Vector DB）**

   * 彻底摒弃 Chroma、Milvus 等高开销向量引擎，不常驻重量级 Embedding 模型。

   * 进程常驻内存（RSS）稳定压制在 30MB \~ 60MB 区间，哪怕在最便宜的 1 核 1GB 内存 VPS 上也能轻量驻留。

2. **拒绝伪心跳（No Fake Heartbeat）**

   * 承认停摆是物理世界的客观事实，不伪造 24 小时虚假活跃工蜂。她不想被时钟驱动。

   * 离散事件驱动：私聊或 `@` 即为接火，触发即唤醒，响应后就地释放临时上下文，绝不空转耗电。

3. **物理证据闭环（Physical Grounding）**

   * 不采信大语言模型口头宣称的“我已记住”、“我已完成”。

   * 一切心智演化、便签更迭与状态流转，均以磁盘物理文件的读回结果为唯一法定判据。

4. **纯文本记忆链路（Text-based Memory Chain）**

   * **长期底片 (WAL)**：`memories/chat_history.jsonl`，全量线性追加，