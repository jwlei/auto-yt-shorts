import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

from modules.paths import PROJECT_ROOT

RUNS_DIR = os.path.join(PROJECT_ROOT, "assets", "runs")
# Same order as main.py's STAGES, which can't be imported here: it pulls in the whole pipeline.
STAGES = ("Topic & script", "Voice", "Stock footage", "Avatar clips", "Rendering scenes", "Stitching", "Upload")
# What the panel sets per run rather than on the command line. A continued run keeps them.
RUN_ENV = ("AVATAR_SOURCE", "COMFYUI_WORKFLOW", "AVATAR_CLIPS_MIN", "AVATAR_CLIPS_MAX",
           "AVATAR_MIN_SECONDS", "AVATAR_MAX_SECONDS")
RUN_ID = re.compile(r"run_\d{8}_\d{6}_\d+")
SAVE_ATTEMPTS = 5


@dataclass
class RunState:
    """
    What one video's run has finished so far, saved after every step and every
    accepted clip, so a run that was stopped, failed or cut off by a crash can
    continue without paying for that work again. Its working files live in the
    same folder, so runs can't overwrite each other's.

    A clip counts once it passed its checks: a file on disk may be a rejected
    render, or half a download.
    """

    id: str
    argv: list  # main.py's options, without --resume
    env: dict
    number: int  # This video's place in the batch
    videos_after: int  # Videos of the batch still to come after this one
    topic: str = None
    script: list = None
    voiced: bool = False
    video_pairs: list = None
    avatar_indices: list = None
    reference_image: str = None  # Each video picks a persona at random, and has to stay her
    avatar_clips: dict = field(default_factory=dict)
    avatar_done: bool = False
    final: str = None

    @classmethod
    def new(cls, argv, number, videos_after):
        return cls(id=f"run_{datetime.now():%Y%m%d_%H%M%S}_{number}", argv=list(argv),
                   env={name: os.environ[name] for name in RUN_ENV if os.environ.get(name)},
                   number=number, videos_after=videos_after)

    @classmethod
    def load(cls, run_id):
        """The saved run, with anything whose files have gone marked as still to do. None if there isn't one."""
        if not RUN_ID.fullmatch(str(run_id)):
            return None
        try:
            with open(os.path.join(RUNS_DIR, run_id, "state.json"), "r", encoding="utf-8") as f:
                state = cls(**json.load(f))
            state.avatar_clips = {int(index): path for index, path in state.avatar_clips.items()}
            state._forget_missing()
        except (OSError, ValueError, TypeError, AttributeError):
            return None  # Nothing saved, or a broken file: nothing to continue
        return state

    # --- Where its files go ---

    @property
    def folder(self):
        return os.path.join(RUNS_DIR, self.id)

    @property
    def path(self):
        return os.path.join(self.folder, "state.json")

    @property
    def audio_dir(self):
        return os.path.join(self.folder, "audio")

    @property
    def clips_dir(self):
        return os.path.join(self.folder, "clips")

    @property
    def temp_dir(self):
        return os.path.join(self.folder, "temp")

    # --- Saving ---

    def update(self, **fields):
        for name, value in fields.items():
            setattr(self, name, value)
        self.save()

    def keep_clip(self, index, path):
        self.avatar_clips[index] = path
        self.save()

    def save(self):
        """Never stops the render it is saving: a failed save only costs the chance to continue from here."""
        # Before the script exists, only a later video of a batch has anything to continue: the videos left.
        if not self.script and self.number == 1:
            return
        temp = self.path + ".tmp"
        try:
            os.makedirs(self.folder, exist_ok=True)
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, ensure_ascii=False, indent=1)
            for attempt in range(SAVE_ATTEMPTS):
                try:
                    os.replace(temp, self.path)
                    return
                except PermissionError:  # The panel has it open for a moment
                    if attempt == SAVE_ATTEMPTS - 1:
                        raise
                    time.sleep(0.2)
        except (OSError, TypeError, ValueError) as e:
            print(f"   ⚠️ Could not save this run's progress: {e}")

    def release_batch(self):
        """The batch moved on without this video, so continuing it must not make the rest again."""
        self.videos_after = 0
        if self.script:
            self.save()
        else:
            self.remove()

    def remove(self, keep_files=False):
        """State first: without it the run is gone from the list, even if a file refuses to be deleted."""
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"   ⚠️ Could not remove {self.path}: {e}")
        if not keep_files:
            shutil.rmtree(self.folder, ignore_errors=True)

    # --- What is left ---

    def next_stage(self):
        if not self.script:
            return "Topic & script"
        if not self.voiced:
            return "Voice"
        if self.video_pairs is None:
            return "Stock footage"
        if not self.avatar_done:
            return "Avatar clips"
        # Rendering and stitching take seconds, so they are done again rather than tracked scene by scene.
        return "Rendering scenes" if not self.final else "Upload"

    def kept_stages(self):
        return STAGES[:STAGES.index(self.next_stage())]

    def _forget_avatar(self):
        self.avatar_indices, self.reference_image = None, None
        self.avatar_clips, self.avatar_done = {}, False

    def _forget_missing(self):
        if self.voiced and not all(os.path.exists(scene.get("audio_path", "")) for scene in self.script):
            # Her clips are lip-synced to the voice that's gone.
            self.voiced = False
            self.final = None
            self._forget_avatar()
        if self.video_pairs and not all(os.path.exists(path) for pair in self.video_pairs for path in pair or ()):
            self.video_pairs = None
        if self.reference_image and not os.path.exists(self.reference_image):
            self._forget_avatar()
        kept = {index: path for index, path in self.avatar_clips.items() if os.path.exists(path)}
        if len(kept) < len(self.avatar_clips):
            self.avatar_clips, self.avatar_done = kept, False
        if self.final and not os.path.exists(self.final):
            self.final = None


def list_runs():
    """Every run that can be continued, newest first."""
    try:
        names = os.listdir(RUNS_DIR)
    except OSError:
        return []
    runs = (RunState.load(name) for name in sorted(names, reverse=True))
    return [run for run in runs if run]


def folder_size(path):
    return sum(os.path.getsize(os.path.join(folder, name))
               for folder, _, names in os.walk(path) for name in names
               if os.path.exists(os.path.join(folder, name)))
