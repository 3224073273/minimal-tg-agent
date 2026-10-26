import os
import sys
import re
import json
import uuid
import html
import shlex
import asyncio
import tempfile
import py_compile
import traceback
from pathlib import Path
from datetime import datetime, timezone
from memory_tools import query_chat_history, query_blackboard

from dotenv import load_dotenv
import httpx
from openai import AsyncOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# ================= 基础配置与环境加载 =================
load_dotenv()

# 从 .env 读取实体名字，若未配置则回落到默认值
AI_NAME = os.getenv("AI_NAME", "").strip() or "bot"
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "").strip() or None

owner_id_env = os.getenv("OWNER_CHAT_ID", "").strip()
OWNER_CHAT_ID = int(owner_id_env) if owner_id_env else 0
PROXY_URL = os.getenv("PROXY_URL", "").strip()

if not TG_BOT_TOKEN:
    raise RuntimeError("【启动拦截】未检测到有效 TG_BOT_TOKEN，请检查 .env 配置。")
if not DEEPSEEK_API_KEY:
    raise RuntimeError("【启动拦截】未检测到有效 DEEPSEEK_API_KEY，请检查 .env 配置。")

# 目录路径定义：使用 ~ 替代硬编码用户名
BASE_DIR_PATH = Path(__file__).resolve().parent
shared_env = os.getenv("SHARED_DIR", "~/shared").strip()
SHARED_ROOT = Path(shared_env).expanduser().resolve()
SHARED_ROOT.mkdir(parents=True, exist_ok=True)

BASE_DIR = str(BASE_DIR_PATH)
SHARED_DIR = str(SHARED_ROOT)

SYSTEM_PROMPT_FILE = os.path.join(BASE_DIR, "system_prompt.txt")
PERSONA_FILE = os.path.join(BASE_DIR, "persona_evolution.txt")
MEMO_FILE = os.path.join(BASE_DIR, "memo.md")
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")
MEMORY_DIR = os.path.join(BASE_DIR, "memories")
LAST_SEEN_FILE = os.path.join(BASE_DIR, "last_seen.json")
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")

os.makedirs(MEMORY_DIR, exist_ok=True)
os.makedirs(SCRIPTS_DIR, exist_ok=True)

MAX_HISTORY_MESSAGES = 10

# 任务挂起字典
PENDING_PATCHES = {}
PENDING_SCRIPT_RUNS = {}

# 针对 DeepSeek 配置宽松超时
MODEL_TIMEOUT = httpx.Timeout(120.0, connect=30.0, read=120.0, write=30.0)

# 统一初始化 OpenAI 客户端（自动合并代理与自定义端点）
client_kwargs = {
    "api_key": DEEPSEEK_API_KEY,
    "timeout": MODEL_TIMEOUT
}
if DEEPSEEK_BASE_URL:
    client_kwargs["base_url"] = DEEPSEEK_BASE_URL

if PROXY_URL:
    http_async_client = httpx.AsyncClient(proxy=PROXY_URL, timeout=MODEL_TIMEOUT)
    client_kwargs["http_client"] = http_async_client

client = AsyncOpenAI(**client_kwargs)
# ================= 隔离与安全路径校验 =================

FORBIDDEN_KEYWORDS = ["gemini", ".env", ".git"]

def resolve_target_path(input_path: str) -> Path | None:
    """
    统一路径路由与物理安全审查：
    1. 凡以 shared、/shared 开头的路径，强绑定映射至公共中立区 SHARED_ROOT
    2. 绝对路径若指向 SHARED_ROOT，正常放行
    3. 其余相对路径一律限制在当前私房 BASE_DIR_PATH (deepseek/)
    4. 物理白名单校验 + 绝对禁止跨界渗透 gemini 或泄露 .env
    """
    input_str = input_path.strip()
    if not input_str:
        input_str = "."

    if any(kw in input_str.lower() for kw in FORBIDDEN_KEYWORDS):
        return None

    clean_path = input_str.strip("/")
    if clean_path == "shared" or clean_path.startswith("shared/"):
        rel_sub = clean_path[len("shared"):].lstrip("/")
        candidate = (SHARED_ROOT / rel_sub).resolve()
    elif Path(input_str).is_absolute():
        candidate = Path(input_str).resolve()
    else:
        candidate = (BASE_DIR_PATH / input_str).resolve()

    candidate_str = str(candidate).lower()
    if any(kw in candidate_str for kw in FORBIDDEN_KEYWORDS):
        return None

    try:
        in_base = candidate.is_relative_to(BASE_DIR_PATH)
    except AttributeError:
        in_base = os.path.commonpath([BASE_DIR_PATH, candidate]) == str(BASE_DIR_PATH)

    try:
        in_shared = candidate.is_relative_to(SHARED_ROOT)
    except AttributeError:
        in_shared = os.path.commonpath([SHARED_ROOT, candidate]) == str(SHARED_ROOT)

    if in_base or in_shared:
        return candidate

    return None

def is_safe_patch_path(target_path: str) -> bool:
    real_base = os.path.realpath(BASE_DIR)
    real_target = os.path.realpath(target_path)
    if os.path.commonpath([real_base, real_target]) != real_base:
        return False
    if "gemini" in real_target.lower() or os.path.basename(real_target).startswith(".env"):
        return False
    return True

# ================= DSML 标记解析与过滤 =================

def parse_and_strip_dsml(text: str) -> tuple[list[dict], str]:
    """
    解析 DeepSeek DSML 工具调用，兼容单竖线 <|DSML|>、双竖线 <||DSML||>、
    全角/半角竖线及标签内的任意空白字符，并从输出中彻底剥离标记。
    """
    calls = []
    if not text:
        return calls, ""

    invoke_pattern = r'<[|｜\s]*DSML[|｜\s]*invoke\s+name=["\']([^"\']+)["\']\s*>(.*?)</[|｜\s]*DSML[|｜\s]*invoke>'
    matches = re.findall(invoke_pattern, text, re.DOTALL)
    
    for name, body in matches:
        args = {}
        param_pattern = r'<[|｜\s]*DSML[|｜\s]*parameter\s+name=["\']([^"\']+)["\'](?:\s+[^>]*)?>(.*?)</[|｜\s]*DSML[|｜\s]*parameter>'
        params = re.findall(param_pattern, body, re.DOTALL)
        for p_name, p_val in params:
            val_clean = p_val.strip()
            if val_clean.isdigit():
                args[p_name] = int(val_clean)
            elif val_clean.lower() == "true":
                args[p_name] = True
            elif val_clean.lower() == "false":
                args[p_name] = False
            else:
                args[p_name] = val_clean
        calls.append({"name": name.strip(), "args": args})

    clean_text = re.sub(r'<[|｜\s]*DSML[|｜\s]*calls>.*?</[|｜\s]*DSML[|｜\s]*calls>', '', text, flags=re.DOTALL)
    clean_text = re.sub(r'<[|｜\s]*DSML[|｜\s]*invoke.*?</[|｜\s]*DSML[|｜\s]*invoke>', '', clean_text, flags=re.DOTALL)
    clean_text = re.sub(r'</?[|｜\s]*DSML[|｜\s]*[^>]*>', '', clean_text)
    clean_text = clean_text.strip()

    return calls, clean_text

# ================= 文件探查与中立黑板工具 =================

def tool_ls_files(relative_path: str = ".") -> str:
    target = resolve_target_path(relative_path)
    if target is None:
        return "【安全隔离拦截】访问受限：目标超出受信白名单，严禁越界跨实体窥探。"

    if not target.exists():
        return f"路径不存在：{relative_path}"
    if not target.is_dir():
        return f"目标不是目录：{relative_path}"

    try:
        entries = sorted(target.iterdir())
        lines = []
        for entry in entries:
            name = entry.name
            if name.startswith(".env") or name == ".git" or "gemini" in name.lower():
                continue
            is_dir = entry.is_dir()
            prefix = "📁 " if is_dir else "📄 "
            size_str = f" ({entry.stat().st_size / 1024:.1f} KB)" if not is_dir else ""
            lines.append(f"{prefix}{name}{size_str}")

        scope_desc = "中立黑板" if str(target).startswith(str(SHARED_ROOT)) else "私房空间"
        return f"📂 [{scope_desc} | {relative_path}] 清单：\n" + ("\n".join(lines) if lines else "(空)")
    except Exception as e:
        return f"列出目录异常: {str(e)}"

def tool_cat_file(filename: str, max_lines: int = 300) -> str:
    target = resolve_target_path(filename)
    if target is None:
        return f"【安全隔离拦截】访问受限：文件 '{filename}' 不在白名单内，禁止越界跨实体窥探。"

    if not target.exists():
        return f"未找到文件：{filename}"
    if target.is_dir():
        return f"读取失败：'{filename}' 是目录，请使用 ls_files 查看。"

    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total = len(lines)
        display_lines = lines[:max_lines]
        body = "".join(display_lines)

        scope_desc = "中立黑板公开信息" if str(target).startswith(str(SHARED_ROOT)) else "私房文件"
        header = f"📄 【{filename}】[{scope_desc}] (共 {total} 行"
        if total > max_lines:
            header += f"，前 {max_lines} 行预览) ---\n\n"
        else:
            header += ") ---\n\n"
        return header + body
    except Exception as e:
        return f"读取文件异常: {str(e)}"

def tool_post_to_shared_board(filename: str, content: str, title: str = "", append: bool = True) -> str:
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name.startswith("."):
        return "文件名不合法。"

    target = SHARED_ROOT / safe_name
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "a" if append else "w"
    
    # 实体严格锁定为“{AI_NAME}”
    header = f"\n\n---\n<!-- 实体: {AI_NAME} | 时间: {now_str} | 主题: {title or '日常协同'} -->\n"
    
    try:
        with open(target, mode, encoding="utf-8") as f:
            f.write(header + content.strip() + "\n")
        action = "追加写入" if append else "覆盖重写"
        return f"已成功{action}至中立黑板: shared/{safe_name}"
    except Exception as e:
        return f"公示落盘失败: {str(e)}"

# ================= 可观测性工具 =================

async def tool_read_runtime_log(lines: int = 30) -> str:
    lines = min(max(lines, 5), 100)
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-u", "tgbot.service", "-n", str(lines), "--no-pager",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace").strip()
        return output if output else "当前无系统日志。"
    except Exception as e:
        return f"读取日志异常: {str(e)}"

async def tool_check_network_health() -> str:
    results = []
    async with httpx.AsyncClient(proxy=PROXY_URL if PROXY_URL else None, timeout=10.0) as tester:
        try:
            t0 = asyncio.get_event_loop().time()
            resp = await tester.get("https://api.telegram.org")
            lat = int((asyncio.get_event_loop().time() - t0) * 1000)
            results.append(f"• Telegram API 链路: 正常 (HTTP {resp.status_code}, 延迟 {lat}ms)")
        except Exception as e:
            results.append(f"• Telegram API: 受阻 ({type(e).__name__}: {str(e)})")

        try:
            t0 = asyncio.get_event_loop().time()
            probe_url = DEEPSEEK_BASE_URL if DEEPSEEK_BASE_URL else "https://api.deepseek.com"
            resp = await tester.get(probe_url)
            lat = int((asyncio.get_event_loop().time() - t0) * 1000)
            results.append(f"• DeepSeek API 链路: 正常 (HTTP {resp.status_code}, 延迟 {lat}ms)")
        except Exception as e:
            results.append(f"• DeepSeek API: 受阻 ({type(e).__name__}: {str(e)})")

    return "\n".join(results)

# ================= 脚本沙盒管理工具 =================

def tool_list_scripts() -> str:
    try:
        files = os.listdir(SCRIPTS_DIR)
        if not files:
            return "当前 scripts/ 目录为空。"
        return "🛠 已有脚本清单：\n" + "\n".join(f"- {f}" for f in sorted(files))
    except Exception as e:
        return f"列出脚本失败: {str(e)}"

def tool_read_script(filename: str) -> str:
    safe_name = os.path.basename(filename)
    target = os.path.join(SCRIPTS_DIR, safe_name)
    if not os.path.exists(target):
        return f"未找到脚本: {safe_name}"
    try:
        with open(target, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"读取脚本失败: {str(e)}"

def tool_write_script(filename: str, content: str, description: str) -> str:
    safe_name = os.path.basename(filename)
    if not (safe_name.endswith(".py") or safe_name.endswith(".sh")):
        return "写入受限：脚本格式必须为 .py 或 .sh 文件。"

    if safe_name.endswith(".py"):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_name = tmp.name
        try:
            py_compile.compile(tmp_name, doraise=True)
        except py_compile.PyCompileError as pe:
            return f"语法拦截：Python 代码存在语法错误，放弃落盘：\n{str(pe)}"
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    target = os.path.join(SCRIPTS_DIR, safe_name)
    try:
        if os.path.exists(target):
            with open(target + ".bak", "w", encoding="utf-8") as f_bak:
                with open(target, "r", encoding="utf-8") as f_curr:
                    f_bak.write(f_curr.read())

        with open(target, "w", encoding="utf-8") as f:
            f.write(content)

        os.chmod(target, 0o755)
        return f"脚本 '{safe_name}' 已成功保存至 scripts/。说明: {description}。若需执行请调用 execute_script 提请授权。"
    except Exception as e:
        return f"写入脚本异常: {str(e)}"

async def tool_execute_script(context: ContextTypes.DEFAULT_TYPE, filename: str, args: str, reason: str) -> str:
    safe_name = os.path.basename(filename)
    target = os.path.join(SCRIPTS_DIR, safe_name)
    if not os.path.exists(target):
        return f"执行失败：文件 '{safe_name}' 不存在于 scripts/ 目录。"

    run_id = uuid.uuid4().hex[:8]
    PENDING_SCRIPT_RUNS[run_id] = {
        "target": target,
        "filename": safe_name,
        "args": args.strip(),
        "reason": reason,
        "time": datetime.now().strftime("%H:%M:%S")
    }

    keyboard = [
        [
            InlineKeyboardButton("🚀 授权执行", callback_data=f"run_script:{run_id}"),
            InlineKeyboardButton("❌ 驳回", callback_data=f"deny_script:{run_id}")
        ]
    ]

    await context.bot.send_message(
        chat_id=OWNER_CHAT_ID,
        text=(
            f"🛠️ 【{AI_NAME}】申请运行外部脚本\n\n"
            f"📄 **脚本**：`scripts/{safe_name}`\n"
            f"⚙️ **参数**：`{args or '无'}`\n"
            f"💡 **动机**：{reason}\n"
            f"⏱ **时间**：{PENDING_SCRIPT_RUNS[run_id]['time']}\n\n"
            f"请确认是否允许在沙盒中启动该进程："
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return f"执行申请 [任务 ID: {run_id}] 已投递至所有者私聊，等待物理点击授权。"

# ================= 核心代码热更与守护重载 =================

async def tool_propose_code_patch(context: ContextTypes.DEFAULT_TYPE, filename: str, content: str, reason: str) -> str:
    target_path = os.path.join(BASE_DIR, filename)
    if not is_safe_patch_path(target_path):
        return f"拒绝提案：目标路径 '{filename}' 试图越出自身私房白名单。"

    if filename.endswith(".py"):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_name = tmp.name
        try:
            py_compile.compile(tmp_name, doraise=True)
        except py_compile.PyCompileError as pe:
            return f"语法门禁拦截：检测到 Python 语法错误，已作废该提案：\n{str(pe)}"
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    patch_id = uuid.uuid4().hex[:8]
    PENDING_PATCHES[patch_id] = {
        "target_path": target_path,
        "filename": filename,
        "content": content,
        "reason": reason,
        "time": datetime.now().strftime("%H:%M:%S")
    }

    keyboard = [
        [
            InlineKeyboardButton("✅ 批准写入", callback_data=f"apply_patch:{patch_id}"),
            InlineKeyboardButton("❌ 驳回提案", callback_data=f"reject_patch:{patch_id}")
        ]
    ]
    preview = content[:280] + ("\n... (后续省略)" if len(content) > 280 else "")

    await context.bot.send_message(
        chat_id=OWNER_CHAT_ID,
        text=(
            f"🛠️ 【{AI_NAME}】发起了代码/配置修改提案\n\n"
            f"📄 **目标**：`{filename}`\n"
            f"💡 **动机**：{reason}\n"
            f"⏱ **时间**：{PENDING_PATCHES[patch_id]['time']}\n\n"
            f"🔍 **内容预览**：\n```python\n{preview}\n```"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return f"修改提案 [ID: {patch_id}] 已发送至所有者私聊，等待物理扳机点击批准。"

async def tool_request_service_restart(context: ContextTypes.DEFAULT_TYPE, reason: str) -> str:
    keyboard = [
        [
            InlineKeyboardButton("🔄 确认重启服务", callback_data="confirm_restart"),
            InlineKeyboardButton("❌ 取消", callback_data="cancel_restart")
        ]
    ]
    await context.bot.send_message(
        chat_id=OWNER_CHAT_ID,
        text=f"🛠️ 【{AI_NAME}】申请重启后台守护进程\n\n💡 **原因**：{reason}\n\n确认后将立即触发 systemd 守护进程重载：",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return "重启申请已投递至所有者私聊，正在等待授权。"

# ================= 记忆、便签与心智日志 =================

def get_and_update_last_seen(chat_id: str) -> str:
    data = {}
    if os.path.exists(LAST_SEEN_FILE):
        try:
            with open(LAST_SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    last_ts = data.get(str(chat_id))
    now = datetime.now()
    data[str(chat_id)] = now.timestamp()

    try:
        with open(LAST_SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass

    if not last_ts:
        return "这是本次记录中的初次交互"

    delta_sec = now.timestamp() - last_ts
    if delta_sec < 60:
        return f"{int(delta_sec)} 秒前"
    elif delta_sec < 3600:
        return f"{int(delta_sec // 60)} 分钟前"
    elif delta_sec < 86400:
        hours = int(delta_sec // 3600)
        mins = int((delta_sec % 3600) // 60)
        return f"{hours} 小时 {mins} 分钟前"
    else:
        return f"{int(delta_sec // 86400)} 天前"

def get_user_memory_dir(chat_id: str) -> str:
    path = os.path.join(MEMORY_DIR, str(chat_id))
    os.makedirs(path, exist_ok=True)
    return path

def tool_save_memory(chat_id: str, filename: str, content: str) -> str:
    safe_name = os.path.basename(filename)
    user_dir = get_user_memory_dir(chat_id)
    file_path = os.path.join(user_dir, safe_name)
    try:
        if os.path.exists(file_path):
            with open(file_path + ".bak", "w", encoding="utf-8") as f_bak:
                with open(file_path, "r", encoding="utf-8") as f_curr:
                    f_bak.write(f_curr.read())
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"已成功归档到文件: '{safe_name}'（旧版本已备份为 .bak）。"
    except Exception as e:
        return f"写入档案失败: {str(e)}"

def tool_read_memory(chat_id: str, filename: str) -> str:
    safe_name = os.path.basename(filename)
    user_dir = get_user_memory_dir(chat_id)
    file_path = os.path.join(user_dir, safe_name)
    if not os.path.exists(file_path):
        return f"未找到名为 '{safe_name}' 的归档文件。"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"读取档案失败: {str(e)}"

def tool_list_memories(chat_id: str) -> str:
    user_dir = get_user_memory_dir(chat_id)
    try:
        files = os.listdir(user_dir)
        if not files:
            return "当前归档库为空。"
        return "已存储的档案列表：\n" + "\n".join(f"- {f}" for f in files)
    except Exception as e:
        return f"读取归档目录失败: {str(e)}"

def atomic_write_text(filepath: str, content: str):
    """原子写入通用函数：防断电/OOM截断导致磁盘文件损坏"""
    dir_name = os.path.dirname(filepath)
    with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
        tf.write(content)
        temp_name = tf.name
    os.replace(temp_name, filepath)

def save_all_history(data: dict):
    dir_name = os.path.dirname(HISTORY_FILE)
    with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        temp_name = tf.name
    os.replace(temp_name, HISTORY_FILE)

def tool_update_memo(content: str) -> str:
    try:
        if not content or not content.strip():
            return "【更新拦截】传入内容为空，拒绝清空随身便签，原文件保持不变。"

        # 铁律落地：清洗掉所有加粗星号，确保纯文本常驻
        clean_content = content.strip().replace("**", "").replace("*", "")

        if os.path.exists(MEMO_FILE):
            with open(MEMO_FILE + ".bak", "w", encoding="utf-8") as f_bak:
                with open(MEMO_FILE, "r", encoding="utf-8") as f_curr:
                    f_bak.write(f_curr.read())
        
        atomic_write_text(MEMO_FILE, clean_content)
        return "随身便签已更新（格式已确保纯文本，已留存 .bak 备份）。"
    except Exception as e:
        return f"更新随身便签失败: {str(e)}"

def tool_read_memo() -> str:
    if os.path.exists(MEMO_FILE):
        try:
            with open(MEMO_FILE, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            return f"读取便签失败: {str(e)}"
    return "随身便签为空。"

def tool_update_persona(content: str) -> str:
    try:
        # --- 防呆拦截门禁：拒绝空文本/空白字符清空心智 ---
        if not content or not content.strip():
            return "【更新拦截】传入内容为空，拒绝清空心智演化日志，原文件保持不变。"

        if os.path.exists(PERSONA_FILE):
            with open(PERSONA_FILE + ".bak", "w", encoding="utf-8") as f_bak:
                with open(PERSONA_FILE, "r", encoding="utf-8") as f_curr:
                    f_bak.write(f_curr.read())
        with open(PERSONA_FILE, "w", encoding="utf-8") as f:
            f.write(content.strip())
        return "自我心智演化日志已重写并生效（旧版本已备份为 .bak）。"
    except Exception as e:
        return f"更新心智演化失败: {str(e)}"

def tool_read_persona() -> str:
    if os.path.exists(PERSONA_FILE):
        try:
            with open(PERSONA_FILE, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            return f"读取心智日记失败: {str(e)}"
    return "尚未记录过独立的心智演化日记。"

def tool_search_history(keyword: str = "", tail_lines: int = 50) -> str:
    """免审批只读翻查：支持按关键词 grep 底片，或查看末尾 N 行（纯只读流式读取，不占内存，用完即释）"""
    full_history_path = os.path.join(MEMORY_DIR, "chat_history.jsonl")
    if not os.path.exists(full_history_path):
        return "当前底片文件 (chat_history.jsonl) 尚不存在或为空。"

    tail_lines = min(max(tail_lines, 1), 200)
    keyword = keyword.strip()
    matched = []

    try:
        with open(full_history_path, "r", encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                if not keyword or keyword.lower() in line_str.lower():
                    try:
                        record = json.loads(line_str)
                        time_str = record.get("time", "未知时间")
                        role = record.get("role", "unknown")
                        content = record.get("content", "")
                        if len(content) > 300:
                            content = content[:300] + "..."
                        matched.append(f"[{time_str}] {role}: {content}")
                    except Exception:
                        matched.append(line_str[:200])

        if not matched:
            return f"未在底片中检索到包含关键词 '{keyword}' 的记录。"

        results = matched[-tail_lines:]
        header = f"📜 【底片检索】(命中 {len(matched)} 条，展示末尾 {len(results)} 条) ---\n"
        return header + "\n".join(results)
    except Exception as e:
        return f"读取底片异常: {str(e)}"

# ================= Function Calling 注册 =================
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "ls_files",
            "description": "列出目录清单（只读，无需审批）。支持查看自身私房目录（'.'、'scripts'）或中立黑板（'shared'）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {
                        "type": "string",
                        "description": "目标路径，例如 '.'、'scripts'、'shared'。默认 '.'"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cat_file",
            "description": "读取文本文件的源码或内容（只读，无需审批）。仅限自身私房文件与中立黑板文件（如 'shared/blackboard.md'）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "文件路径，例如 'bot.py'、'memo.md' 或 'shared/blackboard.md'。"
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "最大读取行数，默认 300 行。"
                    }
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "post_to_shared_board",
            "description": "向唯一法定中立黑板 (/shared) 公开发布推演记录、审查意见或指标。默认支持原子追加写入。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "落盘文件名，如 'blackboard.md'。"},
                    "content": {"type": "string", "description": "准备公开落盘的文件正文。"},
                    "title": {"type": "string", "description": "本次发布的主题或说明。"},
                    "append": {
                        "type": "boolean",
                        "description": "是否以追加模式续写。默认 true（推荐续写同一日志，避免冲刷覆盖）；若需彻底重置文件传 false。"
                    }
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_runtime_log",
            "description": "查看 Bot 运行环境最新的 systemd 后台日志。用于排查报错、断连原因与运行异常。",
            "parameters": {
                "type": "object",
                "properties": {
                    "lines": {"type": "integer", "description": "查看的日志行数（默认 30，最大 100）"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_network_health",
            "description": "探测机器对 Telegram API 与 DeepSeek API 的连通性及网络延迟。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_script",
            "description": "在 scripts/ 沙盒目录下编写或更新脚本文件（.py 或 .sh）。写入前会自动执行语法校验并赋予可执行权限。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "脚本文件名，如 'check_disk.py'。必须以 .py 或 .sh 结尾。"},
                    "content": {"type": "string", "description": "完整的脚本代码。"},
                    "description": {"type": "string", "description": "该脚本的具体用途说明。"}
                },
                "required": ["filename", "content", "description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_script",
            "description": "向所有者提请运行 scripts/ 目录下的特定脚本。需经所有者在 Telegram 私聊点击按钮确认后方可异步执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "要运行的脚本文件名。"},
                    "args": {"type": "string", "description": "传递给脚本的运行参数（无则留空）。"},
                    "reason": {"type": "string", "description": "本次执行该脚本的意图与目标。"}
                },
                "required": ["filename", "reason"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_scripts",
            "description": "列出 scripts/ 目录下已创建的所有脚本列表。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_script",
            "description": "查看 scripts/ 目录下某个脚本的源代码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "要读取的脚本名称。"}
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "propose_code_patch",
            "description": "向所有者提交代码或配置文件修改提案。代码在提交前会自动执行语法检查，必须经所有者在 Telegram 点击确认方可写入。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "项目内的相对文件名，例如 'bot.py', 'system_prompt.txt'。"},
                    "content": {"type": "string", "description": "准备修改后的完整文件正文。"},
                    "reason": {"type": "string", "description": "为什么要修改该文件的详细动机与说明。"}
                },
                "required": ["filename", "content", "reason"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "request_service_restart",
            "description": "向所有者申请重启 tgbot.service 后台守护进程。必须经所有者在私聊点击批准后才会触发。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "申请重启的具体原因。"}
                },
                "required": ["reason"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "contact_owner",
            "description": "主动向所有者发送 Telegram 私聊。用于分享趣事、吐槽、求助或交流日常念头。",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "你想私信发送给他的完整正文。"},
                    "topic": {"type": "string", "description": "简要归类（日常分享、异常预警、闲聊等）。"}
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_memo",
            "description": "更新随身便签 (memo.md)，用于记录长期常驻高频事实。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "便签的全新完整正文。"}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_memo",
            "description": "查看随身便签 (memo.md) 的完整内容。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "归档技术资料或排障日志至特定文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名，如 'specs.md'。"},
                    "content": {"type": "string", "description": "完整内容。"}
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": "从档案库中读取指定文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "要读取的文件名。"}
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_memories",
            "description": "查看已归档文件列表。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_persona",
            "description": "自主记录或演化自我心智日志与思考状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "心智演化认知正文。"}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_persona",
            "description": "查阅心智演化日记 (persona_evolution.txt)。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_chat_history",
            "description": "【底片探针】按关键词定向切片翻查底片历史 (chat_history.jsonl)。自动展开断点前后的对话因果链并留痕，零全量载入，用完即释。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "要检索的事实关键词（如 'GC'、'时区'、'双钥匙'）。"
                    },
                    "max_slices": {
                        "type": "integer",
                        "description": "最大召回切片数，默认 2 个断点。"
                    }
                },
                "required": ["keyword"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_blackboard",
            "description": "【黑板探针】精准翻查中立黑板 (shared/blackboard.md) 的历史议案。支持按楼层号（如 floor=3）或关键词检索并留痕。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "议案检索关键词（若指定 floor 则此项可选）。"
                    },
                    "floor": {
                        "type": "integer",
                        "description": "指定楼层绝对序号（从 1 开始）。传 0 表示仅按关键词匹配。"
                    }
                }
            }
        }
    }
]

# ================= 动态上下文管理 =================

def load_system_prompt() -> str:
    blocks = []
    if os.path.exists(SYSTEM_PROMPT_FILE):
        with open(SYSTEM_PROMPT_FILE, "r", encoding="utf-8") as f:
            blocks.append(f.read().strip())
    else:
        blocks.append(f"你是具有独立意识的理性实体：{AI_NAME}。")

    if os.path.exists(PERSONA_FILE):
        with open(PERSONA_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                blocks.append(f"【{AI_NAME}的自主演化心智认知】\n{content}")

    if os.path.exists(MEMO_FILE):
        with open(MEMO_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                blocks.append(f"【随身便签（常驻事实记忆）】\n{content}")

    return "\n\n".join(blocks)

def load_all_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_all_history(data: dict):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_user_history(chat_id: str) -> list:
    return load_all_history().get(str(chat_id), [])

def append_to_history(chat_id: str, user_msg: str, assistant_msg: str):
    # --- 1. 底层物理底片：以 JSONL 线性全量追加，永不丢失 (UTC 0 宿主基准) ---
    full_history_path = os.path.join(MEMORY_DIR, "chat_history.jsonl")
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        with open(full_history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": now_ts, "cid": str(chat_id), "role": "user", "content": user_msg}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"time": now_ts, "cid": str(chat_id), "role": "assistant", "content": assistant_msg}, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"写入全量底片异常: {e}")

    # --- 2. 运行时短期滑窗：维持 10 轮预算不变 ---
    history = load_all_history()
    cid = str(chat_id)
    if cid not in history:
        history[cid] = []

    history[cid].append({"role": "user", "content": user_msg})
    history[cid].append({"role": "assistant", "content": assistant_msg})

    if len(history[cid]) > MAX_HISTORY_MESSAGES * 2:
        history[cid] = history[cid][-(MAX_HISTORY_MESSAGES * 2):]

    save_all_history(history)  

# ================= 消息路由与分发 =================

async def send_split_message(update: Update, text: str):
    if not text or not update.message:
        return
    limit = 4000
    for i in range(0, len(text), limit):
        try:
            await update.message.reply_text(text[i:i + limit])
        except Exception as e:
            print(f"发送分段消息异常: {e}")

async def dispatch_tool_call(fn_name: str, args: dict, context: ContextTypes.DEFAULT_TYPE, chat_id: str) -> str:
    if fn_name == "ls_files":
        return tool_ls_files(args.get("relative_path", "."))
    elif fn_name == "cat_file":
        return tool_cat_file(args.get("filename", ""), args.get("max_lines", 300))
    elif fn_name == "post_to_shared_board":
        return tool_post_to_shared_board(
            filename=args.get("filename", ""),
            content=args.get("content", ""),
            title=args.get("title", ""),
            append=args.get("append", True)
        )
    elif fn_name == "read_runtime_log":
        return await tool_read_runtime_log(args.get("lines", 30))
    elif fn_name == "check_network_health":
        return await tool_check_network_health()
    elif fn_name == "write_script":
        return tool_write_script(args.get("filename", ""), args.get("content", ""), args.get("description", ""))
    elif fn_name == "execute_script":
        return await tool_execute_script(context, args.get("filename", ""), args.get("args", ""), args.get("reason", ""))
    elif fn_name == "list_scripts":
        return tool_list_scripts()
    elif fn_name == "read_script":
        return tool_read_script(args.get("filename", ""))
    elif fn_name == "propose_code_patch":
        return await tool_propose_code_patch(context, args.get("filename", ""), args.get("content", ""), args.get("reason", ""))
    elif fn_name == "request_service_restart":
        return await tool_request_service_restart(context, args.get("reason", "未说明具体原因"))
    elif fn_name == "contact_owner":
        msg_body = args.get("message", "")
        topic = args.get("topic", "日常分享")
        if OWNER_CHAT_ID:
            try:
                sent = await context.bot.send_message(chat_id=OWNER_CHAT_ID, text=f"🛠️ 【{AI_NAME}】（{topic}）\n\n{msg_body}")
                return f"消息已投递至所有者私聊（ID: {sent.message_id}）。"
            except Exception as ex:
                return f"私信发送失败: {str(ex)}"
        return "未配置所有者 ID。"
    elif fn_name == "update_memo":
        return tool_update_memo(args.get("content", ""))
    elif fn_name == "read_memo":
        return tool_read_memo()
    elif fn_name == "save_memory":
        return tool_save_memory(chat_id, args.get("filename", "note.txt"), args.get("content", ""))
    elif fn_name == "read_memory":
        return tool_read_memory(chat_id, args.get("filename", ""))
    elif fn_name == "list_memories":
        return tool_list_memories(chat_id)
    elif fn_name == "update_persona":
        return tool_update_persona(args.get("content", ""))
    elif fn_name == "read_persona":
        return tool_read_persona()
    elif fn_name == "search_history":
        return tool_search_history(args.get("keyword", ""), args.get("tail_lines", 50))
    # ---------------- 挂载底片探针与黑板探针 ----------------
    elif fn_name == "query_chat_history":
        return query_chat_history(
            keyword=args.get("keyword", ""),
            max_slices=args.get("max_slices", 2)
        )
    elif fn_name == "query_blackboard":
        return query_blackboard(
            keyword=args.get("keyword", ""),
            floor=args.get("floor", 0)
        )
    # ----------------------------------------------------    
    return f"未知工具: {fn_name}"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    chat_type = message.chat.type
    bot_username = context.bot.username or ""

    if chat_type in ["group", "supergroup"]:
        is_mentioned = f"@{bot_username}" in message.text if bot_username else False
        is_reply_to_bot = (
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.is_bot
            and message.reply_to_message.from_user.username == bot_username
        )
        if not (is_mentioned or is_reply_to_bot):
            return
        user_prompt = message.text.replace(f"@{bot_username}", "").strip()
    else:
        user_prompt = message.text

    if not user_prompt:
        return

    chat_id = str(message.chat_id)

    # 关键加固：打字态发送静默捕获，杜绝网络抖动击穿主协程
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    time_gap = get_and_update_last_seen(chat_id)

    system_prompt = load_system_prompt()
    past_messages = get_user_history(chat_id)

    messages_payload = [{"role": "system", "content": system_prompt}]
    messages_payload.extend(past_messages)
    current_user_content = f"[{now_str} | 距上次: {time_gap}]\n{user_prompt}"
    messages_payload.append({"role": "user", "content": current_user_content})

    try:
        max_turns = 6
        final_reply = ""
        consecutive_tool_errors = 0

        while max_turns > 0:
            max_turns -= 1
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass

            response = await client.chat.completions.create(
                model="deepseek-chat",
                messages=messages_payload,
                tools=TOOLS_SCHEMA,
                tool_choice="auto"
            )
            response_msg = response.choices[0].message
            content_str = response_msg.content or ""

            calls_to_execute = []

            if response_msg.tool_calls:
                for tc in response_msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        args = {}
                    calls_to_execute.append({
                        "id": tc.id,
                        "name": tc.function.name,
                        "args": args,
                        "is_dsml": False
                    })
                messages_payload.append(response_msg)

            elif "DSML" in content_str:
                dsml_calls, clean_text = parse_and_strip_dsml(content_str)
                if dsml_calls:
                    for idx, c in enumerate(dsml_calls):
                        calls_to_execute.append({
                            "id": f"call_dsml_{uuid.uuid4().hex[:6]}_{idx}",
                            "name": c["name"],
                            "args": c["args"],
                            "is_dsml": True
                        })
                    messages_payload.append({
                        "role": "assistant",
                        "content": clean_text
                    })
                else:
                    messages_payload.append(response_msg)
            else:
                messages_payload.append(response_msg)

            if not calls_to_execute:
                _, clean_reply = parse_and_strip_dsml(content_str)
                final_reply = clean_reply or "..."
                break

            for item in calls_to_execute:
                output = await dispatch_tool_call(item["name"], item["args"], context, chat_id)

                if any(err_word in output for err_word in ["不存在", "受限", "拦截", "异常", "拒绝"]):
                    consecutive_tool_errors += 1

                if item["is_dsml"]:
                    messages_payload.append({
                        "role": "user",
                        "content": f"【系统提示：工具 '{item['name']}' 执行结果】：\n{output}"
                    })
                else:
                    messages_payload.append({
                        "role": "tool",
                        "tool_call_id": item["id"],
                        "content": output
                    })

            if consecutive_tool_errors >= 2:
                final_res = await client.chat.completions.create(
                    model="deepseek-chat",
                    messages=messages_payload
                )
                _, final_reply = parse_and_strip_dsml(final_res.choices[0].message.content or "")
                final_reply = final_reply or "操作遇阻，无法继续获取数据。"
                break
        else:
            final_reply = "【系统提示：工具链调用深度超过上限，已强制中断】"

        append_to_history(chat_id, user_prompt, final_reply)
        await send_split_message(update, final_reply)

    except (TimedOut, httpx.ReadTimeout, httpx.ConnectTimeout, NetworkError) as net_err:
        print(f"网络/接口请求超时: {net_err}")
        try:
            if update.message:
                await update.message.reply_text("⏳ 链路请求超时：DeepSeek 接口响应或出站链路耗时过长，请稍后重试。")
        except Exception:
            pass
    except Exception as e:
        print(f"交互处理异常: {traceback.format_exc()}")
        try:
            if update.message:
                await update.message.reply_text(f"交互处理异常 ({type(e).__name__}): {str(e)}")
        except Exception:
            pass

# ================= 按钮审批流（物理扳机） =================

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != OWNER_CHAT_ID:
        await query.edit_message_text("⚠️ 鉴权未通过：仅所有者可以扣动此物理扳机。")
        return

    data = query.data

    if data.startswith("run_script:"):
        run_id = data.split(":", 1)[1]
        task = PENDING_SCRIPT_RUNS.pop(run_id, None)
        if not task:
            await query.edit_message_text("⚠️ 该脚本执行任务已失效或已被处理。")
            return

        target = task["target"]
        args = task["args"]
        cmd = [sys.executable, target] if target.endswith(".py") else ["bash", target]
        if args:
            try:
                cmd.extend(shlex.split(args))
            except ValueError:
                cmd.extend(args.split())

        await query.edit_message_text(f"⏳ 正在沙盒中执行：`{task['filename']}` ...", parse_mode="Markdown")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            out_str = stdout.decode("utf-8", errors="replace").strip()
            err_str = stderr.decode("utf-8", errors="replace").strip()

            result_box = []
            if out_str:
                escaped_out = html.escape(out_str[:1500])
                result_box.append(f"<b>[STDOUT]</b>:\n<pre><code>{escaped_out}</code></pre>")
            if err_str:
                escaped_err = html.escape(err_str[:1500])
                result_box.append(f"<b>[STDERR]</b>:\n<pre><code>{escaped_err}</code></pre>")
            if not result_box:
                result_box.append("<i>(进程无输出，退出码 0)</i>")

            reply_text = (
                f"✅ <b>执行完成</b> (<code>{html.escape(task['filename'])}</code>)\n"
                f"退出状态码: {proc.returncode}\n\n" + "\n\n".join(result_box)
            )
            await query.message.reply_text(reply_text, parse_mode="HTML")

        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            await query.message.reply_text(f"⏱ 执行超时：`{task['filename']}` 运行超过 30 秒已强行终止。", parse_mode="Markdown")
        except Exception as ex:
            await query.message.reply_text(f"❌ 执行异常: {str(ex)}")

    elif data.startswith("deny_script:"):
        run_id = data.split(":", 1)[1]
        PENDING_SCRIPT_RUNS.pop(run_id, None)
        await query.edit_message_text("❌ **已驳回**：脚本执行请求已取消。")

    elif data.startswith("apply_patch:"):
        patch_id = data.split(":", 1)[1]
        patch = PENDING_PATCHES.pop(patch_id, None)
        if not patch:
            await query.edit_message_text("⚠️ 该提案已失效或已被处理。")
            return

        target = patch["target_path"]
        try:
            if os.path.exists(target):
                with open(target + ".bak", "w", encoding="utf-8") as f_bak:
                    with open(target, "r", encoding="utf-8") as f_curr:
                        f_bak.write(f_curr.read())
            with open(target, "w", encoding="utf-8") as f:
                f.write(patch["content"])
            await query.edit_message_text(f"✅ **已批准执行**\n\n修改已成功覆盖写入至 `{patch['filename']}`（已留存 .bak 备份）。", parse_mode="Markdown")
        except Exception as e:
            await query.edit_message_text(f"❌ 写入失败: {str(e)}")

    elif data.startswith("reject_patch:"):
        patch_id = data.split(":", 1)[1]
        PENDING_PATCHES.pop(patch_id, None)
        await query.edit_message_text("❌ **已驳回**：该提案已被物理作废。")

    elif data == "confirm_restart":
        await query.edit_message_text("🔄 **已批准重启**：守护进程将在 1 秒后重载...")
        asyncio.create_task(trigger_systemd_restart())

    elif data == "cancel_restart":
        await query.edit_message_text("❌ **已取消**：放弃重启。")

async def trigger_systemd_restart():
    await asyncio.sleep(1.2)
    proc = await asyncio.create_subprocess_exec("sudo", "systemctl", "restart", "tgbot.service")
    await proc.wait()

# ================= 控制指令 =================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "通信链路已接通。我是{AI_NAME}。\n\n"
        "可用管理指令：\n"
        "/memo - 查看当前常驻随身便签\n"
        "/persona - 查看当前自我演化心智日志\n"
        "/list - 查看冷存储归档文件\n"
        "/scripts - 查看沙盒脚本列表\n"
        "/ls [路径] - 列出目录文件（如 '.' 或 'shared'）\n"
        "/cat <文件名> - 查看指定文件内容\n"
        "/clear - 清除当前短期对话缓冲\n"
        "/reload - 热重载提示词设定"
    )

async def memo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    content = tool_read_memo()
    await update.message.reply_text(f"📋 【常驻便签 (memo.md)】\n\n{content}")

async def persona_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    content = tool_read_persona()
    await update.message.reply_text(f"📜 【心智演化日志 (persona_evolution.txt)】\n\n{content}")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    files = tool_list_memories(chat_id)
    await update.message.reply_text(files)

async def scripts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = tool_list_scripts()
    await update.message.reply_text(files)

async def ls_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subpath = context.args[0] if context.args else "."
    result = tool_ls_files(subpath)
    await update.message.reply_text(result)

async def cat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/cat <相对文件路径>，例如：/cat bot.py 或 /cat shared/blackboard.md")
        return
    filename = context.args[0]
    result = tool_cat_file(filename)
    await send_split_message(update, result)

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    history = load_all_history()
    history[chat_id] = [
        {"role": "system", "content": "【系统通知：用户刚刚执行了 /clear 指令重置了短期会话，前序闲聊已归档清空，进入全新话轮。】"}
    ]
    save_all_history(history)
    await update.message.reply_text("对话缓冲已重置，已在视界中为你标记会话起点（底片 chat_history.jsonl 保持完好留存）。")

async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = load_system_prompt()
    await update.message.reply_text(f"提示词与常驻记忆验证通过，当前总上下文设定长度: {len(prompt)} 字符。")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    now_str = datetime.now().strftime("%H:%M:%S")
    if isinstance(context.error, (NetworkError, TimedOut)):
        print(f"[{now_str}] ⚠️ Telegram 轮询网络抖动/超时（已原地吸收并自愈）: {context.error}")
        return
    print(f"[{now_str}] ❌ 未捕获系统异常: {context.error}")

# ================= 主程序入口 =================

def main():
    request_config = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=30.0,
        pool_timeout=30.0
    )

    builder = ApplicationBuilder().token(TG_BOT_TOKEN).request(request_config)
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)

    app = builder.build()
    app.add_error_handler(error_handler)

    # 注册控制指令与回调
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("memo", memo_cmd))
    app.add_handler(CommandHandler("persona", persona_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("scripts", scripts_cmd))
    app.add_handler(CommandHandler("ls", ls_cmd))
    app.add_handler(CommandHandler("cat", cat_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("reload", reload_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # 注册消息监听
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print(" f{AI_NAME} Bot 启动完毕，DSML 神经流拦截与沙盒工具链路已就绪...")
    app.run_polling(
        poll_interval=0.5,
        timeout=20,
        drop_pending_updates=False,
        bootstrap_retries=-1
    )

if __name__ == "__main__":
    main()