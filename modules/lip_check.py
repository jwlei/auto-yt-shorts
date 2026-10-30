import os

import cv2
import mediapipe
import numpy as np
import requests
from mediapipe.tasks.python import BaseOptions, vision

# Downloaded on first use; about 4 MB.
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
             "face_landmarker/float16/1/face_landmarker.task")
MODEL_PATH = os.path.join("assets", "models", "face_landmarker.task")

# Face mesh points: inner upper and lower lip, and between the eyes to the chin.
UPPER_LIP, LOWER_LIP = 13, 14
BETWEEN_EYES, CHIN = 168, 152


def _model_path():
    path = os.path.join(os.getcwd(), MODEL_PATH)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        response = requests.get(MODEL_URL, timeout=60)
        response.raise_for_status()
        with open(path, "wb") as f:
            f.write(response.content)
    return path


def _distance(points, a, b, width, height):
    return np.hypot((points[a].x - points[b].x) * width, (points[a].y - points[b].y) * height)


def mouth_movement(clip, seconds):
    """
    How much the mouth moves in the first `seconds` of a clip: the spread of the
    gap between the lips, relative to face size so a camera push-in doesn't count.
    Spread rather than the gap itself, as a mouth frozen half open has a wide gap
    but isn't speaking. Frames without a face count as a closed mouth.
    """
    options = vision.FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path=_model_path()),
                                           running_mode=vision.RunningMode.VIDEO, num_faces=1)
    capture = cv2.VideoCapture(clip)
    fps = capture.get(cv2.CAP_PROP_FPS) or 24
    gaps = []
    try:
        with vision.FaceLandmarker.create_from_options(options) as landmarker:
            while len(gaps) < seconds * fps:
                ok, frame = capture.read()
                if not ok:
                    break
                image = mediapipe.Image(image_format=mediapipe.ImageFormat.SRGB,
                                        data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                faces = landmarker.detect_for_video(image, int(len(gaps) * 1000 / fps)).face_landmarks
                if not faces:
                    gaps.append(0.0)
                    continue
                height, width = frame.shape[:2]
                face_size = _distance(faces[0], BETWEEN_EYES, CHIN, width, height)
                gaps.append(_distance(faces[0], UPPER_LIP, LOWER_LIP, width, height) / max(face_size, 1e-6))
    finally:
        capture.release()
    return float(np.std(gaps)) if gaps else 0.0
