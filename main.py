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
    
    # 安全字段
    password_hash = Column(String, nullable=True) 
    login_attempts = Column(Integer, default=0)   
    is_locked = Column(Boolean, default=False)    
    
    # 机制
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
    STATE_WILL_MENU,            
    STATE_ADD_WILL_CONTENT,     
    STATE_ADD_WILL_RECIPIENTS,  
    STATE_FREQ_SELECT           
) = range(6)

CTX_NEXT_ACTION = 'next_action'

# --- 5. 核心逻辑：锁定与验证 ---

async def handle_password_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    context.application.create_task(auto_delete_message(context, msg.chat_id, msg.message_id, delay=0))
    
    input_pwd = msg.text
    user_id = update.effective_user.id
    
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        
        if hash_password(input_pwd) == user.password_hash:
            user.login_attempts = 0
            await session.commit()
            
            next_action = context.user_data.get(CTX_NEXT_ACTION)
            if next_action == 'wills':
                return await show_will_menu(update, context)
            elif next_action == 'contacts':
                return await show_contacts_menu(update, context)
            elif next_action == 'settings':
                return await show_freq_menu(update, context)
            else:
                await msg.reply_text("✅ 验证通过。", reply_markup=get_main_menu())
                return ConversationHandler.END
        else:
            user.login_attempts += 1
            attempts_left = 5 - user.login_attempts
            
            if attempts_left <= 0:
                user.is_locked = True
                await session.commit()
                await msg.reply_text("⛔️ **密码错误次数过多，账号已锁定！**\n正在通知紧急联系人...", reply_markup=ReplyKeyboardRemove())
                await broadcast_lockout(context, user_id, session)
                return ConversationHandler.END
            else:
                await session.commit()
                prompt = await msg.reply_text(f"❌ **密码错误**\n您还有 {attempts_left} 次机会，否则账号将被锁定。\n请重新输入：")
                context.application.create_task(auto_delete_message(context, user_id, prompt.message_id, delay=10))
                return STATE_VERIFY_PASSWORD

async def broadcast_lockout(context, user_id, session):
    contacts = await get_contacts(session, user_id)
    if not contacts: return
    
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔓 确认身份并解锁账号", callback_data=f"unlock_req_{user_id}")]])
    
    for c in contacts:
        try:
            await context.bot.send_message(
                chat_id=c.contact_chat_id,
                text=f"🚨 **紧急安全警报**\n\n用户 ID `{user_id}` 的账号因多次密码错误被锁定。\n\n如果您确认这是本人操作，请点击下方按钮为他解锁。",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=markup
            )
        except: pass

async def handle_unlock_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    target_user_id = int(query.data.split("_")[2])
    keyboard = [
        [InlineKeyboardButton("✅ 是本人，立即解锁", callback_data=f"unlock_conf_{target_user_id}")],
        [InlineKeyboardButton("🚫 不是本人/不确定", callback_data="unlock_deny")]
    ]
    await query.edit_message_text(
        f"⚠️ **请再次确认**\n\n您确定是用户 `{target_user_id}` 本人要求解锁吗？\n如果是不明身份的人在尝试破解，请点击拒绝。",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def confirm_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "unlock_deny":
        await query.edit_message_text("🚫 操作已拒绝。账号保持锁定状态。")
        return
        
    target_user_id = int(query.data.split("_")[2])
    executor_name = update.effective_user.first_name
    
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, target_user_id)
        user.is_locked = False
        user.login_attempts = 0
        await session.commit()
    
    await query.edit_message_text(f"✅ 已成功解锁用户 ID {target_user_id} 的账号。")
    try:
        await context.bot.send_message(target_user_id, f"🎉 **账号已解锁**\n\n感谢紧急联系人 **{executor_name}** 的协助。\n请务必牢记您的密码，或重新设置。", reply_markup=get_main_menu())
    except: pass

# --- 6. 启动与密码设置 ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, delay=1))
    
    async with AsyncSessionLocal() as session:
        db_user = await get_db_user(session, user.id, user.username)
        
        args = context.args
        if args and args[0].startswith("connect_"):
            target_id = int(args[0].split("_")[1])
            if target_id == user.id:
                await update.message.reply_text("❌ 不能绑定自己。")
                return
            exists = (await session.execute(select(EmergencyContact).where(EmergencyContact.owner_chat_id==target_id, EmergencyContact.contact_chat_id==user.id))).scalar()
            if exists:
                await update.message.reply_text("✅ 已经是联系人了。")
                return
            
            kb = [[InlineKeyboardButton("✅ 接受委托", callback_data=f"accept_bind_{target_id}"), InlineKeyboardButton("🚫 拒绝", callback_data="decline_bind")]]
            await update.message.reply_text(f"🛡️ **收到委托**\nID `{target_id}` 请求绑定。", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
            return

        if not db_user.password_hash:
            await update.message.reply_text(
                "👋 **欢迎使用 死了么LifeSignal**\n\n为了保障隐私，首次使用必须设置 **访问密码**。\n\n👉 **请直接发送您想设置的密码：**"
            )
            return STATE_SET_PASSWORD
        
        await update.message.reply_text(f"👋 欢迎回来，{user.first_name}。", reply_markup=get_main_menu())
        return ConversationHandler.END

async def set_password_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text
    context.application.create_task(auto_delete_message(context, update.effective_user.id, update.message.message_id, delay=0))
    
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, update.effective_user.id)
        user.password_hash = hash_password(pwd)
        await session.commit()
    
    await update.message.reply_text("✅ **密码设置成功！**\n请牢记此密码。若忘记，需通过紧急联系人解锁。", reply_markup=get_main_menu())
    return ConversationHandler.END

# --- 7. 通用验证入口 ---

async def request_password_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    context.application.create_task(auto_delete_message(context, user_id, update.message.message_id, delay=1))
    
    if text == BTN_WILLS: context.user_data[CTX_NEXT_ACTION] = 'wills'
    elif text == BTN_CONTACTS: context.user_data[CTX_NEXT_ACTION] = 'contacts'
    elif text == BTN_SETTINGS: context.user_data[CTX_NEXT_ACTION] = 'settings'
    
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        if user.is_locked:
            await update.message.reply_text("⛔️ **账号已锁定**\n请联系您的紧急联系人进行解锁。")
            return ConversationHandler.END
    
    prompt = await update.message.reply_text("🔐 **身份验证**\n\n访问敏感区域，请输入您的密码：")
    context.application.create_task(auto_delete_message(context, user_id, prompt.message_id, delay=30))
    return STATE_VERIFY_PASSWORD

# --- 8. 遗嘱管理系统 ---

async def show_will_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        wills = await get_wills(session, user_id)
        keyboard = []
        if wills:
            for w in wills:
                try:
                    decrypted = decrypt_data(w.content)
                    preview = decrypted[:10] + "..." if w.msg_type == 'text' else f"[{w.msg_type}]"
                except: preview = "无法解密"
                keyboard.append([InlineKeyboardButton(f"📄 {preview}", callback_data=f"view_will_{w.id}")])
        
        keyboard.append([InlineKeyboardButton("➕ 添加新遗嘱", callback_data="add_will")])
        text = f"📜 **我的遗嘱库**\n\n当前共有 {len(wills)} 份遗嘱。\n每份遗嘱可独立分配给不同的联系人。"
        
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    
    return STATE_WILL_MENU

async def handle_will_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "add_will":
        await query.edit_message_text("📝 **请输入遗嘱内容**\n\n支持文字、图片、视频。\n发送后将加密存储并自动销毁原消息。")
        return STATE_ADD_WILL_CONTENT
    
    if data.startswith("view_will_"):
        will_id = int(data.split("_")[2])
        keyboard = [
            [InlineKeyboardButton("👁 显示完整内容", callback_data=f"reveal_{will_id}")],
            [InlineKeyboardButton("👥 修改接收人", callback_data=f"assign_{will_id}")],
            [InlineKeyboardButton("🗑 删除", callback_data=f"del_will_{will_id}"), InlineKeyboardButton("🔙 返回", callback_data="back_wills")]
        ]
        await query.edit_message_text(f"📄 **遗嘱 #{will_id} 选项**", reply_markup=InlineKeyboardMarkup(keyboard))
        return STATE_WILL_MENU
    
    if data == "back_wills":
        return await show_will_menu(update, context)

    if data.startswith("reveal_"):
        will_id = int(data.split("_")[1])
        async with AsyncSessionLocal() as session:
            will = await session.get(Will, will_id)
            if will:
                content = decrypt_data(will.content)
                if will.msg_type == 'text':
                    msg = await query.message.reply_text(f"🔐 **解密内容** (15秒后销毁)：\n\n{content}", parse_mode=ParseMode.MARKDOWN)
                elif will.msg_type == 'photo':
                    msg = await query.message.reply_photo(content, caption="🔐 **解密图片** (15秒后销毁)")
                context.application.create_task(auto_delete_message(context, update.effective_chat.id, msg.message_id, delay=15))
        return STATE_WILL_MENU

    if data.startswith("del_will_"):
        will_id = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Will).where(Will.id == will_id))
            await session.commit()
        await query.edit_message_text("✅ 遗嘱已删除。")
        return await show_will_menu(update, context)

    if data.startswith("assign_"):
        will_id = int(data.split("_")[1])
        context.user_data['editing_will_id'] = will_id
        return await render_assign_keyboard(update, context)

async def process_add_will_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    context.application.create_task(auto_delete_message(context, msg.chat_id, msg.message_id, 15))
    
    content, w_type = None, 'text'
    if msg.text: content, w_type = encrypt_data(msg.text), 'text'
    elif msg.photo: content, w_type = encrypt_data(msg.photo[-1].file_id), 'photo'
    elif msg.video: content, w_type = encrypt_data(msg.video.file_id), 'video'
    elif msg.voice: content, w_type = encrypt_data(msg.voice.file_id), 'voice'
    else: return STATE_ADD_WILL_CONTENT
    
    context.user_data['new_will_content'] = content
    context.user_data['new_will_type'] = w_type
    context.user_data['selected_recipients'] = [] 
    
    return await render_assign_keyboard(update, context, is_new=True)

async def render_assign_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE, is_new=False):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        contacts = await get_contacts(session, user_id)
        
        selected = context.user_data.get('selected_recipients', [])
        if not is_new and not selected:
             will_id = context.user_data.get('editing_will_id')
             will = await session.get(Will, will_id)
             if will and will.recipient_ids:
                 selected = [int(x) for x in will.recipient_ids.split(",") if x]
                 context.user_data['selected_recipients'] = selected

        keyboard = []
        for c in contacts:
            mark = "✅" if c.contact_chat_id in selected else "⭕️"
            keyboard.append([InlineKeyboardButton(f"{mark} {c.contact_name}", callback_data=f"toggle_rec_{c.contact_chat_id}")])
        
        btn_text = "💾 保存遗嘱"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data="save_will_final")])
        
        text = "👥 **分配接收人**\n\n请勾选此遗嘱要发送给谁："
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            
    return STATE_ADD_WILL_RECIPIENTS

async def handle_assign_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("toggle_rec_"):
        contact_id = int(data.split("_")[2])
        selected = context.user_data.get('selected_recipients', [])
        if contact_id in selected: selected.remove(contact_id)
        else: selected.append(contact_id)
        context.user_data['selected_recipients'] = selected
        is_new = 'new_will_content' in context.user_data
        return await render_assign_keyboard(update, context, is_new)
    
    if data == "save_will_final":
        selected = context.user_data.get('selected_recipients', [])
        rec_str = ",".join(map(str, selected))
        user_id = update.effective_user.id
        
        async with AsyncSessionLocal() as session:
            if 'new_will_content' in context.user_data:
                new_will = Will(
                    user_id=user_id,
                    content=context.user_data['new_will_content'],
                    msg_type=context.user_data['new_will_type'],
                    recipient_ids=rec_str
                )
                session.add(new_will)
                del context.user_data['new_will_content']
            else:
                will_id = context.user_data.get('editing_will_id')
                will = await session.get(Will, will_id)
                if will: will.recipient_ids = rec_str
            
            await session.commit()
        
        await query.edit_message_text("✅ 遗嘱保存成功。")
        return await show_will_menu(update, context)

# --- 9. 联系人管理 (修复版) ---

# ✅ 修复: 添加缺失的 confirm_bind_callback
async def confirm_bind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """联系人同意绑定"""
    query = update.callback_query
    await query.answer()
    data = query.data
    executor = update.effective_user
    
    if data == "decline_bind":
        await query.edit_message_text("🚫 您已婉拒该委托。")
        return
    
    requester_id = int(data.split("_")[2])
    async with AsyncSessionLocal() as session:
        # Check existing
        existing = (await session.execute(select(EmergencyContact).where(
            EmergencyContact.owner_chat_id == requester_id,
            EmergencyContact.contact_chat_id == executor.id
        ))).scalar()
        
        if existing:
            await query.edit_message_text("✅ 您已经是对方的联系人了。")
            return
            
        # Check limit
        count = await get_contact_count(session, requester_id)
        if count >= 10:
            await query.edit_message_text("⚠️ 对方联系人列表已满 (10人)，绑定失败。")
            return

        # Add
        session.add(EmergencyContact(
            owner_chat_id=requester_id,
            contact_chat_id=executor.id,
            contact_name=executor.first_name
        ))
        await get_db_user(session, executor.id)
        await session.commit()
    
    await query.edit_message_text(f"✅ 绑定成功！您已成为 ID {requester_id} 的紧急联系人。")
    try:
        await context.bot.send_message(requester_id, f"🎉 **{executor.first_name}** 已接受邀请，成为您的紧急联系人！")
    except: pass

async def show_contacts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        contacts = await get_contacts(session, user_id)
        keyboard = []
        for c in contacts:
            keyboard.append([InlineKeyboardButton(f"👤 {c.contact_name}", callback_data="noop"), InlineKeyboardButton("❌ 解绑", callback_data=f"try_unbind_{c.id}")])
        if len(contacts) < 10:
            keyboard.append([InlineKeyboardButton("➕ 邀请新联系人", switch_inline_query="invite")])
        
        text = f"👥 **联系人管理 ({len(contacts)}/10)**\n\n点击解绑可移除。"
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

async def try_unbind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    contact_db_id = int(query.data.split("_")[2])
    
    async with AsyncSessionLocal() as session:
        contact = await session.get(EmergencyContact, contact_db_id)
        if not contact: return
        
        wills = await get_wills(session, contact.owner_chat_id)
        is_assigned = False
        for w in wills:
            if w.recipient_ids and str(contact.contact_chat_id) in w.recipient_ids.split(","):
                is_assigned = True
                break
        
        if is_assigned:
            keyboard = [[InlineKeyboardButton("⚠️ 确认解绑", callback_data=f"confirm_unbind_{contact_db_id}"), InlineKeyboardButton("取消", callback_data="cancel_action")]]
            await query.edit_message_text(f"⚠️ **高危操作警告**\n\n联系人 **{contact.contact_name}** 已被分配了一份或多份遗嘱。\n\n解绑后，他将**不再接收**这些遗嘱。\n您确认要继续吗？", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        else:
            await perform_unbind(update, context, contact, session)

async def confirm_unbind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    contact_db_id = int(query.data.split("_")[2])
    async with AsyncSessionLocal() as session:
        contact = await session.get(EmergencyContact, contact_db_id)
        if contact: await perform_unbind(update, context, contact, session)

async def perform_unbind(update, context, contact, session):
    c_id, owner_id, name = contact.contact_chat_id, contact.owner_chat_id, contact.contact_name
    await session.delete(contact)
    await session.commit()
    await update.callback_query.message.edit_text(f"✅ 已解绑 {name}。")
    try: await context.bot.send_message(c_id, f"ℹ️ 用户 {owner_id} 已解绑您。")
    except: pass

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("✅ 操作已取消。")
    return ConversationHandler.END

# --- 10. 频率设置 ---
async def show_freq_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("1 天", callback_data="set_freq_24"), InlineKeyboardButton("3 天", callback_data="set_freq_72"), InlineKeyboardButton("7 天", callback_data="set_freq_168")]]
    await update.message.reply_text("⚙️ **设置确认频率**", reply_markup=InlineKeyboardMarkup(keyboard))
    return STATE_FREQ_SELECT

async def handle_freq_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    hours = int(query.data.split("_")[2])
    
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, update.effective_user.id)
        user.check_frequency = hours
        await session.commit()
    
    await query.edit_message_text(f"✅ 频率已更新为：{int(hours/24)} 天。")
    return ConversationHandler.END

# --- 杂项 ---
async def handle_im_safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 1))
    
    async with AsyncSessionLocal() as session:
        db_user = await get_db_user(session, user.id)
        contacts = await get_contacts(session, user.id)
        if not contacts:
            msg = await update.message.reply_text("⚠️ 请先绑定联系人。", reply_markup=get_main_menu())
            context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 5))
            return
        db_user.last_active = datetime.now(timezone.utc)
        db_user.status = 'active'
        await session.commit()
    
    msg = await update.message.reply_text("✅ 已确认安全。", reply_markup=get_main_menu())
    context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 5))

async def handle_security(update, context):
    context.application.create_task(auto_delete_message(context, update.effective_chat.id, update.message.message_id, delay=1))
    text = "🛡️ **透明是信任的基石**\n\n点击下方按钮查看源代码。"
    keyboard = [[InlineKeyboardButton("👨‍💻 GitHub 源码", url=GITHUB_REPO_URL)]]
    await update.message.reply_markdown(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def inline_query_handler(update, context):
    query = update.inline_query.query
    user = update.effective_user
    if query == "invite":
        link = f"https://t.me/{context.bot.username}?start=connect_{user.id}"
        results = [InlineQueryResultArticle(id=str(uuid4()), title="邀请联系人", input_message_content=InputTextMessageContent(f"📩 **来自 {user.first_name} 的信任委托**\n\n我希望将你设为我的紧急联系人。\n👇 **请点击下方链接接受：**", parse_mode=ParseMode.MARKDOWN), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ 接受委托", url=link)]]))]
        await update.inline_query.answer(results)

async def check_dead_mans_switch(app: Application):
    # 定时任务逻辑保留
    pass 

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

def main():
    persistence = PicklePersistence(filepath='persistence.pickle')
    app = Application.builder().token(TOKEN).persistence(persistence).build()

    auth_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^({BTN_WILLS}|{BTN_CONTACTS}|{BTN_SETTINGS})$"), request_password_entry)
        ],
        states={
            STATE_VERIFY_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password_verification)],
            STATE_WILL_MENU: [CallbackQueryHandler(handle_will_menu_callback)],
            STATE_ADD_WILL_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, process_add_will_content)],
            STATE_ADD_WILL_RECIPIENTS: [CallbackQueryHandler(handle_assign_callback)],
            STATE_FREQ_SELECT: [CallbackQueryHandler(handle_freq_set)]
        },
        fallbacks=[CommandHandler("cancel", cancel_action), CallbackQueryHandler(cancel_action, pattern="^cancel_action")],
        name="auth_conversation", persistent=True
    )
    
    setup_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={STATE_SET_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_password_finish)]},
        fallbacks=[],
        name="onboarding"
    )

    app.add_handler(setup_handler)
    app.add_handler(auth_handler)
    
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SAFE}$"), handle_im_safe))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SECURITY}$"), handle_security))
    
    app.add_handler(CallbackQueryHandler(confirm_bind_callback, pattern="^accept_bind_"))
    app.add_handler(CallbackQueryHandler(confirm_unlock, pattern="^(unlock_conf|unlock_deny)"))
    app.add_handler(CallbackQueryHandler(handle_unlock_request, pattern="^unlock_req_"))
    app.add_handler(CallbackQueryHandler(try_unbind_callback, pattern="^try_unbind_"))
    app.add_handler(CallbackQueryHandler(confirm_unbind_callback, pattern="^confirm_unbind_"))
    
    app.add_handler(InlineQueryHandler(inline_query_handler))

    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_dead_mans_switch, 'interval', hours=1, args=[app])
    scheduler.start()
    
    print("🚀 死了么LifeSignal Ultimate Bot is running...")
    app.run_polling()

if __name__ == '__main__':
    main()
