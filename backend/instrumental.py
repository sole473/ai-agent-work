"""Arrange and render short instrumental tracks on the CPU."""

from array import array
from hashlib import sha256
from io import BytesIO
import json
import logging
import os
import shutil
import subprocess
import tempfile
from math import exp, sin, tau
from pathlib import Path
from random import Random
import sys
from typing import Annotated
import wave

import httpx
import pretty_midi
from pydantic import BaseModel, Field, ValidationError


SAMPLE_RATE = 22050

MAJOR = ((0, 4, 7), (2, 5, 9), (4, 7, 11), (5, 9, 12), (7, 11, 14), (9, 12, 16))
MINOR = ((0, 3, 7), (2, 5, 8), (3, 7, 10), (5, 8, 12), (7, 11, 14), (8, 12, 15))
PROGRESSIONS = ((0, 4, 5, 3), (0, 2, 3, 4), (0, 5, 1, 4))
PHRASES = (
    ((0, 0, .8), (1, 1, .6), (2.5, 2, 1.2)),
    ((.5, 2, .5), (1.5, 1, .65), (2.25, 0, 1.4)),
    ((0, 1, .7), (.75, 2, .55), (1.5, 1, .5), (3, 0, .85)),
)

SKILLS_DIR = Path(__file__).resolve().parent / "arrangement_skills"

STYLE_WORDS = {
    "lofi": ("lo-fi", "lofi", "嘻哈", "hip-hop"),
    "folk": ("吉他", "民谣", "guitar", "folk"),
    "piano": ("钢琴", "piano"),
    "ambient": ("空灵", "氛围", "冥想", "ambient", "meditation"),
}

# 每种音色使用独立泛音和包络参数，保持本地合成无需下载音源文件。
INSTRUMENT_PROFILES = {
    "piano": {"harmonics": (1.0, 0.48, 0.20, 0.08), "decay": 1.0, "sustain": 1.0, "attack": .012, "pluck": .08},
    "guzheng": {"harmonics": (1.0, 0.34, 0.16, 0.10), "decay": 2.1, "sustain": 0.72, "attack": .004, "pluck": .16},
    "music_box": {"harmonics": (0.75, 0.12, 0.62, 0.32), "decay": 2.7, "sustain": 0.58, "attack": .003, "pluck": .04},
    "guitar": {"harmonics": (1.0, 0.40, 0.18, 0.06), "decay": 1.65, "sustain": 0.82, "attack": .006, "pluck": .13},
    "synth_pad": {"harmonics": (1.0, 0.30, 0.14, 0.08), "decay": 0.42, "sustain": 1.65, "attack": .16, "pluck": .01},
}

Pitch = Annotated[int, Field(ge=-12, le=36)]
Onset = Annotated[float, Field(ge=0, lt=4)]
Length = Annotated[float, Field(gt=0, le=4)]


class Measure(BaseModel):
    chord: list[Pitch] = Field(min_length=2, max_length=4)
    melody: list[tuple[Onset, Pitch, Length]] = Field(max_length=24)
    rhythm: list[Onset] = Field(max_length=16)
    active_instruments: list[str] = Field(default_factory=list)
    parts: dict[str, list[tuple[Onset, Pitch, Length]]] = Field(default_factory=dict)
    energy: float = Field(ge=0.25, le=1.25)


class Arrangement(BaseModel):
    measures: list[Measure] = Field(min_length=2, max_length=90)


def plan_arrangement(api_key: str, theme: str, style: str, duration: int, skill: str, model: str,
                     instruments: list[str], focus_instrument: str, support_level: str) -> Arrangement:
    instructions = """Arrange instrumental music in 4/4. Return only a JSON object like
{"measures":[{"chord":[0,4,7,11],"melody":[[0,7,1]],"rhythm":[0,2.5],"energy":0.8,"active_instruments":["piano","violin"],"parts":{"piano":[[0,7,1]],"violin":[[2,12,1]]}}]}.
The example shows one bar; return a complete multi-bar arrangement. All pitches are
semitones above a shared tonic, -12 to 36. Put the chord root first. Chords may use
2 to 4 distinct notes, including sevenths, ninths and suspended tones. Use register
changes, inversions encoded as octave-shifted chord tones, syncopation, rests, and
motif variations across sections; avoid repeating the same bar. Keep bass notes in
the lower register and melody generally above the chord. Each bar can contain up to
24 melody notes and 16 accompaniment onsets; use them musically. Melody triples are
[onset in beats, pitch, length in beats]. Rhythm lists accompaniment onsets.
Onsets must be in [0,4); note lengths in (0,4]. Energy ranges from 0.25 to 1.25.
Foreground instrument: {focus_instrument}. Supporting level: {support_level}; soft
means sparse and quiet support, balanced means moderate accompaniment, and full means
a denser ensemble. Keep the foreground clearly dominant. Use silence, contrast,
motif variation and a clear ending. Make the musical choices
For every bar, choose active_instruments from the user's available instrument list;
write independent instrument-specific parts in parts. Arrange sections freely:
solo passages, changing lead instruments, call-and-response, overlapping duets,
and full ensemble moments. Do not rotate instruments mechanically or keep the same
combination throughout. Available instruments: {instruments}. Make parts musically
distinct and playable, with rests and clear entrances/exits. Make choices reflect
the user's description. Choose a bar count for the requested duration, using
bars ≈ duration * BPM / 240. Rendered BPM is derived from the returned bar count."""
    with httpx.Client(timeout=httpx.Timeout(75, connect=10)) as client:
        response = client.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
                "max_tokens": 8192,
                "messages": [
                    {"role": "system", "content": instructions.replace("{instruments}", ", ".join(instruments)).replace("{focus_instrument}", focus_instrument).replace("{support_level}", support_level) + f"\n\nStyle reference:\n{skill}"},
                    {"role": "user", "content": json.dumps({
                        "theme": theme, "style": style, "duration_seconds": duration,
                    }, ensure_ascii=False)},
                ],
            },
        )
        response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    plan = Arrangement.model_validate_json(content)
    if not 40 <= len(plan.measures) * 240 / duration <= 180:
        raise ValueError("Arrangement tempo is out of range.")
    if not any(bar.melody or bar.rhythm for bar in plan.measures):
        raise ValueError("Arrangement contains no notes.")
    return plan


def render_instrumental(theme: str, style: str, duration: int, instrument: str = "piano", *, api_key: str | None = None,
                        job_id: str | None = None, arrangement_model: str = "local",
                        focus_instrument: str = "piano", support_level: str = "balanced") -> tuple[bytes, str]:
    # API 输入已限制选项。保留默认值以兼容旧任务和直接函数调用。
    instruments = instrument if isinstance(instrument, list) else [instrument]
    instrument = instruments[0] if instruments else "piano"
    if focus_instrument not in instruments:
        focus_instrument = instrument
    instrument_profile = INSTRUMENT_PROFILES.get(instrument, INSTRUMENT_PROFILES["piano"])
    seed = sha256(f"{theme}\n{style}\n{instruments}".encode()).digest()
    rng = Random(seed)
    style = style.lower()
    description = f"{theme.lower()} {style}"
    preset = next((
        name for source in (style, theme.lower())
        for name, words in STYLE_WORDS.items() if any(word in source for word in words)
    ), "piano")
    skill = (SKILLS_DIR / preset / "SKILL.md").read_text(encoding="utf-8")
    profile = json.loads(skill.split("```json", 1)[1].split("```", 1)[0])
    bpm = profile["bpm"]
    rhythm = profile["rhythm"]
    register = profile["register"]
    brightness = profile["brightness"]
    decay = profile["decay"]
    quiet = any(word in description for word in ("雨", "夜", "舒缓", "忧伤", "sad", "rain", "calm"))
    energetic = any(word in description for word in ("热烈", "欢快", "追逐", "激昂", "upbeat", "energetic"))
    airy = any(word in description for word in ("空灵", "梦幻", "dreamy", "airy"))
    bpm += 24 if energetic else -10 if quiet else 0
    measures = max(3, round(duration * bpm / 240))
    plan = None
    if arrangement_model != "local":
        if not api_key:
            raise ValueError("所选模型编曲需要配置 DEEPSEEK_API_KEY。")
        try:
            plan = plan_arrangement(api_key, theme, style, duration, skill, arrangement_model, instruments,
                                    focus_instrument, support_level)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            validation_errors = None
            if isinstance(exc, ValidationError):
                # 只记录字段位置、错误类型和说明，不写入 DeepSeek 的完整回复。
                validation_errors = [
                    {
                        "field": ".".join(str(part) for part in error["loc"]),
                        "type": error["type"],
                        "message": error["msg"],
                    }
                    for error in exc.errors()
                ][:5]
            logging.getLogger(__name__).warning(
                "arrangement_fallback job_id=%s reason=%s provider_status=%s validation_errors=%s",
                job_id, type(exc).__name__, status, validation_errors,
            )
            raise ValueError(f"模型编曲失败（{type(exc).__name__}），未使用本地规则回退。") from exc
    if plan:
        measures = len(plan.measures)
    beat = duration / (4 * measures)
    key = 48 + rng.randrange(7)
    chords = MINOR if quiet and not energetic else MAJOR
    progression = rng.choice(PROGRESSIONS)
    phrase_offset = rng.randrange(len(PHRASES))
    # 留出轻微的左右声像变化和延迟空间，避免所有声部挤在同一中心点。
    samples = array("f", [0.0]) * (duration * SAMPLE_RATE)
    right_samples = array("f", [0.0]) * (duration * SAMPLE_RATE)
    midi = pretty_midi.PrettyMIDI(initial_tempo=60 / beat)
    programs = {"piano": 0, "guzheng": 104, "music_box": 10, "guitar": 24, "synth_pad": 89,
                "violin": 40, "strings": 48, "flute": 73, "bass": 33, "drums": 0,
                "organ": 16, "harp": 46, "trumpet": 56, "saxophone": 65, "choir": 52}
    midi_instruments = [pretty_midi.Instrument(
        program=programs.get(name, 0), name=name, is_drum=(name == "drums"),
    ) for name in instruments]

    def add_note(pitch, start, length, volume, brightness, decay, pan=0.0, voice=0):
        # 写入 MIDI 音符，实际音色由 SoundFont 采样演奏。
        if shutil.which("fluidsynth"):
            note = pretty_midi.Note(
                velocity=max(1, min(127, round(volume * 127 * 2.7))),
                pitch=max(0, min(127, int(round(pitch)))),
                start=max(0, start), end=max(0.01, start + length),
            )
            midi_instruments[voice % len(midi_instruments)].notes.append(note)
            return
        offset = round(start * SAMPLE_RATE)
        length_frames = max(1, round(length * SAMPLE_RATE))
        count = min(length_frames, len(samples) - offset)
        phase_step = tau * 440 * 2 ** ((pitch - 69) / 12) / SAMPLE_RATE
        for index in range(count):
            phase = phase_step * index
            progress = index / length_frames
            attack_frames = max(1, round(instrument_profile["attack"] * SAMPLE_RATE))
            envelope = min(1, index / attack_frames, (length_frames - index) / 900) * exp(-decay * progress)
            overtone = brightness * (1 - progress)
            tone = sum(
                weight * sin((harmonic + 1) * phase)
                for harmonic, weight in enumerate(instrument_profile["harmonics"])
            )
            # 轻微非谐波泛音、拨弦瞬态和音高下沉，避免静态正弦叠加的合成器感。
            tone += instrument_profile["pluck"] * exp(-index / 95) * sin(phase * 2.73)
            tone += overtone * .10 * sin(phase * 5.01)
            phase -= phase_step * .012 * exp(-index / 260)
            value = volume * envelope * tone
            samples[offset + index] += value * (1 - max(0, pan))
            right_samples[offset + index] += value * (1 + min(0, pan))

    intro = max(1, measures // 5)
    outro = max(1, measures // 6)
    for measure in range(measures):
        start = measure * 4
        closing = measure >= measures - outro
        developed = intro <= measure < measures - outro
        chord_index = progression[measure % len(progression)]
        if measure == measures - 1:
            chord_index = 0
        elif measure == measures - 2 and measures > 3:
            chord_index = 4
        chord = chords[chord_index]
        bar = plan.measures[measure] if plan else None
        energy = bar.energy if bar else 1
        if bar:
            chord = bar.chord
        active_names = [name for name in bar.active_instruments if name in instruments] if bar and bar.active_instruments else instruments
        active_voices = [instruments.index(name) for name in active_names] or [0]
        if (developed or closing) and profile["bass"]:
            bass_voice = next((i for i, name in enumerate(instruments) if name == "bass"), min(1, len(instruments) - 1))
            add_note(key + chord[0] - 12, start * beat, 2.8 * beat * instrument_profile["sustain"], .18 * energy, .13,
                     1.5 * instrument_profile["decay"], voice=bass_voice)
        onsets = bar.rhythm if bar else rhythm if developed else rhythm[:2]
        for step, onset in enumerate(onsets):
            index = (0, 1, 2, 1)[step % 4] % len(chord)
            voicing = (chord[index],) if profile["arpeggio"] else chord
            gain = (.115 if developed else .085) * rng.uniform(.82, 1.12) * energy
            for chord_tone, interval in enumerate(voicing):
                pan = ((index % 3) - 1) * .16
                for voice in active_voices:
                    voice_name = instruments[voice]
                    octave = -12 if voice_name == "bass" else 12 if voice_name in ("violin", "flute", "trumpet", "saxophone") else 0
                    add_note(key + interval + octave, (start + onset) * beat,
                             beat * profile["sustain"] * instrument_profile["sustain"],
                             gain / (len(voicing) * len(active_voices)), brightness,
                             decay * instrument_profile["decay"], pan, voice)
        phrase = PHRASES[(phrase_offset + measure) % len(PHRASES)]
        if profile["sparse_melody"]:
            phrase = ((1, 2, 2.8),)
        if closing and measure == measures - 1:
            phrase = ((.25, 1, .7), (1.25, 0, 2.5))
        if bar and bar.parts:
            parts = {name: notes for name, notes in bar.parts.items() if name in active_names}
        else:
            parts = {active_names[0]: bar.melody if bar else [(onset, chord[degree], length) for onset, degree, length in phrase]}
        for part_name, part_notes in parts.items():
            voice = instruments.index(part_name)
            part_register = register + (12 if part_name in ("violin", "flute", "trumpet", "saxophone") else -12 if part_name == "bass" else 0)
            for onset, interval, length in part_notes:
                pitch = key + interval + part_register + (12 if airy else 0)
                prominence = 1.0 if part_name == focus_instrument else {"soft": .38, "balanced": .62, "full": .82}[support_level]
                gain = (.24 if developed else .18) * rng.uniform(.86, 1.09) * energy * prominence
                if energetic and developed:
                    gain *= 1.2
                add_note(pitch, (start + onset) * beat, length * beat * instrument_profile["sustain"], gain,
                         brightness, decay * instrument_profile["decay"], .12, voice)
        # 加入低音脉冲和稀疏高音点缀，增强段落重心与纵向层次。
        if developed and measure % 2 == 1:
            ornament_voice = (measure + 2) % len(instruments)
            add_note(key + chord[0] + 19, (start + 3) * beat, .55 * beat, .045 * energy,
                     brightness * .7, decay * 1.25, -.22, ornament_voice)
        if profile["drums"] and developed:
            for onset in (0, 2.5):
                add_note(31, (start + onset) * beat, .19, .21, .1, 4)
        if "drums" in instruments and (not bar or not bar.active_instruments or "drums" in bar.active_instruments):
            drum_voice = instruments.index("drums")
            # GM 打击乐：底鼓、军鼓和踩镲组成轻量节奏型。
            for onset, drum_pitch, level in (
                (0, 36, .72), (1, 42, .28), (2, 38, .52), (3, 42, .30),
                (2.5, 36, .38), (3.5, 42, .24),
            ):
                if not closing and not developed and onset not in (0, 2):
                    continue
                add_note(drum_pitch, (start + onset) * beat, .12 * beat, level * energy,
                         .1, 4, voice=drum_voice)

    if shutil.which("fluidsynth"):
        midi.instruments.extend(midi_instruments)
        soundfont = next((candidate for candidate in (
            "/usr/share/sounds/sf2/FluidR3_GM.sf2",
            "/usr/share/soundfonts/FluidR3_GM.sf2",
            "/usr/share/sounds/sf2/default-GM.sf2",
        ) if Path(candidate).is_file()), None)
        if soundfont:
            with tempfile.TemporaryDirectory() as temp_dir:
                midi_path = Path(temp_dir) / "arrangement.mid"
                wav_path = Path(temp_dir) / "render.wav"
                midi.write(str(midi_path))
                subprocess.run([
                    "fluidsynth", "-ni", soundfont, str(midi_path), "-F", str(wav_path),
                    "-r", str(SAMPLE_RATE), "-g", "0.8",
                ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
                if wav_path.is_file():
                    return wav_path.read_bytes(), "deepseek" if plan else "local"

    fade = SAMPLE_RATE // 2
    pcm = array("h", (
        round(max(-1, min(1, (sample + right_samples[index] * .82) * .5)) * 32767 * min(1, (len(samples) - index - 1) / fade))
        for index, sample in enumerate(samples)
    ))
    if sys.byteorder == "big":
        pcm.byteswap()
    buffer = BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        output.writeframes(pcm.tobytes())
    return buffer.getvalue(), "deepseek" if plan else "local"
