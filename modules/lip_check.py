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
# Centre and edge of each iris.
IRISES = ((468, 469), (473, 474))
# Part of the iris radius sampled for its colour: outside the pupil, inside the rim.
IRIS_RING = (0.45, 0.8)
# An iris smaller than this many pixels has no colour worth reading.
MIN_IRIS_RADIUS = 3


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


def _iris_color(frame, points):
    """The median colour of both irises in Lab, which keeps brightness apart from hue. None if too small to read."""
    height, width = frame.shape[:2]
    pixels = []
    for centre, edge in IRISES:
        x, y = points[centre].x * width, points[centre].y * height
        radius = np.hypot(points[edge].x * width - x, points[edge].y * height - y)
        if radius < MIN_IRIS_RADIUS:
            return None
        top, left = max(int(y - radius), 0), max(int(x - radius), 0)
        crop = frame[top:int(y + radius) + 1, left:int(x + radius) + 1]
        rows, columns = np.ogrid[top:top + crop.shape[0], left:left + crop.shape[1]]
        distance = np.hypot(columns - x, rows - y)
        pixels.append(crop[(distance > radius * IRIS_RING[0]) & (distance < radius * IRIS_RING[1])])
    pixels = np.vstack(pixels)
    if not len(pixels):
        return None
    lab = cv2.cvtColor(pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2LAB).reshape(-1, 3)
    return np.median(lab, axis=0)


def eye_color(image_path):
    """The iris colour in an image, or None if no face or iris can be read in it."""
    options = vision.FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path=_model_path()),
                                           running_mode=vision.RunningMode.IMAGE, num_faces=1)
    frame = cv2.imread(image_path)
    if frame is None:
        return None
    with vision.FaceLandmarker.create_from_options(options) as landmarker:
        faces = landmarker.detect(mediapipe.Image(image_format=mediapipe.ImageFormat.SRGB,
                                                  data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))).face_landmarks
    return _iris_color(frame, faces[0]) if faces else None


def _windows(values, size):
    """Every run of `size` neighbouring values, or all of them if there are fewer."""
    return [values[i:i + size] for i in range(max(len(values) - size, 0) + 1)]


def measure_clip(clip, seconds, expected_eye_color=None):
    """
    Scores the first `seconds` of a clip, each on its worst second, since a clip
    that's fine on average can still go wrong for a moment:
      mouth: spread of the gap between the lips, relative to face size so a camera
             push-in doesn't count, in the second it moves least. Spread rather than
             the gap itself, as a mouth frozen half open has a wide gap but isn't
             speaking. Frames without a face count as a closed mouth.
      stare: MediaPipe's 'eye wide' score (0-1) in the second it is highest.
      eye_shift: how far the iris colour strays from expected_eye_color (Lab distance)
             in the second it strays most. 0 without an expected colour.
      eye_color: the clip's overall iris colour, or None if it could not be read.
    """
    options = vision.FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path=_model_path()),
                                           running_mode=vision.RunningMode.VIDEO, num_faces=1,
                                           output_face_blendshapes=True)
    capture = cv2.VideoCapture(clip)
    fps = capture.get(cv2.CAP_PROP_FPS) or 24
    gaps, wide, irises = [], [], []
    try:
        with vision.FaceLandmarker.create_from_options(options) as landmarker:
            while len(gaps) < seconds * fps:
                ok, frame = capture.read()
                if not ok:
                    break
                image = mediapipe.Image(image_format=mediapipe.ImageFormat.SRGB,
                                        data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                result = landmarker.detect_for_video(image, int(len(gaps) * 1000 / fps))
                if not result.face_landmarks:
                    gaps.append(0.0)
                    wide.append(0.0)
                    irises.append(None)
                    continue
                points = result.face_landmarks[0]
                height, width = frame.shape[:2]
                face_size = _distance(points, BETWEEN_EYES, CHIN, width, height)
                gaps.append(_distance(points, UPPER_LIP, LOWER_LIP, width, height) / max(face_size, 1e-6))
                scores = {shape.category_name: shape.score for shape in result.face_blendshapes[0]}
                wide.append((scores["eyeWideLeft"] + scores["eyeWideRight"]) / 2)
                irises.append(_iris_color(frame, points))
    finally:
        capture.release()

    size = max(int(fps), 1)
    read = [color for color in irises if color is not None]
    shifts = [0.0]
    if expected_eye_color is not None:
        for window in _windows(irises, size):
            colors = [color for color in window if color is not None]
            # Mostly unreadable (a blink, a turned head) says nothing about the colour.
            if len(colors) * 2 >= size:
                shifts.append(float(np.linalg.norm(np.median(colors, axis=0) - expected_eye_color)))
    return {
        "mouth": min(float(np.std(window)) for window in _windows(gaps, size)) if gaps else 0.0,
        "stare": max(float(np.mean(window)) for window in _windows(wide, size)) if wide else 0.0,
        "eye_shift": max(shifts),
        "eye_color": np.median(read, axis=0) if read else None,
    }
