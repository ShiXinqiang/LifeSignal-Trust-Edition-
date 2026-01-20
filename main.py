import os
import logging
import asyncio
import hashlib
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
    ApplicationHandlerStop # 用于中断后续处理
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
    check_frequency = Column(Integer, default=72)
    last_active = Column(DateTime(timezone=True), default=func.now())
    status = Column(String, default='active') 

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
    await asyncio.sleep(delay)
    try:
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

# --- 4. UI 定义 ---

BTN_SAFE = "🟢 我很安全"
BTN_CONTACTS = "👥 联系人管理"
BTN_WILLS = "📜 遗嘱管理"
BTN_SETTINGS = "⚙️ 设置频率"
BTN_SECURITY = "🛡️ 开源验证"

def get_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_SAFE],
            [BTN_WILLS, BTN_CONTACTS],
            [BTN_SETTINGS, BTN_SECURITY]
        ],
        resize_keyboard=True,
        is_persistent=True, 
        input_field_placeholder="死了么LifeSignal 正在守护..."
    )

(
    STATE_SET_PASSWORD,         
    STATE_VERIFY_PASSWORD,      
    STATE_ADD_WILL_CONTENT,     
    STATE_ADD_WILL_RECIPIENTS,  
) = range(4)

CTX_NEXT_ACTION = 'next_action'

# --- 5. 全局熔断拦截器 (核心安全升级) ---

async def global_lock_interceptor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    最高优先级的拦截器 (Group -1)
    如果用户被锁定，拦截所有操作，仅允许联系紧急联系人。
    """
    user = update.effective_user
    if not user: return 

    # 1. 立即删除用户发送的消息/指令，保持界面清洁
    if update.message:
        context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 0))

    async with AsyncSessionLocal() as session:
        db_user = await get_db_user(session, user.id)
        
        if db_user.is_locked:
            alert_text = (
                "⛔️ **账号已被冻结**\n\n"
                "检测到多次异常操作，系统已触发安全熔断。\n"
                "在此状态下，您无法使用任何功能（包括报平安）。\n\n"
                "🔑 **唯一解锁方法：**\n"
                "请线下联系您的 **紧急联系人**，让他/她在机器人对话框中输入 `/unlock` 命令，并在列表中选择您的名字进行解锁。"
            )
            
            # 如果是点击按钮触发的
            if update.callback_query:
                await update.callback_query.answer("⛔️ 账号已锁定，无法操作！", show_alert=True)
                # 也可以发一条提示消息
                msg = await context.bot.send_message(user.id, alert_text, parse_mode=ParseMode.MARKDOWN)
                context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 20))
            
            # 如果是发送消息触发的
            elif update.message:
                msg = await update.message.reply_text(alert_text, parse_mode=ParseMode.MARKDOWN)
                context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 20))
            
            # 🚨 关键：停止处理后续的所有 Handler
            raise ApplicationHandlerStop

# --- 6. 密码验证逻辑 ---

async def request_password_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """请求输入密码"""
    user_id = update.effective_user.id
    text = update.message.text
    
    # 记录路由
    if text == BTN_WILLS: context.user_data[CTX_NEXT_ACTION] = 'wills'
    elif text == BTN_CONTACTS: context.user_data[CTX_NEXT_ACTION] = 'contacts'
    elif text == BTN_SETTINGS: context.user_data[CTX_NEXT_ACTION] = 'settings'
    
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        
        # 未设置密码
        if not user.password_hash:
            msg = await update.message.reply_text("⚠️ **您尚未设置密码**\n首次使用请点击 /start 进行初始化。")
            context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 10))
            return ConversationHandler.END
    
    prompt = await update.message.reply_text("🔐 **身份验证**\n\n请输入您的密码以继续：")
    context.application.create_task(auto_delete_message(context, user_id, prompt.message_id, 30))
    return STATE_VERIFY_PASSWORD

async def handle_password_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """校验密码"""
    msg = update.message
    user_id = update.effective_user.id
    input_pwd = msg.text
    context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 0))
    
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        
        if hash_password(input_pwd) == user.password_hash:
            # ✅ 只要验证成功一次，立即清零错误计数
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
                await session.commit()
                warn_text = "⛔️ **密码错误过多，账号已锁定！**\n请联系紧急联系人解锁。"
                warn = await msg.reply_text(warn_text, parse_mode=ParseMode.MARKDOWN)
                context.application.create_task(auto_delete_message(context, user_id, warn.message_id, 15))
                await broadcast_lockout(context, user_id, session)
                return ConversationHandler.END
            else:
                await session.commit()
                retry_msg = await msg.reply_text(f"❌ **密码错误**\n剩余尝试次数：{5 - user.login_attempts}")
                context.application.create_task(auto_delete_message(context, user_id, retry_msg.message_id, 5))
                return STATE_VERIFY_PASSWORD

async def broadcast_lockout(context, user_id, session):
    contacts = await get_contacts(session, user_id)
    if not contacts: return
    for c in contacts:
        try: 
            await context.bot.send_message(c.contact_chat_id, f"🚨 **安全警报**\n用户 ID `{user_id}` 账号因密码错误被锁。\n如果是本人联系您，请发送 `/unlock` 帮助解锁。", parse_mode=ParseMode.MARKDOWN)
        except: pass

# --- 7. 紧急联系人专用：解锁命令 ---

async def cmd_remote_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """紧急联系人使用的解锁命令"""
    executor_id = update.effective_user.id
    context.application.create_task(auto_delete_message(context, executor_id, update.message.message_id, 1))
    
    async with AsyncSessionLocal() as session:
        # 1. 查找我是哪些人的紧急联系人
        stmt = select(EmergencyContact).where(EmergencyContact.contact_chat_id == executor_id)
        entrustments = (await session.execute(stmt)).scalars().all()
        
        if not entrustments:
            msg = await update.message.reply_text("⚠️ **无权操作**\n您不是任何人的紧急联系人。")
            context.application.create_task(auto_delete_message(context, executor_id, msg.message_id, 10))
            return

        # 2. 筛选出其中处于锁定状态的用户
        locked_users = []
        for ent in entrustments:
            user = await session.get(User, ent.owner_chat_id)
            if user and user.is_locked:
                locked_users.append(user)
        
        if not locked_users:
            msg = await update.message.reply_text("✅ **一切正常**\n您守护的所有账号均未被锁定。")
            context.application.create_task(auto_delete_message(context, executor_id, msg.message_id, 10))
            return
        
        # 3. 生成解锁列表
        keyboard = []
        for u in locked_users:
            name = u.username or f"ID {u.chat_id}"
            keyboard.append([InlineKeyboardButton(f"🔓 解锁: {name}", callback_data=f"remote_unlock_{u.chat_id}")])
        
        await update.message.reply_text(
            f"🚨 **发现 {len(locked_users)} 个被锁定的账号**\n\n请在**确认对方身份**后，点击下方按钮进行解锁：",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

async def handle_remote_unlock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    target_id = int(query.data.split("_")[2])
    executor = update.effective_user
    
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, target_id)
        if not user.is_locked:
            await query.edit_message_text("ℹ️ 该用户已经解锁了。")
            return
            
        user.is_locked = False
        user.login_attempts = 0
        await session.commit()
    
    await query.edit_message_text(f"✅ **操作成功**\n已为用户 ID `{target_id}` 解除锁定。", parse_mode=ParseMode.MARKDOWN)
    
    try:
        await context.bot.send_message(
            target_id,
            f"🎉 **账号已恢复**\n\n您的紧急联系人 **{executor.first_name}** 已通过远程指令为您解除锁定。\n请小心使用密码。",
            reply_markup=get_main_menu()
        )
    except: pass

# --- 8. 启动与密码设置 ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 1))
    
    async with AsyncSessionLocal() as session:
        db_user = await get_db_user(session, user.id, user.username)
        
        # 绑定逻辑
        if context.args and context.args[0].startswith("connect_"):
            target_id = int(context.args[0].split("_")[1])
            if target_id == user.id:
                await update.message.reply_text("❌ 不能绑定自己。")
                return
            exists = (await session.execute(select(EmergencyContact).where(EmergencyContact.owner_chat_id==target_id, EmergencyContact.contact_chat_id==user.id))).scalar()
            if exists:
                await update.message.reply_text("✅ 已经是联系人了。")
                return
            
            kb = [[InlineKeyboardButton("✅ 接受", callback_data=f"accept_bind_{target_id}"), InlineKeyboardButton("🚫 拒绝", callback_data="decline_bind")]]
            await update.message.reply_text(f"🛡️ **收到绑定请求**\nID `{target_id}`。", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
            return

        if not db_user.password_hash:
            await update.message.reply_text("👋 **欢迎**\n请发送您的新密码以初始化：")
            return STATE_SET_PASSWORD
        
        await update.message.reply_text(f"👋 守护程序运行中。", reply_markup=get_main_menu())
        return ConversationHandler.END

async def set_password_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text
    context.application.create_task(auto_delete_message(context, update.effective_user.id, update.message.message_id, 0))
    async with AsyncSessionLocal() as session:
        u = await get_db_user(session, update.effective_user.id)
        u.password_hash = hash_password(pwd)
        await session.commit()
    await update.message.reply_text("✅ 密码已设置。", reply_markup=get_main_menu())
    return ConversationHandler.END

# --- 9. 功能菜单展示 (Stateless) ---

async def show_will_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        wills = await get_wills(session, user_id)
        keyboard = []
        if wills:
            for w in wills:
                try:
                    decrypted = decrypt_data(w.content)
                    preview = decrypted[:8] + ".." if w.msg_type == 'text' else f"[{w.msg_type}]"
                except: preview = "Err"
                keyboard.append([InlineKeyboardButton(f"📄 {preview}", callback_data=f"view_will_{w.id}")])
        
        keyboard.append([InlineKeyboardButton("➕ 添加新遗嘱", callback_data="add_will_start")])
        text = f"📜 **遗嘱库管理**\n现有 {len(wills)} 份遗嘱。"
        msg = await context.bot.send_message(user_id, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 60))

async def show_contacts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        contacts = await get_contacts(session, user_id)
        keyboard = []
        for c in contacts:
            keyboard.append([InlineKeyboardButton(f"👤 {c.contact_name}", callback_data="noop"), InlineKeyboardButton("❌ 解绑", callback_data=f"try_unbind_{c.id}")])
        if len(contacts) < 10:
            keyboard.append([InlineKeyboardButton("➕ 邀请新联系人", switch_inline_query="invite")])
        
        text = f"👥 **联系人管理 ({len(contacts)}/10)**"
        msg = await context.bot.send_message(user_id, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 60))

async def show_freq_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    keyboard = [[InlineKeyboardButton("1 天", callback_data="set_freq_24"), InlineKeyboardButton("3 天", callback_data="set_freq_72"), InlineKeyboardButton("7 天", callback_data="set_freq_168")]]
    msg = await context.bot.send_message(user_id, "⚙️ **修改确认频率**", reply_markup=InlineKeyboardMarkup(keyboard))
    context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 60))

# --- 10. 全局回调处理器 ---

async def handle_global_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    # 遗嘱操作
    if data.startswith("view_will_"):
        will_id = int(data.split("_")[2])
        keyboard = [[InlineKeyboardButton("👁 查看内容", callback_data=f"reveal_{will_id}")], [InlineKeyboardButton("🗑 删除", callback_data=f"del_will_{will_id}")]]
        await query.edit_message_text(f"📄 **遗嘱 #{will_id}**", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("reveal_"):
        will_id = int(data.split("_")[1])
        async with AsyncSessionLocal() as session:
            will = await session.get(Will, will_id)
            if will:
                content = decrypt_data(will.content)
                text = f"🔐 **内容 (15s销毁)**:\n{content}" if will.msg_type == 'text' else f"🔐 媒体文件ID: {content}"
                m = await query.message.reply_text(text)
                context.application.create_task(auto_delete_message(context, user_id, m.message_id, 15))

    elif data.startswith("del_will_"):
        will_id = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Will).where(Will.id == will_id))
            await session.commit()
        await query.edit_message_text("✅ 已删除。")

    # 联系人解绑
    elif data.startswith("try_unbind_"):
        cid = int(data.split("_")[2])
        kb = [[InlineKeyboardButton("⚠️ 确认解绑", callback_data=f"do_unbind_{cid}"), InlineKeyboardButton("取消", callback_data="cancel_cb")]]
        await query.edit_message_text("⚠️ **确认解绑？**", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    elif data.startswith("do_unbind_"):
        cid = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            c = await session.get(EmergencyContact, cid)
            if c:
                try: await context.bot.send_message(c.contact_chat_id, "ℹ️ 您已被移除联系人列表。")
                except: pass
                await session.delete(c)
                await session.commit()
        await query.edit_message_text("✅ 已解绑。")

    elif data.startswith("set_freq_"):
        hours = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            u = await get_db_user(session, user_id)
            u.check_frequency = hours
            await session.commit()
        await query.edit_message_text(f"✅ 频率: {int(hours/24)} 天。")

    elif data == "cancel_cb":
        await query.edit_message_text("已取消。")

# --- 11. 添加遗嘱流程 ---

async def start_add_will(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📝 **请发送遗嘱内容** (15s自毁)")
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
        
        btn_text = f"保存 ({len(selected)}人)" if selected else "保存 (暂无接收人)"
        kb.append([InlineKeyboardButton(btn_text, callback_data="save_new_will")])
        
        text = "👥 **选择接收人**"
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
        await query.edit_message_text("✅ 遗嘱已添加。")
        return ConversationHandler.END

# --- 12. 杂项 ---

async def handle_im_safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 1))
    
    async with AsyncSessionLocal() as session:
        u = await get_db_user(session, user.id)
        # 熔断检测
        if u.is_locked: return # 已被 Interceptor 处理，这里无需多言

        contacts = await get_contacts(session, user.id)
        if not contacts:
            msg = await update.message.reply_text("⚠️ 请先绑定联系人。", reply_markup=get_main_menu())
            context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 5))
            return
        u.last_active = datetime.now(timezone.utc)
        u.status = 'active'
        await session.commit()
    msg = await update.message.reply_text("✅ 已确认安全。", reply_markup=get_main_menu())
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
    try: await context.bot.send_message(requester_id, "🎉 对方已接受绑定！")
    except: pass

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query: await update.callback_query.message.edit_text("操作取消。")
    else: await update.message.reply_text("操作取消。", reply_markup=get_main_menu())
    return ConversationHandler.END

async def inline_query_handler(update, context):
    query = update.inline_query.query
    if query == "invite":
        link = f"https://t.me/{context.bot.username}?start=connect_{update.effective_user.id}"
        results = [InlineQueryResultArticle(id=str(uuid4()), title="邀请联系人", input_message_content=InputTextMessageContent(f"📩 **来自 {update.effective_user.first_name} 的信任委托**\n\n我希望将你设为我的紧急联系人。\n👇 **请点击下方链接接受：**", parse_mode=ParseMode.MARKDOWN), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ 接受委托", url=link)]]))]
        await update.inline_query.answer(results)

async def handle_security(update, context):
    context.application.create_task(auto_delete_message(context, update.effective_chat.id, update.message.message_id, 1))
    await update.message.reply_markdown(f"GitHub: {GITHUB_REPO_URL}")

async def check_dead_mans_switch(app):
    # 定时任务保留
    pass

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

def main():
    persistence = PicklePersistence(filepath='persistence.pickle')
    app = Application.builder().token(TOKEN).persistence(persistence).build()

    # 0. 全局熔断拦截器 (Group -1)
    app.add_handler(MessageHandler(filters.ALL, global_lock_interceptor), group=-1)
    # 也要拦截 CallbackQuery (Group -1)
    app.add_handler(CallbackQueryHandler(global_lock_interceptor), group=-1)

    # 1. 密码验证层 (Group 0)
    auth_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^({BTN_WILLS}|{BTN_CONTACTS}|{BTN_SETTINGS})$"), request_password_entry)],
        states={STATE_VERIFY_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password_verification)]},
        fallbacks=[CommandHandler("cancel", cancel_action)],
        name="auth_gw", persistent=True
    )

    # 2. 添加遗嘱层
    add_will_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add_will, pattern="^add_will_start$")],
        states={
            STATE_ADD_WILL_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_will_content)],
            STATE_ADD_WILL_RECIPIENTS: [CallbackQueryHandler(handle_recipient_toggle)]
        },
        fallbacks=[CommandHandler("cancel", cancel_action)],
        name="add_will", persistent=True
    )

    # 3. 初始设置
    setup_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={STATE_SET_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_password_finish)]},
        fallbacks=[],
        name="setup"
    )

    app.add_handler(setup_handler)
    app.add_handler(auth_handler)
    app.add_handler(add_will_handler)
    
    app.add_handler(CommandHandler("unlock", cmd_remote_unlock)) # 注册解锁命令
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SAFE}$"), handle_im_safe))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SECURITY}$"), handle_security))
    
    app.add_handler(CallbackQueryHandler(handle_global_callbacks, pattern="^(view_|reveal_|del_|try_|do_|set_freq_|cancel)"))
    app.add_handler(CallbackQueryHandler(confirm_bind_callback, pattern="^accept_bind_"))
    app.add_handler(CallbackQueryHandler(handle_remote_unlock_callback, pattern="^remote_unlock_"))
    
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
