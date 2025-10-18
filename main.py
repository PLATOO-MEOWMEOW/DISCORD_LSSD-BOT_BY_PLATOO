import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
# ✅ แก้ไข: นำเข้า timezone จาก datetime เพื่อใช้เป็น tzinfo (e.g., timezone.utc)
from datetime import datetime, timezone 
import aiosqlite
import re
# ✅ แก้ไข: เปลี่ยนชื่อ timezone ที่นำเข้าจาก pytz เป็น pytz_timezone เพื่อป้องกันการทับซ้อนกับ datetime.timezone
from pytz import timezone as pytz_timezone 
from myserver import server_on # <--- ต้องมีไฟล์ myserver.py
import threading
import asyncio

# -------------------- CONFIGURATION --------------------
load_dotenv()
token = os.getenv('DISCORD_TOKEN')

DB_FILE = "database.db"

# *** 1. ต้องกำหนด GUILD_ID (ID เซิร์ฟเวอร์ของคุณ) ***
# แทนที่ด้วย ID เซิร์ฟเวอร์จริงของคุณ
GUILD_ID_STR = os.getenv("GUILD_ID") or '1428284368602005536' 
try:
    GUILD_ID = int(GUILD_ID_STR)
except ValueError:
    raise ValueError("GUILD_ID must be an integer.")

# *** 2. กำหนด LOG CHANNEL ID ***
# แทนที่ด้วย ID ช่อง Log จริงของคุณ
LOG_CHANNEL_ID_STR = os.getenv("LOG_CHANNEL_ID") or '123456789012345678'
try:
    LOG_CHANNEL_ID = int(LOG_CHANNEL_ID_STR)
except ValueError:
    LOG_CHANNEL_ID = None # ไม่ใช้ Log Channel ถ้า ID ไม่ถูกต้อง

secret_role = "Visitor"
DEPARTMENT_NAME = "Los Santos County Sheriff Department"

# *** LSSD Logo URL ***
LSSD_LOGO_URL = "https://static.wikia.nocookie.net/gtawiki/images/1/10/LSSD-GTAV-Logo.png/revision/latest?cb=20150829032550"

# *** กำหนด Timezone ของประเทศไทย (Asia/Bangkok = UTC+7) ***
THAI_TZ = pytz_timezone('Asia/Bangkok')

# *** ลิงก์กฎหมาย (Centralized Link) ***
PENAL_CODE_LINK = "https://sites.google.com/view/san-andreas-rp--state-law/%E0%B8%AB%E0%B8%99%E0%B8%B2%E0%B9%81%E0%B8%A3%E0%B8%81?authuser=0"

# -------------------- BOT SETUP --------------------
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')

intents = discord.Intents.default()
intents.messages = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='/', intents=intents)

# Global Cache (In-memory storage)
on_duty_users = set()
user_callsigns = {}

# -------------------- DATABASE FUNCTIONS --------------------

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS members
                         (
                             user_id INTEGER PRIMARY KEY,
                             callsign TEXT,
                             on_duty INTEGER DEFAULT 0,
                             start_time TEXT,
                             end_time TEXT
                         );
                         """)
        await db.commit()


async def load_initial_data():
    """Load callsigns and duty status from DB into memory when the bot starts"""
    global on_duty_users, user_callsigns
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT user_id, callsign, on_duty FROM members")
        rows = await cur.fetchall()
        for user_id, callsign, on_duty_status in rows:
            if callsign:
                user_callsigns[user_id] = callsign
            if on_duty_status == 1:
                on_duty_users.add(user_id)
    print(f"✅ Loaded {len(on_duty_users)} users on duty and {len(user_callsigns)} callsigns from DB.")


async def set_member(user_id: int, callsign=None, on_duty=None, start_time=None, end_time=None):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR IGNORE INTO members (user_id) VALUES (?)", (user_id,))
        updates = []
        params = []

        if callsign is not None:
            updates.append("callsign=?")
            params.append(callsign)
        if on_duty is not None:
            updates.append("on_duty=?")
            params.append(int(on_duty))
        if start_time is not None:
            updates.append("start_time=?")
            params.append(start_time)
        if end_time is not None:
            updates.append("end_time=?")
            params.append(end_time)

        if updates:
            query = f"UPDATE members SET {', '.join(updates)} WHERE user_id=?"
            params.append(user_id)
            await db.execute(query, tuple(params))
        await db.commit()


async def set_last_activity_time(user_id: int):
    """Updates the 'start_time' column with the current UTC time (used for 'Last Activity' tracking)."""
    current_time_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_FILE) as db:
        # อัปเดต start_time ซึ่งจะใช้เป็น Last Activity Time เมื่อ on_duty=1
        await db.execute("UPDATE members SET start_time=? WHERE user_id=? AND on_duty=1",
                         (current_time_iso, user_id))
        await db.commit()


async def get_member(user_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT callsign, on_duty, start_time, end_time FROM members WHERE user_id=?",
                               (user_id,))
        return await cur.fetchone()


# -------------------- UTILITY FUNCTIONS --------------------
def format_time(dt: datetime) -> str:
    """Formats datetime object to the desired string format (e.g., September 4, 2025 8:39 PM)"""
    # ฟังก์ชันนี้ใช้ THAI_TZ เพื่อให้มั่นใจว่าการแสดงผลถูกต้อง
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        # หากไม่มี Timezone กำหนด ให้สมมติว่าเป็น UTC ก่อน (เพื่อการแปลงที่ถูกต้อง)
        dt = dt.replace(tzinfo=timezone.utc)

    dt_thai = dt.astimezone(THAI_TZ)
    return dt_thai.strftime("%B %d, %Y %I:%M %p")


async def update_nickname(member: discord.Member, callsign: str):
    """Updates the user's nickname in the format: {Callsign} - {Username} or removes it if callsign is empty."""

    if member.id == member.guild.owner_id:
        print(f"⚠️ Cannot change nickname for {member.name} (Guild Owner restriction).")
        return

    if callsign:
        new_nickname = f"{callsign} - {member.name}"
    else:
        new_nickname = None

    if member.guild.me.top_role > member.top_role:
        try:
            trimmed_nickname = (new_nickname or member.name)[:32]
            await member.edit(nick=trimmed_nickname if new_nickname else None)
        except discord.Forbidden:
            print(f"❌ Bot lacks 'Manage Nicknames' permission for {member.name}.")
        except Exception as e:
            print(f"❌ Error updating nickname for {member.name}: {e}")
    else:
        print(f"⚠️ Cannot change nickname for {member.name} (Bot role too low).")


async def set_loa_nickname(member: discord.Member):
    """Sets the user's nickname to 'LOA - [Current Display Name]'."""

    current_display_name = member.display_name
    new_nickname = f"LOA - {current_display_name}"

    if member.id == member.guild.owner_id:
        print(f"⚠️ Cannot change nickname for {member.name} (Guild Owner restriction).")
        return

    if member.guild.me.top_role > member.top_role:
        try:
            trimmed_nickname = new_nickname[:32]
            await member.edit(nick=trimmed_nickname)
            print(f"✅ Changed nickname of {member.name} to {trimmed_nickname}")
        except discord.Forbidden:
            print(f"❌ Bot lacks 'Manage Nicknames' permission for {member.name}.")
        except Exception as e:
            print(f"❌ Error updating LOA nickname for {member.name}: {e}")
    else:
        print(f"⚠️ Cannot change LOA nickname for {member.name} (Bot role too low).")


# -------------------- LOA VIEWS --------------------
# *** ต้องเพิ่มโค้ด BackToWorkButton ที่คุณมีอยู่ตรงนี้ ***
class BackToWorkButton(discord.ui.View):
    # ... (โค้ด BackToWorkButton) ...
    pass # ตัวอย่าง: ลบบรรทัดนี้แล้วใส่โค้ดจริง

# -------------------- PAGER VIEWS --------------------
# *** ต้องเพิ่มโค้ด SITSelectView และ SITSelect ที่คุณมีอยู่ตรงนี้ ***
class SITSelectView(discord.ui.View):
    # ... (โค้ด SITSelectView) ...
    pass # ตัวอย่าง: ลบบรรทัดนี้แล้วใส่โค้ดจริง

class SITSelect(discord.ui.Select):
    # ... (โค้ด SITSelect) ...
    pass # ตัวอย่าง: ลบบรรทัดนี้แล้วใส่โค้ดจริง

# -------------------- MODAL: Pager --------------------
# *** ต้องเพิ่มโค้ด PagerModal ที่คุณมีอยู่ตรงนี้ ***
class PagerModal(discord.ui.Modal, title="Pager"):
    # ... (โค้ด PagerModal) ...
    pass # ตัวอย่าง: ลบบรรทัดนี้แล้วใส่โค้ดจริง


# -------------------- MODAL: Change Callsign (DUTY) --------------------
# *** ต้องเพิ่มโค้ด ChangeCallsignModal ที่คุณมีอยู่ตรงนี้ ***
class ChangeCallsignModal(discord.ui.Modal, title="Change Callsign"):
    # ... (โค้ด ChangeCallsignModal) ...
    pass # ตัวอย่าง: ลบบรรทัดนี้แล้วใส่โค้ดจริง


# -------------------- MODAL: Start Duty (DUTY) (แก้ไข Attribute Error แล้ว) --------------------
class DutyStartModal(discord.ui.Modal, title="Start Duty Log"):
    
    def __init__(self, member: discord.Member, current_callsign=""):
        super().__init__()
        self.member = member
        self.callsign_input.default = current_callsign
        self.name_input.default = member.display_name

    callsign_input = discord.ui.TextInput(label="Callsign (EX. 2L56)",
                                          placeholder="Required: Your operational callsign", max_length=32)
    name_input = discord.ui.TextInput(label="Name (EX. Tristan J. Miller)",
                                      placeholder="Required: Your full name/display name", max_length=50,
                                      style=discord.TextStyle.short)

    async def on_submit(self, interaction: discord.Interaction):
        # 🚨 ห่อหุ้มด้วย try...except เพื่อจับข้อผิดพลาดและตอบกลับ Discord เสมอ
        try:
            user_id = interaction.user.id

            # ✅ แก้ไข: ใช้ timezone.utc ที่นำเข้าจาก datetime เพื่อกำหนดเวลา UTC
            current_time_utc = datetime.now(timezone.utc)
            current_time_display = datetime.now(THAI_TZ)

            callsign = self.callsign_input.value.strip()
            name_log = self.name_input.value.strip()

            user_callsigns[user_id] = callsign
            start_time_iso = current_time_utc.isoformat()

            await set_member(user_id, callsign=callsign, on_duty=True, start_time=start_time_iso, end_time=None)
            on_duty_users.add(user_id)

            await update_nickname(self.member, callsign)

            embed = discord.Embed(
                title=f"{callsign} — ON DUTY",
                color=discord.Color.green(),
                timestamp=current_time_display
            )
            embed.set_author(name=interaction.user.guild.name,
                            icon_url=interaction.user.guild.icon.url if interaction.user.guild.icon else None)
            embed.add_field(name="Name", value=name_log, inline=False)
            embed.add_field(name="Start Time", value=format_time(current_time_display), inline=False)

            # NEW: แสดง Last Activity เป็น Discord Relative Timestamp
            timestamp_string = f"<t:{int(current_time_utc.timestamp())}:R>"
            embed.add_field(name="Last Activity", value=timestamp_string, inline=False)

            embed.add_field(name="Department", value=DEPARTMENT_NAME, inline=False)

            # ส่ง Embed Log สาธารณะ (ต้องตอบกลับ Modal ภายใน 3 วินาที)
            await interaction.response.send_message(embed=embed, ephemeral=False)

        except Exception as e:
            # พิมพ์ข้อผิดพลาดใน Console ของบอท
            print(f"❌ ERROR in DutyStartModal for user {interaction.user.name}: {e}")
            
            # ตอบกลับผู้ใช้ทันทีด้วยข้อความแจ้งเตือน (ephemeral)
            try:
                # ส่งข้อความแจ้ง error ที่ผู้ใช้เห็น (แทน "Something went wrong")
                await interaction.response.send_message(
                    f"⚠️ **Error!** Failed to start duty. Details: `{type(e).__name__}: {str(e)[:100]}...` "
                    f"Duty was **not** recorded. (Check bot console for full error: {e})",
                    ephemeral=True
                )
            except Exception:
                pass


# -------------------- MODAL: Leave of Absence (LOA) --------------------
# *** ต้องเพิ่มโค้ด LoaModal ที่คุณมีอยู่ตรงนี้ ***
class LoaModal(discord.ui.Modal, title="Request Leave of Absence (LOA)"):
    # ... (โค้ด LoaModal) ...
    pass # ตัวอย่าง: ลบบรรทัดนี้แล้วใส่โค้ดจริง


# -------------------- DUTY BUTTONS (พร้อมเงื่อนไข Callsign) --------------------
# *** ต้องเพิ่มโค้ด DutyButtons ที่คุณมีอยู่ตรงนี้ ***
class DutyButtons(discord.ui.View):
    # ... (โค้ด DutyButtons) ...
    pass # ตัวอย่าง: ลบบรรทัดนี้แล้วใส่โค้ดจริง


# -------------------- PREFIX COMMANDS --------------------
# *** ต้องเพิ่มโค้ด prefix commands (assign, law) ที่คุณมีอยู่ตรงนี้ ***
@bot.command()
async def assign(ctx, member: discord.Member, callsign: str):
    # ... (โค้ด assign) ...
    pass # ตัวอย่าง: ลบบรรทัดนี้แล้วใส่โค้ดจริง

@bot.command(name='law')
async def law_prefix(ctx):
    # ... (โค้ด law) ...
    pass # ตัวอย่าง: ลบบรรทัดนี้แล้วใส่โค้ดจริง

# -------------------- SLASH COMMANDS --------------------
# *** ต้องเพิ่มโค้ด slash commands (register, law_slash, duty, loa, pager) ที่คุณมีอยู่ตรงนี้ ***
@bot.tree.command(name='register', guild=discord.Object(id=GUILD_ID))
async def register(interaction: discord.Interaction):
    # ... (โค้ด register) ...
    pass # ตัวอย่าง: ลบบรรทัดนี้แล้วใส่โค้ดจริง

@bot.tree.command(name='law', description="แสดงลิงก์ประมวลกฎหมาย", guild=discord.Object(id=GUILD_ID))
async def law_slash(interaction: discord.Interaction):
    # ... (โค้ด law_slash) ...
    pass # ตัวอย่าง: ลบบรรทัดนี้แล้วใส่โค้ดจริง

@bot.tree.command(name='duty', description="แสดงปุ่ม Duty", guild=discord.Object(id=GUILD_ID))
async def duty(interaction: discord.Interaction):
    # ... (โค้ด duty) ...
    pass # ตัวอย่าง: ลบบรรทัดนี้แล้วใส่โค้ดจริง

@bot.tree.command(name='loa', description="ยื่นคำร้องขอลา (Leave of Absence)", guild=discord.Object(id=GUILD_ID))
async def loa(interaction: discord.Interaction):
    # ... (โค้ด loa) ...
    pass # ตัวอย่าง: ลบบรรทัดนี้แล้วใส่โค้ดจริง

@bot.tree.command(name='pager', description="เรียกสถานีหรือเจ้าหน้าที่", guild=discord.Object(id=GUILD_ID))
async def pager(interaction: discord.Interaction):
    # ... (โค้ด pager) ...
    pass # ตัวอย่าง: ลบบรรทัดนี้แล้วใส่โค้ดจริง

# -------------------- EVENTS & READY STATE --------------------

@bot.event
async def on_ready():
    await init_db()
    await load_initial_data()
    
    # *** สำคัญมาก: โหลด Persistent Views (DutyButtons) ***
    bot.add_view(DutyButtons())
    
    print(f"✅ Logged in as {bot.user}")
    try:
        guild = discord.Object(id=GUILD_ID)
        if GUILD_ID:
            await bot.tree.sync(guild=guild)
            print(f"✅ Synced slash commands to guild {GUILD_ID}")
        else:
            print("⚠️ GUILD_ID is missing or invalid. Skipping guild sync.")
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}")


@bot.event
async def on_message(message: discord.Message):
    # ตรวจสอบการใช้งาน (Last Activity)
    if message.guild and message.guild.id == GUILD_ID and message.author.id in on_duty_users:
        # ใช้ asyncio.create_task เพื่อให้การอัปเดต DB ไม่ block การทำงานหลักของบอท
        asyncio.create_task(set_last_activity_time(message.author.id))

    await bot.process_commands(message)


# -------------------- RUN BOT --------------------
if __name__ == '__main__':
    # รันเซิร์ฟเวอร์ Keep Alive ก่อนรันบอท
    # โค้ดนี้จะใช้ threading เพื่อให้ Flask และ Discord Bot ทำงานพร้อมกัน
    print("🚀 Starting Keep Alive Server...")
    server_on() 
    
    try:
        # log_handler=handler ทำให้ log เขียนลงไฟล์ discord.log
        bot.run(token, log_handler=handler)
    except discord.errors.LoginFailure:
        print("❌ Login Failed. Please check your DISCORD_TOKEN in the .env file.")
