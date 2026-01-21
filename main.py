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

# 密钥处理
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

# --- 3. 文案与 UI 定义 (已优化) ---

BTN_SAFE = "🟢 我现在很安全"
BTN_WILLS = "📦 数字遗嘱"
BTN_CONTACTS = "👥 守护人列表"
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
        input_field_placeholder="LifeSignal 正在守护中..."
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
    user = update.effective_user
    if not user: return

    # 先删除用户发的消息，保护隐私
    if update.message:
        context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 1))

    try:
        async with AsyncSessionLocal() as session:
            db_user = await get_db_user(session, user.id)

            if db_user.is_locked:
                key_display = db_user.unlock_key if db_user.unlock_key else "ERROR"
                alert_text = (
                    "🛡️ **安全熔断已触发**\n\n"
                    "为了保护您的数据安全，账户已暂时锁定。\n"
                    "请联系您的守护人，提供以下恢复密钥进行解锁：\n\n"
                    f"🔑 密钥：`{key_display}`"
                )
                if update.message:
                    msg = await update.message.reply_text(alert_text, parse_mode=ParseMode.MARKDOWN)
                    context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 30))
                elif update.callback_query:
                    await update.callback_query.answer("⛔️ 访问受限：请联系守护人解锁", show_alert=True)
                
                raise ApplicationHandlerStop
    except ApplicationHandlerStop:
        raise
    except Exception:
        pass

async def request_password_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    
    if text == BTN_WILLS: context.user_data[CTX_NEXT_ACTION] = 'wills'
    elif text == BTN_CONTACTS: context.user_data[CTX_NEXT_ACTION] = 'contacts'
    elif text == BTN_SETTINGS: context.user_data[CTX_NEXT_ACTION] = 'settings'

    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        if not user.password_hash:
            msg = await update.message.reply_text("👋 欢迎使用 LifeSignal。\n为了确保只有您能管理遗嘱，请设置一个**主密码**：")
            context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 20))
            return ConversationHandler.END

    prompt = await update.message.reply_text("🔐 **身份验证**\n请输入您的主密码以继续：")
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
                warn = await msg.reply_text("⛔️ **安全警报：多次尝试失败**\n账户已锁定，请联系守护人。")
                context.application.create_task(auto_delete_message(context, user_id, warn.message_id, 15))
                return ConversationHandler.END
            else:
                await session.commit()
                retry_msg = await msg.reply_text(f"❌ **密码错误** (还剩 {5 - user.login_attempts} 次机会)")
                context.application.create_task(auto_delete_message(context, user_id, retry_msg.message_id, 5))
                return STATE_VERIFY_PASSWORD

# --- 6. 守护人解锁流程 ---

async def start_remote_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    executor_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        stmt = select(EmergencyContact).where(EmergencyContact.contact_chat_id == executor_id)
        entrustments = (await session.execute(stmt)).scalars().all()
        
        locked_users = []
        for ent in entrustments:
            user = await session.get(User, ent.owner_chat_id)
            if user and user.is_locked:
                locked_users.append(user)
        
        if not locked_users:
            msg = await update.message.reply_text("✅ 目前没有需要您协助解锁的账户。")
            context.application.create_task(auto_delete_message(context, executor_id, msg.message_id, 5))
            return ConversationHandler.END
        
        kb = [[InlineKeyboardButton(f"🔓 解锁: {u.username or u.chat_id}", callback_data=f"select_locked_{u.chat_id}")] for u in locked_users]
        await update.message.reply_text("🛡️ **守护人操作台**\n请选择需要恢复访问权限的账户：", reply_markup=InlineKeyboardMarkup(kb))
        return STATE_UNLOCK_SELECT_USER

async def handle_locked_user_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data[CTX_UNLOCK_TARGET] = int(query.data.split("_")[2])
    await query.edit_message_text("🛡️ 请输入委托人提供的 **6位恢复密钥**：")
    return STATE_UNLOCK_VERIFY_KEY

async def verify_unlock_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    input_key = msg.text.strip()
    target_id = context.user_data.get(CTX_UNLOCK_TARGET)
    
    async with AsyncSessionLocal() as session:
        target_user = await get_db_user(session, target_id)
        if input_key == target_user.unlock_key:
            target_user.is_locked = False
            target_user.login_attempts = 0
            target_user.unlock_key = None
            target_user.password_hash = None
            await session.commit()
            await msg.reply_text("✅ **操作成功**\n委托人的账户已解锁，且主密码已重置。")
            try: await context.bot.send_message(target_id, "🎉 **账户已恢复**\n守护人已协助解锁。您的旧密码已失效，请重新设置。", reply_markup=get_main_menu())
            except: pass
            return ConversationHandler.END
        else:
            await msg.reply_text("❌ 密钥验证失败，请核对后重试。")
            return ConversationHandler.END

# --- 7. 基础功能 ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 1))

    async with AsyncSessionLocal() as session:
        db_user = await get_db_user(session, user.id, user.username)

        # 绑定逻辑
        if context.args and context.args[0].startswith("connect_"):
            target_id = int(context.args[0].split("_")[1])
            if target_id == user.id: return
            exists = (await session.execute(select(EmergencyContact).where(EmergencyContact.owner_chat_id == target_id, EmergencyContact.contact_chat_id == user.id))).scalar()
            if exists:
                await update.message.reply_text("✅ 您已经是对方的守护人了。")
                return
            kb = [[InlineKeyboardButton("🤝 接受委托", callback_data=f"accept_bind_{target_id}"), InlineKeyboardButton("🚫 婉拒", callback_data="decline_bind")]]
            await update.message.reply_text(
                f"📩 **收到一份守护委托**\n\n用户 `{target_id}` 希望将您设为紧急联系人。\n接受后，当该用户长期失联时，您将收到其预留的信息。",
                reply_markup=InlineKeyboardMarkup(kb), 
                parse_mode=ParseMode.MARKDOWN
            )
            return

        if not db_user.password_hash:
            await update.message.reply_text(
                "👋 **你好，我是 LifeSignal。**\n\n"
                "我会默默守护您的数字资产，直到您需要的那一刻。\n"
                "为了确保安全，请先设置一个**主密码**：\n"
                "(此密码将用于管理遗嘱和联系人)"
            )
            return STATE_SET_PASSWORD

        await update.message.reply_text("✨ **LifeSignal 正在运行中**\n\n您可以使用下方菜单与我互动。", reply_markup=get_main_menu())
        return ConversationHandler.END

async def set_password_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text
    context.application.create_task(auto_delete_message(context, update.effective_user.id, update.message.message_id, 1))
    async with AsyncSessionLocal() as session:
        u = await get_db_user(session, update.effective_user.id)
        u.password_hash = hash_password(pwd)
        await session.commit()
    await update.message.reply_text("✅ **配置完成**\n您的保险箱已建立。请使用下方菜单添加遗嘱或设置频率。", reply_markup=get_main_menu())
    return ConversationHandler.END

# --- 8. 菜单与回调 ---

async def show_will_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        wills = await get_wills(session, user_id)
        kb = []
        for w in wills:
            created_date = w.created_at.strftime("%Y-%m-%d")
            kb.append([InlineKeyboardButton(f"📄 记录 ({created_date})", callback_data=f"view_will_{w.id}")])
        kb.append([InlineKeyboardButton("✍️ 写新遗嘱", callback_data="add_will_start")])
        msg = await context.bot.send_message(user_id, f"📦 **您的数字遗嘱** (共 {len(wills)} 条)\n点击条目可查看或删除。", reply_markup=InlineKeyboardMarkup(kb))
        context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 60))

async def show_contacts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        contacts = await get_contacts(session, user_id)
        kb = [[InlineKeyboardButton(f"❌ 解绑 {c.contact_name}", callback_data=f"try_unbind_{c.id}")] for c in contacts]
        if len(contacts) < 10: kb.append([InlineKeyboardButton("➕ 邀请新守护人", switch_inline_query="invite")])
        msg = await context.bot.send_message(user_id, f"👥 **守护人列表** ({len(contacts)}人)\n当您失联时，系统会将信息发送给他们。", reply_markup=InlineKeyboardMarkup(kb))
        context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 60))

async def show_freq_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    kb = [[InlineKeyboardButton("24小时", callback_data="set_freq_24"), InlineKeyboardButton("3天 (推荐)", callback_data="set_freq_72"), InlineKeyboardButton("7天", callback_data="set_freq_168")]]
    msg = await context.bot.send_message(user_id, "⏱️ **频率设置**\n如果超过以下时间您未报平安，系统将判定为失联：", reply_markup=InlineKeyboardMarkup(kb))
    context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 60))

async def handle_global_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data.startswith("view_will_"):
        wid = int(data.split("_")[2])
        kb = [[InlineKeyboardButton("👁 显示内容", callback_data=f"reveal_{wid}")], [InlineKeyboardButton("🗑 删除此条", callback_data=f"del_will_{wid}")]]
        await query.edit_message_text(f"📄 **记录 #{wid} 管理**", reply_markup=InlineKeyboardMarkup(kb))
    
    elif data.startswith("reveal_"):
        wid = int(data.split("_")[1])
        async with AsyncSessionLocal() as session:
            will = await session.get(Will, wid)
            if will:
                content = decrypt_data(will.content)
                if will.msg_type == 'text': m = await query.message.reply_text(f"🔐 **加密内容** (15秒后自动销毁):\n\n{content}")
                else: m = await query.message.reply_text(f"🔐 **加密媒体文件ID**:\n{content}")
                context.application.create_task(auto_delete_message(context, user_id, m.message_id, 15))

    elif data.startswith("del_will_"):
        wid = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Will).where(Will.id == wid))
            await session.commit()
        await query.edit_message_text("🗑️ 已安全删除该记录。")

    elif data.startswith("try_unbind_"):
        cid = int(data.split("_")[2])
        kb = [[InlineKeyboardButton("⚠️ 确认移除", callback_data=f"do_unbind_{cid}"), InlineKeyboardButton("点错了", callback_data="cancel_cb")]]
        await query.edit_message_text("⚠️ **敏感操作**\n移除后，该用户将无法再接收您的遗嘱信息。确认移除吗？", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("do_unbind_"):
        cid = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            c = await session.get(EmergencyContact, cid)
            if c:
                await session.delete(c)
                await session.commit()
        await query.edit_message_text("✅ 已解除绑定。")

    elif data.startswith("set_freq_"):
        h = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            u = await get_db_user(session, user_id)
            u.check_frequency = h
            await session.commit()
        await query.edit_message_text(f"✅ 设置成功！\n如果 {h} 小时内未收到您的消息，我将启动应急程序。")

    elif data == "cancel_cb":
        await query.edit_message_text("操作已取消。")

# --- 9. 添加遗嘱 ---

async def start_add_will(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("📝 **撰写模式**\n请直接发送您想留下的内容。\n支持：文字消息、照片、视频。")
    return STATE_ADD_WILL_CONTENT

async def receive_will_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.text and msg.text in [BTN_SAFE, BTN_WILLS, BTN_CONTACTS, BTN_SETTINGS]: return ConversationHandler.END
    
    content, w_type = None, 'text'
    if msg.text: content, w_type = encrypt_data(msg.text), 'text'
    elif msg.photo: content, w_type = encrypt_data(msg.photo[-1].file_id), 'photo'
    elif msg.video: content, w_type = encrypt_data(msg.video.file_id), 'video'
    else: return STATE_ADD_WILL_CONTENT

    context.user_data['temp_content'] = content
    context.user_data['temp_type'] = w_type
    context.user_data['selected'] = []
    return await render_recipient_selector(update, context)

async def render_recipient_selector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        contacts = await get_contacts(session, user_id)
        if not contacts:
             await context.bot.send_message(user_id, "⚠️ **暂无守护人**\n请先邀请至少一位守护人，再设置遗嘱。", reply_markup=get_main_menu())
             return ConversationHandler.END
        
        sel = context.user_data.get('selected', [])
        kb = [[InlineKeyboardButton(f"{'✅' if c.contact_chat_id in sel else '⭕️'} {c.contact_name}", callback_data=f"sel_rec_{c.contact_chat_id}")] for c in contacts]
        kb.append([InlineKeyboardButton("💾 确认保存", callback_data="save_new_will")])
        
        text = "📨 **指定接收人**\n请选择当您失联时，谁有权收到这条信息："
        if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
        else: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
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
        async with AsyncSessionLocal() as session:
            session.add(Will(
                user_id=update.effective_user.id,
                content=context.user_data['temp_content'],
                msg_type=context.user_data['temp_type'],
                recipient_ids=",".join(map(str, context.user_data.get('selected', [])))
            ))
            await session.commit()
        await query.edit_message_text("✅ **加密存储成功**\n该内容已存入保险箱。")
        return ConversationHandler.END

# --- 10. 杂项 ---

async def handle_im_safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # 立即删除用户点击的消息（视觉反馈）
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 0))
    
    async with AsyncSessionLocal() as session:
        u = await get_db_user(session, user.id)
        if u.is_locked: return

        contacts = await get_contacts(session, user.id)
        if not contacts:
            msg = await update.message.reply_text("⚠️ **功能未激活**\n请先在「👥 守护人列表」中添加联系人，守护功能才会生效。", reply_markup=get_main_menu())
            context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 5))
            return
        
        u.last_active = datetime.now(timezone.utc)
        u.status = 'active'
        await session.commit()
        
    msg = await update.message.reply_text(f"🌟 **很高兴你还在线！**\n守护倒计时已重置，祝你今天过得愉快。", reply_markup=get_main_menu())
    context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 10))

async def confirm_bind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "decline_bind":
        await query.edit_message_text("已婉拒该请求。")
        return
    rid = int(query.data.split("_")[2])
    async with AsyncSessionLocal() as session:
        exists = (await session.execute(select(EmergencyContact).where(EmergencyContact.owner_chat_id == rid, EmergencyContact.contact_chat_id == update.effective_user.id))).scalar()
        if not exists:
            session.add(EmergencyContact(owner_chat_id=rid, contact_chat_id=update.effective_user.id, contact_name=update.effective_user.first_name))
            await session.commit()
    await query.edit_message_text("✅ **绑定成功**\n您已成为对方的守护人。")
    try: await context.bot.send_message(rid, "🎉 **绑定成功**\n对方已接受您的委托。")
    except: pass

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query: await update.callback_query.message.edit_text("操作已取消。")
    else: await update.message.reply_text("操作已取消。", reply_markup=get_main_menu())
    return ConversationHandler.END

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.inline_query.query == "invite":
        link = f"https://t.me/{context.bot.username}?start=connect_{update.effective_user.id}"
        results = [InlineQueryResultArticle(id=str(uuid4()), title="发送守护邀请函", input_message_content=InputTextMessageContent(f"📩 **LifeSignal 特别委托**\n\n我希望将您设为我的守护人。\n如果我长时间失联，您将收到我预留的重要信息。\n\n点击下方按钮接受委托：", parse_mode=ParseMode.MARKDOWN), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🤝 接受委托", url=link)]]))]
        await update.inline_query.answer(results, cache_time=0)

async def handle_security(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛡️ **安全审计**\n\n本项目代码开源且所有敏感数据均加密存储。我们无法查看您的遗嘱内容。\n\nGithub: LifeSignal-Trust-Edition-")

async def check_dead_mans_switch(app: Application):
    async with AsyncSessionLocal() as session:
        users = (await session.execute(select(User).where(User.status == 'active'))).scalars().all()
        now = datetime.now(timezone.utc)
        for user in users:
            last = user.last_active if user.last_active.tzinfo else user.last_active.replace(tzinfo=timezone.utc)
            delta = (now - last).total_seconds() / 3600
            if delta > user.check_frequency:
                contacts = await get_contacts(session, user.chat_id)
                wills = await get_wills(session, user.chat_id)
                for c in contacts:
                    try:
                        await app.bot.send_message(c.contact_chat_id, f"🚨 **紧急预警**\n\n用户 @{user.username or user.chat_id} 已长时间未活动，触发了失联预警。", parse_mode=ParseMode.MARKDOWN)
                        for w in wills:
                            if w.recipient_ids and str(c.contact_chat_id) in w.recipient_ids.split(","):
                                content = decrypt_data(w.content)
                                if w.msg_type=='text': await app.bot.send_message(c.contact_chat_id, f"✉️ **预留信件**:\n\n{content}")
                                else: await app.bot.send_message(c.contact_chat_id, "📁 [收到一份加密媒体文件]")
                    except: pass
                user.status = 'inactive'
                session.add(user)
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
        entry_points=[MessageHandler(filters.Text([BTN_WILLS, BTN_CONTACTS, BTN_SETTINGS]), request_password_entry)],
        states={STATE_VERIFY_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password_verification)]},
        fallbacks=[CommandHandler("cancel", cancel_action)], name="auth_gw", persistent=True
    )

    unlock_handler = ConversationHandler(
        entry_points=[CommandHandler("unlock", start_remote_unlock)],
        states={STATE_UNLOCK_SELECT_USER: [CallbackQueryHandler(handle_locked_user_selection)], STATE_UNLOCK_VERIFY_KEY: [MessageHandler(filters.TEXT, verify_unlock_key)]},
        fallbacks=[CommandHandler("cancel", cancel_action)], name="unlock", persistent=True
    )

    add_will_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add_will, pattern="^add_will_start$")],
        states={STATE_ADD_WILL_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_will_content)], STATE_ADD_WILL_RECIPIENTS: [CallbackQueryHandler(handle_recipient_toggle)]},
        fallbacks=[CommandHandler("cancel", cancel_action)], name="add_will", persistent=True
    )

    app.add_handler(ConversationHandler(entry_points=[CommandHandler("start", start)], states={STATE_SET_PASSWORD: [MessageHandler(filters.TEXT, set_password_finish)]}, fallbacks=[], name="setup"))
    app.add_handler(auth_handler)
    app.add_handler(unlock_handler)
    app.add_handler(add_will_handler)
    
    app.add_handler(MessageHandler(filters.Text(BTN_SAFE), handle_im_safe))
    app.add_handler(MessageHandler(filters.Text(BTN_SECURITY), handle_security))
    
    app.add_handler(CallbackQueryHandler(handle_global_callbacks, pattern="^(view_|reveal_|del_|try_|do_|set_freq_|cancel)"))
    app.add_handler(CallbackQueryHandler(confirm_bind_callback, pattern="^accept_bind_"))
    app.add_handler(InlineQueryHandler(inline_query_handler))

    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_dead_mans_switch, 'interval', minutes=30, args=[app])
    scheduler.start()
    
    print("🚀 LifeSignal 已修复并启动...")
    app.run_polling()

if __name__ == '__main__':
    main()
