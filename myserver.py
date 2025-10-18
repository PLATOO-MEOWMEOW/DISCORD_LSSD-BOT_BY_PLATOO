# โค้ดที่ควรจะเป็นใน myserver.py:
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot Server is Running"

def run():
    app.run(host='0.0.0.0', port=8080)

def server_on():
    t = Thread(target=run)
    t.start()

# ถ้า myserver.py มีบรรทัดนี้อยู่แล้ว ให้แน่ใจว่ามันถูกลบ:
# from myserver import server_on