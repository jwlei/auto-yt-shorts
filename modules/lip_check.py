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
# Top of the forehead, the tip of the nose, the two sides of the face and the outer eye corners.
FOREHEAD, NOSE_TIP, FACE_LEFT, FACE_RIGHT, EYE_LEFT, EYE_RIGHT = 10, 1, 234, 454, 33, 263
# Centre and edge of each iris.
IRISES = ((468, 469), (473, 474))
# Part of the iris radius sampled for its colour: outside the pupil, inside the rim.
IRIS_RING = (0.45, 0.8)
# An iris smaller than this many pixels has no colour worth reading.
MIN_IRIS_RADIUS = 3
# How many frames of each second of a clip are considered for the thumbnail's still.
STILL_FRAMES_PER_SECOND = 2
# Blink score above which a frame can't be the still.
MAX_STILL_BLINK = 0.4
# A still crops to this many face heights, with the face's middle this far down it.
STILL_FACE_HEIGHTS = 3.2
STILL_FACE_LINE = 0.35
# The detector misses a face that is small in a wide image, so it also looks at this many squares of it.
FACE_SEARCH_SQUARES = 5


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


def _image_landmarker():
    options = vision.FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path=_model_path()),
                                           running_mode=vision.RunningMode.IMAGE, num_faces=1,
                                           output_face_blendshapes=True)
    return vision.FaceLandmarker.create_from_options(options)


def _detect(landmarker, frame):
    return landmarker.detect(mediapipe.Image(image_format=mediapipe.ImageFormat.SRGB,
                                             data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))


def eye_color(image_path):
    """The iris colour in an image, or None if no face or iris can be read in it."""
    frame = cv2.imread(image_path)
    if frame is None:
        return None
    with _image_landmarker() as landmarker:
        faces = _detect(landmarker, frame).face_landmarks
    return _iris_color(frame, faces[0]) if faces else None


def _still_fault(result, frame):
    """
    How far a frame is from a good still of her, where 0 is a level face turned straight
    to the camera, mouth shut and eyes relaxed. None if it can't be one: no face, part of
    her face out of frame, or her eyes closing.
    """
    if not result.face_landmarks:
        return None
    points = result.face_landmarks[0]
    if not all(0 <= point.x <= 1 and 0 <= point.y <= 1 for point in points):
        return None
    scores = {shape.category_name: shape.score for shape in result.face_blendshapes[0]}
    if (scores["eyeBlinkLeft"] + scores["eyeBlinkRight"]) / 2 > MAX_STILL_BLINK:
        return None
    height, width = frame.shape[:2]
    # The nose sits halfway between the sides of a face turned straight to the camera.
    turned = abs((points[NOSE_TIP].x - points[FACE_LEFT].x)
                 / max(points[FACE_RIGHT].x - points[FACE_LEFT].x, 1e-6) - 0.5) * 2
    tilted = abs(np.degrees(np.arctan2((points[EYE_RIGHT].y - points[EYE_LEFT].y) * height,
                                       (points[EYE_RIGHT].x - points[EYE_LEFT].x) * width))) / 45
    wide = (scores["eyeWideLeft"] + scores["eyeWideRight"]) / 2
    return 2 * turned + 2 * tilted + scores["jawOpen"] + wide


def best_face_frame(clips, path):
    """
    Saves the frame that makes the best still of her from the shown part of each clip.
    clips: [(clip path, seconds shown)]. Returns False if no frame could be one.
    """
    best, best_fault = None, None
    with _image_landmarker() as landmarker:
        for clip, seconds in clips:
            capture = cv2.VideoCapture(clip)
            fps = capture.get(cv2.CAP_PROP_FPS) or 24
            step = max(round(fps / STILL_FRAMES_PER_SECOND), 1)
            try:
                for number in range(int(seconds * fps)):
                    ok, frame = capture.read()
                    if not ok:
                        break
                    if number % step:
                        continue
                    fault = _still_fault(_detect(landmarker, frame), frame)
                    if fault is not None and (best_fault is None or fault < best_fault):
                        best, best_fault = frame, fault
            finally:
                capture.release()
    return best is not None and cv2.imwrite(path, best, [cv2.IMWRITE_JPEG_QUALITY, 95])


def _locate_face(landmarker, frame):
    """Where a face is in the frame, in pixels: (top of forehead, chin, nose's x), or None."""
    height, width = frame.shape[:2]
    faces = _detect(landmarker, frame).face_landmarks
    if faces:
        points = faces[0]
        return points[FOREHEAD].y * height, points[CHIN].y * height, points[NOSE_TIP].x * width

    # The square with the face nearest its middle, as one that cuts the face off reads it badly.
    found, off_centre = None, None
    for left in np.linspace(0, max(width - height, 0), FACE_SEARCH_SQUARES).astype(int):
        faces = _detect(landmarker, frame[:, left:left + height]).face_landmarks
        if faces:
            points = faces[0]
            distance = abs(points[NOSE_TIP].x - 0.5)
            if off_centre is None or distance < off_centre:
                off_centre = distance
                found = points[FOREHEAD].y * height, points[CHIN].y * height, left + points[NOSE_TIP].x * height
    return found


def crop_to_face(image_path, path):
    """Saves a 9:16 crop around the face in an image, for a wide reference image. Returns False without a face."""
    frame = cv2.imread(image_path)
    if frame is None:
        return False
    with _image_landmarker() as landmarker:
        face = _locate_face(landmarker, frame)
    if not face:
        return False
    top_of_face, chin, nose_x = face
    height, width = frame.shape[:2]
    crop_height = min(int((chin - top_of_face) * STILL_FACE_HEIGHTS), height)
    crop_width = min(crop_height * 9 // 16, width)
    left = int(nose_x - crop_width / 2)
    top = int((top_of_face + chin) / 2 - crop_height * STILL_FACE_LINE)
    left, top = max(min(left, width - crop_width), 0), max(min(top, height - crop_height), 0)
    return cv2.imwrite(path, frame[top:top + crop_height, left:left + crop_width], [cv2.IMWRITE_JPEG_QUALITY, 95])


def face_share(image_path):
    """How much of the image's height her face takes up, forehead to chin, or None if no face can be seen."""
    frame = cv2.imread(image_path)
    if frame is None:
        return None
    with _image_landmarker() as landmarker:
        faces = _detect(landmarker, frame).face_landmarks
    return abs(faces[0][CHIN].y - faces[0][FOREHEAD].y) if faces else None


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
