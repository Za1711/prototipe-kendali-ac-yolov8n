import time
import json
import logging
import cv2
import requests
import os
import shutil
from ultralytics import YOLO
import paho.mqtt.client as mqtt
import ssl
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def load_env(filepath=None):
    if filepath is None:
        filepath = os.path.join(BASE_DIR, ".env")
    env_vars = {}
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    env_vars[key.strip()] = val.strip()
    return env_vars

# Load initial env
ENV = load_env()
API_URL = ENV.get("AACS_API_URL", "http://127.0.0.1/api/v1")

STREAM_URL = "http://localhost:5000/video_feed"
MODEL_PATH = os.path.join(BASE_DIR, "models/best.pt")
INTERVAL_SECONDS = 120       # Ambil gambar & inferensi tiap 2 menit (120 detik)
HEARTBEAT_INTERVAL = 120     # Kirim heartbeat berkala tiap 120 detik
ROOM_REFRESH_INTERVAL = 300  # Refresh mapping room tiap 5 menit

# Konfigurasi Jam Operasional (07:00 - 17:00 WIB)
START_HOUR = 7   # 07:00 Pagi
END_HOUR = 17    # 05:00 Sore (17:00)

# Konfigurasi Penyimpanan Gambar & Retensi
CAPTURES_DIR = os.path.join(BASE_DIR, "captures")
RETENTION_DAYS = 4  # Hapus folder jika sudah >= 4 hari (menyimpan data 3 hari terakhir)

MQTT_HOST = "f62988abf955495196dcc5ef9ec94248.s1.eu.hivemq.cloud"
MQTT_PORT = 8883
MQTT_USER = "AACS"
MQTT_PASS = "Capstone123"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

logging.info(f"Memuat model YOLO dari {MODEL_PATH}...")
model = YOLO(MODEL_PATH)

class State:
    room_id = None
    occupancy_topic = None
    last_room_check = datetime.min
    last_publish_time = datetime.min
    last_people_count = -1
    is_active_hours = True
    last_cleanup_check = datetime.min

def is_operating_hours(now: datetime) -> bool:
    """Cek apakah saat ini berada dalam jam operasional 07:00 - 17:00 WIB."""
    return START_HOUR <= now.hour < END_HOUR

def cleanup_old_captures():
    """Hapus folder harian yang usianya sudah >= 4 hari agar penyimpanan aman."""
    if not os.path.exists(CAPTURES_DIR):
        return

    today = datetime.now().date()
    try:
        for entry in os.listdir(CAPTURES_DIR):
            folder_path = os.path.join(CAPTURES_DIR, entry)
            if os.path.isdir(folder_path):
                try:
                    folder_date = datetime.strptime(entry, "%Y-%m-%d").date()
                    diff_days = (today - folder_date).days
                    if diff_days >= RETENTION_DAYS:
                        shutil.rmtree(folder_path)
                        logging.info(f"[CLEANUP] Folder gambar lama dihapus (usia {diff_days} hari): {entry}")
                except ValueError:
                    # Lewati jika bukan format tanggal YYYY-MM-DD
                    pass
    except Exception as e:
        logging.error(f"[CLEANUP] Error saat membersihkan folder lama: {e}")

def save_annotated_image(results, now: datetime, count: int) -> str:
    """Simpan gambar hasil inferensi YOLO dengan bounding box ke folder harian YYYY-MM-DD."""
    try:
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H-%M-%S")
        
        day_dir = os.path.join(CAPTURES_DIR, date_str)
        os.makedirs(day_dir, exist_ok=True)
        
        filename = f"{date_str}_{time_str}_count_{count}.jpg"
        filepath = os.path.join(day_dir, filename)
        
        annotated_frame = results[0].plot()
        cv2.imwrite(filepath, annotated_frame)
        logging.info(f"[IMAGE] Hasil bounding box disimpan: {filepath}")
        return filepath
    except Exception as e:
        logging.error(f"[IMAGE] Gagal menyimpan gambar: {e}")
        return ""

def get_device_room_info():
    token = load_env().get("AACS_DEVICE_TOKEN", "")
    if not token:
        logging.error("[ROOM CHECK] AACS_DEVICE_TOKEN kosong!")
        return None

    try:
        url = f"{API_URL}/iot/my-room"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        res = requests.get(url, headers=headers, timeout=10)
        
        if res.status_code == 200:
            return res.json().get("data", {})
        elif res.status_code == 401:
            logging.error("[ROOM CHECK] Token invalid (401). Hentikan publish sementara.")
            return "401"
        elif res.status_code == 403:
            logging.error("[ROOM CHECK] Device bukan RASPBERRY_PI (403).")
            return "403"
        elif res.status_code == 409:
            logging.warning("[ROOM CHECK] Device belum memiliki room (409). Retry dengan backoff.")
            return "409"
        else:
            logging.error(f"[ROOM CHECK] Gagal API: {res.status_code}")
    except Exception as e:
        logging.error(f"[ROOM CHECK] Gagal koneksi ke API.")
    return None

def publish_to_esp32(topic, count, max_retries=2):
    for attempt in range(1, max_retries + 1):
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"yolo-node-{int(time.time())}")
            client.username_pw_set(MQTT_USER, MQTT_PASS)
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLSv1_2)
            
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
            client.loop_start()
            
            res = client.publish(topic, str(count), qos=1)
            res.wait_for_publish(timeout=5)
            
            client.loop_stop()
            client.disconnect()
            logging.info(f"[OCCUPANCY] count={count} publish=success")
            return True
        except Exception as e:
            if attempt == max_retries:
                logging.error(f"[MQTT] Publish gagal setelah {max_retries} percobaan.")
            time.sleep(2)
    return False

def save_to_database(room_id, count):
    token = load_env().get("AACS_DEVICE_TOKEN", "")
    url = f"{API_URL}/rooms/{room_id}/occupancy"
    try:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}
        requests.post(url, json={"people_count": count}, headers=headers, timeout=5)
    except:
        pass

def main():
    logging.info("=== Service Deteksi Okupansi Dimulai ===")
    os.makedirs(CAPTURES_DIR, exist_ok=True)
    cleanup_old_captures()

    while True:
        now = datetime.now()

        # 1. Pengecekan Jam Operasional (07:00 - 17:00)
        if not is_operating_hours(now):
            if State.is_active_hours:
                logging.info(f"[SCHEDULE] Di luar jam operasional (07:00 - 17:00). Standby... (Saat ini: {now.strftime('%H:%M:%S')})")
                State.is_active_hours = False
            time.sleep(30)
            continue
        else:
            if not State.is_active_hours:
                logging.info(f"[SCHEDULE] Memasuki jam operasional (07:00 - 17:00). Deteksi aktif. (Saat ini: {now.strftime('%H:%M:%S')})")
                State.is_active_hours = True

        # 2. Pembersihan folder lama secara berkala (tiap 1 jam)
        if (now - State.last_cleanup_check).total_seconds() > 3600:
            cleanup_old_captures()
            State.last_cleanup_check = now

        # 3. Refresh room mapping berkala atau jika belum terisi
        if State.room_id is None or (now - State.last_room_check).total_seconds() > ROOM_REFRESH_INTERVAL:
            info = get_device_room_info()
            if isinstance(info, str):
                # Error status (401, 403, 409)
                State.room_id = None
                time.sleep(15) # Backoff
                continue
            elif isinstance(info, dict) and info.get("room_id"):
                State.room_id = info["room_id"]
                State.occupancy_topic = info["occupancy_topic"]
                State.last_room_check = now
                logging.info(f"[ROOM] room_id={State.room_id} topic={State.occupancy_topic}")
            else:
                # API Timeout/5xx, pertahankan cache jika ada, tunggu sebentar
                time.sleep(5)

        if not State.room_id:
            time.sleep(5)
            continue

        # 4. Capture & Inferensi
        cap = cv2.VideoCapture(STREAM_URL)
        if not cap.isOpened():
            time.sleep(5)
            continue

        ret, frame = cap.read()
        cap.release()

        if ret and frame is not None:
            results = model.predict(source=frame, conf=0.4, imgsz=640, verbose=False)
            people_count = len(results[0].boxes) if results[0].boxes is not None else 0

            # Simpan gambar hasil deteksi dengan bounding box ke folder harian
            save_annotated_image(results, now, people_count)

            # Publish logic: jika nilai berubah ATAU sudah mencapai interval heartbeat
            time_since_publish = (now - State.last_publish_time).total_seconds()
            if people_count != State.last_people_count or time_since_publish >= HEARTBEAT_INTERVAL:
                success = publish_to_esp32(State.occupancy_topic, people_count)
                if success:
                    State.last_people_count = people_count
                    State.last_publish_time = now
                    save_to_database(State.room_id, people_count)

        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
