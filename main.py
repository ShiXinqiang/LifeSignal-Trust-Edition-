import os
import logging
import asyncio
import hashlib
import random
import string
from uuid import uuid4 
from datetime import datetime, timedelta, timezone

# Telegram 相关库
from telegram import (
    Update, 
    ReplyKeyboardMarkup, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    InlineQueryResultArticle, 
    InputTextMessageContent,
    ReplyKeyboardRemove
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler, 
    ContextTypes,
    filters,
    ConversationHandler,
    PicklePersistence,
    ApplicationHandlerStop
)
from telegram.constants import ParseMode

# 数据库相关库
from sqlalchemy import Column, BigInteger, Text, DateTime, String, Integer, Boolean, select, ForeignKey, func, delete
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 加密库
from cryptography.fernet import Fernet

# --- 1. 配置与初始化 ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
BOT_USERNAME = os.getenv("BOT_USERNAME", "LifeSignal_Bot") 
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY") 
GITHUB_REPO_URL = "https://github.com/ShiXinqiang/LifeSignal-Trust-Edition-" 

if not TOKEN or not DATABASE_URL:
    raise ValueError("❌ 启动失败: 缺少 TELEGRAM_BOT_TOKEN 或 DATABASE_URL")

if not ENCRYPTION_KEY:
    logger.warning("⚠️以此模式运行不安全！未检测到 ENCRYPTION_KEY，正在使用临时密钥。")
    ENCRYPTION_KEY = Fernet.generate_key().decode()

cipher_suite = Fernet(ENCRYPTION_KEY.encode())

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://") and not DATABASE_URL.startswith("postgresql+asyncpg://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# --- 2. 数据库模型 ---
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    chat_id = Column(BigInteger, primary_key=True)
    username = Column(String, nullable=True)
    password_hash = Column(String, nullable=True) 
    login_attempts = Column(Integer, default=0)   
    is_locked = Column(Boolean, default=False)
    unlock_key = Column(String, nullable=True)
    check_frequency = Column(Integer, default=72)
    last_active = Column(DateTime(timezone=True), default=func.now())
    status = Column(String, default='active') 
    will_content = Column(Text, nullable=True) 
    will_type = Column(String, default='text') 
    will_recipients = Column(String, default="") 

class Will(Base):
    __tablename__ = 'wills'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.chat_id'), index=True)
    content = Column(Text) 
    msg_type = Column(String) 
    recipient_ids = Column(String, default="") 
    created_at = Column(DateTime(timezone=True), default=func.now())

class EmergencyContact(Base):
    __tablename__ = 'contacts'
    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_chat_id = Column(BigInteger, ForeignKey('users.chat_id'), index=True)
    contact_chat_id = Column(BigInteger)
    contact_name = Column(String)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# --- 3. 辅助函数 ---

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def generate_unlock_key() -> str:
    return ''.join(random.choices(string.digits, k=6))

def encrypt_data(data: str) -> str:
    if not data: return None
    return cipher_suite.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data: str) -> str:
    if not encrypted_data: return None
    try:
        return cipher_suite.decrypt(encrypted_data.encode()).decode()
    except Exception:
        return "[数据无法解密]"

async def auto_delete_message(context, chat_id, message_id, delay=1):
    try:
        await asyncio.sleep(delay)
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def get_db_user(session, chat_id, username=None):
    stmt = select(User).where(User.chat_id == chat_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        user = User(chat_id=chat_id, username=username)
        session.add(user)
    elif username:
        user.username = username
    return user

async def get_contacts(session, owner_id):
    stmt = select(EmergencyContact).where(EmergencyContact.owner_chat_id == owner_id)
    result = await session.execute(stmt)
    return result.scalars().all()

async def get_contact_count(session, owner_id):
    stmt = select(func.count()).where(EmergencyContact.owner_chat_id == owner_id)
    result = await session.execute(stmt)
    return result.scalar()

async def get_wills(session, user_id):
    stmt = select(Will).where(Will.user_id == user_id).order_by(Will.created_at)
    result = await session.execute(stmt)
    return result.scalars().all()

# --- 4. UI 定义 (恢复专业文案) ---

BTN_SAFE = "🟢 我很安全"
BTN_CONTACTS = "👥 紧急联系人"
BTN_WILLS = "📜 遗嘱库管理"
BTN_SETTINGS = "⚙️ 触发频率"
BTN_SECURITY = "🛡️ 开源审计"

def get_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_SAFE],
            [BTN_WILLS, BTN_CONTACTS],
            [BTN_SETTINGS, BTN_SECURITY]
        ],
        resize_keyboard=True,
        is_persistent=True, 
        input_field_placeholder="LifeSignal 运行中..."
    )

(
    STATE_SET_PASSWORD,         
    STATE_VERIFY_PASSWORD,      
    STATE_ADD_WILL_CONTENT,     
    STATE_ADD_WILL_RECIPIENTS,  
    STATE_UNLOCK_SELECT_USER,   
    STATE_UNLOCK_VERIFY_KEY     
) = range(6)

CTX_NEXT_ACTION = 'next_action'
CTX_UNLOCK_TARGET = 'unlock_target_id'

# --- 5. 全局熔断拦截器 ---

async def global_lock_interceptor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user: return 

    if update.message:
        context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 0))

    try:
        async with AsyncSessionLocal() as session:
            db_user = await get_db_user(session, user.id)
            
            if db_user.is_locked:
                key_display = db_user.unlock_key if db_user.unlock_key else "ERROR"
                
                alert_text = (
                    "⛔️ **安全熔断机制已触发 (Security Lockdown)**\n\n"
                    "系统检测到多次未授权的访问尝试，账户已执行保护性冻结。\n"
                    "在此状态下，所有指令（含报平安）均被拒绝执行。\n\n"
                    "🔐 **社会化恢复流程 (Social Recovery)**：\n"
                    "1. 请通过线下渠道联系您的 **紧急联系人**。\n"
                    f"2. 告知对方此恢复密钥：`{key_display}`\n"
                    "3. 对方需发送 `/unlock` 命令并输入该密钥以解除熔断。"
                )
                
                if update.callback_query:
                    await update.callback_query.answer("⛔️ 拒绝访问：账户已锁定", show_alert=True)
                    msg = await context.bot.send_message(user.id, alert_text, parse_mode=ParseMode.MARKDOWN)
                    context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 30))
                elif update.message:
                    msg = await update.message.reply_text(alert_text, parse_mode=ParseMode.MARKDOWN)
                    context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 30))
                
                raise ApplicationHandlerStop
    except: pass

# --- 6. 密码验证逻辑 ---

async def request_password_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    context.application.create_task(auto_delete_message(context, user_id, update.message.message_id, 1))
    
    if text == BTN_WILLS: context.user_data[CTX_NEXT_ACTION] = 'wills'
    elif text == BTN_CONTACTS: context.user_data[CTX_NEXT_ACTION] = 'contacts'
    elif text == BTN_SETTINGS: context.user_data[CTX_NEXT_ACTION] = 'settings'
    
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        if not user.password_hash:
            msg = await update.message.reply_text("⚠️ **未检测到主密码**\n为保障数据安全，首次使用请点击 /start 初始化加密环境。")
            context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 10))
            return ConversationHandler.END
    
    prompt = await update.message.reply_text("🔐 **身份鉴权**\n\n请输入您的主密码以解密访问权限：")
    context.application.create_task(auto_delete_message(context, user_id, prompt.message_id, 30))
    return STATE_VERIFY_PASSWORD

async def handle_password_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = update.effective_user.id
    input_pwd = msg.text
    context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 0))
    
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        
        if hash_password(input_pwd) == user.password_hash:
            user.login_attempts = 0
            await session.commit()
            
            action = context.user_data.get(CTX_NEXT_ACTION)
            if action == 'wills': await show_will_menu(update, context)
            elif action == 'contacts': await show_contacts_menu(update, context)
            elif action == 'settings': await show_freq_menu(update, context)
            return ConversationHandler.END
        else:
            user.login_attempts += 1
            if user.login_attempts >= 5:
                user.is_locked = True
                user.unlock_key = generate_unlock_key()
                await session.commit()
                
                warn = await msg.reply_text("⛔️ **鉴权失败次数过多，账户已熔断！**")
                context.application.create_task(auto_delete_message(context, user_id, warn.message_id, 15))
                await broadcast_lockout(context, user_id, session)
                return ConversationHandler.END
            else:
                await session.commit()
                retry_msg = await msg.reply_text(f"❌ **密码错误**\n剩余重试机会：{5 - user.login_attempts}")
                context.application.create_task(auto_delete_message(context, user_id, retry_msg.message_id, 5))
                return STATE_VERIFY_PASSWORD

async def broadcast_lockout(context, user_id, session):
    contacts = await get_contacts(session, user_id)
    if not contacts: return
    for c in contacts:
        try: await context.bot.send_message(c.contact_chat_id, f"🚨 **安全警报**\n\n用户 ID `{user_id}` 账户已触发安全熔断。\n\n如果是本人操作，请等待对方提供**恢复密钥**，然后使用 `/unlock` 命令协助恢复。", parse_mode=ParseMode.MARKDOWN)
        except: pass

# --- 7. 解锁流程 ---

async def start_remote_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    executor_id = update.effective_user.id
    context.application.create_task(auto_delete_message(context, executor_id, update.message.message_id, 1))
    
    async with AsyncSessionLocal() as session:
        stmt = select(EmergencyContact).where(EmergencyContact.contact_chat_id == executor_id)
        entrustments = (await session.execute(stmt)).scalars().all()
        
        if not entrustments:
            msg = await update.message.reply_text("⚠️ 访问拒绝：您未持有任何有效的紧急委托。")
            context.application.create_task(auto_delete_message(context, executor_id, msg.message_id, 5))
            return ConversationHandler.END

        locked_users = []
        for ent in entrustments:
            user = await session.get(User, ent.owner_chat_id)
            if user and user.is_locked:
                locked_users.append(user)
        
        if not locked_users:
            msg = await update.message.reply_text("✅ 状态正常：您委托列表中的所有账户均处于活跃状态。")
            context.application.create_task(auto_delete_message(context, executor_id, msg.message_id, 5))
            return ConversationHandler.END
        
        keyboard = []
        for u in locked_users:
            name = u.username or f"ID {u.chat_id}"
            keyboard.append([InlineKeyboardButton(f"🔓 解锁账户: {name}", callback_data=f"select_locked_{u.chat_id}")])
        
        await update.message.reply_text(f"🚨 **检测到 {len(locked_users)} 个熔断账户**\n请选择需要恢复的目标：", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return STATE_UNLOCK_SELECT_USER

async def handle_locked_user_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data[CTX_UNLOCK_TARGET] = int(query.data.split("_")[2])
    await query.edit_message_text(f"🛡️ **双重验证 (2FA)**\n\n请输入用户提供的 **6位数字恢复密钥**：", parse_mode=ParseMode.MARKDOWN)
    return STATE_UNLOCK_VERIFY_KEY

async def verify_unlock_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    input_key = msg.text.strip()
    executor_id = update.effective_user.id
    target_id = context.user_data.get(CTX_UNLOCK_TARGET)
    context.application.create_task(auto_delete_message(context, executor_id, msg.message_id, 0))
    
    async with AsyncSessionLocal() as session:
        target_user = await get_db_user(session, target_id)
        
        if input_key == target_user.unlock_key:
            target_user.is_locked = False
            target_user.login_attempts = 0
            target_user.unlock_key = None
            target_user.password_hash = None # 强制重置
            await session.commit()
            
            await msg.reply_text("✅ **恢复成功**\n目标账户已解锁，并强制要求重置主密码。")
            try: await context.bot.send_message(target_id, f"🎉 **账户已恢复**\n\n经紧急联系人 **{update.effective_user.first_name}** 验证，您的账户限制已解除。\n\n⚠️ **安全警告**：旧密码已失效，请立即点击 `/start` 重新配置安全环境。", reply_markup=get_main_menu())
            except: pass
            return ConversationHandler.END
        else:
            await msg.reply_text("❌ **验证失败**\n密钥无效。")
            return ConversationHandler.END

# --- 8. 启动与设置 ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 1))
    
    async with AsyncSessionLocal() as session:
        db_user = await get_db_user(session, user.id, user.username)
        
        if context.args and context.args[0].startswith("connect_"):
            target_id = int(context.args[0].split("_")[1])
            if target_id == user.id:
                await update.message.reply_text("❌ 配置错误：无法绑定自身。")
                return
            exists = (await session.execute(select(EmergencyContact).where(EmergencyContact.owner_chat_id==target_id, EmergencyContact.contact_chat_id==user.id))).scalar()
            if exists:
                await update.message.reply_text("✅ 配置已存在。")
                return
            
            kb = [[InlineKeyboardButton("✅ 接受委托", callback_data=f"accept_bind_{target_id}"), InlineKeyboardButton("🚫 拒绝", callback_data="decline_bind")]]
            await update.message.reply_text(f"🛡️ **收到紧急联系人委托**\n用户 ID `{target_id}` 请求将您设为紧急联系人。", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
            return

        if not db_user.password_hash:
            await update.message.reply_text("👋 **欢迎使用 LifeSignal**\n\n检测到新环境（或密码已重置）。\n为了确保数据安全，请设置一个新的 **主密码**：")
            return STATE_SET_PASSWORD
        
        # 专业版欢迎语
        welcome = (
            f"👋 **LifeSignal 守护程序运行中**\n\n"
            "**当前状态**：✅ 监控中\n"
            "**加密协议**：AES-128\n\n"
            "📌 **核心功能**：\n"
            "1. **死人开关 (Dead Man's Switch)**：定期确认存活状态。\n"
            "2. **数字遗嘱保险箱**：去中心化存储加密遗嘱。\n"
            "3. **隐私保护**：交互痕迹自动销毁。\n\n"
            "👇 **请通过下方控制台操作：**"
        )
        await update.message.reply_markdown(welcome, reply_markup=get_main_menu())
        return ConversationHandler.END

async def set_password_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text
    context.application.create_task(auto_delete_message(context, update.effective_user.id, update.message.message_id, 0))
    async with AsyncSessionLocal() as session:
        u = await get_db_user(session, update.effective_user.id)
        u.password_hash = hash_password(pwd)
        await session.commit()
    await update.message.reply_text("✅ **主密码已更新**\n请务必牢记。系统已准备就绪。", reply_markup=get_main_menu())
    return ConversationHandler.END

# --- 9. 功能菜单 ---

async def show_will_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        wills = await get_wills(session, user_id)
        keyboard = []
        if wills:
            for w in wills:
                try:
                    decrypted = decrypt_data(w.content)
                    preview = decrypted[:10] + ".." if w.msg_type == 'text' else f"[{w.msg_type}]"
                except: preview = "Error"
                keyboard.append([InlineKeyboardButton(f"📄 {preview}", callback_data=f"view_will_{w.id}")])
        
        keyboard.append([InlineKeyboardButton("➕ 录入新遗嘱", callback_data="add_will_start")])
        text = f"📜 **加密遗嘱库**\n当前存储：{len(wills)} 条记录。\n支持独立分配每一条遗嘱的接收对象。"
        msg = await context.bot.send_message(user_id, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 60))

async def show_contacts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        contacts = await get_contacts(session, user_id)
        keyboard = []
        for c in contacts:
            keyboard.append([InlineKeyboardButton(f"👤 {c.contact_name}", callback_data="noop"), InlineKeyboardButton("❌ 解除绑定", callback_data=f"try_unbind_{c.id}")])
        if len(contacts) < 10:
            keyboard.append([InlineKeyboardButton("➕ 生成绑定邀请函", switch_inline_query="invite")])
        
        text = f"👥 **紧急联系人列表 ({len(contacts)}/10)**\n这些用户将在触发机制激活时收到通知。"
        msg = await context.bot.send_message(user_id, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 60))

async def show_freq_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    keyboard = [[InlineKeyboardButton("1 天 (24h)", callback_data="set_freq_24"), InlineKeyboardButton("3 天 (72h)", callback_data="set_freq_72"), InlineKeyboardButton("7 天 (168h)", callback_data="set_freq_168")]]
    msg = await context.bot.send_message(user_id, "⚙️ **配置触发阈值**\n若在此时间段内未收到“报平安”指令，系统将判定为失联。", reply_markup=InlineKeyboardMarkup(keyboard))
    context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 60))

# --- 10. 回调处理 ---

async def handle_global_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data.startswith("view_will_"):
        will_id = int(data.split("_")[2])
        keyboard = [[InlineKeyboardButton("👁 解密查看", callback_data=f"reveal_{will_id}")], [InlineKeyboardButton("🗑 销毁", callback_data=f"del_will_{will_id}")]]
        await query.edit_message_text(f"📄 **遗嘱 #{will_id} 操作面板**", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("reveal_"):
        will_id = int(data.split("_")[1])
        async with AsyncSessionLocal() as session:
            will = await session.get(Will, will_id)
            if will:
                content = decrypt_data(will.content)
                text = f"🔐 **解密内容 (15s销毁)**:\n{content}" if will.msg_type == 'text' else f"🔐 媒体文件ID: {content}"
                m = await query.message.reply_text(text)
                context.application.create_task(auto_delete_message(context, user_id, m.message_id, 15))

    elif data.startswith("del_will_"):
        will_id = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Will).where(Will.id == will_id))
            await session.commit()
        await query.edit_message_text("✅ 记录已销毁。")

    elif data.startswith("try_unbind_"):
        cid = int(data.split("_")[2])
        kb = [[InlineKeyboardButton("⚠️ 确认解除", callback_data=f"do_unbind_{cid}"), InlineKeyboardButton("取消", callback_data="cancel_cb")]]
        await query.edit_message_text("⚠️ **高危操作确认**\n解除绑定后，该联系人将不再接收任何通知。", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    elif data.startswith("do_unbind_"):
        cid = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            c = await session.get(EmergencyContact, cid)
            if c:
                try: await context.bot.send_message(c.contact_chat_id, "ℹ️ 系统通知：您的紧急联系人权限已被撤销。")
                except: pass
                await session.delete(c)
                await session.commit()
        await query.edit_message_text("✅ 绑定已解除。")

    elif data.startswith("set_freq_"):
        hours = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            u = await get_db_user(session, user_id)
            u.check_frequency = hours
            await session.commit()
        await query.edit_message_text(f"✅ 参数已更新：**{int(hours/24)} 天**。")

    elif data == "cancel_cb":
        await query.edit_message_text("操作已中止。")

# --- 11. 添加遗嘱 ---

async def start_add_will(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📝 **请录入遗嘱内容**\n(支持文本/图片/视频，发送后立即加密并销毁原消息)")
    return STATE_ADD_WILL_CONTENT

async def receive_will_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    context.application.create_task(auto_delete_message(context, msg.chat_id, msg.message_id, 15))
    if msg.text and msg.text.startswith(("/", "🟢", "⚙️")): return ConversationHandler.END

    content, w_type = None, 'text'
    if msg.text: content, w_type = encrypt_data(msg.text), 'text'
    elif msg.photo: content, w_type = encrypt_data(msg.photo[-1].file_id), 'photo'
    elif msg.video: content, w_type = encrypt_data(msg.video.file_id), 'video'
    elif msg.voice: content, w_type = encrypt_data(msg.voice.file_id), 'voice'
    else: return STATE_ADD_WILL_CONTENT

    context.user_data['temp_content'] = content
    context.user_data['temp_type'] = w_type
    context.user_data['selected'] = []
    
    return await render_recipient_selector(update, context)

async def render_recipient_selector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        contacts = await get_contacts(session, user_id)
        selected = context.user_data.get('selected', [])
        kb = []
        for c in contacts:
            mark = "✅" if c.contact_chat_id in selected else "⭕️"
            kb.append([InlineKeyboardButton(f"{mark} {c.contact_name}", callback_data=f"sel_rec_{c.contact_chat_id}")])
        kb.append([InlineKeyboardButton(f"💾 保存 ({len(selected)}人)", callback_data="save_new_will")])
        
        text = "👥 **指定接收对象**\n(点击名称进行勾选)"
        if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        else:
            m = await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
            context.application.create_task(auto_delete_message(context, user_id, m.message_id, 60))
    return STATE_ADD_WILL_RECIPIENTS

async def handle_recipient_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("sel_rec_"):
        cid = int(data.split("_")[2])
        sel = context.user_data.get('selected', [])
        if cid in sel: sel.remove(cid)
        else: sel.append(cid)
        context.user_data['selected'] = sel
        return await render_recipient_selector(update, context)
    
    if data == "save_new_will":
        rec_str = ",".join(map(str, context.user_data.get('selected', [])))
        async with AsyncSessionLocal() as session:
            will = Will(user_id=update.effective_user.id, content=context.user_data['temp_content'], msg_type=context.user_data['temp_type'], recipient_ids=rec_str)
            session.add(will)
            await session.commit()
        await query.edit_message_text("✅ 遗嘱已加密归档。")
        return ConversationHandler.END

# --- 12. 杂项 ---

async def handle_im_safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 1))
    
    async with AsyncSessionLocal() as session:
        u = await get_db_user(session, user.id)
        if u.is_locked: return

        contacts = await get_contacts(session, user.id)
        if not contacts:
            msg = await update.message.reply_text("⚠️ **未激活保护**\n请先绑定至少一位紧急联系人。", reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN)
            context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 5))
            return
        u.last_active = datetime.now(timezone.utc)
        u.status = 'active'
        await session.commit()
    msg = await update.message.reply_text("✅ **状态更新**：已确认安全，计时器重置。", reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN)
    context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 5))

async def confirm_bind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "decline_bind":
        await query.edit_message_text("已拒绝。")
        return
    requester_id = int(query.data.split("_")[2])
    async with AsyncSessionLocal() as session:
        session.add(EmergencyContact(owner_chat_id=requester_id, contact_chat_id=update.effective_user.id, contact_name=update.effective_user.first_name))
        await get_db_user(session, update.effective_user.id)
        await session.commit()
    await query.edit_message_text("✅ 绑定成功。")
    try: await context.bot.send_message(requester_id, "🎉 对方已确认委托，绑定成功。")
    except: pass

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query: await update.callback_query.message.edit_text("操作已取消。")
    else: await update.message.reply_text("操作已取消。", reply_markup=get_main_menu())
    return ConversationHandler.END

async def inline_query_handler(update, context):
    query = update.inline_query.query
    if query == "invite":
        link = f"https://t.me/{context.bot.username}?start=connect_{update.effective_user.id}"
        results = [InlineQueryResultArticle(id=str(uuid4()), title="发送绑定邀请", input_message_content=InputTextMessageContent(f"📩 **LifeSignal 紧急委托**\n\n来自 {update.effective_user.first_name} 的安全托管请求。\n我希望将您设为我的紧急联系人。\n\n👇 **点击下方按钮确认：**", parse_mode=ParseMode.MARKDOWN), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ 接受委托", url=link)]]))]
        await update.inline_query.answer(results)

async def handle_security(update, context):
    context.application.create_task(auto_delete_message(context, update.effective_chat.id, update.message.message_id, 1))
    text = "🛡️ **透明性与安全性验证**\n\n本系统代码完全开源，且数据采用 AES-128 高强度加密。\n您可以点击下方链接进行审计。"
    kb = [
        [InlineKeyboardButton("👨‍💻 GitHub 源码审计", url=GITHUB_REPO_URL)],
        [InlineKeyboardButton("🔍 VirusTotal 安全检测", url="https://www.virustotal.com/gui/home/url")]
    ]
    await update.message.reply_markdown(text, reply_markup=InlineKeyboardMarkup(kb))

async def check_dead_mans_switch(app):
    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.status == 'active')
        result = await session.execute(stmt)
        users = result.scalars().all()
        now = datetime.now(timezone.utc)
        
        for user in users:
            last = user.last_active.replace(tzinfo=timezone.utc) if user.last_active.tzinfo is None else user.last_active
            delta_hours = (now - last).total_seconds() / 3600
            
            if delta_hours > user.check_frequency:
                contacts = await get_contacts(session, user.chat_id)
                if contacts:
                    wills = await get_wills(session, user.chat_id)
                    for c in contacts:
                        try:
                            await app.bot.send_message(chat_id=c.contact_chat_id, text=f"🚨 **LifeSignal 紧急通告**\n\n监测到用户 @{user.username or user.chat_id} 已失联。", parse_mode=ParseMode.MARKDOWN)
                            if wills:
                                for w in wills:
                                    if w.recipient_ids and str(c.contact_chat_id) in w.recipient_ids.split(","):
                                        content = decrypt_data(w.content)
                                        if w.msg_type=='text': await app.bot.send_message(c.contact_chat_id, f"🔐 **加密遗嘱内容**:\n{content}", parse_mode=ParseMode.MARKDOWN)
                                        elif w.msg_type=='photo': await app.bot.send_photo(c.contact_chat_id, content, caption="🔐 加密图片")
                                        elif w.msg_type=='video': await app.bot.send_video(c.contact_chat_id, content, caption="🔐 加密视频")
                                        elif w.msg_type=='voice': await app.bot.send_voice(c.contact_chat_id, content, caption="🔐 加密语音")
                        except: pass
                    user.status = 'inactive'
                    session.add(user)
            elif delta_hours > (user.check_frequency * 0.8):
                try: 
                    left_hours = int(user.check_frequency - delta_hours)
                    await app.bot.send_message(user.chat_id, f"⏰ **安全确认**\n请点击“🟢 我很安全”重置计时。\n距离触发机制还剩约 {left_hours} 小时。", reply_markup=get_main_menu())
                except: pass
        await session.commit()

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

def main():
    persistence = PicklePersistence(filepath='persistence.pickle')
    app = Application.builder().token(TOKEN).persistence(persistence).build()

    app.add_handler(MessageHandler(filters.ALL, global_lock_interceptor), group=-1)
    app.add_handler(CallbackQueryHandler(global_lock_interceptor), group=-1)

    auth_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^({BTN_WILLS}|{BTN_CONTACTS}|{BTN_SETTINGS})$"), request_password_entry)],
        states={STATE_VERIFY_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password_verification)]},
        fallbacks=[CommandHandler("cancel", cancel_action)],
        name="auth_gw", persistent=True
    )

    unlock_handler = ConversationHandler(
        entry_points=[CommandHandler("unlock", start_remote_unlock)],
        states={
            STATE_UNLOCK_SELECT_USER: [CallbackQueryHandler(handle_locked_user_selection, pattern="^select_locked_")],
            STATE_UNLOCK_VERIFY_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, verify_unlock_key)]
        },
        fallbacks=[CommandHandler("cancel", cancel_action)],
        name="unlock_flow", persistent=True
    )

    add_will_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add_will, pattern="^add_will_start$")],
        states={
            STATE_ADD_WILL_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_will_content)],
            STATE_ADD_WILL_RECIPIENTS: [CallbackQueryHandler(handle_recipient_toggle)]
        },
        fallbacks=[CommandHandler("cancel", cancel_action)],
        name="add_will", persistent=True
    )

    setup_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={STATE_SET_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_password_finish)]},
        fallbacks=[],
        name="setup"
    )

    app.add_handler(setup_handler)
    app.add_handler(auth_handler)
    app.add_handler(unlock_handler)
    app.add_handler(add_will_handler)
    
    app.add_handler(CommandHandler("unlock", lambda u,c: u.message.reply_text("请点击菜单或重新输入。")))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SAFE}$"), handle_im_safe))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SECURITY}$"), handle_security))
    
    app.add_handler(CallbackQueryHandler(handle_global_callbacks, pattern="^(view_|reveal_|del_|try_|do_|set_freq_|cancel)"))
    app.add_handler(CallbackQueryHandler(confirm_bind_callback, pattern="^accept_bind_"))
    
    app.add_handler(InlineQueryHandler(inline_query_handler))

    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_dead_mans_switch, 'interval', hours=1, args=[app])
    scheduler.start()
    
    print("🚀 死了么LifeSignal Final Stable is running...")
    app.run_polling()

if __name__ == '__main__':
    main()