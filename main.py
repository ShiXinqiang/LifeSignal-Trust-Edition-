import os
import logging
import asyncio
import urllib.parse
import json
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

# --- 2. 数据库模型 (升级版) ---
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    chat_id = Column(BigInteger, primary_key=True)
    username = Column(String, nullable=True)
    
    # 遗嘱内容 (加密)
    will_content = Column(Text, nullable=True) 
    will_type = Column(String, default='text') 
    
    # 遗嘱接收人列表 (存储 ID 字符串，逗号分隔，例如 "123,456")
    will_recipients = Column(String, default="") 
    
    # 机制 (默认 72 小时 / 3天)
    check_frequency = Column(Integer, default=72)
    last_active = Column(DateTime(timezone=True), default=func.now())
    status = Column(String, default='active') 

class EmergencyContact(Base):
    __tablename__ = 'contacts'
    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_chat_id = Column(BigInteger, ForeignKey('users.chat_id'), index=True) # 谁的联系人
    contact_chat_id = Column(BigInteger) # 联系人的 TG ID
    contact_name = Column(String) # 联系人名字

# 异步数据库引擎
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# --- 3. 辅助函数 ---

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
    """自动销毁消息"""
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

# --- 4. UI 定义 ---

BTN_SAFE = "🟢 我很安全"
BTN_CONTACTS = "👥 联系人管理" # 更名：更准确
BTN_SETUP = "⚙️ 设置/重置遗嘱" # 统一定义
BTN_SECURITY = "🛡️ 开源验证"

def get_main_menu() -> ReplyKeyboardMarkup:
    """底部常驻菜单"""
    return ReplyKeyboardMarkup(
        [
            [BTN_SAFE],
            [BTN_SETUP, BTN_CONTACTS],
            [BTN_SECURITY]
        ],
        resize_keyboard=True,
        is_persistent=True, 
        input_field_placeholder="死了么LifeSignal 正在守护..."
    )

# 状态定义
STATE_CHECK_EXISTING, STATE_CHOOSE_FREQ, STATE_UPLOAD_WILL, STATE_SELECT_RECIPIENTS, STATE_CONFIRM = range(5)

# --- 5. 交互逻辑 ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """启动与深度链接"""
    user = update.effective_user
    args = context.args
    
    # 立即删除 start 指令
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, delay=5))

    async with AsyncSessionLocal() as session:
        await get_db_user(session, user.id, user.username)
        await session.commit()
        menu = get_main_menu()

        # 处理绑定请求 connect_{requester_id}
        if args and args[0].startswith("connect_"):
            target_id = int(args[0].split("_")[1])
            if target_id == user.id:
                msg = await update.message.reply_text("❌ 您无法将自己设为联系人。", reply_markup=menu)
                context.application.create_task(auto_delete_message(context, user.id, msg.message_id, delay=5))
                return
            
            # 检查是否已经是联系人
            existing_stmt = select(EmergencyContact).where(
                EmergencyContact.owner_chat_id == target_id,
                EmergencyContact.contact_chat_id == user.id
            )
            existing = (await session.execute(existing_stmt)).scalar()
            
            if existing:
                msg = await update.message.reply_text("✅ 您已经是对方的紧急联系人了，无需重复绑定。", reply_markup=menu)
                context.application.create_task(auto_delete_message(context, user.id, msg.message_id, delay=10))
                return

            keyboard = [
                [InlineKeyboardButton("✅ 接受委托", callback_data=f"accept_bind_{target_id}")],
                [InlineKeyboardButton("🚫 拒绝", callback_data="decline_bind")]
            ]
            await update.message.reply_text(
                f"🛡️ **收到委托请求**\n\n用户 ID `{target_id}` 希望将您设为紧急联系人。\n\n"
                f"**机制说明**：\n只有当系统确认该用户长期失联后，才会通知您（如果他设置了遗嘱给您）。在此之前，您的隐私受到严格保护。",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            return

    welcome_text = (
        f"👋 **你好，{user.first_name}**\n\n"
        "欢迎使用 **死了么LifeSignal** —— 您的数字资产安全守护者。\n\n"
        "✅ **只需绑定一位紧急联系人，即可开启守护。**\n"
        "🔒 遗嘱内容端到端加密，并支持阅后即焚。\n\n"
        "👇 **请点击下方按钮开始使用：**"
    )
    await update.message.reply_markdown(welcome_text, reply_markup=menu)

# --- 报平安逻辑 (Logic Update) ---

async def handle_im_safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # 立即删除用户指令
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, delay=1))

    async with AsyncSessionLocal() as session:
        db_user = await get_db_user(session, user.id)
        # 检查联系人数量
        contact_count = await get_contact_count(session, user.id)
        
        # 只要有联系人，就允许报平安
        if contact_count == 0:
            msg = await update.message.reply_text(
                "⚠️ **未处于保护状态**\n\n"
                "您尚未绑定任何 **紧急联系人**。\n"
                "如果发生意外，机器人无法通知任何人。\n\n"
                "👇 请先点击“👥 联系人管理”进行绑定。",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_main_menu()
            )
            # 警告消息保留久一点
            context.application.create_task(auto_delete_message(context, user.id, msg.message_id, delay=20))
            return

        db_user.last_active = datetime.now(timezone.utc)
        db_user.status = 'active'
        await session.commit()
    
    # 反馈并销毁
    reply = await update.message.reply_text("✅ 已确认！守护倒计时已重置 (周期: 3天)。", reply_markup=get_main_menu())
    context.application.create_task(auto_delete_message(context, user.id, reply.message_id, delay=15))

# --- 联系人管理 (New Logic) ---

async def handle_contacts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示联系人列表和操作面板"""
    user = update.effective_user
    # 删除指令
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, delay=1))
    
    async with AsyncSessionLocal() as session:
        contacts = await get_contacts(session, user.id)
        
        keyboard = []
        # 列出所有联系人，支持解绑
        for c in contacts:
            name = c.contact_name or str(c.contact_chat_id)
            # 按钮显示：👤 名字 [解绑]
            keyboard.append([InlineKeyboardButton(f"👤 {name}", callback_data="noop"), 
                             InlineKeyboardButton("❌ 解绑", callback_data=f"unbind_{c.id}")])
        
        # 如果未满10人，显示添加按钮
        if len(contacts) < 10:
            keyboard.append([InlineKeyboardButton("➕ 添加新联系人 (邀请)", switch_inline_query="invite")])
        
        count_info = f"当前联系人：{len(contacts)}/10"
        
        text = (
            f"👥 **紧急联系人管理**\n\n"
            f"{count_info}\n"
            "您绑定的联系人将在您失联时收到通知。\n"
            "点击“❌ 解绑”可移除联系人 (对方会收到通知)。"
        )
        await update.message.reply_markdown(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def confirm_bind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """联系人同意绑定"""
    query = update.callback_query
    await query.answer()
    data = query.data
    executor = update.effective_user # 接受者
    
    if data == "decline_bind":
        await query.edit_message_text("🚫 您已婉拒该委托。")
        return
    
    requester_id = int(data.split("_")[2]) # 发起者
    
    async with AsyncSessionLocal() as session:
        # 检查是否已存在
        existing = (await session.execute(select(EmergencyContact).where(
            EmergencyContact.owner_chat_id == requester_id,
            EmergencyContact.contact_chat_id == executor.id
        ))).scalar()
        
        if existing:
            await query.edit_message_text("✅ 您已经是对方的联系人了。")
            return
            
        # 检查是否超过10人
        count = await get_contact_count(session, requester_id)
        if count >= 10:
            await query.edit_message_text("⚠️ 对方的联系人列表已满 (10人)，绑定失败。")
            return

        # 添加记录
        new_contact = EmergencyContact(
            owner_chat_id=requester_id,
            contact_chat_id=executor.id,
            contact_name=executor.first_name
        )
        session.add(new_contact)
        
        # 确保接受者也在 User 表里
        await get_db_user(session, executor.id)
        await session.commit()
    
    await query.edit_message_text(f"✅ 绑定成功！您已成为 ID {requester_id} 的紧急联系人。")
    try:
        await context.bot.send_message(requester_id, f"🎉 **{executor.first_name}** 已接受邀请，成为您的紧急联系人！")
    except: pass

async def unbind_contact_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """解绑联系人"""
    query = update.callback_query
    await query.answer()
    contact_db_id = int(query.data.split("_")[1])
    
    async with AsyncSessionLocal() as session:
        # 查找该记录
        contact_record = await session.get(EmergencyContact, contact_db_id)
        if not contact_record:
            await query.edit_message_text("❌ 该联系人不存在或已删除。")
            return
        
        contact_tg_id = contact_record.contact_chat_id
        owner_id = contact_record.owner_chat_id
        contact_name = contact_record.contact_name
        
        # 删除
        await session.delete(contact_record)
        await session.commit()
    
    # 更新列表界面
    await query.message.edit_text(f"✅ 已解除与 {contact_name} 的绑定。")
    
    # 通知被解绑的人
    try:
        await context.bot.send_message(contact_tg_id, f"ℹ️ 用户 ID {owner_id} 已将您从紧急联系人列表中移除。")
    except: pass

# --- 遗嘱设置流程 (支持多选发送) ---

async def setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 删除指令
    context.application.create_task(auto_delete_message(context, update.effective_chat.id, update.message.message_id, delay=1))
    
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        has_will = bool(user.will_content)
    
    if has_will:
        keyboard = [
            [InlineKeyboardButton("⚠️ 覆盖并重新设置", callback_data="overwrite_yes")],
            [InlineKeyboardButton("🚫 取消", callback_data="overwrite_no")]
        ]
        await update.message.reply_text(
            "⚠️ **检测到旧遗嘱**\n\n重新设置将覆盖原有内容。是否继续？",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return STATE_CHECK_EXISTING
    else:
        return await ask_frequency_step(update, context)

async def setup_overwrite_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "overwrite_no":
        msg = await query.message.edit_text("✅ 操作已取消。")
        context.application.create_task(auto_delete_message(context, update.effective_chat.id, msg.message_id, delay=3))
        return ConversationHandler.END
    return await ask_frequency_step(update, context, is_callback=True)

async def ask_frequency_step(update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback=False):
    keyboard = [[
        InlineKeyboardButton("1 天", callback_data="day_1"),
        InlineKeyboardButton("3 天 (推荐)", callback_data="day_3"),
        InlineKeyboardButton("7 天", callback_data="day_7"),
    ]]
    text = "⚙️ **步骤 1/3：选择确认周期**\n\n如果联系不上您超过多久，视为触发条件？"
    if is_callback:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_CHOOSE_FREQ

async def setup_freq_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    days = int(query.data.split("_")[1])
    context.user_data['temp_freq'] = days * 24
    
    await query.edit_message_text(f"✅ 频率已设定为：**{days} 天**", parse_mode=ParseMode.MARKDOWN)
    
    info_text = (
        "📝 **步骤 2/3：录入遗嘱内容**\n\n"
        "请发送文字、图片或视频。\n"
        "🔐 内容将加密，原消息 15 秒后自毁。"
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=info_text, parse_mode=ParseMode.MARKDOWN)
    return STATE_UPLOAD_WILL

async def setup_receive_will(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    context.application.create_task(auto_delete_message(context, update.effective_chat.id, msg.message_id, delay=15))

    # 防误触
    if msg.text and msg.text.startswith(("🟢", "⚙️", "👥", "🛡️")):
        warn = await msg.reply_text("已退出设置。", reply_markup=get_main_menu())
        context.application.create_task(auto_delete_message(context, update.effective_chat.id, warn.message_id, delay=5))
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
    
    # 初始化选中的接收人（默认全选或空，这里设为空，让用户选）
    context.user_data['selected_recipients'] = [] 
    
    return await ask_recipients_step(update, context)

async def ask_recipients_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """选择接收人界面"""
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        contacts = await get_contacts(session, user_id)
    
    if not contacts:
        # 如果没有联系人，提示必须先绑定
        text = "⚠️ **无法完成设置**\n\n您尚未绑定紧急联系人，无法指定遗嘱接收人。\n请先去“👥 联系人管理”绑定至少一人。"
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    # 构建选择键盘
    return await render_recipient_keyboard(update, context, contacts)

async def render_recipient_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE, contacts):
    """渲染多选键盘"""
    selected = context.user_data.get('selected_recipients', [])
    keyboard = []
    
    for c in contacts:
        # 状态标记
        mark = "✅" if c.contact_chat_id in selected else "⭕️"
        name = c.contact_name or str(c.contact_chat_id)
        keyboard.append([InlineKeyboardButton(f"{mark} {name}", callback_data=f"toggle_{c.contact_chat_id}")])
    
    # 确认按钮
    btn_text = f"完成选择 ({len(selected)}人)" if selected else "请至少选择一人"
    if selected:
        keyboard.append([InlineKeyboardButton(f"💾 {btn_text} - 保存", callback_data="recipients_done")])
    
    text = "📬 **步骤 3/3：选择遗嘱接收人**\n\n请点击名字勾选（支持多选）："
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    
    return STATE_SELECT_RECIPIENTS

async def handle_recipient_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "recipients_done":
        # 完成选择，进入保存
        return await setup_confirm(update, context)
        
    if data.startswith("toggle_"):
        contact_id = int(data.split("_")[1])
        selected = context.user_data.get('selected_recipients', [])
        
        if contact_id in selected:
            selected.remove(contact_id)
        else:
            selected.append(contact_id)
            
        context.user_data['selected_recipients'] = selected
        
        # 重新渲染键盘
        user_id = update.effective_user.id
        async with AsyncSessionLocal() as session:
            contacts = await get_contacts(session, user_id)
        await render_recipient_keyboard(update, context, contacts)
        return STATE_SELECT_RECIPIENTS

async def setup_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """最终保存"""
    user_id = update.effective_user.id
    d = context.user_data
    
    # 转换接收人列表为字符串存储
    recipients_str = ",".join(map(str, d['selected_recipients']))
    
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        user.check_frequency = d['temp_freq']
        user.will_content = d['temp_content']
        user.will_type = d['temp_type']
        user.will_recipients = recipients_str # 保存接收人
        user.last_active = datetime.now(timezone.utc)
        await session.commit()

    await update.callback_query.edit_message_text("✅ **遗嘱设置成功！**\n\n已加密存储，将发送给指定的联系人。", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def cancel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 删除指令
    context.application.create_task(auto_delete_message(context, update.effective_user.id, update.message.message_id, delay=1))
    msg = await update.message.reply_text("操作已取消。", reply_markup=get_main_menu())
    context.application.create_task(auto_delete_message(context, update.effective_user.id, msg.message_id, delay=3))
    return ConversationHandler.END

# --- 内联邀请 & 其他 ---

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query
    user = update.effective_user

    if query == "invite":
        bot_username = context.bot.username
        invite_link = f"https://t.me/{bot_username}?start=connect_{user.id}"
        
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="发送遗嘱委托邀请",
                description="邀请对方成为您的紧急联系人",
                input_message_content=InputTextMessageContent(
                    f"📩 **来自 {user.first_name} 的信任委托**\n\n"
                    "我正在使用 **死了么LifeSignal**。\n"
                    "我希望将你设为我的紧急联系人。\n\n"
                    "如果我失联，机器人会通知你。\n"
                    "👇 **请点击下方链接接受：**",
                    parse_mode=ParseMode.MARKDOWN
                ),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ 接受委托", url=invite_link)]])
            )
        ]
        await update.inline_query.answer(results, cache_time=0)

async def handle_security(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """开源验证"""
    context.application.create_task(auto_delete_message(context, update.effective_chat.id, update.message.message_id, delay=1))
    text = "🛡️ **透明是信任的基石**\n\n点击下方按钮查看源代码。"
    keyboard = [[InlineKeyboardButton("👨‍💻 GitHub 源码", url=GITHUB_REPO_URL)]]
    await update.message.reply_markdown(text, reply_markup=InlineKeyboardMarkup(keyboard))

# --- 定时任务 (多联系人发送) ---

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
                # 触发遗嘱
                
                # 1. 解析接收人
                recipient_ids = []
                if user.will_recipients:
                    recipient_ids = [int(x) for x in user.will_recipients.split(",") if x]
                
                # 2. 如果没有指定接收人（旧数据兼容），发送给所有联系人？或者不发送？
                # 逻辑：必须指定了接收人才发遗嘱。
                # 但需要通知所有联系人“他失联了”。
                
                contacts = await get_contacts(session, user.chat_id)
                decrypted_content = None
                
                try:
                    if user.will_content:
                        decrypted_content = decrypt_data(user.will_content)
                except: pass

                if contacts:
                    for contact in contacts:
                        c_id = contact.contact_chat_id
                        
                        # 发送失联通知
                        try:
                            await app.bot.send_message(
                                chat_id=c_id,
                                text=f"🚨 **死了么LifeSignal 紧急通告**\n\n用户 @{user.username or user.chat_id} 已失联超过设定时间。",
                                parse_mode=ParseMode.MARKDOWN
                            )
                            
                            # 如果该联系人在遗嘱接收名单中，发送遗嘱
                            if c_id in recipient_ids and decrypted_content:
                                await app.bot.send_message(c_id, "🔐 **以下是用户留给您的加密遗嘱：**")
                                if user.will_type == 'text':
                                    await app.bot.send_message(c_id, decrypted_content)
                                elif user.will_type == 'photo':
                                    await app.bot.send_photo(c_id, decrypted_content)
                                elif user.will_type == 'video':
                                    await app.bot.send_video(c_id, decrypted_content)
                                elif user.will_type == 'voice':
                                    await app.bot.send_voice(c_id, decrypted_content)
                        except Exception as e:
                            logger.error(f"Failed to notify {c_id}: {e}")

                    # 标记为非活跃
                    user.status = 'inactive'
                    session.add(user)
                else:
                    # 无联系人，仅标记停止
                    user.status = 'inactive'
                    session.add(user)
            
            elif delta_hours > (user.check_frequency * 0.8):
                # 预警逻辑
                try:
                    left_hours = int(user.check_frequency - delta_hours)
                    await app.bot.send_message(
                        chat_id=user.chat_id,
                        text=f"⏰ **温馨提醒**\n\n请点击“🟢 我很安全”重置计时。\n距离触发还剩约 {left_hours} 小时。",
                        reply_markup=get_main_menu()
                    )
                except: pass

        await session.commit()

# --- 主程序 ---

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
            # 新增状态：选择接收人
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
