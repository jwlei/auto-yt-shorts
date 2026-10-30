import json
import os
import time

# Same order as main.py's STAGES, which can't be imported here: it pulls in the whole pipeline.
STAGES = ("Topic & script", "Voice", "Stock footage", "Avatar clips", "Rendering scenes", "Stitching", "Upload")

# Used until a finished step has been timed. Each stage's value is its own time,
# except 'Avatar clips' (time before the first clip) and 'Rendering scenes' (unused,
# see 'scene'): those two scale with the 'clip' and 'scene' values.
DEFAULT_TIMINGS = {
    "Topic & script": 90, "Voice": 60, "Stock footage": 60, "Avatar clips": 30, "Stitching": 45, "Upload": 30,
    "clip": 300,  # One ComfyUI render, including the prompt written for it
    "scene": 3,  # One scene through ffmpeg
    "scenes": 9,  # Scenes per video
    "auto_clips": 5,  # Clips per video in the 'auto' avatar layout
}
# How much each new measurement moves the stored value.
LEARNING_RATE = 0.3


def _load_timings(path):
    timings = dict(DEFAULT_TIMINGS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        timings.update({key: value for key, value in saved.items()
                        if key in timings and isinstance(value, (int, float))})
    except (OSError, ValueError, AttributeError):
        pass  # A broken file only costs accuracy
    return timings


def _option(argv, name, default):
    return argv[argv.index(name) + 1] if name in argv[:-1] else default


class RunEta:
    """
    Estimates what is left of a run from how long each step took before. Steps are
    timed as they finish and saved to `path`, so estimates improve with every run.
    A step cut short by a failure or Stop is never counted.
    """

    def __init__(self, argv, env, path):
        self.path = path
        self.timings = _load_timings(path)
        self.videos_left = int(_option(argv, "--runs", 1)) - 1
        layout = _option(argv, "--avatar-clips", "auto")
        if layout == "0" or (env or {}).get("AVATAR_SOURCE") == "static":
            self.planned_clips = 0  # No renders: the static clip just gets looped
        else:
            self.planned_clips = None if layout == "auto" else int(layout)

        self.number = 0
        self.total = 0
        self.name = None
        self.stage_started = None
        self.finished = {}
        self._reset_video()

    def _reset_video(self):
        self.scene_count = None
        self.scenes_started = 0
        self.clip_count = None
        self.clip_number = 0
        self.first_clip_at = None
        self.attempt_started = None

    # --- What the run reports ---

    def video(self, number, total):
        self.videos_left = total - number

    def scenes(self, count):
        self.scene_count = count

    def stage(self, number, total, name):
        if self.number and (number == self.number + 1 or (number == 1 and self.number == self.total)):
            self._finish_stage()
        if number == 1:
            self.finished = {}
            self._reset_video()
        self.number, self.total, self.name = number, total, name
        self.stage_started = time.time()

    def clip(self, number, total):
        now = time.time()
        self._finish_attempt(now)
        self.clip_number, self.clip_count = number, total
        self.first_clip_at = self.first_clip_at or now
        self.attempt_started = now

    def clip_rejected(self, attempt, attempts):
        """The lip-sync check failed: another render starts now, unless that was the last try."""
        now = time.time()
        self._finish_attempt(now)
        self.attempt_started = now if attempt < attempts else None

    def scene(self):
        self.scenes_started += 1

    def finish(self):
        """The whole run succeeded, so the step it ended on did finish."""
        if self.number and self.number == self.total:
            self._finish_stage()

    # --- Learning ---

    def _learn(self, key, seconds):
        self.timings[key] += LEARNING_RATE * (seconds - self.timings[key])

    def _finish_attempt(self, now):
        if self.attempt_started is not None:
            self._learn("clip", now - self.attempt_started)
            self.attempt_started = None

    def _finish_stage(self):
        now = time.time()
        self.finished[self.number] = now - self.stage_started
        if self.name == "Avatar clips":
            self._finish_attempt(now)
            self._learn(self.name, (self.first_clip_at or now) - self.stage_started)
            if self.planned_clips is None and self.clip_count:
                self._learn("auto_clips", self.clip_count)
        elif self.name == "Rendering scenes":
            if self.scenes_started:
                self._learn("scene", (now - self.stage_started) / self.scenes_started)
                self._learn("scenes", self.scenes_started)
        else:
            self._learn(self.name, now - self.stage_started)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.timings, f, indent=2)
        except OSError:
            pass  # Next run just starts from older numbers

    # --- Estimating ---

    def _stage_seconds(self, name, clips, scenes):
        if name == "Avatar clips":
            return self.timings[name] + clips * self.timings["clip"]
        if name == "Rendering scenes":
            return scenes * self.timings["scene"]
        return self.timings[name]

    def _planned_clip_count(self):
        return self.timings["auto_clips"] if self.planned_clips is None else self.planned_clips

    def _current_left(self, now):
        elapsed = now - self.stage_started
        if self.name == "Avatar clips" and self.clip_count is not None:
            clips_after = self.clip_count - self.clip_number
            if self.attempt_started is None:
                return clips_after * self.timings["clip"]
            this_clip = self.timings["clip"] - (now - self.attempt_started)
            return max(this_clip, 0) + clips_after * self.timings["clip"]
        clips = self._planned_clip_count() if self.clip_count is None else self.clip_count
        scenes = self.timings["scenes"] if self.scene_count is None else self.scene_count
        return max(self._stage_seconds(self.name, clips, scenes) - elapsed, 0)

    def estimate(self):
        """Returns seconds per step and left overall, or None before the first step (or for a run without steps)."""
        if not self.number:
            return None
        now = time.time()
        clips = self._planned_clip_count()
        scenes = self.timings["scenes"] if self.scene_count is None else self.scene_count
        names = STAGES[:self.total]

        steps = []
        left = 0
        for number, name in enumerate(names, 1):
            if number < self.number:
                steps.append({"state": "done", "seconds": round(self.finished.get(number, 0))})
            elif number == self.number:
                left = self._current_left(now)
                steps.append({"state": "now", "seconds": round(left)})
            else:
                seconds = self._stage_seconds(name, clips, scenes)
                left += seconds
                steps.append({"state": "next", "seconds": round(seconds)})

        video = sum(self._stage_seconds(name, clips, self.timings["scenes"]) for name in names)
        return {"steps": steps, "total_left": round(left + self.videos_left * video)}
