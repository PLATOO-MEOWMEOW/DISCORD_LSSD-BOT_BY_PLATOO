import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
# ✅ แก้ไขการนำเข้า: นำเข้า timezone จาก datetime เพื่อใช้เป็น tzinfo (e.g., timezone.utc)
from datetime import datetime, timezone
import aiosqlite
import re
# ✅ แก้ไขการนำเข้า: เปลี่ยนชื่อ timezone ที่นำเข้าจาก pytz เป็น pytz_timezone เพื่อป้องกันการทับซ้อน
from pytz import timezone as pytz_timezone
from myserver import server_on
import threading

# -------------------- CONFIGURATION --------------------
load_dotenv()
token = os.getenv('DISCORD_TOKEN')

DB_FILE = "database.db"

# *** 1. ต้องกำหนด GUILD_ID (ID เซิร์ฟเวอร์ของคุณ) ***
GUILD_ID_STR = os.getenv("GUILD_ID") or '1428284368602005536'
try:
    GUILD_ID = int(GUILD_ID_STR)
except ValueError:
    raise ValueError("GUILD_ID must be an integer.")

# *** 2. กำหนด LOG/WELCOME/GOODBYE CHANNEL ID ***
LOG_CHANNEL_ID_STR = os.getenv("LOG_CHANNEL_ID") or '123456789012345678'
try:
    LOG_CHANNEL_ID = int(LOG_CHANNEL_ID_STR)
except ValueError:
    LOG_CHANNEL_ID = None

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
                             user_id
                             INTEGER
                             PRIMARY
                             KEY,
                             callsign
                             TEXT,
                             on_duty
                             INTEGER
                             DEFAULT
                             0,
                             start_time
                             TEXT,
                             end_time
                             TEXT
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


# Function to update Last Activity Time
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
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        # ใช้วิธีการกำหนด UTC ที่ถูกต้อง
        dt = dt.replace(tzinfo=timezone.utc)

    dt_thai = dt.astimezone(THAI_TZ)
    return dt_thai.strftime("%B %d, %Y %I:%M %p")


async def update_nickname(member: discord.Member, callsign: str):
    """Updates the user's nickname in the format: {Callsign} - {Username} or removes it if callsign is empty."""

    if member.id == member.guild.owner_id:
        print(f"⚠️ Cannot change nickname for {member.name} (Guild Owner restriction).")
        return

    # LOGIC: ถ้า callsign เป็นค่าว่าง จะทำการลบ Nickname ทิ้ง
    if callsign:
        new_nickname = f"{callsign} - {member.name}"
    else:
        # กำหนดให้เป็น None เพื่อรีเซ็ต Nickname กลับไปเป็นชื่อผู้ใช้ Discord เดิม
        new_nickname = None

    if member.guild.me.top_role > member.top_role:
        try:
            trimmed_nickname = (new_nickname or member.name)[:32]
            # หาก new_nickname เป็น None จะเป็นการรีเซ็ต Nickname
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
class BackToWorkButton(discord.ui.View):
    def __init__(self, original_user: discord.Member):
        # Persistent View (timeout=None)
        super().__init__(timeout=None)
        self.original_user_id = original_user.id
        self.original_user_mention = original_user.mention
        self.original_user_display_name = original_user.display_name

    @discord.ui.button(label="BACK TO WORK", style=discord.ButtonStyle.success)
    async def back_to_work_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        global LSSD_LOGO_URL

        if interaction.user.id != self.original_user_id:
            return await interaction.response.send_message("❌ This button is for the LOA member only.", ephemeral=True)

        # 1. ล้าง LOA nickname
        await update_nickname(interaction.user, "")

        # 2. สร้าง Embed สำหรับ Welcome Back
        welcome_embed = discord.Embed(
            title="🎉  WELCOME BACK TO DUTY !  🎉",
            description=(
                f"**{interaction.user.display_name}** ({self.original_user_mention}) "
                f"ได้กลับจากการลาพักงาน (LOA) แล้ว และพร้อมปฏิบัติหน้าที่แล้ว! "
                f"\n(Nickname ถูกรีเซ็ตแล้ว)"
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        welcome_embed.set_thumbnail(url=LSSD_LOGO_URL)

        # 3. ส่ง Embed แจ้งกลับ
        await interaction.response.send_message(
            content=f"## **WELCOME BACK** {self.original_user_mention}",
            embed=welcome_embed,
            allowed_mentions=discord.AllowedMentions.all()
        )

        # 4. ปิดการใช้งานปุ่มในข้อความ LOA เดิม
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)


# -------------------- PAGER VIEWS --------------------
class SITSelectView(discord.ui.View):
    def __init__(self, case_no, from_user, to_target, location):
        super().__init__(timeout=300)
        self.case_no = case_no
        self.from_user = from_user
        self.to_target = to_target
        self.location = location
        self.add_item(SITSelect())

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(content="⚠️ Pager request timed out. Please try again.", view=None)
        except Exception:
            pass


class SITSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="SHOOTING ALERT", description="Armed Suspects/Shots Fired"),
            discord.SelectOption(label="BARRICADE SUSPECT", description="Suspect Barricaded Inside Structure"),
            discord.SelectOption(label="ROBBERRY ALERT", description="In-Progress Robbery"),
            discord.SelectOption(label="KIDNAPE SITUATION", description="Abduction of a Person"),
            discord.SelectOption(label="HOSTAGE SITUATION", description="Hostage Taker Present"),
        ]
        super().__init__(placeholder="Select Situation (SIT)", min_values=1, max_values=1, options=options,
                         custom_id="sit_select_menu")

    async def callback(self, interaction: discord.Interaction):
        view: SITSelectView = self.view
        selected_sit = self.values[0]

        pager_log = (
            f"## **PAGER — PAGER — PAGER**\n"
            f"**CASE NO:** {view.case_no}\n"
            f"**FROM:** {view.from_user}\n"
            f"**TO:** {view.to_target}\n"
            f"**LOC:** {view.location}\n"
            f"**SIT:** **{selected_sit}**\n"
            f"## **PAGER — PAGER — PAGER**"
        )
        await interaction.response.send_message(pager_log, allowed_mentions=discord.AllowedMentions.all())
        for item in view.children:
            item.disabled = True
        await interaction.message.edit(content=f"Pager Sent! Case {view.case_no}. (SIT: {selected_sit})", view=view)


# -------------------- MODAL: Pager --------------------
class PagerModal(discord.ui.Modal, title="LSSD Pager Command"):
    def __init__(self, member: discord.Member):
        super().__init__()
        self.member = member

    case_no_input = discord.ui.TextInput(label="Case No.", placeholder="e.g., 031, 2025-001A", max_length=10,
                                         required=True)
    to_input = discord.ui.TextInput(label="TO (Target: @Mention or Division Name)",
                                    placeholder="e.g., @Bravo Team, Mission Row Division", max_length=100,
                                    required=True)
    loc_input = discord.ui.TextInput(label="LOC (Location)", placeholder="Los Santos Freeway, Vinewood Hills",
                                     max_length=100, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        case_no = self.case_no_input.value.strip()
        from_user_mention = interaction.user.mention
        to_target_raw = self.to_input.value.strip()
        location = self.loc_input.value.strip()

        to_target_mention = to_target_raw
        id_match = re.search(r'(<[@!&]?\d{17,20}>)', to_target_raw)

        if id_match:
            to_target_mention = to_target_raw
        elif to_target_raw.startswith('@'):
            mention_name = to_target_raw[1:].strip()
            role = discord.utils.get(interaction.guild.roles, name=mention_name)
            if role:
                to_target_mention = role.mention
            else:
                member = interaction.guild.get_member_named(mention_name)
                if member:
                    to_target_mention = member.mention
                else:
                    to_target_mention = to_target_raw

        view = SITSelectView(case_no, from_user_mention, to_target_mention, location)

        await interaction.response.send_message(
            "✅ Select the Situation (SIT) for the Pager:",
            view=view,
            ephemeral=True
        )
        view.message = await interaction.original_response()

    # -------------------- MODAL: Change Callsign (DUTY) --------------------


class ChangeCallsignModal(discord.ui.Modal, title="Change Callsign"):
    def __init__(self, current_callsign=""):
        super().__init__()
        self.new_callsign.default = current_callsign

    new_callsign = discord.ui.TextInput(label="Enter your new callsign",
                                        placeholder="Example: A-21, BRAVO-2, J. Miller", max_length=32)

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        callsign = self.new_callsign.value.strip()
        current_time = datetime.now(THAI_TZ)

        user_callsigns[user_id] = callsign
        await set_member(user_id, callsign=callsign)
        await update_nickname(interaction.user, callsign)

        # อัปเดต Last Activity หากกำลัง On Duty
        member_data = await get_member(user_id)
        if user_id in on_duty_users:
            # บันทึกเวลาที่กดเปลี่ยน Callsign เป็น Last Activity
            await set_last_activity_time(user_id)

        if user_id not in on_duty_users:
            return await interaction.response.send_message(
                f"✅ Callsign updated to **{callsign}**.",
                ephemeral=True
            )

        start_time_iso = member_data[2] if member_data and member_data[2] else None

        # ดึง Last Activity Time และสร้าง Discord Timestamp
        last_activity_str = "N/A"
        if start_time_iso:
            try:
                # แปลง ISO string กลับเป็น datetime object (คาดว่าเป็น UTC)
                start_dt_utc = datetime.fromisoformat(start_time_iso).astimezone(timezone.utc)
                # ใช้ Discord Relative Timestamp: <t:timestamp:R>
                last_activity_str = f"<t:{int(start_dt_utc.timestamp())}:R>"
            except Exception:
                pass

        name_log = interaction.user.display_name

        embed = discord.Embed(
            title=f"Callsign Change: {callsign} — STILL ON DUTY",
            description=f"User **{name_log}** has updated their operational callsign while on duty.",
            color=discord.Color.blue(),
            timestamp=current_time
        )
        embed.set_author(name=interaction.user.guild.name,
                         icon_url=interaction.user.guild.icon.url if interaction.user.guild.icon else None)
        embed.add_field(name="New Callsign", value=callsign, inline=False)
        # ใช้ Last Activity Time แทน Original Duty Start Time
        embed.add_field(name="Last Activity (Updated)", value=last_activity_str, inline=False)
        embed.add_field(name="Department", value=DEPARTMENT_NAME, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=False)
        await interaction.followup.send(
            f"✅ Callsign updated to **{callsign}** and public change log posted. Duty time is still running.",
            ephemeral=True)


# -------------------- MODAL: Start Duty (DUTY) (เพิ่ม try...except และแก้ไข timezone) --------------------
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

            # บันทึก start_time เป็นเวลาเริ่ม Duty (และ Last Activity ครั้งแรก)
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
                await interaction.response.send_message(
                    f"⚠️ **Error!** Failed to start duty. Details: `{type(e).__name__}: {str(e)[:100]}...` "
                    f"Please contact an administrator. Duty was **not** recorded.",
                    ephemeral=True
                )
            except Exception:
                pass


# -------------------- MODAL: Leave of Absence (LOA) --------------------
class LoaModal(discord.ui.Modal, title="Request Leave of Absence (LOA)"):
    name_input = discord.ui.TextInput(label="Name (ชื่อ)", placeholder="Your full name/display name", max_length=50)
    duration_input = discord.ui.TextInput(label="Duration (ระยะเวลา)",
                                          placeholder="e.g., 2 weeks, 1 month (28/10/2025 - 28/11/2025)",
                                          max_length=100, style=discord.TextStyle.short)
    reason_input = discord.ui.TextInput(label="Reason (เหตุผล)", placeholder="Briefly describe the reason for absence",
                                        max_length=500, style=discord.TextStyle.long)

    def __init__(self, member: discord.Member):
        super().__init__()
        self.member = member
        self.name_input.default = member.display_name

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_input.value.strip()
        duration = self.duration_input.value.strip()
        reason = self.reason_input.value.strip()
        user_mention = interaction.user.mention
        current_time = datetime.now(THAI_TZ)
        global LSSD_LOGO_URL, DEPARTMENT_NAME

        await set_loa_nickname(self.member)

        # สร้าง Embed สำหรับประกาศ LOA
        embed = discord.Embed(
            title="🔔 LEAVE OF ABSENCE (LOA) REQUEST",
            description=f"**{name} ({user_mention})** ได้แจ้งขอลาพักงานชั่วคราวจาก **{DEPARTMENT_NAME}**",
            color=discord.Color.gold(),
            timestamp=current_time
        )

        embed.set_thumbnail(url=LSSD_LOGO_URL)
        embed.set_author(name=interaction.guild.name,
                         icon_url=interaction.guild.icon.url if interaction.guild.icon else None)

        embed.add_field(name="👤 Member", value=user_mention, inline=True)
        embed.add_field(name="👤 Name", value=name, inline=True)
        embed.add_field(name="📅 Duration (ระยะเวลา)", value=duration, inline=False)
        embed.add_field(name="📝 Reason (เหตุผล)", value=reason, inline=False)
        embed.set_footer(text=f"Request by: {interaction.user.display_name} | User ID: {interaction.user.id}")

        view = BackToWorkButton(self.member)

        # ส่งข้อความเป็น Embed พร้อมปุ่ม
        await interaction.response.send_message(
            content=f"## **OFFICIAL LOA ANNOUNCEMENT** {user_mention}",
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.all()
        )


# -------------------- DUTY BUTTONS (พร้อมเงื่อนไข Callsign) --------------------
class DutyButtons(discord.ui.View):
    def __init__(self):
        # *** Persistent View (timeout=None) ***
        super().__init__(timeout=None)
        self.custom_id = "duty_control_view"

    @discord.ui.button(label="On Duty", style=discord.ButtonStyle.success, custom_id="duty_on_button")
    async def on_duty(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        if user_id in on_duty_users:
            return await interaction.response.send_message("🚨 You're already on duty.", ephemeral=True)
        callsign = user_callsigns.get(user_id, "")
        member = interaction.user
        await interaction.response.send_modal(DutyStartModal(member, callsign))

    @discord.ui.button(label="Off Duty", style=discord.ButtonStyle.danger, custom_id="duty_off_button")
    async def off_duty(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        end_time = datetime.now(THAI_TZ)
        if user_id not in on_duty_users:
            return await interaction.response.send_message("🚨 You're not currently on duty.", ephemeral=True)
        on_duty_users.remove(user_id)
        callsign = user_callsigns.get(user_id, "Unknown")
        member_data = await get_member(user_id)

        start_time_iso = member_data[2] if member_data and member_data[2] else None

        # คำนวณเวลาการทำงานรวม (Duration)
        overall_time = "N/A"
        try:
            if start_time_iso:
                start_time_utc = datetime.fromisoformat(start_time_iso).astimezone(timezone.utc)
                end_time_utc = end_time.astimezone(timezone.utc)

                duration = end_time_utc - start_time_utc
                duration_hours = duration.total_seconds() / 3600
                overall_time = f"{duration_hours:.2f} Hours"
        except Exception as e:
            print(f"❌ Error calculating duty time for {user_id}: {e}")

        await update_nickname(interaction.user, "")

        # รีเซ็ต start_time และ on_duty status
        await set_member(user_id, on_duty=False, end_time=end_time.isoformat(), start_time=None)

        embed = discord.Embed(
            title=f"{callsign} — OFF DUTY",
            color=discord.Color.red(),
            timestamp=end_time
        )
        embed.set_author(name=interaction.user.guild.name,
                         icon_url=interaction.user.guild.icon.url if interaction.user.guild.icon else None)
        embed.add_field(name="Name", value=interaction.user.display_name, inline=False)
        embed.add_field(name="End Time", value=format_time(end_time), inline=False)
        embed.add_field(name="Overall Time", value=overall_time, inline=False)
        embed.add_field(name="Department", value=DEPARTMENT_NAME, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=False)

    @discord.ui.button(label="Change Callsign", style=discord.ButtonStyle.primary, custom_id="duty_change_callsign")
    async def change_callsign(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id

        # 🚨 ตรวจสอบ: ต้อง On Duty ก่อน
        if user_id not in on_duty_users:
            return await interaction.response.send_message(
                "❌ **คุณต้องกด On Duty ก่อน** จึงจะสามารถเปลี่ยน Callsign ได้",
                ephemeral=True
            )

        # อัปเดต Last Activity
        await set_last_activity_time(user_id)

        callsign = user_callsigns.get(user_id, "")
        await interaction.response.send_modal(ChangeCallsignModal(callsign))


# -------------------- PREFIX COMMANDS --------------------

@bot.command()
async def assign(ctx):
    role = discord.utils.get(ctx.guild.roles, name=secret_role)
    if role:
        try:
            await ctx.author.add_roles(role)
            await ctx.send(f"✅ {ctx.author.mention} is now assigned to **{secret_role}**")
        except discord.Forbidden:
            await ctx.send("❌ I do not have permissions to assign that role. Check bot role hierarchy.")
        except Exception as e:
            await ctx.send(f"❌ An error occurred: {e}")
    else:
        await ctx.send(f"❌ Role **{secret_role}** doesn't exist on this server.")


@bot.command()
async def law(ctx: commands.Context):
    """Prefix command to provide the link to the San Andreas Penal Code."""
    embed = discord.Embed(
        title="SAN ANDREAS PENAL CODE",
        description="Click the button below to view the official legal documentation.",
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow()
    )
    button = discord.ui.Button(
        label="VIEW PENAL CODE",
        style=discord.ButtonStyle.link,
        url=PENAL_CODE_LINK
    )
    view = discord.ui.View()
    view.add_item(button)
    await ctx.send(embed=embed, view=view)


# -------------------- SLASH COMMANDS --------------------

@bot.tree.command(name="register", description="Apply to LSSD", guild=discord.Object(id=GUILD_ID))
async def register(interaction: discord.Interaction):
    embed = discord.Embed(
        title="REGISTER",
        description="Application form for **Los Santos Sheriff's Department**",
        color=0xFFCC00,
        timestamp=discord.utils.utcnow()
    )
    button = discord.ui.Button(
        label="APPLY HERE",
        style=discord.ButtonStyle.link,
        url="https://docs.google.com/forms/d/e/1FAIpQLSfMaytXev-LM-Sr03Xy5zVzaD914vRBUX2BQgDK5U8r4-Grjg/viewform"
    )
    view = discord.ui.View()
    view.add_item(button)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="law", description="View the San Andreas Penal Code", guild=discord.Object(id=GUILD_ID))
async def law_slash(interaction: discord.Interaction):
    """Slash command to provide the link to the San Andreas Penal Code."""
    embed = discord.Embed(
        title="SAN ANDREAS PENAL CODE",
        description="Click the button below to view the official legal documentation.",
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow()
    )
    button = discord.ui.Button(
        label="VIEW PENAL CODE",
        style=discord.ButtonStyle.link,
        url=PENAL_CODE_LINK
    )
    view = discord.ui.View()
    view.add_item(button)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="duty", description="Open duty control menu", guild=discord.Object(id=GUILD_ID))
async def duty(interaction: discord.Interaction):
    view = DutyButtons()
    embed = discord.Embed(
        title="DUTY LOG",
        description="Select your duty status:",
        color=0x3498DB,
        timestamp=discord.utils.utcnow()
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="loa", description="Submit a Leave of Absence (LOA) request", guild=discord.Object(id=GUILD_ID))
async def loa(interaction: discord.Interaction):
    modal = LoaModal(interaction.user)
    await interaction.response.send_modal(modal)


@bot.tree.command(name="pager", description="Send a Pager alert", guild=discord.Object(id=GUILD_ID))
async def pager(interaction: discord.Interaction):
    modal = PagerModal(interaction.user)
    await interaction.response.send_modal(modal)


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
async def on_member_join(member: discord.Member):
    if LOG_CHANNEL_ID is None:
        return

    guild = member.guild
    log_channel = guild.get_channel(LOG_CHANNEL_ID)

    if log_channel:
        speech = (
            "## 👮‍♂️ WELCOME TO LSSD! — A new recruit has joined!\n"
            "**\"Protecting and Serving the County of Los Santos\"**\n\n"
            f"Welcome **{member.mention}**! Please follow the steps below to join the department:\n"
            "1. **Register/Apply:** Use **/register**\n"
            "2. **Study the Penal Code:** Use **/law**\n"
            "**We are glad to have you!**"
        )

        embed = discord.Embed(
            title="NEW RECRUIT HAS ARRIVED",
            description=speech,
            color=discord.Color.green(),
            timestamp=datetime.now(THAI_TZ)
        )

        embed.set_thumbnail(url=LSSD_LOGO_URL)

        try:
            await log_channel.send(member.mention, embed=embed)
        except discord.Forbidden:
            print(f"❌ Forbidden to send message in channel {LOG_CHANNEL_ID}")
        except Exception as e:
            print(f"❌ Error sending welcome message: {e}")


@bot.event
async def on_member_remove(member: discord.Member):
    if LOG_CHANNEL_ID is None:
        return

    guild = member.guild
    log_channel = guild.get_channel(LOG_CHANNEL_ID)

    if log_channel:
        goodbye_message = (
            f"**{member.display_name}** has left the Los Santos County Sheriff Department.\n\n"
            "**We regret to see you go and hope we will see you soon. Good luck!**"
        )

        embed = discord.Embed(
            title="MEMBER LEFT",
            description=goodbye_message,
            color=discord.Color.red(),
            timestamp=datetime.now(THAI_TZ)
        )
        embed.set_footer(text=f"User ID: {member.id}")

        if LSSD_LOGO_URL:
            embed.set_thumbnail(url=LSSD_LOGO_URL)

        try:
            await log_channel.send(embed=embed)
        except discord.Forbidden:
            print(f"❌ Forbidden to send goodbye message in channel {LOG_CHANNEL_ID}")
        except Exception as e:
            print(f"❌ Error sending goodbye message: {e}")


# -------------------- MESSAGE PROCESSOR (อัปเดต Last Activity และ Anti-Swear) --------------------

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    user_id = message.author.id

    # 1. อัปเดต Last Activity Time หากผู้ใช้อยู่ในสถานะ On Duty
    if user_id in on_duty_users:
        await set_last_activity_time(user_id)

        # 2. Anti-swear logic
    swear_words = [r'\bshit\b', r'\bfuck\b', r'\basshole\b', r'\bเหี้ย\b', r'\bสัส\b', r'\bควย\b', r'\bหี\b']

    message_content_lower = message.content.lower()

    is_swearing = False
    for word_pattern in swear_words:
        if re.search(word_pattern, message_content_lower):
            is_swearing = True
            break

    if is_swearing:
        try:
            await message.delete()
            # ส่งข้อความเตือนแบบ ephemeral
            await message.channel.send(f"🚨 **{message.author.mention}** - อย่าใช้คำหยาบคาย! ข้อความของคุณถูกลบแล้ว",
                                       delete_after=5)
            return  # หยุดการทำงาน
        except discord.Forbidden:
            print(f"❌ Cannot delete message in channel {message.channel.id} (Forbidden).")
        except Exception as e:
            print(f"❌ Error deleting message: {e}")

    # *** 3. สำคัญมาก: ต้องเรียก process_commands เพื่อให้ Prefix Commands ทำงาน ***
    await bot.process_commands(message)

# -------------------- RUN BOT --------------------
if __name__ == '__main__':
    # รันเซิร์ฟเวอร์ (ถ้ามีไฟล์ myserver.py)
    server_on()
    # รันบอท
    try:
        bot.run(token, log_handler=handler)
    except discord.errors.LoginFailure:
        print("❌ Login Failed. Please check your DISCORD_TOKEN in the .env file.")