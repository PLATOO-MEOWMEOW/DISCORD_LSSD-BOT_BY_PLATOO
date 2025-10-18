from flask import Flask
from threading import Thread
import os

# สร้าง Flask App
app = Flask('')

@app.route('/')
def home():
    """เส้นทางหลักที่ UptimeRobot จะใช้ Ping"""
    return "LSSD Bot is Alive!"

def run():
    """ฟังก์ชันสำหรับรัน Flask ใน Thread แยก"""
    # ใช้พอร์ตที่ Render กำหนด (หรือ 8080 เป็นค่า default)
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def server_on():
    """ฟังก์ชันที่ถูกเรียกใช้จาก main.py เพื่อเริ่ม Web Server"""
    # รัน Flask ใน Thread แยก เพื่อไม่ให้ขัดขวางการทำงานของ Discord Bot
    thread = Thread(target=run)
    thread.start()
