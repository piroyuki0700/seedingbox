#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
育苗箱温度管理プログラム
・DS18B20センサーから温度取得
・設定に応じてUSB接続ヒーターマットのGPIOをON/OFF制御
・Flaskサーバーで現在状態表示と設定更新を実施
・Ctrl-C/SIGTERMでグレースフルに終了し、リソースを解放
"""

import os
import json
import threading
import time
import signal
import sys
from datetime import datetime
from flask import Flask, jsonify, request, render_template
import logging
import socket

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
DS18B20_DEVICE = "/sys/bus/w1/devices/28-01204bfedcd9/w1_slave"  # 実際のデバイスファイルに変更してください
HEATER_GPIO_PIN = 24  # ヒーター制御用
LED_GPIO_PIN    = 23  # LED制御用
CONFIG_FILE = "seedbox_config.json"
LOG_FILE = "seedbox_control.log"
LOG_TO_FILE = True  # Trueならファイル出力、Falseならコンソール出力

# グローバル変数
config = {}
config_lock = threading.RLock()  # 設定更新時の排他制御用
current_temp = None
heater_status = False  # True: ヒーターON, False: OFF
led_status = False     # True: LED ON, False: OFF
time_of_day = 1
temp_status = "normal"

logger = None

# 終了用イベント
stop_event = threading.Event()

# ログ設定
def setup_logger():
    global logger
    logger = logging.getLogger("SeedboxControl")  # ルートロガーを取得
    logger.setLevel(logging.INFO)    # ログレベルを設定
    # ハンドラー設定
    if LOG_TO_FILE:
        handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stdout)
    # フォーマット設定
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    # ハンドラーをロガーに追加
    logger.addHandler(handler)

# GPIO初期化
GPIO.setmode(GPIO.BCM)
GPIO.setup(HEATER_GPIO_PIN, GPIO.OUT)
GPIO.setup(LED_GPIO_PIN, GPIO.OUT)
# 初期状態は両方OFF
GPIO.output(HEATER_GPIO_PIN, GPIO.LOW)
GPIO.output(LED_GPIO_PIN, GPIO.LOW)

# 設定ファイルの読み込み／保存
def load_config():
    global config
    if not os.path.exists(CONFIG_FILE):
        # ファイルがなければ初期設定を作成
        default_config = {
            "control_enabled": True,
            "day_start": "09:00",
            "day_end": "17:00",
            "day_temp_min": 20,
            "day_temp_max": 25,
            "night_temp_min": 10,
            "night_temp_max": 15,
            "led_on": "07:00",
            "led_off": "17:00",
            "led_force": False,
            "cycle_time": 10
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

def heater_off():
    global heater_status
    if heater_status:
        GPIO.output(HEATER_GPIO_PIN, GPIO.LOW)
        heater_status = False

# LED制御用関数
def led_on():
    global led_status
    if not led_status:
        GPIO.output(LED_GPIO_PIN, GPIO.HIGH)
        led_status = True
        logger.info("LED ON")

def led_off():
    global led_status
    if led_status:
        GPIO.output(LED_GPIO_PIN, GPIO.LOW)
        led_status = False
        logger.info("LED OFF")

# 温度・LED制御ループ（バックグラウンドスレッド）
def control_loop():
    global current_temp, config, stop_event, time_of_day, temp_status
    while not stop_event.is_set():
        with config_lock:
            local_config = config.copy()
        now = datetime.now().time()

        temp = read_temperature()
        if temp is not None:
            current_temp = temp
            # ヒーター制御
            if local_config.get("control_enabled", False):
                try:
                    day_start = datetime.strptime(local_config["day_start"], "%H:%M").time()
                    day_end = datetime.strptime(local_config["day_end"], "%H:%M").time()
                except Exception as e:
                    logger.info("時間設定エラー: " + str(e))
                    # デフォルト値
                    day_start = datetime.strptime("06:00", "%H:%M").time()
                    day_end = datetime.strptime("18:00", "%H:%M").time()
                # 昼間か夜間かの判定
                if day_start <= now <= day_end:
                    time_of_day = 1
                    temp_min = local_config["day_temp_min"]
                    temp_max = local_config["day_temp_max"]
                else:
                    time_of_day = 0
                    temp_min = local_config["night_temp_min"]
                    temp_max = local_config["night_temp_max"]
                temp_status = "normal"
                if current_temp is not None:
                    if current_temp < temp_min:
                        temp_status = "low"
                    elif current_temp > temp_max:
                        temp_status = "high"
                        
                # 温度制御
                if temp < float(temp_min):
                    logger.info(f"ヒーター ON temp={temp} < {temp_min}")
                    heater_on()
                elif temp > float(temp_max):
                    logger.info(f"ヒーター OFF temp={temp} > {temp_max}")
                    heater_off()
                # 温度が範囲内なら現在の状態を維持
            else:
                # 制御OFFの場合はヒーターOFF
                heater_off()
        else:
            logger.info("温度取得失敗")
            heater_off()
 
        # まず、LED強制点灯が有効なら優先して LED を点灯
        if local_config.get("led_force", False):
            if not led_status:
                led_on()
        else:
       	    # LED制御：現在時刻と設定値で判定
            try:
                led_on_time = datetime.strptime(local_config["led_on"], "%H:%M").time()
                led_off_time = datetime.strptime(local_config["led_off"], "%H:%M").time()
            except Exception as e:
                logger.info("LED時間設定エラー: " + str(e))
                led_on_time = datetime.strptime("08:00", "%H:%M").time()
                led_off_time = datetime.strptime("20:00", "%H:%M").time()
            if led_on_time <= now < led_off_time:
                if not led_status:
                    led_on()
            else:
                if led_status:
                    led_off()

        stop_event.wait(timeout=local_config["cycle_time"])  # 制御周期

def cleanup():
    heater_off()
    GPIO.cleanup()
    logger.info("リソースを解放しました")

# Flaskアプリケーション設定
app = Flask(__name__)

# サーバーのローカルIPアドレスを取得
def get_local_ip():
    try:
        # 外部に接続せずにローカルIPを取得
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # Google DNSを利用 (実際に接続しない)
            return s.getsockname()[0]  # IPアドレスを取得
    except Exception as e:
        print(f"IPアドレス取得エラー: {e}")
        return "127.0.0.1"

# ルート：HTML画面の表示
@app.route("/")
def index():
    ip_address = get_local_ip()  # サーバーのIPアドレス取得
    return render_template('index.html', server_ip=ip_address)

# ステータス取得エンドポイント（GET /status）
@app.route("/api/status")
def get_status():
    with config_lock:
        local_config = config.copy()
    return jsonify({
        "temperature": current_temp if current_temp is not None else "--",
        "time_of_day": time_of_day,
        "temp_status": temp_status,
        "heater": heater_status,
        "led": led_status,
        "day_start": local_config.get("day_start", "--"),
        "day_end": local_config.get("day_end", "--"),
        "day_min": local_config.get("day_temp_min", "--"),
        "day_max": local_config.get("day_temp_max", "--"),
        "night_min": local_config.get("night_temp_min", "--"),
        "night_max": local_config.get("night_temp_max", "--"),
        "led_on": local_config.get("led_on", "--"),
        "led_off": local_config.get("led_off", "--"),
        "led_force": local_config.get("led_force", False),
        "cycle_time": local_config.get("cycle_time", "--"),
        "control_enabled": local_config.get("control_enabled", False)
    })

# 設定更新エンドポイント（POST /update_config）
@app.route("/api/update_config", methods=["POST"])
def update_config():
    global config
    data = request.get_json()
    if not data:
        return jsonify({"result": "error", "message": "No data provided"}), 400
    with config_lock:
        # 更新可能な項目のみ反映
        for key in ["control_enabled", "day_start", "day_end", 
                    "day_temp_min", "day_temp_max", "night_temp_min", "night_temp_max",
                    "led_on", "led_off", "led_force", "cycle_time"]:
            if key in data:
                config[key] = data[key]
        save_config()
    return jsonify({"result": "success", "config": config})

if __name__ == "__main__":
    setup_logger()
    load_config()
    try:
        # 温度制御用のバックグラウンドスレッド開始
        control_thread = threading.Thread(target=control_loop, daemon=True)
        control_thread.start()

        if len(sys.argv) > 1 and sys.argv[1] == 'on':
            # Flask の reloader を無効にして実行
            app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt を検知")
    finally:
        if control_thread and control_thread.is_alive():
                stop_event.set()
                control_thread.join(timeout=3)
        cleanup()

