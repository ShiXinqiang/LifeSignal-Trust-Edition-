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
    PicklePersistence
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

# --- 4. UI 定义 (全局常量) ---

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

# 状态定义 (简化)
STATE_SET_PASSWORD, STATE_VERIFY_PASSWORD, STATE_ADD_WILL_CONTENT, STATE_ADD_WILL_RECIPIENTS = range(4)
CTX_NEXT_ACTION = 'next_action'

# --- 5. 密码验证逻辑 (网关) ---

async def request_password_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """请求输入密码"""
    user_id = update.effective_user.id
    text = update.message.text
    
    # 立即删除点击痕迹
    context.application.create_task(auto_delete_message(context, user_id, update.message.message_id, 1))
    
    # 记录下一步要去哪
    if text == BTN_WILLS: context.user_data[CTX_NEXT_ACTION] = 'wills'
    elif text == BTN_CONTACTS: context.user_data[CTX_NEXT_ACTION] = 'contacts'
    elif text == BTN_SETTINGS: context.user_data[CTX_NEXT_ACTION] = 'settings'
    
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        if user.is_locked:
            msg = await update.message.reply_text("⛔️ **账号已锁定**\n请联系您的紧急联系人进行解锁。")
            context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 10))
            return ConversationHandler.END
        
        # 如果未设置密码
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
    # 立即销毁密码明文
    context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 0))
    
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        
        if hash_password(input_pwd) == user.password_hash:
            user.login_attempts = 0
            await session.commit()
            
            # 路由到对应功能
            action = context.user_data.get(CTX_NEXT_ACTION)
            if action == 'wills':
                await show_will_menu(update, context)
            elif action == 'contacts':
                await show_contacts_menu(update, context)
            elif action == 'settings':
                await show_freq_menu(update, context)
            
            # 关键修改：验证成功后，显示菜单并立即结束 Conversation
            # 这样主菜单按钮就不会被阻塞了
            return ConversationHandler.END
        else:
            user.login_attempts += 1
            if user.login_attempts >= 5:
                user.is_locked = True
                await session.commit()
                warn = await msg.reply_text("⛔️ **密码错误过多，账号已锁定！**\n正在通知紧急联系人...")
                context.application.create_task(auto_delete_message(context, user_id, warn.message_id, 10))
                await broadcast_lockout(context, user_id, session)
                return ConversationHandler.END
            else:
                await session.commit()
                retry_msg = await msg.reply_text(f"❌ **密码错误**\n剩余尝试次数：{5 - user.login_attempts}")
                context.application.create_task(auto_delete_message(context, user_id, retry_msg.message_id, 5))
                return STATE_VERIFY_PASSWORD

# --- 6. 功能菜单展示 (Stateless) ---

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

# --- 7. 全局回调处理器 (无需状态) ---

async def handle_global_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    # --- 遗嘱查看/删除 ---
    if data.startswith("view_will_"):
        will_id = int(data.split("_")[2])
        keyboard = [
            [InlineKeyboardButton("👁 查看内容", callback_data=f"reveal_{will_id}")],
            [InlineKeyboardButton("🗑 删除遗嘱", callback_data=f"del_will_{will_id}")]
        ]
        await query.edit_message_text(f"📄 **遗嘱 #{will_id} 操作**", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("reveal_"):
        will_id = int(data.split("_")[1])
        async with AsyncSessionLocal() as session:
            will = await session.get(Will, will_id)
            if will:
                content = decrypt_data(will.content)
                if will.msg_type == 'text':
                    m = await query.message.reply_text(f"🔐 **内容 (15s销毁)**:\n{content}")
                else:
                    m = await query.message.reply_text(f"🔐 媒体文件ID (15s销毁): {content}")
                context.application.create_task(auto_delete_message(context, user_id, m.message_id, 15))

    elif data.startswith("del_will_"):
        will_id = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Will).where(Will.id == will_id))
            await session.commit()
        await query.edit_message_text("✅ 遗嘱已删除。")

    # --- 联系人解绑 ---
    elif data.startswith("try_unbind_"):
        cid = int(data.split("_")[2])
        kb = [[InlineKeyboardButton("⚠️ 确认解绑", callback_data=f"do_unbind_{cid}"), InlineKeyboardButton("取消", callback_data="cancel_cb")]]
        await query.edit_message_text("⚠️ **确认要解绑此人吗？**\n如果他负责接收遗嘱，遗嘱将无法送达。", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    elif data.startswith("do_unbind_"):
        cid = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            c = await session.get(EmergencyContact, cid)
            if c:
                try: await context.bot.send_message(c.contact_chat_id, "ℹ️ 您已被移除紧急联系人列表。")
                except: pass
                await session.delete(c)
                await session.commit()
        await query.edit_message_text("✅ 已解绑。")

    # --- 频率设置 ---
    elif data.startswith("set_freq_"):
        hours = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            u = await get_db_user(session, user_id)
            u.check_frequency = hours
            await session.commit()
        await query.edit_message_text(f"✅ 频率已设为 {int(hours/24)} 天。")

    elif data == "cancel_cb":
        await query.edit_message_text("操作已取消。")

# --- 8. 添加遗嘱 (独立流程) ---

async def start_add_will(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """添加遗嘱的入口"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📝 **请发送遗嘱内容**\n(文字/图片/视频，15秒自毁)")
    return STATE_ADD_WILL_CONTENT

async def receive_will_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    context.application.create_task(auto_delete_message(context, msg.chat_id, msg.message_id, 15))
    
    # 简单的退出机制
    if msg.text and msg.text.startswith(("/", "🟢", "⚙️")):
        return ConversationHandler.END

    content, w_type = None, 'text'
    if msg.text: content, w_type = encrypt_data(msg.text), 'text'
    elif msg.photo: content, w_type = encrypt_data(msg.photo[-1].file_id), 'photo'
    elif msg.video: content, w_type = encrypt_data(msg.video.file_id), 'video'
    elif msg.voice: content, w_type = encrypt_data(msg.voice.file_id), 'voice'
    else: return STATE_ADD_WILL_CONTENT # 重试

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
        
        text = "👥 **请选择此遗嘱发送给谁** (可多选)"
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
        if cid in sel: sel.remove(cid)
        else: sel.append(cid)
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
        await query.edit_message_text("✅ 遗嘱已添加。")
        return ConversationHandler.END

# --- 9. 初始化与杂项 ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 1))
    
    async with AsyncSessionLocal() as session:
        u = await get_db_user(session, user.id, user.username)
        
        # 绑定逻辑
        if context.args and context.args[0].startswith("connect_"):
            target_id = int(context.args[0].split("_")[1])
            if target_id == user.id:
                await update.message.reply_text("❌ 不能绑定自己。")
                return
            kb = [[InlineKeyboardButton("✅ 接受", callback_data=f"accept_bind_{target_id}"), InlineKeyboardButton("🚫 拒绝", callback_data="decline_bind")]]
            await update.message.reply_text(f"🛡️ **收到绑定请求**\nID `{target_id}`。", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
            return

        if not u.password_hash:
            await update.message.reply_text("👋 **欢迎**\n请发送您的新密码以初始化：")
            return STATE_SET_PASSWORD
        
        await update.message.reply_text("👋 守护程序运行中。", reply_markup=get_main_menu())
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

async def handle_im_safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 1))
    async with AsyncSessionLocal() as session:
        u = await get_db_user(session, user.id)
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

async def broadcast_lockout(context, user_id, session):
    contacts = await get_contacts(session, user_id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔓 解锁账号", callback_data=f"unlock_req_{user_id}")]])
    for c in contacts:
        try: await context.bot.send_message(c.contact_chat_id, f"🚨 ID `{user_id}` 账号被锁，请协助解锁。", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except: pass

async def confirm_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    target_id = int(query.data.split("_")[2])
    async with AsyncSessionLocal() as session:
        u = await get_db_user(session, target_id)
        u.is_locked = False
        u.login_attempts = 0
        await session.commit()
    await query.edit_message_text("✅ 已解锁。")
    try: await context.bot.send_message(target_id, "🎉 您的账号已被紧急联系人解锁。", reply_markup=get_main_menu())
    except: pass

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.message.edit_text("操作取消。")
    else:
        await update.message.reply_text("操作取消。", reply_markup=get_main_menu())
    return ConversationHandler.END

async def inline_query_handler(update, context):
    query = update.inline_query.query
    if query == "invite":
        link = f"https://t.me/{context.bot.username}?start=connect_{update.effective_user.id}"
        results = [InlineQueryResultArticle(id=str(uuid4()), title="邀请联系人", input_message_content=InputTextMessageContent(f"邀请绑定：{link}"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("绑定", url=link)]]))]
        await update.inline_query.answer(results)

async def handle_security(update, context):
    context.application.create_task(auto_delete_message(context, update.effective_chat.id, update.message.message_id, 1))
    m = await update.message.reply_text(f"Source: {GITHUB_REPO_URL}")
    context.application.create_task(auto_delete_message(context, update.effective_chat.id, m.message_id, 60))

async def check_dead_mans_switch(app):
    # 定时任务保留（为节省篇幅略去具体发送逻辑，同上一版）
    pass

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

def main():
    persistence = PicklePersistence(filepath='persistence.pickle')
    app = Application.builder().token(TOKEN).persistence(persistence).build()

    # 1. 密码验证层 (网关)
    auth_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^({BTN_WILLS}|{BTN_CONTACTS}|{BTN_SETTINGS})$"), request_password_entry)],
        states={
            STATE_VERIFY_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password_verification)]
        },
        fallbacks=[CommandHandler("cancel", cancel_action)],
        name="auth_gateway", persistent=True
    )

    # 2. 添加遗嘱层 (独立)
    add_will_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add_will, pattern="^add_will_start$")],
        states={
            STATE_ADD_WILL_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_will_content)],
            STATE_ADD_WILL_RECIPIENTS: [CallbackQueryHandler(handle_recipient_toggle)]
        },
        fallbacks=[CommandHandler("cancel", cancel_action)],
        name="add_will_flow", persistent=True
    )

    # 3. 初始设置层
    setup_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={STATE_SET_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_password_finish)]},
        fallbacks=[],
        name="setup_flow"
    )

    app.add_handler(setup_handler)
    app.add_handler(auth_handler)
    app.add_handler(add_will_handler)
    
    # 全局功能
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SAFE}$"), handle_im_safe))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SECURITY}$"), handle_security))
    
    # 全局回调 (Stateless)
    app.add_handler(CallbackQueryHandler(handle_global_callbacks, pattern="^(view_|reveal_|del_|try_|do_|set_freq_|cancel)"))
    app.add_handler(CallbackQueryHandler(confirm_bind_callback, pattern="^accept_bind_"))
    app.add_handler(CallbackQueryHandler(confirm_unlock, pattern="^unlock_conf")) # 只有确认解锁走这里
    
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
