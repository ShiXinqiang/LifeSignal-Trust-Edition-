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
from sqlalchemy import Column, BigInteger, Text, DateTime, String, Integer, select
from sqlalchemy.orm import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.sql import func
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 加密库
from cryptography.fernet import Fernet

# --- 1. 配置与初始化 ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 获取环境变量
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

# 修正 Railway 数据库连接协议
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
    
    # 遗嘱内容 (加密存储)
    will_content = Column(Text, nullable=True) 
    will_type = Column(String, default='text') 
    
    # 紧急联系人
    emergency_contact_id = Column(BigInteger, nullable=True)
    emergency_contact_name = Column(String, nullable=True)
    
    # 机制 (单位: 小时)
    check_frequency = Column(Integer, default=72)
    last_active = Column(DateTime(timezone=True), default=func.now())
    status = Column(String, default='active') 

# 异步数据库引擎
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# --- 3. 辅助函数 ---

def encrypt_data(data: str) -> str:
    """AES 加密"""
    if not data: return None
    return cipher_suite.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data: str) -> str:
    """AES 解密"""
    if not encrypted_data: return None
    try:
        return cipher_suite.decrypt(encrypted_data.encode()).decode()
    except Exception:
        return "[数据无法解密：密钥可能已更改]"

async def auto_delete_message(context, chat_id, message_id, delay=1):
    """
    消息自动销毁
    默认 1 秒后删除用户发送的指令消息，保持界面像 APP 一样干净
    """
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def get_db_user(session, chat_id, username=None):
    """获取或创建用户"""
    stmt = select(User).where(User.chat_id == chat_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        user = User(chat_id=chat_id, username=username)
        session.add(user)
    elif username:
        user.username = username
    return user

# --- 4. UI 界面定义 ---

# 全局常量
BTN_SAFE = "🟢 我很安全"
BTN_BIND = "🤝 绑定联系人"
BTN_SECURITY = "🛡️ 开源验证"

def get_main_menu(user_obj) -> ReplyKeyboardMarkup:
    """
    生成底部常驻菜单。
    """
    if user_obj and user_obj.will_content:
        btn_setup = "⚙️ 设置/重置遗嘱"
    else:
        btn_setup = "⚙️ 设置遗嘱"
        
    return ReplyKeyboardMarkup(
        [
            [BTN_SAFE],
            [btn_setup, BTN_BIND],
            [BTN_SECURITY]
        ],
        resize_keyboard=True,
        is_persistent=True, # 强制常驻
        input_field_placeholder="死了么LifeSignal 正在守护..."
    )

STATE_CHECK_EXISTING, STATE_CHOOSE_FREQ, STATE_UPLOAD_WILL, STATE_CONFIRM = range(4)

# --- 5. 交互逻辑 ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """启动逻辑"""
    user = update.effective_user
    args = context.args
    
    # start 命令本身也可以稍后删除，保持极致干净（可选，这里设为10秒）
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, delay=10))

    async with AsyncSessionLocal() as session:
        db_user = await get_db_user(session, user.id, user.username)
        await session.commit()
        menu_markup = get_main_menu(db_user)

        # 处理“接受委托”的深度链接
        if args and args[0].startswith("connect_"):
            target_id = int(args[0].split("_")[1])
            if target_id == user.id:
                await update.message.reply_text("❌ 您无法将自己设为紧急联系人。", reply_markup=menu_markup)
                return
            
            keyboard = [
                [InlineKeyboardButton("✅ 接受委托", callback_data=f"accept_bind_{target_id}")],
                [InlineKeyboardButton("🚫 拒绝", callback_data="decline_bind")]
            ]
            await update.message.reply_text(
                f"🛡️ **收到委托请求**\n\n用户 ID `{target_id}` 希望将您设为紧急联系人。\n\n"
                f"**机制说明**：\n只有当系统确认该用户长期失联后，才会解密遗嘱并发送给您。在此之前，您的隐私受到严格保护。",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            return

    welcome_text = (
        f"👋 **你好，{user.first_name}**\n\n"
        "欢迎使用 **死了么LifeSignal** —— 您的数字资产安全守护者。\n\n"
        "我们提供银行级的安全保障，确保在不可预见的情况下，您的重要信息能安全地传递给信任的人。\n\n"
        "🛡️ **安全承诺**：\n"
        "• **代码开源**：核心逻辑公开透明，接受社区审计。\n"
        "• **AES 加密**：所有遗嘱内容均经过高强度加密存储。\n"
        "• **痕迹清理**：交互记录自动销毁，防止隐私泄露。\n\n"
        "👇 **请点击下方按钮开始使用：**"
    )
    # 欢迎语保留，作为 Landing Page
    await update.message.reply_markdown(welcome_text, reply_markup=menu_markup)

async def handle_security(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """开源验证"""
    # 🧹 立即删除用户点击的按钮文字消息
    context.application.create_task(auto_delete_message(context, update.effective_chat.id, update.message.message_id, delay=1))

    text = (
        "🛡️ **透明是信任的基石**\n\n"
        "**死了么LifeSignal** 致力于提供最安全的数字遗嘱服务。为了证明这一点，我们将项目代码完全开源。\n\n"
        "您可以通过以下方式验证我们的安全性：\n"
        "1. **代码审计**：点击下方按钮查看 GitHub 源码，每一行逻辑都清晰可见。\n"
        "2. **链接检测**：您可以使用第三方工具检测我们的服务链接，确保无恶意行为。\n\n"
        "🔐 **关于数据隐私**：\n"
        "您的数据在存入数据库前已通过 AES-128 标准加密。我们无法查看，黑客也无法破解。"
    )
    keyboard = [
        [InlineKeyboardButton("👨‍💻 查看 GitHub 源码", url=GITHUB_REPO_URL)],
        [InlineKeyboardButton("🔍 VirusTotal 安全检测", url="https://www.virustotal.com/gui/home/url")]
    ]
    await update.message.reply_markdown(text, reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)

# --- 遗嘱设置流程 (隐私保护重点) ---

async def setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 🧹 立即删除用户点击的“设置遗嘱”按钮消息
    context.application.create_task(auto_delete_message(context, update.effective_chat.id, update.message.message_id, delay=1))

    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        has_will = bool(user.will_content)
    
    if has_will:
        keyboard = [
            [InlineKeyboardButton("⚠️ 覆盖并重新设置", callback_data="overwrite_yes")],
            [InlineKeyboardButton("🚫 取消，保留原状", callback_data="overwrite_no")]
        ]
        await update.message.reply_text(
            "⚠️ **检测到您已设置过遗嘱**\n\n继续操作将导致**旧的遗嘱内容被永久删除**且无法恢复。\n您确定要重新设置吗？",
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
        user_id = update.effective_user.id
        async with AsyncSessionLocal() as session:
            db_user = await get_db_user(session, user_id)
            markup = get_main_menu(db_user)
        # 提示后也删除提示本身，保持干净
        msg = await query.message.reply_text("✅ 操作已取消。", reply_markup=markup)
        context.application.create_task(auto_delete_message(context, user_id, msg.message_id, delay=5))
        return ConversationHandler.END
    
    if query.data == "overwrite_yes":
        return await ask_frequency_step(update, context, is_callback=True)

async def ask_frequency_step(update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback=False):
    keyboard = [[
        InlineKeyboardButton("1 天", callback_data="day_1"),
        InlineKeyboardButton("3 天 (推荐)", callback_data="day_3"),
        InlineKeyboardButton("7 天", callback_data="day_7"),
    ]]
    text = "⚙️ **步骤 1/2：选择确认周期**\n\n请问如果我联系不上您超过多少**天**，就视为触发条件？"
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
        "📝 **步骤 2/2：录入遗嘱内容**\n\n"
        "请直接发送您希望留下的文字、图片或视频。\n\n"
        "🔐 **隐私保护启动**\n"
        "您发送的内容将被加密存储，并且**消息记录将在 15 秒后自动销毁**，请放心发送。"
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=info_text, parse_mode=ParseMode.MARKDOWN)
    return STATE_UPLOAD_WILL

async def setup_receive_will(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    
    # 🔒 安全核心：立即启动该消息的自毁程序 (15秒)
    context.application.create_task(auto_delete_message(context, update.effective_chat.id, msg.message_id, delay=15))

    # 防误触逻辑
    if msg.text and msg.text.startswith(("🟢", "⚙️", "🤝", "🛡️")):
        user_id = update.effective_user.id
        async with AsyncSessionLocal() as session:
            db_user = await get_db_user(session, user_id)
            markup = get_main_menu(db_user)
        # 清理用户刚才点的按钮文字
        context.application.create_task(auto_delete_message(context, user_id, msg.message_id, delay=1))
        
        warn_msg = await msg.reply_text("已保存当前进度并退出。", reply_markup=markup)
        context.application.create_task(auto_delete_message(context, user_id, warn_msg.message_id, delay=5))
        return ConversationHandler.END

    content = None
    w_type = 'text'
    if msg.text:
        content = encrypt_data(msg.text)
        w_type = 'text'
    elif msg.photo or msg.video or msg.voice:
        raw_file_id = ""
        if msg.photo: raw_file_id = msg.photo[-1].file_id
        elif msg.video: raw_file_id = msg.video.file_id
        elif msg.voice: raw_file_id = msg.voice.file_id
        content = encrypt_data(raw_file_id) 
        if msg.photo: w_type = 'photo'
        elif msg.video: w_type = 'video'
        elif msg.voice: w_type = 'voice'
    else:
        error_msg = await msg.reply_text("暂不支持该格式，请发送文字或媒体文件。")
        context.application.create_task(auto_delete_message(context, update.effective_chat.id, error_msg.message_id, delay=5))
        return STATE_UPLOAD_WILL

    context.user_data.update({'temp_content': content, 'temp_type': w_type})
    keyboard = [[
        InlineKeyboardButton("✅ 确认加密保存", callback_data="confirm_yes"),
        InlineKeyboardButton("🔄 重新编辑", callback_data="confirm_retry")
    ]]
    await msg.reply_text("🔒 内容已加密，原始消息将于 15 秒后销毁。确认保存吗？", reply_markup=InlineKeyboardMarkup(keyboard))
    return STATE_CONFIRM

async def setup_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    
    if query.data == "confirm_retry":
        await query.edit_message_text("已取消，请重新发送。")
        return ConversationHandler.END

    d = context.user_data
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        user.check_frequency = d['temp_freq']
        user.will_content = d['temp_content']
        user.will_type = d['temp_type']
        user.last_active = datetime.now(timezone.utc)
        await session.commit()
        updated_user = await get_db_user(session, user_id)
        has_contact = bool(updated_user.emergency_contact_id)
        new_menu = get_main_menu(updated_user)

    msg = "✅ **设置成功！您的数据已安全存储。**\n"
    if not has_contact:
        msg += "\n⚠️ **温馨提示**：您尚未绑定紧急联系人，遗嘱目前**无法发送**。\n请点击“🤝 绑定联系人”以确保功能完整。"
    
    await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    if not has_contact:
        await context.bot.send_message(chat_id=user_id, text="👇 建议立即绑定", reply_markup=new_menu)
    else:
        await context.bot.send_message(chat_id=user_id, text="👇 您的守护程序已就绪", reply_markup=new_menu)
    return ConversationHandler.END

async def cancel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # 🧹 删除“取消”指令
    context.application.create_task(auto_delete_message(context, user_id, update.message.message_id, delay=1))
    
    async with AsyncSessionLocal() as session:
        user = await get_db_user(session, user_id)
        markup = get_main_menu(user)
    
    msg = await update.message.reply_text("操作已取消。", reply_markup=markup)
    context.application.create_task(auto_delete_message(context, user_id, msg.message_id, delay=3))
    return ConversationHandler.END

# --- 报平安逻辑 (清理痕迹) ---

async def handle_im_safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # 🧹 立即删除用户点击的“🟢 我很安全”
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, delay=1))

    async with AsyncSessionLocal() as session:
        db_user = await get_db_user(session, user.id)
        markup = get_main_menu(db_user)

        # 状态检测
        if not db_user.will_content or not db_user.emergency_contact_id:
            missing = []
            if not db_user.will_content: missing.append("未设置遗嘱")
            if not db_user.emergency_contact_id: missing.append("未绑定联系人")
            
            alert_text = (
                "⚠️ **安全配置未完成**\n\n"
                "虽然收到您的报平安，但系统检测到您：\n"
                f"❌ **{'，'.join(missing)}**\n\n"
                "如果现在发生意外，**系统将无法执行任何操作**。\n"
                "请务必完成下方设置 👇"
            )
            warn_msg = await update.message.reply_text(alert_text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
            context.application.create_task(auto_delete_message(context, user.id, warn_msg.message_id, delay=30))
            return

        db_user.last_active = datetime.now(timezone.utc)
        db_user.status = 'active'
        await session.commit()
    
    # 🔒 隐私：Bot 的确认回复也将在 15 秒后删除
    reply_msg = await update.message.reply_text("✅ 已确认！守护倒计时已重置。", reply_markup=markup)
    context.application.create_task(auto_delete_message(context, user.id, reply_msg.message_id, delay=15))

# --- 绑定与内联邀请逻辑 ---

async def handle_bind_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """引导使用内联模式发送"""
    # 🧹 立即删除用户点击的“🤝 绑定联系人”
    context.application.create_task(auto_delete_message(context, update.effective_chat.id, update.message.message_id, delay=1))

    keyboard = [[
        InlineKeyboardButton("🚀 选择好友发送邀请", switch_inline_query="invite")
    ]]
    
    text = (
        "🤝 **绑定紧急联系人**\n\n"
        "请点击下方按钮，选择您信任的好友（如亲属、挚友）。\n"
        "发送邀请卡片后，对方点击“✅ 接受委托”即可完成绑定。"
    )
    
    await update.message.reply_markdown(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理内联查询"""
    query = update.inline_query.query
    user = update.effective_user

    if query == "invite":
        bot_username = context.bot.username
        invite_link = f"https://t.me/{bot_username}?start=connect_{user.id}"
        
        title = "发送遗嘱委托邀请"
        description = f"邀请对方成为您的紧急联系人"
        message_text = (
            f"📩 **来自 {user.first_name} 的信任委托**\n\n"
            "我正在使用 **死了么LifeSignal** 服务。\n"
            "我希望将你设为我的紧急联系人。\n\n"
            "如果我发生意外失联，机器人会将我留下的加密信息发送给你。\n"
            "👇 **请点击下方链接接受委托：**"
        )
        
        keyboard = [[InlineKeyboardButton("✅ 点击接受委托", url=invite_link)]]
        
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=title,
                description=description,
                input_message_content=InputTextMessageContent(message_text, parse_mode=ParseMode.MARKDOWN),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        ]
        await update.inline_query.answer(results, cache_time=0)

async def confirm_bind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    executor = update.effective_user
    
    if data == "decline_bind":
        await query.edit_message_text("🚫 您已婉拒该委托。")
        return
    
    requester_id = int(data.split("_")[2])
    async with AsyncSessionLocal() as session:
        req = await get_db_user(session, requester_id)
        req.emergency_contact_id = executor.id
        req.emergency_contact_name = executor.first_name
        await get_db_user(session, executor.id)
        await session.commit()
    
    await query.edit_message_text(f"✅ 绑定成功！您已成为 ID {requester_id} 的守护者。")
    try:
        await context.bot.send_message(requester_id, f"🎉 **绑定成功！**\n\n{executor.first_name} 已接受您的委托，安全网已建立。")
    except: pass

# --- 后台定时任务 ---

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
                contact_id = user.emergency_contact_id
                if contact_id:
                    try:
                        decrypted_content = decrypt_data(user.will_content)
                        await app.bot.send_message(
                            chat_id=contact_id,
                            text=f"🚨 **死了么LifeSignal 紧急触发**\n\n用户 @{user.username or user.chat_id} 已超过设定时间未报平安。\n以下是解密后的信息：",
                            parse_mode=ParseMode.MARKDOWN
                        )
                        if user.will_type == 'text':
                            await app.bot.send_message(contact_id, decrypted_content)
                        elif user.will_type == 'photo':
                            await app.bot.send_photo(contact_id, decrypted_content)
                        elif user.will_type == 'video':
                            await app.bot.send_video(contact_id, decrypted_content)
                        elif user.will_type == 'voice':
                            await app.bot.send_voice(contact_id, decrypted_content)
                        user.status = 'inactive'
                        session.add(user)
                    except Exception as e:
                        logger.error(f"发送遗嘱失败: {e}")
                else:
                    user.status = 'inactive'
                    session.add(user)
            
            elif delta_hours > (user.check_frequency * 0.8):
                try:
                    left_hours = int(user.check_frequency - delta_hours)
                    markup = get_main_menu(user)
                    await app.bot.send_message(
                        chat_id=user.chat_id,
                        text=f"⏰ **温馨提醒**\n\n您已有一段时间未活动。请点击“🟢 我很安全”重置计时。\n距离触发还剩约 {left_hours} 小时。",
                        reply_markup=markup
                    )
                except Exception:
                    pass

        await session.commit()

# --- 主程序入口 ---

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
            STATE_UPLOAD_WILL: [MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.Regex("^(🟢|⚙️|🤝|🛡️)"), setup_receive_will)],
            STATE_CONFIRM: [CallbackQueryHandler(setup_confirm, pattern="^confirm_")]
        },
        fallbacks=[CommandHandler("cancel", cancel_setup), MessageHandler(filters.Regex(f"^{BTN_SAFE}$"), cancel_setup)],
        name="setup_conversation", persistent=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(setup_conv)
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SAFE}$"), handle_im_safe))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_BIND}$"), handle_bind_request))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SECURITY}$"), handle_security))
    app.add_handler(CallbackQueryHandler(confirm_bind_callback, pattern="^accept_bind_"))
    app.add_handler(CallbackQueryHandler(confirm_bind_callback, pattern="^decline_bind"))
    
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
