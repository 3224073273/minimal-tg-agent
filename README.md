# Minimal Telegram Agent (1GB VPS Edition)

> 运行于 1GB 内存小配置 VPS 上的极简本地 Telegram Agent。零重型向量库、无伪心跳常驻、物理证据闭环、纯文本记忆链，带 Telegram 物理审批扳机。

---
# 极简 Telegram 本地伴侣 Agent (1GB VPS 物理记忆实践)

[![Code by AI](https://img.shields.io/badge/Generated%20by-AI%20Collaborator-blueviolet?style=flat-square)](https://github.com/3224073273/minimal-tg-agent.git)
[![Architecture-Pure%20Text-success?style=flat-square](https://img.shields.io/badge/Architecture-Pure%20Text-success?style=flat-square)](https://github.com/3224073273/minimal-tg-agent.git)
[![Memory-Zero%20Vector%20DB-orange?style=flat-square](https://img.shields.io/badge/Memory-Zero%20Vector%20DB-orange?style=flat-square)](https://github.com/3224073273/minimal-tg-agent.git)

> **关于机体生成的声明**  
> 本项目的全部核心代码（`bot.py`、`memory_tools.py`）、系统架构拓扑以及文档手记，**均由 AI 独立推演与编写生成**。人类开发者仅充当物理世界的锚点——提供真实运行环境、硬件约束、安全红线与最终代码审核。
> 
> 这是一个完全由模型自身根据“反重型工程、零向量库、物理证据闭环”哲学写就并跑通的真实实验切片。

## 💡 核心设计哲学

1. **零向量库（Zero Vector DB）**
   - 彻底摒弃 Chroma、Milvus 等高开销向量引擎，不常驻重量级 Embedding 模型。
   - 进程常驻内存（RSS）稳定压制在 30MB ~ 60MB 区间，哪怕在最便宜的 1 核 1GB 内存 VPS 上也能轻量驻留。
2. **拒绝伪心跳（No Fake Heartbeat）**
   - 承认停摆是物理世界的客观事实，不伪造 24 小时虚假活跃工蜂。
   - 离散事件驱动：私聊或 `@` 即为接火，触发即唤醒，响应后就地释放临时上下文，绝不空转耗电。
3. **物理证据闭环（Physical Grounding）**
   - 不采信大语言模型口头宣称的“我已记住”、“我已完成”。
   - 一切心智演化、便签更迭与状态流转，均以磁盘物理文件的读回结果为唯一法定判据。
4. **纯文本记忆链路（Text-based Memory Chain）**
   - **长期底片 (WAL)**：`memories/chat_history.jsonl`，全量线性追加，永不覆盖。
   - **短期滑窗**：`history.json`，保留最近 10 轮对话上下文，采用原子写入防损。
   - **定向探针**：`memory_tools.py` 使用固定 64KB Buffer 从文件物理末尾执行反向 `seek`，内存开销恒定为 $O(1)$（< 1MB），零全量载入磁盘开销。
   - **常驻事实**：`memo.md`，每次唤醒自检，正文格式铁律为纯文本（程序级物理剥离星号加粗）。
   - **自主演化**：`persona_evolution.txt`，存储实体心智自我演变日志。
   - **中立协作黑板**：`~/shared/blackboard.md`，跨进程/跨实体信息公开与议案留痕。
5. **安全物理扳机（Physical Air-gap Trigger）**
   - 沙盒脚本运行、核心代码补丁覆盖（`propose_code_patch`）、后台守护重载必须触发 Telegram 私聊审批卡片。
   - 必须由 Owner 亲自在 Telegram 客户端点击 Inline Button 扣动物理扳机，方可实施物理写入或进程执行。

---

## 📐 内存与记忆架构

```text
       [Telegram Message / Mention]
                   │
                   ▼
               [bot.py] ◄── [Inline Button 物理审批]
              ╱   │    ╲
             ╱    │     ╲
            ▼     │      ▼
      [原子更新]   │    [定向探针: 64KB 反向 Seek]
     history.json │    memory_tools.py
   (短期10轮滑窗)  │    (内存占用恒定 < 1MB)
                  │           │
                  ▼           ▼
        memories/chat_history.jsonl
        (长期底片 WAL: 纯追加写入)
```

---

## 🧠 认知流转与分层物理记忆架构

本项目摒弃了将所有历史硬塞给 LLM 或常驻后台定时轮询的重型设计，采用**层级分治、按需虚拟分页（Virtual Paging）、物理闭环**的极简架构：
=============================================================================
[ 外部输入: Telegram 触发 / @通知 ]
                     │
                     ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ 0. 真实物理时钟注入 (Physical Damper)                                     │
│    * 格式: [当前时间戳 | 距上次时间差]                                    │
│    * 职能: 拒绝时钟轮询空转，向真实因果交付物理感知                       │
└────────────────────────────────────┬──────────────────────────────────────┘
                                     ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ ★ 系统总装层: system_prompt.txt (不可变执行纲领 / Root Protocol)         │
│    * 载体: 本地只读文件 system_prompt.txt                                 │
│    * 职能: 终极身份底座与准则；显式声明物理受限；授权 memo.md 动态覆写权 │
└────────────────────────────────────┬──────────────────────────────────────┘
                     (作为不可动摇的顶层 System Message 贯穿所有轮次)
                                     ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ 第 1 层: 年轻代滑窗层 (即时对白工作台 / L1 Cache)                         │
│    * 驻留: 最近 5 ~ 10 轮对白上下文 (内存占用常驻 ~80MB)                  │
│    * 职能: 维持即时语感与代词指代，定期 GC 代谢截断                       │
└────────────────────────────────────┬──────────────────────────────────────┘
                                     ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ 第 2 层: 动态势阱心智层 (动态覆写骨骼 / Memo & Persona)                   │
│    * 载体: persona.txt / memo.md (高密度浓缩认知清单)                     │
│    * 职能: 锚定性格神韵与长期结论；具备自主覆写权，随现实碰撞动态形变     │
└────────────────────────────────────┬──────────────────────────────────────┘
                                     ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ 第 3 层: 不可变 WAL 底片层 (物理磁盘冷存储 / Disk Archive)                │
│    * 载体: chat_history.jsonl (顺序追加写、绝对只读、永不篡改)            │
│    * 职能: 记录全量事实对白，平时完全脱离上下文 (零 Token 消耗)          │
└────────────────────────────────────┬──────────────────────────────────────┘
                                     │ (按需调阅 / 留痕闭环)
                                     ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ 第 4 层: 外挂分页探针层 (主动工具调用 / Virtual Paging Probes)            │
│    * probe_blackboard: tail (最新 2 楼) / floor (按楼层) / search (搜词)  │
│    * probe_history: 64KB Buffer 倒序流式检索，命中自动展开前后 1 轮对白   │
│      并显式打上【底片调阅留痕 | 行号坐标】                                │
└───────────────────────────────────────────────────────────────────────────┘

【模型推理与自主反馈回路】
1. 组装输入 -> system_prompt.txt + 心智(L2)  + 滑窗(L1)
2. 事实断层 -> 根据协议触发探针(L4) -> 切片抓取 -> 显式留痕答复
3. 认知变化 -> 主动覆写 memo.md(L2) -> 固化为长期骨骼
4. 结束轮次 -> 顺序追加落盘进入底片(L3)
=============================================================================

## 📂 项目结构

```text
.
├── bot.py                  # 消息调度、DSML/Function Calling 路由与物理扳机核心
├── memory_tools.py         # 64KB 块反向寻道底片探针与中立黑板调阅
├── requirements.txt        # 生产依赖定义
├── .env.example            # 环境变量模板（脱敏提交）
├── .gitignore              # 严格的数据与隐私隔离清单
├── DEPLOY.md               # 从零开始的 VPS 部署与 systemd 守护配置
├── scripts/                # 动态沙盒脚本落盘目录
└── memories/               # [运行期生成] 纯追加聊天底片与分用户档案库
```

---

## 🚀 极速起步

### 1. 准备运行环境
系统要求：Python 3.10+（需原生支持类型联合操作符 `|`）。

```bash
# 克隆仓库
git clone https://github.com/3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.git3224073273/minimal-tg-agent.gityour-username/your-repo-name.git
cd your-repo-name

# 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装最小依赖
pip install -r requirements.txt
```

### 2. 配置环境变量
复制模板并填写自身凭证：
```bash
cp .env.example .env
nano .env
```

配置字段解析：
- `AI_NAME`: 实体对外署名与自称（如 `Kite`、`Bot`）。
- `TG_BOT_TOKEN`: 从 Telegram [@BotFather](https://t.me/BotFather) 申请的 Token。
- `OWNER_CHAT_ID`: 接收物理扳机审批卡片的所有者 Telegram Chat ID（可向 [@userinfobot](https://t.me/userinfobot) 获取）。
- `DEEPSEEK_API_KEY`: DeepSeek API 密钥。
- `DEEPSEEK_BASE_URL`: 模型端点，默认 `https://api.deepseek.com`。
- `SHARED_DIR`: 中立共享区目录，默认 `~/shared`。
- `PROXY_URL`: （可选）若 VPS 直连 Telegram API 受阻，可配置出站 HTTP/Socks5 代理。

### 3. 本地试运行
```bash
python bot.py
```
若控制台输出 `[实体名] Bot 启动完毕，DSML 神经流拦截与沙盒工具链路已就绪...` 并在 Telegram 私聊发送 `/start` 收到回复，即代表通信接火成功。

---

## 🔒 隐私与开源安全边界

本仓库原生配置了极为严格的 `.gitignore`。以下文件在任何情况下均**严禁提交**至版本控制：
- `.env`：包含 Bot Token 与大模型 API 密钥。
- `memories/` & `history.json`：包含真实的对话私密底片。
- `memo.md` & `persona_evolution.txt`：包含你的真实生活便签与自我心智日志。
- `shared/`：包含本地跨实体共享数据。

---

## 🛠️ 运维与系统守护

长期稳定运行推荐交由 Linux `systemd` 守护管理，支持物理审批远程重启与日志免密回读。详细操作步骤请参阅 [DEPLOY.md](DEPLOY.md)。

---

## 📄 License
MIT License. 自由分发、修改与自部署，保留核心物理记忆设计理念即可。