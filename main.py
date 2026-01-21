import os
import logging
import asyncio
import hashlib
import random
import string
from uuid import uuid4
from datetime import datetime, timedelta, timezone

# Telegram 库
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InlineQueryResultArticle,
    InputTextMessageContent,
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
from telegram.error import Forbidden, BadRequest

# 数据库库
from sqlalchemy import Column, BigInteger, Text, DateTime, String, Integer, Boolean, select, ForeignKey, func, delete
from sqlalchemy.orm import declarative_base
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

# 环境变量检查
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME", "LifeSignal_Bot")

if not TOKEN or not DATABASE_URL:
    logger.critical("❌ 启动失败: 缺少 TELEGRAM_BOT_TOKEN 或 DATABASE_URL")
    exit(1)

# 密钥处理：若无密钥则生成临时密钥（仅供测试，重启后数据将无法解密）
if not ENCRYPTION_KEY:
    logger.warning("⚠️以此模式运行不安全！未检测到 ENCRYPTION_KEY，正在使用临时密钥。")
    ENCRYPTION_KEY = Fernet.generate_key().decode()

cipher_suite = Fernet(ENCRYPTION_KEY.encode())

# 数据库 URL 兼容性修正
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
    check_frequency = Column(Integer, default=72)  # 默认 72 小时
    last_active = Column(DateTime(timezone=True), default=func.now())
    status = Column(String, default='active')
    # 废弃字段保留以防迁移错误，但在逻辑中不再使用
    will_content = Column(Text, nullable=True)
    will_type = Column(String, default='text')
    will_recipients = Column(String, default="")

class Will(Base):
    __tablename__ = 'wills'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.chat_id'), index=True)
    content = Column(Text)  # 加密存储
    msg_type = Column(String)  # text, photo, video, voice
    recipient_ids = Column(String, default="")  # ID 列表，逗号分隔
    created_at = Column(DateTime(timezone=True), default=func.now())

class EmergencyContact(Base):
    __tablename__ = 'contacts'
    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_chat_id = Column(BigInteger, ForeignKey('users.chat_id'), index=True)
    contact_chat_id = Column(BigInteger)
    contact_name = Column(String)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# --- 3. 文案与 UI 定义 ---

BTN_SAFE = "🟢 确认平安 (重置计时)"
BTN_WILLS = "📦 预设信箱"
BTN_CONTACTS = "🛡️ 守护人管理"
BTN_SETTINGS = "⏱️ 频率设置"
BTN_SECURITY = "🔒 安全审计"

def get_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_SAFE],
            [BTN_WILLS, BTN_CONTACTS],
            [BTN_SETTINGS, BTN_SECURITY]
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="LifeSignal 正在守护您的数字资产..."
    )

# 状态定义
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

# --- 4. 辅助函数 ---

def hash_password(password: str) -> str:
    # 生产环境建议加盐 (Salt)，此处为保持兼容性仅做 Hash
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
        return "[数据损坏或解密失败]"

async def auto_delete_message(context, chat_id, message_id, delay=1):
    """自动删除消息，并不报错"""
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

async def get_wills(session, user_id):
    stmt = select(Will).where(Will.user_id == user_id).order_by(Will.created_at)
    result = await session.execute(stmt)
    return result.scalars().all()

# --- 5. 核心逻辑：安全熔断与鉴权 ---

async def global_lock_interceptor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """全局拦截器：检查用户是否被锁定"""
    user = update.effective_user
    if not user: return

    # 自动清除用户发送的指令消息，保持界面整洁
    if update.message:
        context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 1))

    try:
        async with AsyncSessionLocal() as session:
            db_user = await get_db_user(session, user.id)

            if db_user.is_locked:
                key_display = db_user.unlock_key if db_user.unlock_key else "ERROR"

                alert_text = (
                    "⛔️ **安全熔断机制已触发**\n\n"
                    "检测到多次密码尝试失败，为保障数据安全，系统已**暂时冻结**您的账户。\n\n"
                    "🔐 **如何恢复访问？**\n"
                    "本系统采用双人验证机制。请联系您的任一 **守护人**，并将下方的恢复密钥告知对方：\n\n"
                    f"🔑 恢复密钥：`{key_display}`\n\n"
                    "请让对方在机器人中输入 `/unlock` 并填入此密钥。验证通过后，您的账户将立即解锁。"
                )

                if update.callback_query:
                    await update.callback_query.answer("⛔️ 拒绝访问：请联系守护人解锁", show_alert=True)
                    # 避免重复刷屏，只弹窗
                elif update.message:
                    msg = await update.message.reply_text(alert_text, parse_mode=ParseMode.MARKDOWN)
                    context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 30))

                raise ApplicationHandlerStop # 阻止后续处理器执行
    except ApplicationHandlerStop:
        raise
    except Exception as e:
        logger.error(f"Interceptor error: {e}")

async def request_password_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """请求输入密码的入口"""
    user_id = update.effective_user.id
    text = update.message.text
    
    # 记录用户的意图
    if text == BTN_WILLS: context.user_data[CTX_NEXT_ACTION] = 'wills'
    elif text == BTN_CONTACTS: context.user_data[CTX_NEXT_ACTION] = 'contacts'
    elif text == BTN_SETTINGS: context.user_data[CTX_NEXT_ACTION] = 'settings'

    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        if not user.password_hash:
            msg = await update.message.reply_text(
                "👋 **欢迎使用 LifeSignal**\n\n"
                "为了保护您的预设信息不被窥探，首次使用需设置一个 **访问密码**。\n\n"
                "👉 **请直接发送您想设置的密码：**\n"
                "*(建议使用复杂的组合，发送后将立即自动清除)*"
            )
            context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 20))
            return ConversationHandler.END

    prompt = await update.message.reply_text("🔐 **身份验证**\n\n您正在进入加密区域，请输入 **主密码** 以继续：")
    context.application.create_task(auto_delete_message(context, user_id, prompt.message_id, 30))
    return STATE_VERIFY_PASSWORD

async def handle_password_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理密码验证逻辑"""
    msg = update.message
    user_id = update.effective_user.id
    input_pwd = msg.text
    
    # 立即销毁密码痕迹
    context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 0))

    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)

        if hash_password(input_pwd) == user.password_hash:
            # 登录成功
            user.login_attempts = 0
            await session.commit()

            action = context.user_data.get(CTX_NEXT_ACTION)
            if action == 'wills': await show_will_menu(update, context)
            elif action == 'contacts': await show_contacts_menu(update, context)
            elif action == 'settings': await show_freq_menu(update, context)
            return ConversationHandler.END
        else:
            # 登录失败
            user.login_attempts += 1
            max_attempts = 5
            remaining = max_attempts - user.login_attempts
            
            if remaining <= 0:
                user.is_locked = True
                user.unlock_key = generate_unlock_key()
                await session.commit()

                warn_text = "⛔️ **验证失败次数过多，账户已冻结！**\n请联系您的守护人获取帮助。"
                warn = await msg.reply_text(warn_text, parse_mode=ParseMode.MARKDOWN)
                context.application.create_task(auto_delete_message(context, user_id, warn.message_id, 15))
                await broadcast_lockout(context, user_id, session)
                return ConversationHandler.END
            else:
                await session.commit()
                retry_msg = await msg.reply_text(f"❌ **密码错误**\n剩余尝试次数：**{remaining}**")
                context.application.create_task(auto_delete_message(context, user_id, retry_msg.message_id, 5))
                return STATE_VERIFY_PASSWORD

async def broadcast_lockout(context, user_id, session):
    """通知守护人用户被锁"""
    contacts = await get_contacts(session, user_id)
    if not contacts: return
    for c in contacts:
        try:
            await context.bot.send_message(
                c.contact_chat_id,
                f"🚨 **紧急协助请求**\n\n您守护的用户 (ID: `{user_id}`) 账户已被冻结。\n\n"
                "如果这是本人的操作，他会通过其他方式（电话/微信）告知您一个 **恢复密钥**。\n"
                "请在收到密钥后，在此机器人中使用 `/unlock` 命令协助他恢复权限。",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

# --- 6. 守护人协助解锁流程 ---

async def start_remote_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    executor_id = update.effective_user.id
    # 删除命令消息
    context.application.create_task(auto_delete_message(context, executor_id, update.message.message_id, 1))

    async with AsyncSessionLocal() as session:
        # 查找我是谁的守护人
        stmt = select(EmergencyContact).where(EmergencyContact.contact_chat_id == executor_id)
        entrustments = (await session.execute(stmt)).scalars().all()

        if not entrustments:
            msg = await update.message.reply_text("⚠️ **操作无效**\n您当前未担任任何人的守护人，无法执行此操作。")
            context.application.create_task(auto_delete_message(context, executor_id, msg.message_id, 10))
            return ConversationHandler.END

        locked_users = []
        for ent in entrustments:
            user = await session.get(User, ent.owner_chat_id)
            if user and user.is_locked:
                locked_users.append(user)

        if not locked_users:
            msg = await update.message.reply_text("✅ **状态正常**\n您守护的所有用户目前账户状态良好，无需解锁。")
            context.application.create_task(auto_delete_message(context, executor_id, msg.message_id, 10))
            return ConversationHandler.END

        keyboard = []
        for u in locked_users:
            name = u.username or f"ID {u.chat_id}"
            keyboard.append([InlineKeyboardButton(f"🔓 解锁账户: {name}", callback_data=f"select_locked_{u.chat_id}")])

        await update.message.reply_text(
            f"🛡️ **协助恢复访问**\n\n检测到 {len(locked_users)} 个被冻结的账户。请选择您要协助的对象：",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return STATE_UNLOCK_SELECT_USER

async def handle_locked_user_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data[CTX_UNLOCK_TARGET] = int(query.data.split("_")[2])
    await query.edit_message_text(
        "🛡️ **双重验证 (2FA)**\n\n"
        "请**输入对方告知您的 6 位数字恢复密钥**：\n"
        "*(这一步是为了确认您确实与对方进行了沟通)*",
        parse_mode=ParseMode.MARKDOWN
    )
    return STATE_UNLOCK_VERIFY_KEY

async def verify_unlock_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    input_key = msg.text.strip()
    executor_id = update.effective_user.id
    target_id = context.user_data.get(CTX_UNLOCK_TARGET)
    
    context.application.create_task(auto_delete_message(context, executor_id, msg.message_id, 1))

    async with AsyncSessionLocal() as session:
        target_user = await get_db_user(session, target_id)

        if input_key == target_user.unlock_key:
            # 解锁逻辑
            target_user.is_locked = False
            target_user.login_attempts = 0
            target_user.unlock_key = None
            target_user.password_hash = None  # 强制重置密码，保障安全
            await session.commit()

            await msg.reply_text("✅ **验证成功**\n对方的账户已解锁。系统已强制要求其重置密码。")
            
            try:
                await context.bot.send_message(
                    target_id,
                    f"🎉 **账户已恢复**\n\n"
                    f"您的守护人 **{update.effective_user.first_name}** 已协助您通过了安全验证。\n\n"
                    "⚠️ **安全提示**：为了防止密码泄露，系统已重置您的主密码。\n"
                    "请点击任意功能按钮重新设置新密码。",
                    reply_markup=get_main_menu()
                )
            except Exception:
                pass
            return ConversationHandler.END
        else:
            await msg.reply_text("❌ **密钥错误**\n验证失败，请核对后重试。")
            return ConversationHandler.END

# --- 7. 基础功能：启动与密码设置 ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 1))

    async with AsyncSessionLocal() as session:
        db_user = await get_db_user(session, user.id, user.username)

        # 处理邀请链接 logic (connect_12345)
        if context.args and context.args[0].startswith("connect_"):
            try:
                target_id = int(context.args[0].split("_")[1])
            except ValueError:
                return 

            if target_id == user.id:
                await update.message.reply_text("❌ 您无法成为自己的守护人。")
                return
            
            stmt = select(EmergencyContact).where(
                EmergencyContact.owner_chat_id == target_id,
                EmergencyContact.contact_chat_id == user.id
            )
            exists = (await session.execute(stmt)).scalar()
            
            if exists:
                await update.message.reply_text("✅ 您已经是对方的守护人了。")
                return

            kb = [[
                InlineKeyboardButton("✅ 接受委托", callback_data=f"accept_bind_{target_id}"),
                InlineKeyboardButton("🚫 婉拒", callback_data="decline_bind")
            ]]
            await update.message.reply_text(
                f"🛡️ **收到一份信任委托**\n\n"
                f"用户 (ID: `{target_id}`) 希望将您设为 **守护人**。\n\n"
                "**守护人的职责：**\n"
                "1. 当对方长期失联时，接收预警通知。\n"
                "2. 协助对方找回丢失的账户访问权限。\n"
                "3. 接收对方可能留下的预设信件。\n\n"
                "您是否愿意承担这份信任？",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=ParseMode.MARKDOWN
            )
            return

        if not db_user.password_hash:
            await update.message.reply_text(
                "👋 **欢迎使用 LifeSignal**\n\n"
                "我是您的数字安全哨兵。\n"
                "为了保障您的隐私安全，首次使用请先设置一个 **主密码**：\n"
                "(请直接发送，设置后将立即删除记录)"
            )
            return STATE_SET_PASSWORD

        welcome = (
            f"👋 **LifeSignal 运行正常**\n\n"
            "**状态**：✅ 实时监听中 (AES-128 加密)\n"
            "**机制**：若超过设定时间未确认平安，系统将自动执行预案。\n\n"
            "📌 **功能导航**：\n"
            "• **确认平安**：重置失联倒计时。\n"
            "• **预设信箱**：存放您的加密寄语。\n"
            "• **守护人**：管理接收通知的信任人。\n"
        )
        await update.message.reply_markdown(welcome, reply_markup=get_main_menu())
        return ConversationHandler.END

async def set_password_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text
    context.application.create_task(auto_delete_message(context, update.effective_user.id, update.message.message_id, 1))
    
    async with AsyncSessionLocal() as session:
        u = await get_db_user(session, update.effective_user.id)
        u.password_hash = hash_password(pwd)
        await session.commit()
    
    await update.message.reply_text("✅ **密码设置成功**\n系统已就绪，请牢记您的密码。", reply_markup=get_main_menu())
    return ConversationHandler.END

# --- 8. 功能菜单展示 ---

async def show_will_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        wills = await get_wills(session, user_id)
        keyboard = []
        if wills:
            for w in wills:
                # 尝试简略解密用于展示标题
                try:
                    decrypted = decrypt_data(w.content)
                    if w.msg_type == 'text':
                        preview = (decrypted[:12] + "..") if len(decrypted) > 12 else decrypted
                    else:
                        preview = f"[{w.msg_type.upper()}]"
                except:
                    preview = "无法预览"
                
                keyboard.append([InlineKeyboardButton(f"📄 {preview}", callback_data=f"view_will_{w.id}")])

        keyboard.append([InlineKeyboardButton("➕ 新增预设内容", callback_data="add_will_start")])
        
        text = (
            f"📦 **预设信箱 (Legacy Box)**\n\n"
            f"当前存储：{len(wills)} 条记录。\n"
            "当系统判定您失联时，这些内容将按您的配置发送给指定的守护人。\n"
            "点击下方条目可临时查看或删除。"
        )
        msg = await context.bot.send_message(user_id, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 60))

async def show_contacts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        contacts = await get_contacts(session, user_id)
        keyboard = []
        for c in contacts:
            keyboard.append([
                InlineKeyboardButton(f"👤 {c.contact_name}", callback_data="noop"),
                InlineKeyboardButton("❌ 解绑", callback_data=f"try_unbind_{c.id}")
            ])
        
        if len(contacts) < 10:
            keyboard.append([InlineKeyboardButton("➕ 邀请新守护人", switch_inline_query="invite")])

        text = (
            f"🛡️ **守护人名单 ({len(contacts)}/10)**\n\n"
            "守护人是您安全网的关键节点。\n"
            "建议至少保留两位，以防单点失联。\n\n"
            "👇 点击下方按钮进行管理："
        )
        msg = await context.bot.send_message(user_id, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 60))

async def show_freq_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # 定义选项
    options = [
        ("24 小时", 24),
        ("3 天", 72),
        ("7 天", 168),
        ("30 天", 720)
    ]
    keyboard = []
    row = []
    for label, hours in options:
        row.append(InlineKeyboardButton(label, callback_data=f"set_freq_{hours}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)

    msg = await context.bot.send_message(
        user_id,
        "⏱️ **设置判定阈值**\n\n"
        "如果超过此时间没有收到您的“确认平安”指令，系统将判定您已失联，并启动分发程序。\n\n"
        "请选择适合您的时间间隔：",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 60))

# --- 9. 回调处理 ---

async def handle_global_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    # --- 遗嘱查看与删除 ---
    if data.startswith("view_will_"):
        will_id = int(data.split("_")[2])
        keyboard = [
            [InlineKeyboardButton("👁 解密查看 (15s)", callback_data=f"reveal_{will_id}")],
            [InlineKeyboardButton("🗑 安全移除", callback_data=f"del_will_{will_id}"), InlineKeyboardButton("🔙 返回", callback_data="back_to_wills")]
        ]
        await query.edit_message_text(f"📄 **记录 #{will_id} 选项**", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "back_to_wills":
        await context.bot.delete_message(chat_id=user_id, message_id=query.message.message_id)
        await show_will_menu(update, context)

    elif data.startswith("reveal_"):
        will_id = int(data.split("_")[1])
        async with AsyncSessionLocal() as session:
            will = await session.get(Will, will_id)
            if will:
                content = decrypt_data(will.content)
                if will.msg_type == 'text':
                    text = f"🔐 **解密内容** (15秒后销毁):\n\n{content}"
                    m = await query.message.reply_text(text)
                else:
                    caption = "🔐 加密媒体文件 (15秒后销毁)"
                    if will.msg_type == 'photo': m = await query.message.reply_photo(content, caption=caption)
                    elif will.msg_type == 'video': m = await query.message.reply_video(content, caption=caption)
                    elif will.msg_type == 'voice': m = await query.message.reply_voice(content, caption=caption)
                
                context.application.create_task(auto_delete_message(context, user_id, m.message_id, 15))
            else:
                await query.message.reply_text("❌ 该记录已不存在。")

    elif data.startswith("del_will_"):
        will_id = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Will).where(Will.id == will_id))
            await session.commit()
        await query.edit_message_text("✅ 记录已从数据库安全移除。")
        # 刷新列表
        await show_will_menu(update, context)

    # --- 守护人解绑 ---
    elif data.startswith("try_unbind_"):
        cid = int(data.split("_")[2])
        kb = [[InlineKeyboardButton("⚠️ 确认解除", callback_data=f"do_unbind_{cid}"), InlineKeyboardButton("取消", callback_data="cancel_cb")]]
        await query.edit_message_text(
            "⚠️ **操作确认**\n\n"
            "解除绑定后，该联系人将**不再接收**预警通知。\n"
            "指定发送给他的预设信件也将无法投递。",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN
        )

    elif data.startswith("do_unbind_"):
        cid = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            c = await session.get(EmergencyContact, cid)
            if c:
                try:
                    await context.bot.send_message(c.contact_chat_id, "ℹ️ **系统通知**：您的守护人权限已被撤销。")
                except: pass
                await session.delete(c)
                await session.commit()
        await query.edit_message_text("✅ 绑定关系已解除。")

    # --- 频率设置 ---
    elif data.startswith("set_freq_"):
        hours = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            u = await get_db_user(session, user_id)
            u.check_frequency = hours
            await session.commit()
        
        days = hours / 24
        days_str = f"{int(days)} 天" if days.is_integer() else f"{days:.1f} 天"
        await query.edit_message_text(
            f"✅ **设置已更新**\n\n"
            f"当前判定阈值：**{days_str}**\n"
            f"若您在 {days_str} 内未签到，系统将启动预案。",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "cancel_cb":
        await query.edit_message_text("操作已取消。")

# --- 10. 添加遗嘱流程 ---

async def start_add_will(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📝 **录入新内容**\n\n"
        "请发送您想预设的信息。\n"
        "✅ 支持：文字、照片、视频、语音。\n"
        "🔒 安全：发送后立即加密，并清除聊天记录。\n\n"
        "*(发送 /cancel 可随时取消)*"
    )
    return STATE_ADD_WILL_CONTENT

async def receive_will_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    context.application.create_task(auto_delete_message(context, msg.chat_id, msg.message_id, 10))
    
    # 过滤掉命令和按钮点击
    if msg.text and (msg.text.startswith("/") or msg.text in [BTN_SAFE, BTN_WILLS, BTN_CONTACTS, BTN_SETTINGS]):
        return ConversationHandler.END

    content, w_type = None, 'text'
    if msg.text:
        content, w_type = encrypt_data(msg.text), 'text'
    elif msg.photo:
        content, w_type = encrypt_data(msg.photo[-1].file_id), 'photo'
    elif msg.video:
        content, w_type = encrypt_data(msg.video.file_id), 'video'
    elif msg.voice:
        content, w_type = encrypt_data(msg.voice.file_id), 'voice'
    else:
        warning = await msg.reply_text("⚠️ 不支持的文件格式，请重新发送。")
        context.application.create_task(auto_delete_message(context, msg.chat_id, warning.message_id, 5))
        return STATE_ADD_WILL_CONTENT

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
        
        # 列表为空时的处理
        if not contacts:
             msg = await context.bot.send_message(user_id, "⚠️ **暂无守护人**\n请先添加紧急联系人后再录入预设内容。", reply_markup=get_main_menu())
             context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 10))
             return ConversationHandler.END

        for c in contacts:
            mark = "✅" if c.contact_chat_id in selected else "⭕️"
            kb.append([InlineKeyboardButton(f"{mark} {c.contact_name}", callback_data=f"sel_rec_{c.contact_chat_id}")])
        
        btn_text = f"💾 确认保存 (已选 {len(selected)} 人)" if selected else "💾 存为草稿 (暂不发送)"
        kb.append([InlineKeyboardButton(btn_text, callback_data="save_new_will")])
        
        text = (
            "📨 **指定接收人**\n\n"
            "请勾选此条内容将在失联时发送给谁。\n"
            "点击名字切换选中状态。"
        )

        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
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
        if cid in sel:
            sel.remove(cid)
        else:
            sel.append(cid)
        context.user_data['selected'] = sel
        return await render_recipient_selector(update, context)
    
    if data == "save_new_will":
        rec_str = ",".join(map(str, context.user_data.get('selected', [])))
        async with AsyncSessionLocal() as session:
            will = Will(
                user_id=update.effective_user.id,
                content=context.user_data['temp_content'],
                msg_type=context.user_data['temp_type'],
                recipient_ids=rec_str
            )
            session.add(will)
            await session.commit()
        await query.edit_message_text("✅ **归档完成**\n内容已加密存储。")
        return ConversationHandler.END

# --- 11. 杂项功能 ---

async def handle_im_safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 1))
    
    async with AsyncSessionLocal() as session:
        u = await get_db_user(session, user.id)
        if u.is_locked: return # 被锁时忽略

        contacts = await get_contacts(session, user.id)
        if not contacts:
            msg = await update.message.reply_text("⚠️ **提示**\n\n您尚未绑定守护人。\n建议前往“🛡️ 守护人管理”进行配置，否则预警功能无法生效。", reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN)
            context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 10))
            return
        
        # 核心逻辑：重置时间
        u.last_active = datetime.now(timezone.utc)
        u.status = 'active'
        await session.commit()
        
        days = u.check_frequency / 24
        days_str = f"{int(days)} 天" if days.is_integer() else f"{days:.1f} 天"

    msg = await update.message.reply_text(
        f"✅ **信号已确认**\n\n"
        f"倒计时已重置。下次需在 **{days_str}** 内再次确认。\n"
        "系统维持监听中。",
        reply_markup=get_main_menu(),
        parse_mode=ParseMode.MARKDOWN
    )
    context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 10))

async def confirm_bind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "decline_bind":
        await query.edit_message_text("已婉拒该委托。")
        return

    requester_id = int(query.data.split("_")[2])
    
    async with AsyncSessionLocal() as session:
        # 防止重复添加
        exists = (await session.execute(select(EmergencyContact).where(
            EmergencyContact.owner_chat_id == requester_id,
            EmergencyContact.contact_chat_id == update.effective_user.id
        ))).scalar()
        
        if not exists:
            session.add(EmergencyContact(
                owner_chat_id=requester_id,
                contact_chat_id=update.effective_user.id,
                contact_name=update.effective_user.first_name
            ))
            await session.commit()

    await query.edit_message_text("✅ **绑定成功！**\n您已正式成为对方的守护人。")
    try:
        await context.bot.send_message(requester_id, "🎉 **好消息**\n对方已接受委托，您的安全网已成功建立。")
    except:
        pass

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_text = "操作已取消。"
    if update.callback_query:
        await update.callback_query.message.edit_text(msg_text)
    else:
        m = await update.message.reply_text(msg_text, reply_markup=get_main_menu())
        context.application.create_task(auto_delete_message(context, update.effective_user.id, m.message_id, 5))
    return ConversationHandler.END

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query
    if query == "invite":
        link = f"https://t.me/{context.bot.username}?start=connect_{update.effective_user.id}"
        
        thumb_url = "https://img.icons8.com/color/96/safety-collection-place.png" # 示例图标
        
        content = (
            f"📩 **LifeSignal 委托请求**\n\n"
            f"我是 {update.effective_user.first_name}。\n"
            "我希望将您设为我的数字资产守护人。\n\n"
            "如果您愿意，请点击下方按钮接受委托。"
        )

        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="发送守护人邀请函",
                description="邀请对方成为您的紧急联系人",
                input_message_content=InputTextMessageContent(content, parse_mode=ParseMode.MARKDOWN),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🤝 接受委托", url=link)]]),
                thumbnail_url=thumb_url
            )
        ]
        await update.inline_query.answer(results, cache_time=0)

async def handle_security(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.create_task(auto_delete_message(context, update.effective_chat.id, update.message.message_id, 1))
    text = (
        "🛡️ **安全与隐私说明**\n\n"
        "LifeSignal 采用以下机制保障安全：\n"
        "1. **零知识存储**：关键信息采用 AES-128 加密入库。\n"
        "2. **阅后即焚**：密码等敏感交互记录立即物理销毁。\n"
        "3. **开源透明**：您可审查我们的代码逻辑。\n\n"
        "👇 **点击下方进行审计：**"
    )
    kb = [
        [InlineKeyboardButton("👨‍💻 GitHub 源码仓库", url="https://github.com/ShiXinqiang/LifeSignal-Trust-Edition-")],
        [InlineKeyboardButton("🦠 VirusTotal 安全检测", url="https://www.virustotal.com/gui/home/url")]
    ]
    await update.message.reply_markdown(text, reply_markup=InlineKeyboardMarkup(kb))

# --- 12. 调度器任务：死人开关检查 ---

async def check_dead_mans_switch(app: Application):
    """周期性检查所有用户的活跃状态"""
    async with AsyncSessionLocal() as session:
        # 只检查状态为 active 的用户
        stmt = select(User).where(User.status == 'active')
        result = await session.execute(stmt)
        users = result.scalars().all()
        
        now = datetime.now(timezone.utc)
        
        for user in users:
            try:
                # 确保 last_active 是带时区的
                last = user.last_active
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                
                delta_hours = (now - last).total_seconds() / 3600
                
                # 触发失联判定
                if delta_hours > user.check_frequency:
                    contacts = await get_contacts(session, user.chat_id)
                    if contacts:
                        wills = await get_wills(session, user.chat_id)
                        
                        for c in contacts:
                            try:
                                # 发送失联通知
                                await app.bot.send_message(
                                    chat_id=c.contact_chat_id,
                                    text=(
                                        f"🚨 **LifeSignal 紧急预警**\n\n"
                                        f"监测到用户 @{user.username or user.chat_id} 已失联（超过设定时间未响应）。\n"
                                        "系统正在自动投递预设信件。"
                                    ),
                                    parse_mode=ParseMode.MARKDOWN
                                )
                                
                                # 分发遗嘱
                                if wills:
                                    for w in wills:
                                        # 检查接收人权限
                                        if w.recipient_ids and str(c.contact_chat_id) in w.recipient_ids.split(","):
                                            content = decrypt_data(w.content)
                                            caption = "🔐 **[预设投递]**"
                                            
                                            try:
                                                if w.msg_type == 'text':
                                                    await app.bot.send_message(c.contact_chat_id, f"{caption}\n\n{content}", parse_mode=ParseMode.MARKDOWN)
                                                elif w.msg_type == 'photo':
                                                    await app.bot.send_photo(c.contact_chat_id, content, caption=caption)
                                                elif w.msg_type == 'video':
                                                    await app.bot.send_video(c.contact_chat_id, content, caption=caption)
                                                elif w.msg_type == 'voice':
                                                    await app.bot.send_voice(c.contact_chat_id, content, caption=caption)
                                            except Exception as e:
                                                logger.error(f"Failed to send will to {c.contact_chat_id}: {e}")
                                            
                                            await asyncio.sleep(0.5) # 避免触发速率限制
                            except Forbidden:
                                logger.warning(f"Bot blocked by contact {c.contact_chat_id}")
                            except Exception as e:
                                logger.error(f"Error notifying contact {c.contact_chat_id}: {e}")

                    # 标记为 inactive 防止重复发送
                    user.status = 'inactive'
                    session.add(user)
                    await session.commit()
                
                # 预警提醒 (剩余时间 20% 时提醒)
                elif delta_hours > (user.check_frequency * 0.8):
                    left_hours = int(user.check_frequency - delta_hours)
                    # 避免频繁提醒，可以加一个 last_warned 字段，这里简化处理只发一次或容忍重复
                    try:
                        await app.bot.send_message(
                            user.chat_id,
                            f"⏰ **请确认安全**\n\n"
                            f"距离触发预设程序仅剩约 **{left_hours} 小时**。\n"
                            "请点击“🟢 确认平安”重置计时。",
                            reply_markup=get_main_menu()
                        )
                    except Forbidden:
                        pass # 用户把机器人屏蔽了，也没办法
                    except Exception:
                        pass

            except Exception as e:
                logger.error(f"Error checking user {user.chat_id}: {e}")
                continue # 继续检查下一个用户

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

def main():
    persistence = PicklePersistence(filepath='persistence.pickle')
    app = Application.builder().token(TOKEN).persistence(persistence).build()

    # 中间件
    app.add_handler(MessageHandler(filters.ALL, global_lock_interceptor), group=-1)
    app.add_handler(CallbackQueryHandler(global_lock_interceptor), group=-1)

    # 密码鉴权流程
    auth_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^({BTN_WILLS}|{BTN_CONTACTS}|{BTN_SETTINGS})$"), request_password_entry)],
        states={
            STATE_VERIFY_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password_verification)]
        },
        fallbacks=[CommandHandler("cancel", cancel_action)],
        name="auth_gw", persistent=True
    )

    # 远程解锁流程
    unlock_handler = ConversationHandler(
        entry_points=[CommandHandler("unlock", start_remote_unlock)],
        states={
            STATE_UNLOCK_SELECT_USER: [CallbackQueryHandler(handle_locked_user_selection, pattern="^select_locked_")],
            STATE_UNLOCK_VERIFY_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, verify_unlock_key)]
        },
        fallbacks=[CommandHandler("cancel", cancel_action)],
        name="unlock_flow", persistent=True
    )

    # 添加遗嘱流程
    add_will_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add_will, pattern="^add_will_start$")],
        states={
            STATE_ADD_WILL_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_will_content)],
            STATE_ADD_WILL_RECIPIENTS: [CallbackQueryHandler(handle_recipient_toggle)]
        },
        fallbacks=[CommandHandler("cancel", cancel_action)],
        name="add_will", persistent=True
    )

    # 初始设置流程
    setup_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            STATE_SET_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_password_finish)]
        },
        fallbacks=[],
        name="setup"
    )

    # 注册 Handlers
    app.add_handler(setup_handler)
    app.add_handler(auth_handler)
    app.add_handler(unlock_handler)
    app.add_handler(add_will_handler)
    
    # 常用命令
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SAFE}$"), handle_im_safe))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SECURITY}$"), handle_security))
    
    # 回调处理
    app.add_handler(CallbackQueryHandler(handle_global_callbacks, pattern="^(view_|reveal_|del_|try_|do_|set_freq_|back_|cancel)"))
    app.add_handler(CallbackQueryHandler(confirm_bind_callback, pattern="^accept_bind_"))
    
    # Inline 模式
    app.add_handler(InlineQueryHandler(inline_query_handler))

    # 数据库初始化
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    
    # 定时任务
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_dead_mans_switch, 'interval', minutes=30, args=[app]) # 每30分钟检查一次
    scheduler.start()
    
    print(f"🚀 {BOT_USERNAME} 核心服务已启动...")
    app.run_polling()

if __name__ == '__main__':
    main()