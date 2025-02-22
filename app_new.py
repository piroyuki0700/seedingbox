#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
育苗箱温度管理プログラム
・DS18B20センサーから温度取得
・設定に応じてUSB接続ヒーターマットのGPIOをON/OFF制御
・Flaskサーバーで現在状態表示と設定更新を実施
"""

import os
import json
import threading
import time
from datetime import datetime
from flask import Flask, jsonify, request, render_template

# Raspberry Pi用GPIOライブラリ
try:
    import RPi.GPIO as GPIO
except ImportError:
    # Raspberry Pi以外でのテスト用
    class GPIO_dummy:
        BCM = 'BCM'
        OUT = 'OUT'
        HIGH = True
        LOW = False
        def setmode(self, mode): pass
        def setup(self, pin, mode): pass
        def output(self, pin, state): 
            print(f"GPIO {pin} -> {state}")
        def cleanup(self): pass
    GPIO = GPIO_dummy()

# 定数定義
DS18B20_DEVICE = "/sys/bus/w1/devices/28-XXXXXXXX/w1_slave"  # 実際のデバイスファイルに変更してください
HEATER_GPIO_PIN = 17  # 固定GPIOピン番号
CONFIG_FILE = "seedbox_config.json"
LOG_FILE = "seedbox_control.log"
LOG_ENABLED = True         # ログ出力のON/OFF（Trueで出力）

# グローバル変数
config = {}
config_lock = threading.Lock()  # 設定更新時の排他制御用
current_temp = None
heater_status = False  # True: ヒーターON, False: OFF

# ログ設定
import logging
logger = logging.getLogger("SeedboxControl")
logger.setLevel(logging.INFO)
if LOG_ENABLED:
    fh = logging.FileHandler(LOG_FILE)
    fh.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(message)s')
    fh.setFormatter(formatter)
    logger.addHandler(fh)

# GPIO初期化
GPIO.setmode(GPIO.BCM)
GPIO.setup(HEATER_GPIO_PIN, GPIO.OUT)
# 初期状態はOFF
GPIO.output(HEATER_GPIO_PIN, GPIO.LOW)

# 設定ファイルの読み込み／保存
def load_config():
    global config
    if not os.path.exists(CONFIG_FILE):
        # ファイルがなければ初期設定を作成
        default_config = {
            "control_enabled": True,
            "day_start": "06:00",
            "day_end": "18:00",
            "day_temp_min": 20.0,
            "day_temp_max": 25.0,
            "night_temp_min": 18.0,
            "night_temp_max": 23.0
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(default_config, f, indent=4)
        config = default_config
    else:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
    logger.info("設定読み込み: " + json.dumps(config))

def save_config():
    with config_lock:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)
    logger.info("設定保存: " + json.dumps(config))

# DS18B20から温度を読み取る関数
def read_temperature():
    try:
        with open(DS18B20_DEVICE, 'r') as f:
            lines = f.readlines()
        # 最初の行に"YES"があれば読み取り成功
        if lines[0].strip()[-3:] != "YES":
            return None
        equals_pos = lines[1].find("t=")
        if equals_pos != -1:
            temp_string = lines[1][equals_pos+2:]
            temperature = float(temp_string) / 1000.0
            return temperature
    except Exception as e:
        logger.info("温度センサー読み取りエラー: " + str(e))
        return None

# ヒーター制御用関数
def heater_on():
    global heater_status
    if not heater_status:
        GPIO.output(HEATER_GPIO_PIN, GPIO.HIGH)
        heater_status = True
        logger.info("ヒーター ON")

def heater_off():
    global heater_status
    if heater_status:
        GPIO.output(HEATER_GPIO_PIN, GPIO.LOW)
        heater_status = False
        logger.info("ヒーター OFF")

# 温度制御ループ（バックグラウンドスレッド）
def control_loop():
    global current_temp, config
    while True:
        temp = read_temperature()
        if temp is not None:
            current_temp = temp
            with config_lock:
                local_config = config.copy()
            if local_config.get("control_enabled", False):
                now = datetime.now().time()
                try:
                    day_start = datetime.strptime(local_config["day_start"], "%H:%M").time()
                    day_end = datetime.strptime(local_config["day_end"], "%H:%M").time()
                except Exception as e:
                    logger.info("時間設定エラー: " + str(e))
                    day_start = datetime.strptime("06:00", "%H:%M").time()
                    day_end = datetime.strptime("18:00", "%H:%M").time()
                # 昼間か夜間かの判定
                if day_start <= now <= day_end:
                    temp_min = local_config["day_temp_min"]
                    temp_max = local_config["day_temp_max"]
                else:
                    temp_min = local_config["night_temp_min"]
                    temp_max = local_config["night_temp_max"]
                # 温度制御
                if temp < temp_min:
                    heater_on()
                elif temp > temp_max:
                    heater_off()
                # 温度が範囲内なら現在の状態を維持
            else:
                # 制御OFFの場合はヒーターOFF
                heater_off()
        else:
            logger.info("温度取得失敗")
        time.sleep(10)  # 制御周期（10秒ごと）
        
# Flaskアプリケーション設定
app = Flask(__name__)

# ルート：HTML画面の表示
@app.route("/")
def index():
    return render_template("index.html")

# ステータス取得エンドポイント（GET /status）
@app.route("/status")
def get_status():
    with config_lock:
        local_config = config.copy()
    return jsonify({
        "temperature": current_temp if current_temp is not None else "--",
        "heater": heater_status,
        "day_start": local_config.get("day_start", "--"),
        "day_end": local_config.get("day_end", "--"),
        "day_min": local_config.get("day_temp_min", "--"),
        "day_max": local_config.get("day_temp_max", "--"),
        "night_min": local_config.get("night_temp_min", "--"),
        "night_max": local_config.get("night_temp_max", "--"),
        "control_enabled": local_config.get("control_enabled", False)
    })

# 設定更新エンドポイント（POST /update_config）
@app.route("/update_config", methods=["POST"])
def update_config():
    global config
    data = request.get_json()
    if not data:
        return jsonify({"result": "error", "message": "No data provided"}), 400
    with config_lock:
        for key in ["control_enabled", "day_start", "day_end", 
                    "day_temp_min", "day_temp_max", "night_temp_min", "night_temp_max"]:
            if key in data:
                config[key] = data[key]
        save_config()
    return jsonify({"result": "success", "config": config})

if __name__ == "__main__":
    load_config()
    control_thread = threading.Thread(target=control_loop, daemon=True)
    control_thread.start()
    app.run(host="0.0.0.0", port=5000, debug=False)

