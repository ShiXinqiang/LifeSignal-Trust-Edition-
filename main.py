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

# 环境变量
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME", "LifeSignal_Bot")
GITHUB_REPO_URL = "https://github.com/ShiXinqiang/LifeSignal-Trust-Edition-"

if not TOKEN or not DATABASE_URL:
    logger.critical("❌ 启动失败: 缺少 TELEGRAM_BOT_TOKEN 或 DATABASE_URL")
    exit(1)

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

# --- 3. 文案与 UI 定义 ---

# 保持你要求的键盘文案不变
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
        input_field_placeholder="死了么LifeSignal 正在守护中..."
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
        return "[数据无法读取]"

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
    
    # 自动删除用户发的消息（保持界面整洁）
    if update.message:
        context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 1))

    try:
        async with AsyncSessionLocal() as session:
            db_user = await get_db_user(session, user.id)
            if db_user.is_locked:
                key_display = db_user.unlock_key if db_user.unlock_key else "ERROR"
                alert = (
                    "⛔️ 账户已暂时冻结\n\n"
                    "为了保护您的数据安全，系统检测到多次错误操作，已自动锁定。\n\n"
                    "如何解锁？\n"
                    "1. 请联系您的守护人（您绑定的紧急联系人）。\n"
                    f"2. 把这个【恢复密钥】发给他： {key_display}\n"
                    "3. 他输入/unlock再输入密钥，您的账户就会立刻恢复。"
                )
                if update.message:
                    msg = await update.message.reply_text(alert)
                    context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 30))
                elif update.callback_query:
                    await update.callback_query.answer("⛔️ 拒绝访问：请联系守护人解锁", show_alert=True)
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
            msg = await update.message.reply_text("👋 首次使用，请直接发送您想设置的主密码（以后进入隐私区域需要用到）：")
            context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 20))
            return ConversationHandler.END

    prompt = await update.message.reply_text("🔐 隐私保护\n这里包含敏感信息，请输入您的主密码：")
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
                warn = await msg.reply_text("⛔️ 密码错误次数过多，账户已冻结！")
                context.application.create_task(auto_delete_message(context, user_id, warn.message_id, 15))
                return ConversationHandler.END
            else:
                await session.commit()
                retry_msg = await msg.reply_text(f"❌ 密码错误，请重试 (还剩 {5 - user.login_attempts} 次机会)")
                context.application.create_task(auto_delete_message(context, user_id, retry_msg.message_id, 5))
                return STATE_VERIFY_PASSWORD

# --- 6. 守护人解锁流程 (已修复) ---

async def start_remote_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    1. 守护人输入 /unlock
    2. 系统查找他守护了哪些人，且哪些人是被锁定的
    3. 显示按钮列表
    """
    executor_id = update.effective_user.id
    
    # 立即删除 /unlock 指令
    context.application.create_task(auto_delete_message(context, executor_id, update.message.message_id, 1))

    async with AsyncSessionLocal() as session:
        stmt = select(EmergencyContact).where(EmergencyContact.contact_chat_id == executor_id)
        entrustments = (await session.execute(stmt)).scalars().all()
        
        locked_users = []
        for ent in entrustments:
            user = await session.get(User, ent.owner_chat_id)
            if user and user.is_locked: locked_users.append(user)
        
        if not locked_users:
            msg = await update.message.reply_text("✅ 您守护的人目前都很安全，没有账户被冻结。")
            context.application.create_task(auto_delete_message(context, executor_id, msg.message_id, 10))
            return ConversationHandler.END
        
        kb = [[InlineKeyboardButton(f"🔓 解锁: {u.username or u.chat_id}", callback_data=f"select_locked_{u.chat_id}")] for u in locked_users]
        await update.message.reply_text("🛡️ 收到解锁请求，请选择要协助的对象：", reply_markup=InlineKeyboardMarkup(kb))
        return STATE_UNLOCK_SELECT_USER

async def handle_locked_user_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    点击选择用户按钮后的处理
    """
    query = update.callback_query
    await query.answer()
    
    # 解析 callback data: select_locked_12345
    target_id = int(query.data.split("_")[2])
    context.user_data[CTX_UNLOCK_TARGET] = target_id
    
    await query.edit_message_text(
        "🛡️ 请输入对方告诉您的【6位恢复密钥】：\n\n"
        "（只有填对密钥，才能证明您确实收到了他的求助）"
    )
    return STATE_UNLOCK_VERIFY_KEY

async def verify_unlock_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    验证密钥并解锁
    """
    msg = update.message
    input_key = msg.text.strip()
    target_id = context.user_data.get(CTX_UNLOCK_TARGET)
    
    # 删除密钥消息
    context.application.create_task(auto_delete_message(context, update.effective_user.id, msg.message_id, 1))
    
    async with AsyncSessionLocal() as session:
        target_user = await get_db_user(session, target_id)
        
        if input_key == target_user.unlock_key:
            target_user.is_locked = False
            target_user.login_attempts = 0
            target_user.unlock_key = None
            target_user.password_hash = None # 强制重置密码
            await session.commit()
            
            await msg.reply_text("✅ 操作成功！对方的账户已解锁，并被强制要求重置密码。")
            try: 
                await context.bot.send_message(
                    target_id, 
                    "🎉 账户已恢复！\n您的守护人已帮您解锁。由于原密码可能泄露，请重新设置一个新密码。", 
                    reply_markup=get_main_menu()
                )
            except: pass
            return ConversationHandler.END
        else:
            fail_msg = await msg.reply_text("❌ 密钥不对，请重新核对。")
            context.application.create_task(auto_delete_message(context, update.effective_user.id, fail_msg.message_id, 10))
            return ConversationHandler.END # 也可以选择不END，允许重试，这里END简单点

# --- 7. 基础功能 ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 1))

    async with AsyncSessionLocal() as session:
        db_user = await get_db_user(session, user.id, user.username)

        # 处理别人发来的邀请链接
        if context.args and context.args[0].startswith("connect_"):
            target_id = int(context.args[0].split("_")[1])
            if target_id == user.id: return
            exists = (await session.execute(select(EmergencyContact).where(EmergencyContact.owner_chat_id == target_id, EmergencyContact.contact_chat_id == user.id))).scalar()
            if exists:
                await update.message.reply_text("✅ 您已经是他的守护人了，不用重复接受。")
                return
            kb = [[InlineKeyboardButton("✅ 我愿意守护他", callback_data=f"accept_bind_{target_id}"), InlineKeyboardButton("🚫 拒绝", callback_data="decline_bind")]]
            await update.message.reply_text(f"🛡️ 收到一份委托\n\n用户 `{target_id}` 希望把您设为守护人。\n如果他长期失联，系统会发消息通知您。\n\n您愿意接受吗？", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
            return

        if not db_user.password_hash:
            await update.message.reply_text(
                "👋 欢迎使用 死了么LifeSignal\n\n"
                "这是一个帮你托管秘密的自动程序。\n"
                "简单来说：如果你长时间不来报平安，我会把你预设好的信件发给信任的人。\n\n"
                "👇 为了保护隐私，请先设置一个【主密码】（直接发送给我）："
            )
            return STATE_SET_PASSWORD

        welcome = (
            f"👋 死了么LifeSignal 正常运行中\n\n"
            "目前状态：✅ 监控中\n\n"
            "简单使用指南：\n"
            "1. 记得定期点左上角的【确认平安】，不然我会以为你出事了。\n"
            "2. 在【预设信箱】里写下你想留的话。\n"
            "3. 在【守护人管理】里添加你信任的朋友。\n"
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
    await update.message.reply_text("✅ 密码设置成功，请牢记它。", reply_markup=get_main_menu())
    return ConversationHandler.END

# --- 8. 功能菜单与回调 ---

async def show_will_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示预设信箱主列表"""
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        wills = await get_wills(session, user_id)
        kb = []
        for w in wills:
            try:
                decrypted = decrypt_data(w.content)
                preview = (decrypted[:12] + "..") if w.msg_type == 'text' else f"[{w.msg_type.upper()}]"
            except: preview = "Lock"
            kb.append([InlineKeyboardButton(f"📄 {preview}", callback_data=f"view_will_{w.id}")])
        
        kb.append([InlineKeyboardButton("➕ 写一封新信", callback_data="add_will_start")])
        
        text = f"📦 预设信箱 (共 {len(wills)} 封)\n\n这些信件平时是加密的，只有当你失联后，才会发出去。\n点击下方信件可以管理："
        
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            msg = await context.bot.send_message(user_id, text, reply_markup=InlineKeyboardMarkup(kb))
            context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 60))

async def show_contacts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        contacts = await get_contacts(session, user_id)
        kb = [[InlineKeyboardButton(f"❌ 删除 {c.contact_name}", callback_data=f"try_unbind_{c.id}")] for c in contacts]
        if len(contacts) < 10: kb.append([InlineKeyboardButton("➕ 邀请新守护人", switch_inline_query="invite")])
        msg = await context.bot.send_message(user_id, f"🛡️ 守护人列表 ({len(contacts)}人)\n\n这些人会在你失联时收到通知。", reply_markup=InlineKeyboardMarkup(kb))
        context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 60))

async def show_freq_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    kb = [[InlineKeyboardButton("24小时", callback_data="set_freq_24"), InlineKeyboardButton("3天", callback_data="set_freq_72"), InlineKeyboardButton("7天", callback_data="set_freq_168")]]
    msg = await context.bot.send_message(user_id, "⏱️ 调整失联判定时间\n\n如果你超过这个时间没来【确认平安】，系统就会判定你失联了，从而发出警报和遗嘱信。", reply_markup=InlineKeyboardMarkup(kb))
    context.application.create_task(auto_delete_message(context, user_id, msg.message_id, 60))

# --- 9. 核心交互回调处理 ---

async def handle_global_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    # === 返回主列表 ===
    if data == "menu_wills":
        await show_will_menu(update, context)

    # === 查看详情（控制台）===
    elif data.startswith("view_will_"):
        wid = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            will = await session.get(Will, wid)
            if not will:
                await query.edit_message_text("❌ 这封信好像被删除了", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="menu_wills")]]))
                return
            
            # 获取当前接收人姓名
            rec_ids = will.recipient_ids.split(",") if will.recipient_ids else []
            rec_names = []
            if rec_ids:
                contacts = await get_contacts(session, user_id)
                name_map = {str(c.contact_chat_id): c.contact_name for c in contacts}
                rec_names = [name_map.get(rid, "未知用户") for rid in rec_ids if rid]
            
            rec_str = ", ".join(rec_names) if rec_names else "还没指定人（不会发送）"
            type_str = "文字" if will.msg_type == 'text' else "文件/图片"
            
            text = (
                f"📄 信件详情 #{wid}\n\n"
                f"• 类型：{type_str}\n"
                f"• 创建时间：{will.created_at.strftime('%Y-%m-%d %H:%M')}\n"
                f"• 发给谁：{rec_str}\n\n"
                "你可以进行以下操作："
            )
            
            kb = [
                [InlineKeyboardButton("👁 查看内容", callback_data=f"reveal_{wid}"), InlineKeyboardButton("👥 修改接收人", callback_data=f"edit_rec_{wid}")],
                [InlineKeyboardButton("🗑 删除这封信", callback_data=f"del_will_{wid}")],
                [InlineKeyboardButton("🔙 返回列表", callback_data="menu_wills")]
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))

    # === 临时解密内容 ===
    elif data.startswith("reveal_"):
        wid = int(data.split("_")[1])
        async with AsyncSessionLocal() as session:
            will = await session.get(Will, wid)
            if will:
                content = decrypt_data(will.content)
                if will.msg_type == 'text': m = await query.message.reply_text(f"🔐 解密后的内容 (15秒后销毁):\n\n{content}")
                else: m = await query.message.reply_text(f"🔐 文件ID (15秒后销毁):\n{content}")
                context.application.create_task(auto_delete_message(context, user_id, m.message_id, 15))

    # === 修改接收人 (开始) ===
    elif data.startswith("edit_rec_"):
        wid = int(data.split("_")[2])
        # 暂存正在编辑的 ID
        context.user_data['editing_will_id'] = wid
        async with AsyncSessionLocal() as session:
            will = await session.get(Will, wid)
            contacts = await get_contacts(session, user_id)
            
            if not contacts:
                await query.answer("您还没有添加守护人，请先去添加。", show_alert=True)
                return

            current_ids = will.recipient_ids.split(",") if will.recipient_ids else []
            # 存入临时状态
            context.user_data[f'edit_sel_{wid}'] = [int(i) for i in current_ids if i]
            
            await render_edit_recipient_menu(query, contacts, wid, context)

    # === 修改接收人 (切换勾选) ===
    elif data.startswith("tgl_edit_"):
        parts = data.split("_")
        wid = int(parts[2])
        cid = int(parts[3])
        
        sel = context.user_data.get(f'edit_sel_{wid}', [])
        if cid in sel: sel.remove(cid)
        else: sel.append(cid)
        context.user_data[f'edit_sel_{wid}'] = sel
        
        async with AsyncSessionLocal() as session:
            contacts = await get_contacts(session, user_id)
            await render_edit_recipient_menu(query, contacts, wid, context)

    # === 修改接收人 (保存) ===
    elif data.startswith("save_edit_"):
        wid = int(data.split("_")[2])
        sel = context.user_data.get(f'edit_sel_{wid}', [])
        rec_str = ",".join(map(str, sel))
        
        async with AsyncSessionLocal() as session:
            will = await session.get(Will, wid)
            will.recipient_ids = rec_str
            await session.commit()
        
        # 清理临时数据
        context.user_data.pop(f'edit_sel_{wid}', None)
        context.user_data.pop('editing_will_id', None)
        
        await query.answer("✅ 修改成功")
        # 返回详情页
        query.data = f"view_will_{wid}"
        await handle_global_callbacks(update, context)

    # === 删除信件 ===
    elif data.startswith("del_will_"):
        wid = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Will).where(Will.id == wid))
            await session.commit()
        await query.edit_message_text("✅ 已删除", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回列表", callback_data="menu_wills")]]))

    # === 解绑守护人 ===
    elif data.startswith("try_unbind_"):
        cid = int(data.split("_")[2])
        kb = [[InlineKeyboardButton("⚠️ 确认删除", callback_data=f"do_unbind_{cid}"), InlineKeyboardButton("取消", callback_data="cancel_cb")]]
        await query.edit_message_text("⚠️ 确定要删除这位守护人吗？删除后他将收不到通知。", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("do_unbind_"):
        cid = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            c = await session.get(EmergencyContact, cid)
            if c:
                await session.delete(c)
                await session.commit()
        await query.edit_message_text("✅ 已删除", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回列表", callback_data="menu_contacts")]]))

    elif data.startswith("set_freq_"):
        h = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            u = await get_db_user(session, user_id)
            u.check_frequency = h
            await session.commit()
        await query.edit_message_text(f"✅ 设置成功！如果 {h} 小时没消息，我就启动预案。")

    elif data == "cancel_cb":
        await query.edit_message_text("操作已取消")

async def render_edit_recipient_menu(query, contacts, wid, context):
    """渲染修改接收人的复选框菜单"""
    sel = context.user_data.get(f'edit_sel_{wid}', [])
    kb = []
    for c in contacts:
        mark = "✅" if c.contact_chat_id in sel else "⭕️"
        # 回调数据: tgl_edit_WILLID_CONTACTID
        kb.append([InlineKeyboardButton(f"{mark} {c.contact_name}", callback_data=f"tgl_edit_{wid}_{c.contact_chat_id}")])
    
    kb.append([InlineKeyboardButton("💾 保存修改", callback_data=f"save_edit_{wid}")])
    kb.append([InlineKeyboardButton("🔙 不改了，返回", callback_data=f"view_will_{wid}")])
    
    await query.edit_message_text(f"👥 正在修改信件 #{wid} 的接收人\n请点击名字勾选：", reply_markup=InlineKeyboardMarkup(kb))

# --- 10. 添加遗嘱流程 ---

async def start_add_will(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("📝 请发送您想留下的内容\n\n支持文字、照片或视频。\n发送后我会立即加密存储，并销毁聊天记录。")
    return STATE_ADD_WILL_CONTENT

async def receive_will_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    # 如果用户误触了键盘按钮，直接退出流程
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
             await context.bot.send_message(user_id, "⚠️ 您还没添加守护人，这封信没法发给别人。\n请先去【守护人管理】添加朋友。", reply_markup=get_main_menu())
             return ConversationHandler.END
        
        sel = context.user_data.get('selected', [])
        kb = [[InlineKeyboardButton(f"{'✅' if c.contact_chat_id in sel else '⭕️'} {c.contact_name}", callback_data=f"sel_rec_{c.contact_chat_id}")] for c in contacts]
        kb.append([InlineKeyboardButton("💾 保存信件", callback_data="save_new_will")])
        
        text = "📨 这封信要在失联后发给谁？\n请勾选（可多选）："
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
        await query.edit_message_text("✅ 保存成功！", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回列表", callback_data="menu_wills")]]))
        return ConversationHandler.END

# --- 11. 杂项 ---

async def handle_im_safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.application.create_task(auto_delete_message(context, user.id, update.message.message_id, 0))
    
    async with AsyncSessionLocal() as session:
        u = await get_db_user(session, user.id)
        if u.is_locked: return

        contacts = await get_contacts(session, user.id)
        if not contacts:
            msg = await update.message.reply_text("⚠️ 您还没添加守护人，保护机制暂时无法生效。\n请去【守护人管理】添加信任的朋友。", reply_markup=get_main_menu())
            context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 5))
            return
        
        u.last_active = datetime.now(timezone.utc)
        u.status = 'active'
        await session.commit()
        
    msg = await update.message.reply_text(f"✅ 已确认平安！\n倒计时已重置，我会继续默默守护您。", reply_markup=get_main_menu())
    context.application.create_task(auto_delete_message(context, user.id, msg.message_id, 10))

async def confirm_bind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "decline_bind":
        await query.edit_message_text("已拒绝")
        return
    rid = int(query.data.split("_")[2])
    async with AsyncSessionLocal() as session:
        exists = (await session.execute(select(EmergencyContact).where(EmergencyContact.owner_chat_id == rid, EmergencyContact.contact_chat_id == update.effective_user.id))).scalar()
        if not exists:
            session.add(EmergencyContact(owner_chat_id=rid, contact_chat_id=update.effective_user.id, contact_name=update.effective_user.first_name))
            await session.commit()
    await query.edit_message_text("✅ 接受成功！您已成为他的守护人。")
    try: await context.bot.send_message(rid, "🎉 好消息！\n对方已接受您的请求，现在他是您的守护人了。")
    except: pass

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query: await update.callback_query.message.edit_text("已取消")
    else: await update.message.reply_text("已取消", reply_markup=get_main_menu())
    return ConversationHandler.END

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.inline_query.query == "invite":
        link = f"https://t.me/{context.bot.username}?start=connect_{update.effective_user.id}"
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="发送邀请函",
                description="邀请对方成为您的守护人",
                input_message_content=InputTextMessageContent(
                    f"📩 死了么LifeSignal 委托请求\n\n我是 {update.effective_user.first_name}，我希望将您设为我的【守护人】。\n\n这意味着：如果我长期失联（可能出事了），您会收到我的通知和预设信件。\n\n👇 点击下方按钮接受委托：",
                    parse_mode=ParseMode.MARKDOWN
                ),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🤝 接受委托", url=link)]])
            )
        ]
        await update.inline_query.answer(results, cache_time=0)

async def handle_security(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 删除用户的触发消息以保持清洁
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
    except:
        pass

    text = (
        "🛡️ 死了么LifeSignal 安全说明\n\n"
        "我们如何保护您的隐私？\n"
        "1. 零知识存储：信件都是 AES-128 加密的，只有您和守护人能看到。\n"
        "2. 阅后即焚：密码等敏感信息发完就删。\n"
        "3. 开源透明：代码是公开的，没有后门。\n\n"
        "👇 点击下方按钮进行审查："
    )

    kb = [
        [InlineKeyboardButton("👨‍💻 GitHub 源码仓库", url=GITHUB_REPO_URL)],
        [InlineKeyboardButton("🦠 VirusTotal 安全检测", url="https://www.virustotal.com/gui/home/url")]
    ]

    await update.message.reply_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(kb)
    )

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
                        await app.bot.send_message(c.contact_chat_id, f"🚨 紧急预警\n用户 {user.username or user.chat_id} 已失联（长时间未报平安）。", parse_mode=ParseMode.MARKDOWN)
                        for w in wills:
                            if w.recipient_ids and str(c.contact_chat_id) in w.recipient_ids.split(","):
                                content = decrypt_data(w.content)
                                if w.msg_type=='text': await app.bot.send_message(c.contact_chat_id, f"🔐 预设信件:\n{content}")
                                else: await app.bot.send_message(c.contact_chat_id, "🔐 [收到一份加密文件]")
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

    # 1. 解锁流程 (放在前面)
    # 修复：给CallbackQueryHandler增加了精确的 pattern，确保能抓住 "select_locked_" 开头的按钮
    unlock_handler = ConversationHandler(
        entry_points=[CommandHandler("unlock", start_remote_unlock)],
        states={
            STATE_UNLOCK_SELECT_USER: [CallbackQueryHandler(handle_locked_user_selection, pattern="^select_locked_")], 
            STATE_UNLOCK_VERIFY_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, verify_unlock_key)]
        },
        fallbacks=[CommandHandler("cancel", cancel_action)], 
        name="unlock", 
        persistent=True
    )

    # 2. 密码验证流程
    auth_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Text([BTN_WILLS, BTN_CONTACTS, BTN_SETTINGS]), request_password_entry)],
        states={STATE_VERIFY_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password_verification)]},
        fallbacks=[CommandHandler("cancel", cancel_action)], name="auth_gw", persistent=True
    )

    # 3. 添加信件流程
    add_will_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add_will, pattern="^add_will_start$")],
        states={STATE_ADD_WILL_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_will_content)], STATE_ADD_WILL_RECIPIENTS: [CallbackQueryHandler(handle_recipient_toggle)]},
        fallbacks=[CommandHandler("cancel", cancel_action)], name="add_will", persistent=True
    )

    app.add_handler(ConversationHandler(entry_points=[CommandHandler("start", start)], states={STATE_SET_PASSWORD: [MessageHandler(filters.TEXT, set_password_finish)]}, fallbacks=[], name="setup"))
    
    # 注册 Handler
    app.add_handler(unlock_handler)
    app.add_handler(auth_handler)
    app.add_handler(add_will_handler)
    
    # 快捷按钮
    app.add_handler(MessageHandler(filters.Text(BTN_SAFE), handle_im_safe))
    app.add_handler(MessageHandler(filters.Text(BTN_SECURITY), handle_security))
    
    # 全局回调
    app.add_handler(CallbackQueryHandler(handle_global_callbacks, pattern="^(menu_|view_|reveal_|del_|try_|do_|set_freq_|edit_|tgl_|save_|cancel)"))
    app.add_handler(CallbackQueryHandler(confirm_bind_callback, pattern="^accept_bind_"))
    app.add_handler(InlineQueryHandler(inline_query_handler))

    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_dead_mans_switch, 'interval', minutes=30, args=[app])
    scheduler.start()
    
    print("🚀 死了么LifeSignal Final Stable is running...")
    app.run_polling()

if __name__ == '__main__':
    main()
