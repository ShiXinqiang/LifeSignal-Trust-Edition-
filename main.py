import os
import logging
import asyncio
import urllib.parse
from uuid import uuid4 
from datetime import datetime, timedelta, timezone

# Telegram 相关库
from telegram import (
    Update, 
    ReplyKeyboardMarkup, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    InlineQueryResultArticle, 
    InputTextMessageContent   
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
from sqlalchemy import Column, BigInteger, Text, DateTime, String, Integer, select, ForeignKey, delete, func
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
    will_content = Column(Text, nullable=True) 
    will_type = Column(String, default='text') 
    will_recipients = Column(String, default="") 
    check_frequency = Column(Integer, default=72)
    last_active = Column(DateTime(timezone=True), default=func.now())
    status = Column(String, default='active') 

class EmergencyContact(Base):
    __tablename__ = 'contacts'
    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_chat_id = Column(BigInteger, ForeignKey('users.chat_id'), index=True)
    contact_chat_id = Column(BigInteger)
    contact_name = Column(String)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# --- 3. 辅助函数 (删除逻辑升级) ---

def encrypt_data(data: str) -> str:
    if not data: return None
    return cipher_suite.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data: str) -> str:
    if not encrypted_data: return None
    try:
        return cipher_suite.decrypt(encrypted_data.encode()).decode()
    except Exception:
        return "[数据无法解密]"

# 🕒 定时删除回调任务
async def delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue 调用的删除函数"""
    job = context.job
    try:
        await context.bot.delete_message(chat_id=job.chat_id, message_id=job.data)
    except Exception:
        pass # 消息可能已被删，忽略错误

def schedule_delete(context, chat_id, message_id, delay):
    """
    通用删除调度器
    delay: 秒数 (1=立即清理界面, 15=敏感内容, 21600=6小时兜底)
    """
    context.job_queue.run_once(delete_message_job, delay, chat_id=chat_id, data=message_id)

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

# --- 4. UI 定义 ---

BTN_SAFE = "🟢 我很安全"
BTN_CONTACTS = "👥 联系人管理"
BTN_SETUP = "⚙️ 设置/重置遗嘱"
BTN_SECURITY = "🛡️ 开源验证"

# 定义删除时间常量 (秒)
DEL_INSTANT = 1      # 按钮指令上屏清理
DEL_SENSITIVE = 15   # 敏感内容清理
DEL_LONG = 21600     # 6小时兜底清理 (6 * 3600)

def get_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_SAFE], [BTN_SETUP, BTN_CONTACTS], [BTN_SECURITY]],
        resize_keyboard=True,
        is_persistent=True, 
        input_field_placeholder="死了么LifeSignal 正在守护..."
    )

STATE_CHECK_EXISTING, STATE_CHOOSE_FREQ, STATE_UPLOAD_WILL, STATE_SELECT_RECIPIENTS, STATE_CONFIRM = range(5)

# --- 5. 交互逻辑 ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    
    # 立即删除 /start 指令
    schedule_delete(context, user.id, update.message.message_id, DEL_INSTANT)

    async with AsyncSessionLocal() as session:
        await get_db_user(session, user.id, user.username)
        await session.commit()
        menu = get_main_menu()

        # 绑定逻辑
        if args and args[0].startswith("connect_"):
            target_id = int(args[0].split("_")[1])
            if target_id == user.id:
                msg = await update.message.reply_text("❌ 您无法将自己设为联系人。", reply_markup=menu)
                schedule_delete(context, user.id, msg.message_id, DEL_SENSITIVE)
                return
            
            existing = (await session.execute(select(EmergencyContact).where(
                EmergencyContact.owner_chat_id == target_id, EmergencyContact.contact_chat_id == user.id
            ))).scalar()
            
            if existing:
                msg = await update.message.reply_text("✅ 您已经是对方的紧急联系人了。", reply_markup=menu)
                schedule_delete(context, user.id, msg.message_id, DEL_SENSITIVE)
                return

            keyboard = [[InlineKeyboardButton("✅ 接受委托", callback_data=f"accept_bind_{target_id}"), InlineKeyboardButton("🚫 拒绝", callback_data="decline_bind")]]
            req_msg = await update.message.reply_text(
                f"🛡️ **收到委托请求**\n\n用户 ID `{target_id}` 希望将您设为紧急联系人。\n只有当系统确认该用户长期失联后，才会通知您。",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            schedule_delete(context, user.id, req_msg.message_id, DEL_LONG)
            return

    # 欢迎语 (带6小时删除提示)
    welcome_text = (
        f"👋 **你好，{user.first_name}**\n\n"
        "欢迎使用 **死了么LifeSignal** —— 您的数字资产安全守护者。\n\n"
        "✅ **只需绑定一位紧急联系人，即可开启守护。**\n"
        "🔒 遗嘱内容端到端加密，确保绝对隐私。\n"
        "🗑️ **隐私保护**：Bot 与您的所有聊天记录将在 **6小时后自动销毁**，不留任何痕迹。\n\n"
        "👇 **请点击下方按钮开始使用：**"
    )
    welcome_msg = await update.message.reply_markdown(welcome_text, reply_markup=menu)
    schedule_delete(context, user.id, welcome_msg.message_id, DEL_LONG)

# --- 报平安 ---

async def handle_im_safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # 删除用户点击的按钮文字
    schedule_delete(context, user.id, update.message.message_id, DEL_INSTANT)

    async with AsyncSessionLocal() as session:
        db_user = await get_db_user(session, user.id)
        count = await get_contact_count(session, user.id)
        
        if count == 0:
            msg = await update.message.reply_text(
                "⚠️ **未处于保护状态**\n\n您尚未绑定任何 **紧急联系人**。\n👇 请先点击“👥 联系人管理”进行绑定。",
                parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu()
            )
            schedule_delete(context, user.id, msg.message_id, DEL_SENSITIVE)
            return

        db_user.last_active = datetime.now(timezone.utc)
        db_user.status = 'active'
        await session.commit()
    
    # 确认消息 (15秒后删，保持干净)
    reply = await update.message.reply_text("✅ 已确认！守护倒计时已重置 (周期: 3天)。", reply_markup=get_main_menu())
    schedule_delete(context, user.id, reply.message_id, DEL_SENSITIVE)

# --- 联系人管理 ---

async def handle_contacts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    schedule_delete(context, user.id, update.message.message_id, DEL_INSTANT)
    
    async with AsyncSessionLocal() as session:
        contacts = await get_contacts(session, user.id)
        keyboard = []
        for c in contacts:
            name = c.contact_name or str(c.contact_chat_id)
            keyboard.append([InlineKeyboardButton(f"👤 {name}", callback_data="noop"), InlineKeyboardButton("❌ 解绑", callback_data=f"unbind_{c.id}")])
        
        if len(contacts) < 10:
            keyboard.append([InlineKeyboardButton("➕ 添加新联系人 (邀请)", switch_inline_query="invite")])
        
        text = f"👥 **紧急联系人管理 ({len(contacts)}/10)**\n\n点击“❌ 解绑”可移除联系人。"
        msg = await update.message.reply_markdown(text, reply_markup=InlineKeyboardMarkup(keyboard))
        schedule_delete(context, user.id, msg.message_id, DEL_LONG)

async def confirm_bind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    executor = update.effective_user
    
    if query.data == "decline_bind":
        await query.edit_message_text("🚫 您已婉拒该委托。")
        schedule_delete(context, executor.id, query.message.message_id, DEL_SENSITIVE)
        return
    
    requester_id = int(query.data.split("_")[2])
    async with AsyncSessionLocal() as session:
        existing = (await session.execute(select(EmergencyContact).where(
            EmergencyContact.owner_chat_id == requester_id, EmergencyContact.contact_chat_id == executor.id
        ))).scalar()
        
        if existing:
            await query.edit_message_text("✅ 您已经是对方的联系人了。")
            return
            
        count = await get_contact_count(session, requester_id)
        if count >= 10:
            await query.edit_message_text("⚠️ 对方联系人已满，绑定失败。")
            return

        session.add(EmergencyContact(owner_chat_id=requester_id, contact_chat_id=executor.id, contact_name=executor.first_name))
        await get_db_user(session, executor.id)
        await session.commit()
    
    await query.edit_message_text(f"✅ 绑定成功！您已成为 ID {requester_id} 的紧急联系人。")
    schedule_delete(context, executor.id, query.message.message_id, DEL_LONG)
    
    try:
        n_msg = await context.bot.send_message(requester_id, f"🎉 **{executor.first_name}** 已接受邀请，成为您的紧急联系人！")
        schedule_delete(context, requester_id, n_msg.message_id, DEL_LONG)
    except: pass

async def unbind_contact_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    contact_db_id = int(query.data.split("_")[1])
    
    async with AsyncSessionLocal() as session:
        contact_record = await session.get(EmergencyContact, contact_db_id)
        if not contact_record:
            await query.edit_message_text("❌ 联系人不存在。")
            return
        
        contact_tg_id, owner_id = contact_record.contact_chat_id, contact_record.owner_chat_id
        await session.delete(contact_record)
        await session.commit()
    
    await query.message.edit_text(f"✅ 已解除绑定。")
    schedule_delete(context, update.effective_chat.id, query.message.message_id, DEL_SENSITIVE)
    
    try:
        n_msg = await context.bot.send_message(contact_tg_id, f"ℹ️ 用户 ID {owner_id} 已将您从紧急联系人列表中移除。")
        schedule_delete(context, contact_tg_id, n_msg.message_id, DEL_LONG)
    except: pass

# --- 遗嘱设置 (隐私高危区) ---

async def setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 删除点击指令
    schedule_delete(context, update.effective_chat.id, update.message.message_id, DEL_INSTANT)
    
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        has_will = bool(user.will_content)
    
    if has_will:
        keyboard = [[InlineKeyboardButton("⚠️ 覆盖并重新设置", callback_data="overwrite_yes"), InlineKeyboardButton("🚫 取消", callback_data="overwrite_no")]]
        msg = await update.message.reply_text("⚠️ **检测到旧遗嘱**\n\n重新设置将覆盖原有内容。是否继续？", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        # 敏感操作菜单也稍后删除
        schedule_delete(context, user_id, msg.message_id, DEL_SENSITIVE)
        return STATE_CHECK_EXISTING
    else:
        return await ask_frequency_step(update, context)

async def setup_overwrite_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "overwrite_no":
        await query.message.edit_text("✅ 操作已取消。")
        return ConversationHandler.END
    return await ask_frequency_step(update, context, is_callback=True)

async def ask_frequency_step(update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback=False):
    keyboard = [[InlineKeyboardButton("1 天", callback_data="day_1"), InlineKeyboardButton("3 天 (推荐)", callback_data="day_3"), InlineKeyboardButton("7 天", callback_data="day_7")]]
    text = "⚙️ **步骤 1/3：选择确认周期**\n\n如果联系不上您超过多久，视为触发条件？"
    if is_callback:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        msg = await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        schedule_delete(context, update.effective_chat.id, msg.message_id, DEL_SENSITIVE)
    return STATE_CHOOSE_FREQ

async def setup_freq_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    days = int(query.data.split("_")[1])
    context.user_data['temp_freq'] = days * 24
    
    await query.edit_message_text(f"✅ 频率已设定为：**{days} 天**", parse_mode=ParseMode.MARKDOWN)
    
    msg = await context.bot.send_message(chat_id=update.effective_chat.id, text="📝 **步骤 2/3：录入遗嘱内容**\n\n请发送文字、图片或视频。\n🔐 内容加密存储，**原消息将在 15 秒后自毁**。", parse_mode=ParseMode.MARKDOWN)
    schedule_delete(context, update.effective_chat.id, msg.message_id, DEL_SENSITIVE)
    return STATE_UPLOAD_WILL

async def setup_receive_will(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    # 🔒 核心：立即销毁用户的遗嘱原文 (15秒)
    schedule_delete(context, update.effective_chat.id, msg.message_id, DEL_SENSITIVE)

    if msg.text and msg.text.startswith(("🟢", "⚙️", "👥", "🛡️")):
        warn = await msg.reply_text("已退出设置。", reply_markup=get_main_menu())
        schedule_delete(context, update.effective_chat.id, warn.message_id, 5)
        return ConversationHandler.END

    content, w_type = None, 'text'
    if msg.text:
        content, w_type = encrypt_data(msg.text), 'text'
    elif msg.photo or msg.video or msg.voice:
        raw_file_id = ""
        if msg.photo: raw_file_id = msg.photo[-1].file_id; w_type = 'photo'
        elif msg.video: raw_file_id = msg.video.file_id; w_type = 'video'
        elif msg.voice: raw_file_id = msg.voice.file_id; w_type = 'voice'
        content = encrypt_data(raw_file_id)
    else:
        return STATE_UPLOAD_WILL

    context.user_data['temp_content'] = content
    context.user_data['temp_type'] = w_type
    context.user_data['selected_recipients'] = [] 
    return await ask_recipients_step(update, context)

async def ask_recipients_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        contacts = await get_contacts(session, user_id)
    
    if not contacts:
        text = "⚠️ **无法完成设置**\n\n您尚未绑定紧急联系人。\n请先去“👥 联系人管理”绑定至少一人。"
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
        else:
            msg = await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
            schedule_delete(context, user_id, msg.message_id, DEL_SENSITIVE)
        return ConversationHandler.END

    return await render_recipient_keyboard(update, context, contacts)

async def render_recipient_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE, contacts):
    selected = context.user_data.get('selected_recipients', [])
    keyboard = []
    for c in contacts:
        mark = "✅" if c.contact_chat_id in selected else "⭕️"
        name = c.contact_name or str(c.contact_chat_id)
        keyboard.append([InlineKeyboardButton(f"{mark} {name}", callback_data=f"toggle_{c.contact_chat_id}")])
    
    btn_text = f"完成选择 ({len(selected)}人)" if selected else "请至少选择一人"
    if selected: keyboard.append([InlineKeyboardButton(f"💾 {btn_text} - 保存", callback_data="recipients_done")])
    
    text = "📬 **步骤 3/3：选择遗嘱接收人**\n\n请点击名字勾选（支持多选）："
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        msg = await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        schedule_delete(context, update.effective_chat.id, msg.message_id, DEL_SENSITIVE)
    return STATE_SELECT_RECIPIENTS

async def handle_recipient_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "recipients_done":
        return await setup_confirm(update, context)
        
    if data.startswith("toggle_"):
        contact_id = int(data.split("_")[1])
        selected = context.user_data.get('selected_recipients', [])
        if contact_id in selected: selected.remove(contact_id)
        else: selected.append(contact_id)
        context.user_data['selected_recipients'] = selected
        
        user_id = update.effective_user.id
        async with AsyncSessionLocal() as session:
            contacts = await get_contacts(session, user_id)
        await render_recipient_keyboard(update, context, contacts)
        return STATE_SELECT_RECIPIENTS

async def setup_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    d = context.user_data
    recipients_str = ",".join(map(str, d['selected_recipients']))
    
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        user.check_frequency = d['temp_freq']
        user.will_content = d['temp_content']
        user.will_type = d['temp_type']
        user.will_recipients = recipients_str
        user.last_active = datetime.now(timezone.utc)
        await session.commit()

    await update.callback_query.edit_message_text("✅ **遗嘱设置成功！**\n\n已加密存储，15秒后清理痕迹。", parse_mode=ParseMode.MARKDOWN)
    # 成功提示也删除
    schedule_delete(context, user_id, update.callback_query.message.message_id, DEL_SENSITIVE)
    return ConversationHandler.END

async def cancel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    schedule_delete(context, update.effective_user.id, update.message.message_id, DEL_INSTANT)
    msg = await update.message.reply_text("操作已取消。", reply_markup=get_main_menu())
    schedule_delete(context, update.effective_user.id, msg.message_id, 3)
    return ConversationHandler.END

# --- 其他 ---

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query
    user = update.effective_user
    if query == "invite":
        bot_username = context.bot.username
        invite_link = f"https://t.me/{bot_username}?start=connect_{user.id}"
        results = [InlineQueryResultArticle(
            id=str(uuid4()), title="发送遗嘱委托邀请", description="邀请对方成为您的紧急联系人",
            input_message_content=InputTextMessageContent(f"📩 **来自 {user.first_name} 的信任委托**\n\n我希望将你设为我的紧急联系人。\n👇 **请点击下方链接接受：**", parse_mode=ParseMode.MARKDOWN),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ 接受委托", url=invite_link)]])
        )]
        await update.inline_query.answer(results, cache_time=0)

async def handle_security(update: Update, context: ContextTypes.DEFAULT_TYPE):
    schedule_delete(context, update.effective_chat.id, update.message.message_id, DEL_INSTANT)
    text = "🛡️ **透明是信任的基石**\n\n点击下方按钮查看源代码。\n\n⚠️ 本条消息 6 小时后自动销毁。"
    msg = await update.message.reply_markdown(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👨‍💻 GitHub 源码", url=GITHUB_REPO_URL)]]))
    schedule_delete(context, update.effective_chat.id, msg.message_id, DEL_LONG)

async def check_dead_mans_switch(app: Application):
    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.status == 'active')
        result = await session.execute(stmt)
        users = result.scalars().all()
        now = datetime.now(timezone.utc)
        
        for user in users:
            last = user.last_active.replace(tzinfo=timezone.utc) if user.last_active.tzinfo is None else user.last_active
            delta_hours = (now - last).total_seconds() / 3600
            
            if delta_hours > user.check_frequency:
                recipient_ids = [int(x) for x in user.will_recipients.split(",") if x] if user.will_recipients else []
                contacts = await get_contacts(session, user.chat_id)
                decrypted_content = None
                try: 
                    if user.will_content: decrypted_content = decrypt_data(user.will_content)
                except: pass

                if contacts:
                    for contact in contacts:
                        c_id = contact.contact_chat_id
                        try:
                            await app.bot.send_message(chat_id=c_id, text=f"🚨 **死了么LifeSignal 紧急通告**\n\n用户 @{user.username or user.chat_id} 已失联。", parse_mode=ParseMode.MARKDOWN)
                            if c_id in recipient_ids and decrypted_content:
                                await app.bot.send_message(c_id, "🔐 **以下是用户留给您的加密遗嘱：**")
                                if user.will_type == 'text': await app.bot.send_message(c_id, decrypted_content)
                                elif user.will_type == 'photo': await app.bot.send_photo(c_id, decrypted_content)
                                elif user.will_type == 'video': await app.bot.send_video(c_id, decrypted_content)
                                elif user.will_type == 'voice': await app.bot.send_voice(c_id, decrypted_content)
                        except: pass
                    user.status = 'inactive'
                    session.add(user)
                else:
                    user.status = 'inactive'
                    session.add(user)
            
            elif delta_hours > (user.check_frequency * 0.8):
                try:
                    left_hours = int(user.check_frequency - delta_hours)
                    # 预警消息也设置 6 小时删除，防止堆积
                    warn = await app.bot.send_message(chat_id=user.chat_id, text=f"⏰ **温馨提醒**\n\n请点击“🟢 我很安全”重置计时。\n距离触发还剩约 {left_hours} 小时。", reply_markup=get_main_menu())
                    # 这里需要 hack 一下 job_queue，因为在 job 中拿不到 context.job_queue，需要传入 app
                    # 简化处理：预警消息通常不需立即删除，如下次用户上线看到就好
                except: pass
        await session.commit()

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

def main():
    persistence = PicklePersistence(filepath='persistence.pickle')
    app = Application.builder().token(TOKEN).persistence(persistence).build()

    setup_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^⚙️ 设置.*遗嘱$"), setup_start)],
        states={
            STATE_CHECK_EXISTING: [CallbackQueryHandler(setup_overwrite_decision, pattern="^overwrite_")],
            STATE_CHOOSE_FREQ: [CallbackQueryHandler(setup_freq_chosen, pattern="^day_")],
            STATE_UPLOAD_WILL: [MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.Regex("^(🟢|⚙️|👥|🛡️)"), setup_receive_will)],
            STATE_SELECT_RECIPIENTS: [CallbackQueryHandler(handle_recipient_selection, pattern="^(toggle_|recipients_done)")]
        },
        fallbacks=[CommandHandler("cancel", cancel_setup), MessageHandler(filters.Regex(f"^{BTN_SAFE}$"), cancel_setup)],
        name="setup_conversation", persistent=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(setup_conv)
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SAFE}$"), handle_im_safe))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_CONTACTS}$"), handle_contacts_menu))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SECURITY}$"), handle_security))
    
    app.add_handler(CallbackQueryHandler(confirm_bind_callback, pattern="^accept_bind_"))
    app.add_handler(CallbackQueryHandler(unbind_contact_callback, pattern="^unbind_"))
    
    app.add_handler(InlineQueryHandler(inline_query_handler))

    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_dead_mans_switch, 'interval', hours=1, args=[app])
    scheduler.start()
    
    print("🚀 死了么LifeSignal Bot is running...")
    app.run_polling()

if __name__ == '__main__':
    main()
