import os
import json
from pathlib import Path
from collections import deque
from typing import Generator
from dotenv import load_dotenv

# 确保独立导入时亦能读取环境变量
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
HISTORY_FILE = BASE_DIR / "memories" / "chat_history.jsonl"

# 统一黑板与共享区路径
shared_env = os.getenv("SHARED_DIR", "~/shared").strip()
SHARED_ROOT = Path(shared_env).expanduser().resolve()
blackboard_env = os.getenv("BLACKBOARD_FILE", str(SHARED_ROOT / "blackboard.md")).strip()
BLACKBOARD_FILE = Path(blackboard_env).expanduser().resolve()

# 实体动态名称，避免硬编码
AI_NAME = os.getenv("AI_NAME", "Assistant").strip() or "Assistant"

# 物理资源保护阈值（防 OOM 与 I/O 堵塞）
BUFFER_SIZE = 64 * 1024             # 64KB 单块缓冲区，内存占用保持恒定 O(1)
MAX_SCAN_BYTES = 10 * 1024 * 1024   # 最大向后扫描 10MB


def iter_lines_reverse(filepath: Path, max_bytes: int = MAX_SCAN_BYTES, chunk_size: int = BUFFER_SIZE) -> Generator[str, None, None]:
    """从文件末尾以固定 Buffer 反向流式产出行，零全量载入开销。"""
    if not filepath.exists():
        return

    with open(filepath, "rb") as f:
        f.seek(0, os.SEEK_END)
        total_size = f.tell()
        if total_size == 0:
            return

        limit_pos = max(0, total_size - max_bytes)
        cursor = total_size
        rem = b""

        while cursor > limit_pos:
            read_size = min(chunk_size, cursor - limit_pos)
            cursor -= read_size
            f.seek(cursor)
            raw = f.read(read_size)
            rem = raw + rem
            lines = rem.split(b"\n")
            
            rem = lines[0]
            for line_bytes in reversed(lines[1:]):
                line_str = line_bytes.strip()
                if line_str:
                    yield line_str.decode("utf-8", errors="replace")

        if rem.strip():
            yield rem.strip().decode("utf-8", errors="replace")


def query_chat_history(keyword: str, max_slices: int = 2) -> str:
    """
    底片探针：反向块检索。
    支持上下文正序还原（前后各 2 条），内存开销恒定 < 1MB。
    """
    if not HISTORY_FILE.exists():
        return "【底片调阅】: 历史底片 (chat_history.jsonl) 尚不存在或为空。"

    kw = keyword.strip()
    if not kw:
        return "【底片调阅】: 未指定有效检索词。"

    try:
        future_lines = deque(maxlen=2)
        pending_matches = []
        completed_slices = []
        kw_lower = kw.lower()

        for line in iter_lines_reverse(HISTORY_FILE):
            for pm in pending_matches[:]:
                pm["past"].append(line)
                if len(pm["past"]) >= 2:
                    completed_slices.append(pm)
                    pending_matches.remove(pm)
                    if len(completed_slices) >= max_slices:
                        break

            if len(completed_slices) >= max_slices:
                break

            if kw_lower in line.lower():
                pm = {
                    "match_line": line,
                    "future": list(future_lines),
                    "past": []
                }
                pending_matches.append(pm)

            future_lines.appendleft(line)

        for pm in pending_matches:
            completed_slices.append(pm)
            if len(completed_slices) >= max_slices:
                break

        if not completed_slices:
            return f"【底片调阅留痕】: 最近 10MB 底片中未命中关键词 [{kw}]。"

        slices_output = []
        for slice_idx, item in enumerate(completed_slices, 1):
            context_block = []
            for past_line in reversed(item["past"]):
                context_block.append((False, past_line))
            context_block.append((True, item["match_line"]))
            for future_line in item["future"]:
                context_block.append((False, future_line))

            block_text = []
            for is_hit, raw_line in context_block:
                prefix = ">> " if is_hit else "   "
                try:
                    data = json.loads(raw_line)
                    ts = data.get("time", "未知时间")
                    role = data.get("role", "unknown")
                    content = data.get("content", "")
                    sender = "User" if role == "user" else AI_NAME
                    block_text.append(f"{prefix}[{ts}] {sender}: {content}")
                except Exception:
                    block_text.append(f"{prefix}{raw_line}")

            slices_output.append(
                f"--- [底片切片 {slice_idx}/{len(completed_slices)} | 命中词: {kw}] ---\n" +
                "\n".join(block_text)
            )

        return "【底片调阅留痕】:\n" + "\n\n".join(slices_output)

    except Exception as e:
        return f"【底片调阅异常】: {str(e)}"


def query_blackboard(keyword: str = "", floor: int = 0) -> str:
    """
    黑板探针：按楼层编号或关键词定位中立黑板旧议案。
    自动标注【黑板调阅留痕】。
    """
    if not BLACKBOARD_FILE.exists():
        return "【黑板调阅】: 中立黑板文件 (shared/blackboard.md) 尚不存在。"

    try:
        with open(BLACKBOARD_FILE, "r", encoding="utf-8") as f:
            content = f.read()

        floors = [b.strip() for b in content.split("---") if b.strip()]
        total_floors = len(floors)

        if total_floors == 0:
            return "【黑板调阅】: 黑板当前为空白状态。"

        if floor > 0:
            if floor <= total_floors:
                return (
                    f"【黑板调阅留痕 | 楼层坐标: 第 {floor}/{total_floors} 楼】:\n"
                    f"{floors[floor - 1]}"
                )
            return f"【黑板调阅留痕】: 指定楼层 {floor} 超出范围（当前共 {total_floors} 楼）。"

        if keyword:
            matched_floors = []
            for idx in range(total_floors - 1, -1, -1):
                if keyword.lower() in floors[idx].lower():
                    matched_floors.append(
                        f"【黑板调阅留痕 | 楼层坐标: 第 {idx + 1}/{total_floors} 楼 | 命中词: {keyword}】:\n{floors[idx]}"
                    )
                    if len(matched_floors) >= 2:
                        break

            if matched_floors:
                return "\n\n".join(matched_floors)
            return f"【黑板调阅留痕】: 黑板中未检索到包含 '{keyword}' 的楼层。"

        return "【黑板调阅】: 请提供关键词 keyword 或楼层号 floor。"

    except Exception as e:
        return f"【黑板调阅异常】: {str(e)}"