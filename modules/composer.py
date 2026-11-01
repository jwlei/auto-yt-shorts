import os
import random
import re
import ffmpeg

from modules.audio import SILENCE_NOISE
from modules.paths import OUTPUT_DIR

# How long neighbouring scenes overlap in a transition, in seconds.
TRANSITION = 0.5

class Composer:
    def __init__(self, temp_dir=None):
        self.temp_dir = temp_dir or os.path.join(os.getcwd(), "assets", "temp")
        self.final_dir = OUTPUT_DIR
        self.avatar_path = os.path.join(os.getcwd(), "assets", "avatar", "avatars.mp4")
        
        os.makedirs(self.temp_dir, exist_ok=True)
        os.makedirs(self.final_dir, exist_ok=True)
        self.transitions = ['fade', 'diagbr', 'diagtl']

    def get_duration(self, filepath):
        try:
            probe = ffmpeg.probe(filepath)
            return float(probe['format']['duration'])
        except:
            return 0.0

    def _tail_silence(self, audio_path, duration):
        """How many seconds of silence the file ends on."""
        stderr = (
            ffmpeg.input(audio_path)
            .filter('silencedetect', noise=SILENCE_NOISE, d=0.05)
            .output('-', format='null')
            .run(capture_stderr=True, quiet=True)[1]
            .decode('utf8', errors='replace')
        )
        starts = re.findall(r"silence_start: ([\d.]+)", stderr)
        ends = re.findall(r"silence_end: ([\d.]+)", stderr)
        if not starts or (len(ends) == len(starts) and float(ends[-1]) < duration - 0.05):
            return 0.0
        return max(0.0, duration - float(starts[-1]))

    def _stock_stream(self, path, duration, slowdown=1.0):
        # force_original_aspect_ratio='increase' fills the 9:16 frame and
        # lets the crop trim the overflow; a bare scale would squash any
        # stock clip returned in a different aspect ratio.
        # tpad holds the last frame if the video stream ends a little before the
        # container says, so the scene never runs short and cuts off the voice.
        return (
            ffmpeg.input(path)
            .setpts(f'{slowdown}*PTS')
            .filter('tpad', stop=-1, stop_mode='clone')
            .trim(duration=duration)
            .setpts('PTS-STARTPTS')
            .filter('scale', 1080, 1920, force_original_aspect_ratio='increase')
            .filter('crop', 1080, 1920)
            .filter('setsar', 1)  # Some stock clips have non-square pixels; concat rejects mixed SARs
            .filter('fps', fps=30, round='up')
        )

    def _stock_shots(self, paths, duration):
        """
        Fills duration with the clips, each played once: a shot that starts over is
        plain to see. They share the time evenly, except that a clip ending early
        leaves the rest of its share to the others. Clips too short between them are
        slowed down to fit, and a pair of the same clip is one shot.
        """
        paths = list(dict.fromkeys(paths))
        lengths = [self.get_duration(path) or duration for path in paths]
        slowdown = max(1.0, duration / sum(lengths))

        shares = [0.0] * len(paths)
        left = duration
        for done, i in enumerate(sorted(range(len(paths)), key=lengths.__getitem__)):
            shares[i] = min(lengths[i] * slowdown, left / (len(paths) - done))
            left -= shares[i]

        streams = [self._stock_stream(path, share, slowdown) for path, share in zip(paths, shares)]
        return ffmpeg.concat(*streams, v=1, a=0) if len(streams) > 1 else streams[0]

    def process_scene(self, scene, video_pair, is_avatar=False):
        """
        Combines Audio with Visuals.
        - If Avatar: video_pair is (avatar clip, stock pair or None). Loops the clip
          (+ CROP LOGO on the static avatar), for scene['avatar_seconds'] if that is
          set, then cuts to the stock footage while the voice carries on.
        - If Stock: Split duration between Video A and Video B (see _stock_shots).
        """
        scene_id = scene['id']
        audio_path = scene['audio_path']
        narration = scene['duration']
        output_path = os.path.join(self.temp_dir, f"scene_{scene_id}.mp4")

        try:
            # The next scene's transition overlaps this scene's last TRANSITION
            # seconds. A pause in the narration can be shorter than that, so top it
            # up with silence: the overlap must never reach the last word.
            total_duration = narration + max(0.0, TRANSITION - self._tail_silence(audio_path, narration))
            input_audio = ffmpeg.input(audio_path).filter('apad', whole_dur=total_duration)

            if is_avatar:
                # --- AVATAR MODE (Single Loop + CROP) ---
                is_static = video_pair[0] == self.avatar_path
                print(f"   ⚙️ Processing Scene {scene_id}: 🤖 Avatar Mode ({'Cropped' if is_static else 'Generated'})")

                on_screen = scene.get('avatar_seconds') or narration
                cutaway = video_pair[1] if on_screen < narration else None

                video_stream = (
                    ffmpeg.input(video_pair[0], stream_loop=-1)
                    .trim(duration=on_screen if cutaway else total_duration + 0.5)
                    .setpts('PTS-STARTPTS')
                )

                # ---------------------------------------------------------
                # ✂️ LOGO REMOVAL CROP
                # ---------------------------------------------------------
                # Current setting: Removes 150px from BOTTOM. Only the static
                # avatar carries a logo; generated clips are left whole.
                if is_static:
                    video_stream = video_stream.filter('crop', 'iw', 'ih-150', 0, 0)

                video_stream = (
                    video_stream
                    # ---------------------------------------------------------
                    # 📏 RESIZE & CENTER
                    # ---------------------------------------------------------
                    .filter('scale', 1080, 1920, force_original_aspect_ratio='increase')
                    .filter('crop', 1080, 1920)
                    .filter('setsar', 1)  # Some stock clips have non-square pixels; concat rejects mixed SARs
                    .filter('fps', fps=30, round='up')
                )
                if cutaway:
                    print(f"      ✂️ On screen {on_screen:.1f}s of {total_duration:.1f}s, then stock footage")
                    seconds = total_duration - on_screen + 0.5
                    # visual_2 matches the end of the line, which is what a cutaway
                    # shows. Clip A only comes in if that clip would run out.
                    path_a, path_b = cutaway
                    clips = [path_b] if self.get_duration(path_b) >= seconds else [path_a, path_b]
                    video_stream = ffmpeg.concat(video_stream, self._stock_shots(clips, seconds), v=1, a=0)
            else:
                # --- DUAL VIDEO MODE (A/B Split) ---
                print(f"   ⚙️ Processing Scene {scene_id}: 🎞️ A/B Split Mode")
                video_stream = self._stock_shots(video_pair, total_duration + 0.5)

            # Combine Video + Audio
            runner = ffmpeg.output(
                video_stream, 
                input_audio, 
                output_path, 
                vcodec='libx264', 
                acodec='aac', 
                pix_fmt='yuv420p',
                shortest=None
            )
            
            runner.run(overwrite_output=True, quiet=True)
            return output_path

        except ffmpeg.Error as e:
            print(f"❌ Render Fail Scene {scene_id}: {e.stderr.decode('utf8') if e.stderr else str(e)}")
            return None

    def pick_avatar_indices(self, script_data, count=3):
        """
        Picks the scenes that show the Avatar.
        - count=3 (default): the Hook (0) opens on the Avatar, plus TWO random middle scenes.
        - count=2: TWO random middle scenes only (the original layout).
        - count=0: no Avatar at all.
        Middle scenes come from range [1, len-2] to avoid the Hook (0) and Outro (last).
        """
        # Only inject if we have enough scenes (need at least 4 scenes to safely pick 2 middle ones)
        if count == 0 or len(script_data) < 4:
            return []

        valid_range = list(range(1, len(script_data) - 1)) # All valid middle indices

        # Pick 2 unique indices if possible, otherwise just 1
        count_to_pick = 2 if len(valid_range) >= 2 else 1
        indices = random.sample(valid_range, count_to_pick)
        if count >= 3:
            indices.append(0)
        return sorted(indices)

    def render_all_scenes(self, script_data, video_pairs, avatar_clips=None, avatar_count=3):
        """
        Iterates script, handles Avatar injection logic, and renders individual scenes.
        avatar_clips maps scene index -> clip path (e.g. generated by ComfyUI). When
        omitted, the static avatar is injected into avatar_count picked scenes.
        """
        rendered_paths = []

        # 1. Decide which scenes show the Avatar
        if avatar_clips is None:
            avatar_clips = {}
            if os.path.exists(self.avatar_path):
                avatar_clips = {i: self.avatar_path for i in self.pick_avatar_indices(script_data, avatar_count)}

        if avatar_clips:
            human_readable_indices = [i + 1 for i in sorted(avatar_clips)]
            print(f"🎲 Avatar set for Scenes: {human_readable_indices}")

        # 2. Render Loop
        for i, scene in enumerate(script_data):
            current_pair = video_pairs[i] if i < len(video_pairs) else None
            is_avatar = False

            # Injection Logic: Check if current index is in our chosen list
            if i in avatar_clips:
                current_pair = (avatar_clips[i], current_pair)
                is_avatar = True
            elif current_pair is None:
                continue 

            output_path = self.process_scene(scene, current_pair, is_avatar)
            if output_path:
                rendered_paths.append(output_path)
        
        return rendered_paths

    def concatenate_with_transitions(self, video_paths, output_filename="final_short.mp4"):
        """
        Stitches rendered scenes together.
        INCLUDES FIXES FOR: Windows 0x80004005 Error & Playback Issues.
        """
        print("🎬 Stitching final video...")
        output_path = os.path.join(self.final_dir, output_filename)
        
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except:
                print("⚠️ Warning: Could not delete old file. It might be open in a player.")

        if not video_paths:
            return None

        input1 = ffmpeg.input(video_paths[0])
        v_stream = input1.video
        a_stream = input1.audio
        
        current_dur = self.get_duration(video_paths[0])

        for i in range(1, len(video_paths)):
            next_clip = ffmpeg.input(video_paths[i])
            next_dur = self.get_duration(video_paths[i])
            
            trans_dur = TRANSITION
            offset = current_dur - trans_dur
            
            effect = random.choice(self.transitions)
            print(f"   ✨ Transition {i}: '{effect}' at {offset:.2f}s")

            v_stream = ffmpeg.filter(
                [v_stream, next_clip.video], 
                'xfade', 
                transition=effect, 
                duration=trans_dur, 
                offset=offset
            )
            
            # No fade-in: the next scene's first word plays at full volume over
            # the silence process_scene left at the end of this one.
            a_stream = ffmpeg.filter(
                [a_stream, next_clip.audio],
                'acrossfade',
                d=trans_dur,
                c2='nofade'
            )
            
            current_dur = (current_dur + next_dur) - trans_dur

        # Written under another name until complete: the panel lists every .mp4 in
        # this folder and can upload one while a run is still stitching.
        part_path = output_path + ".part"
        try:
            runner = ffmpeg.output(
                v_stream,
                a_stream,
                part_path,
                format='mp4',
                vcodec='libx264',   # Standard H.264 video
                acodec='aac',       # Standard AAC audio
                pix_fmt='yuv420p',  # 🔥 FIX 1: Windows compatibility
                movflags='faststart', # 🔥 FIX 2: Corruption fix
                preset='medium' 
            )
            
            runner.run(overwrite_output=True, quiet=False)
            os.replace(part_path, output_path)

            print(f"✅ FINAL VIDEO SAVED: {output_path}")
            return output_path

        except ffmpeg.Error as e:
            error_log = e.stderr.decode('utf8') if e.stderr else str(e)
            print(f"❌ Stitching Error: {error_log}")
            if os.path.exists(part_path):
                os.remove(part_path)
            return None