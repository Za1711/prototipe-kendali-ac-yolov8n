import cv2
import subprocess
from flask import Flask, Response

app = Flask(__name__)

# Gunakan 0 untuk USB webcam bawaan, atau ganti dengan "rtsp://user:pass@ip:port/..." jika dari IP camera
SOURCE = 0
FOCUS_VALUE = 240  # Nilai fokus optimal terkunci (titik ketajaman tertinggi)

def lock_camera_focus(device_index=0, focus_val=240):
    """Kunci autofocus agar webcam tidak hunting/blur saat operasional."""
    try:
        subprocess.run([
            'v4l2-ctl', '-d', f'/dev/video{device_index}',
            '--set-ctrl=focus_automatic_continuous=0',
            f'--set-ctrl=focus_absolute={focus_val}'
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[CAM] Autofocus berhasil dimatikan. Fokus dikunci pada nilai {focus_val}.")
    except Exception as e:
        print(f"[CAM] Peringatan: Gagal set v4l2 focus: {e}")

# Kunci fokus pada level driver hardware
lock_camera_focus(SOURCE, FOCUS_VALUE)

cap = cv2.VideoCapture(SOURCE)
lock_camera_focus(SOURCE, FOCUS_VALUE)

# Fallback via OpenCV property
try:
    cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
    cap.set(cv2.CAP_PROP_FOCUS, FOCUS_VALUE)
except:
    pass

def generate_frames():
    while True:
        success, frame = cap.read()
        if not success:
            break
        # Kompres frame ke format JPEG
        ret, buffer = cv2.imencode('.jpg', frame)
        frame_bytes = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/')
def index():
    return f"<h3>Stream Kamera Aktif (Fokus Terkunci: {FOCUS_VALUE})</h3><p>Gunakan endpoint <code>/video_feed</code> untuk OpenCV.</p><img src='/video_feed' width='640'>"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
