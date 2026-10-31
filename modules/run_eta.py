import json
import math
import os
import time

from modules.avatar_generator import DEFAULT_CLIP_RANGE, DEFAULT_ON_SCREEN_RANGE

# Same order as main.py's STAGES, which can't be imported here: it pulls in the whole pipeline.
STAGES = ("Topic & script", "Voice", "Stock footage", "Avatar clips", "Rendering scenes", "Stitching", "Upload")

# Used until a finished step has been timed. Each stage's value is its own time,
# except 'Avatar clips' (time before the first clip) and 'Rendering scenes' (unused,
# see 'scene'): those two scale with the 'clip' and 'scene' values.
DEFAULT_TIMINGS = {
    "Topic & script": 90, "Voice": 60, "Stock footage": 60, "Avatar clips": 30, "Stitching": 45, "Upload": 30,
    "clip": 300,  # One ComfyUI render, including the prompt written for it
    "clip_seconds": 6,  # The length of clip that render time is for: it scales with length
    "scene": 3,  # One scene through ffmpeg
    "scenes": 9,  # Scenes per video
    "auto_clips": 5,  # Clips per video in the 'auto' avatar layout
}
# How much each new measurement moves the stored value.
LEARNING_RATE = 0.3
# A scene's narration caps how long she stays on screen in it.
TYPICAL_SCENE_SECONDS = 5.5
# A clip renders this much longer than she is on screen: rounded up, plus the overlap into the next scene.
CLIP_EXTRA_SECONDS = 1
# Unexplained drift from the original estimate below this isn't worth a line in the panel.
SMALL_DRIFT_SECONDS = 60


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


def _slider(env, low_name, high_name, defaults):
    """A range the panel's sliders set, from the run's own env, else .env, else the default."""
    settings = {**os.environ, **(env or {})}
    return int(settings.get(low_name) or defaults[0]), int(settings.get(high_name) or defaults[1])


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
        self.upload = "--upload" in argv
        self.clip_range = _slider(env, "AVATAR_CLIPS_MIN", "AVATAR_CLIPS_MAX", DEFAULT_CLIP_RANGE)
        shortest, longest = _slider(env, "AVATAR_MIN_SECONDS", "AVATAR_MAX_SECONDS", DEFAULT_ON_SCREEN_RANGE)
        self.planned_clip_seconds = min((shortest + longest) / 2, TYPICAL_SCENE_SECONDS) + CLIP_EXTRA_SECONDS
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

        self.video_number = 1
        self.video_total = self.videos_left + 1
        self.changes = []
        self.started = time.time()
        # Stays as it was at the start, however the run goes, to compare the live ETA with.
        self.original_total = self.plan()["total"]

    def _reset_video(self):
        self.scene_count = None
        self.scenes_started = 0
        self.clip_count = None
        self.clip_number = 0
        self.first_clip_at = None
        self.attempt_started = None
        self.attempt_seconds = self.planned_clip_seconds

    # --- What the run reports ---

    def video(self, number, total):
        self.video_number, self.video_total = number, total
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

    def clip(self, number, total, seconds):
        now = time.time()
        self._finish_attempt(now)
        if self.clip_count is None:
            self._note_clip_count(total)
        self.clip_number, self.clip_count = number, total
        self.first_clip_at = self.first_clip_at or now
        self.attempt_started = now
        self.attempt_seconds = seconds

    def clip_rejected(self, attempt, attempts, problem):
        """The lip-sync check failed: another render starts now, unless that was the last try."""
        now = time.time()
        self._finish_attempt(now)
        clip = f"Clip {self.clip_number} of {self.clip_count}"
        problem = problem.split(",")[0]  # The measurement after the comma only means something in the log
        if attempt < attempts:
            self.attempt_started = now
            self._note(f"{clip} rejected ({problem}), rendering it again", self._render_seconds(self.attempt_seconds))
        else:
            self.attempt_started = None
            self._note(f"{clip} rejected on all {attempts} tries ({problem}), using stock footage", 0)

    def scene(self):
        self.scenes_started += 1

    def finish(self):
        """The whole run succeeded, so the step it ended on did finish."""
        if self.number and self.number == self.total:
            self._finish_stage()

    # --- Why the ETA moved ---

    def _note(self, text, seconds):
        video = f"Video {self.video_number}: " if self.video_total > 1 else ""
        self.changes.append({"text": video + text, "seconds": round(seconds)})

    def _note_clip_count(self, total):
        planned = self._planned_clip_count()
        if round(planned) != total:
            self._note(f"{total} avatar {'clip' if total == 1 else 'clips'} instead of the {round(planned)} expected",
                       (total - planned) * self._render_seconds(self.planned_clip_seconds))

    def _all_changes(self, adjusted):
        """The noted changes, plus whatever they don't explain, so they add up to the adjusted ETA."""
        changes = list(self.changes)
        drift = adjusted - self.original_total - sum(change["seconds"] for change in changes)
        if abs(drift) >= SMALL_DRIFT_SECONDS:
            pace = "slower" if drift > 0 else "faster"
            changes.append({"text": f"Running {pace} than estimated", "seconds": round(drift)})
        return changes

    # --- Learning ---

    def _learn(self, key, seconds):
        self.timings[key] += LEARNING_RATE * (seconds - self.timings[key])

    def _finish_attempt(self, now):
        if self.attempt_started is not None:
            self._learn("clip", now - self.attempt_started)
            self._learn("clip_seconds", self.attempt_seconds)
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

    def _render_seconds(self, clip_seconds):
        return self.timings["clip"] * clip_seconds / self.timings["clip_seconds"]

    def _stage_seconds(self, name, clips, scenes):
        if name == "Avatar clips":
            return self.timings[name] + clips * self._render_seconds(self.planned_clip_seconds)
        if name == "Rendering scenes":
            return scenes * self.timings["scene"]
        return self.timings[name]

    def _planned_clip_count(self):
        if self.planned_clips is not None:
            return self.planned_clips
        # What 'auto' usually picks, within the range this run allows and the scenes there are.
        return min(max(self.timings["auto_clips"], self.clip_range[0]), self.clip_range[1], self.timings["scenes"])

    def _current_left(self, now):
        elapsed = now - self.stage_started
        if self.name == "Avatar clips" and self.clip_count is not None:
            after = (self.clip_count - self.clip_number) * self._render_seconds(self.planned_clip_seconds)
            if self.attempt_started is None:
                return after
            this_clip = self._render_seconds(self.attempt_seconds) - (now - self.attempt_started)
            return max(this_clip, 0) + after
        clips = self._planned_clip_count() if self.clip_count is None else self.clip_count
        scenes = self.timings["scenes"] if self.scene_count is None else self.scene_count
        return max(self._stage_seconds(self.name, clips, scenes) - elapsed, 0)

    def plan(self):
        """What a run with these options should take, for the panel to show before it starts."""
        clips = self._planned_clip_count()
        names = STAGES if self.upload else STAGES[:-1]
        steps = [{"name": name, "seconds": round(self._stage_seconds(name, clips, self.timings["scenes"]))}
                 for name in names]
        video = sum(step["seconds"] for step in steps)
        return {"steps": steps, "video": video, "videos": self.videos_left + 1,
                "total": video * (self.videos_left + 1), "clips": round(clips),
                "clip_seconds": math.ceil(self.planned_clip_seconds),
                "clip_render": round(self._render_seconds(self.planned_clip_seconds))}

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
        total_left = left + self.videos_left * video
        elapsed = now - self.started
        adjusted = elapsed + total_left
        return {"steps": steps, "total_left": round(total_left),
                "original_total": self.original_total, "original_left": round(self.original_total - elapsed),
                "adjusted_total": round(adjusted), "changes": self._all_changes(adjusted)}
