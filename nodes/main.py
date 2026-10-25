"""promboard.omnivoice — OmniVoice(k2-fsa) 목소리 노드 코드.

노드 하나(omnivoice)가 글을 읽어 목소리 WAV(24kHz 모노)를 낸다. 목소리를 정하는 길은 셋이다(공식 README 「Python API」).
  1) 복제 — '참조 목소리'를 이으면 그 목소리로 읽는다. 참조 소리의 대사('참조 글')를 같이 주는 것이 가장 정확하다.
     대사를 비우면 '참조 글 받아쓰기' 설정이 Whisper 일 때만 Whisper large-v3-turbo 로 받아 적는다(공식 코드의 자동 받아쓰기).
  2) 목소리 만들기 — 참조 목소리 없이 성별 · 나이 · 음높이 · 속삭임 · 영어 억양을 하나 이상 고르면 그 속성으로 만든다
     (공식 instruct, omnivoice/utils/voice_design.py 의 영어 속성 이름 그대로 넘긴다).
  3) 자동 — 둘 다 없으면 모델이 목소리를 고른다.
설정(모두 공식 generate() 인자): language · speed · duration · num_step.

모델(manifest requires.models)
  k2-fsa/OmniVoice                 본체(Qwen3 기반 0.6B) + audio_tokenizer(Higgs Audio v2 토크나이저) — 3.27GB
  openai/whisper-large-v3-turbo    '참조 글 받아쓰기' = Whisper 를 고른 때만 받는다 — 1.62GB
장치: cuda 는 float16(공식 예제), mps · cpu 는 float32. mps 에서는 audio_tokenizer 가 CPU 에 놓인다(공식 코드).
노드 코드 약속(NODES · ctx · 입출력 모양)은 PromBoard docs/Scrum4/02_스키마-v0.md §6.
"""
import math
import os
import time

os.environ.setdefault("HF_HUB_OFFLINE", "1")  # 받은 파일만 쓴다(실행 환경은 인터넷이 막혀 있다)
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

MODEL_FILE = "k2-fsa/OmniVoice/config.json"
WHISPER_FILE = "openai/whisper-large-v3-turbo/config.json"
MAX_TEXT_CHARS = 5000  # 한 번에 읽을 글 상한(대략 10분 남짓) — 실행 한 번 시간 예산 안에 끝나게
MAX_REF_SEC = 60.0  # 참조 소리 상한. 공식 권장은 3~10초, 20초를 넘으면 공식 코드가 경고한다
QUALITY_STEPS = {"fast": 16, "normal": 32}  # 공식 README: num_step 32(기본) 또는 16(빠르게)

_STATE = {}


class Progress:
    """ctx.progress 를 [lo, hi] 구간에 맞춰 부른다. 뒤로 가지 않는다."""

    def __init__(self, ctx, lo, hi):
        self.ctx, self.lo, self.hi, self.last = ctx, lo, hi, lo

    def __call__(self, fraction, stage="work"):
        value = max(self.last, self.lo + (self.hi - self.lo) * max(0.0, min(1.0, fraction)))
        self.last = value
        self.ctx.progress(value, stage)


def _dtype(device):
    import torch

    return torch.float16 if device == "cuda" else torch.float32


def _model(ctx):
    """OmniVoice 를 한 번 올려 사이드카가 떠 있는 동안 다시 쓴다."""
    device = ctx.device
    key = ("omnivoice", device)
    if key in _STATE:
        return _STATE[key]
    ctx.progress(0.02, "load-model")  # 올리는 동안 파이썬이 오래 멈춘다 — 바로 앞에 알린다
    from omnivoice import OmniVoice

    start = time.monotonic()
    path = os.path.dirname(ctx.model_path(MODEL_FILE))
    model = OmniVoice.from_pretrained(path, device_map=device, dtype=_dtype(device))
    _STATE.clear()  # 다른 장치로 올린 옛 모델은 버린다
    _STATE[key] = model
    ctx.log(f"OmniVoice loaded on {device} in {time.monotonic() - start:.1f}s")
    return model


def _load_asr(ctx, model):
    if model._asr_pipe is not None:
        return
    ctx.progress(0.1, "load-model")
    path = os.path.dirname(ctx.model_path(WHISPER_FILE))
    model.load_asr_model(model_name=path, device=ctx.device)


def _read_audio(path):
    """참조 소리 → (float32 (1, T) 모노, 샘플레이트). wav · flac · ogg · mp3 는 soundfile, 그 밖(m4a · aac · webm · opus)은 PyAV."""
    import numpy as np

    try:
        import soundfile as sf

        data, sr = sf.read(path, dtype="float32", always_2d=True)
        return data.mean(axis=1, dtype=np.float32)[None, :], int(sr)
    except Exception:
        pass
    import av

    try:
        with av.open(path) as container:
            if not container.streams.audio:
                raise ValueError("참조 목소리 파일에 소리가 없습니다.")
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="flt", layout="mono")
            chunks = []
            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1))
            for out in resampler.resample(None):
                chunks.append(out.to_ndarray().reshape(-1))
            sr = stream.rate
    except av.error.FFmpegError as exc:
        raise ValueError(f"참조 목소리 파일을 읽지 못했습니다. wav · mp3 · flac · ogg · m4a 소리인지 확인해 주세요. ({exc})") from None
    if not chunks:
        raise ValueError("참조 목소리 파일에서 소리를 읽지 못했습니다.")
    return np.concatenate(chunks).astype(np.float32)[None, :], int(sr)


def _instruct(settings):
    """목소리 만들기 속성 → 공식 instruct 글(영어 속성 이름을 쉼표로). 하나도 안 골랐으면 None."""
    parts = [settings[k] for k in ("gender", "age", "pitch") if settings[k] != "auto"]
    if settings["whisper"]:
        parts.append("whisper")
    if settings["accent"] != "none":
        parts.append(settings["accent"])
    return ", ".join(parts) or None


def _expected_steps(model, text, kwargs):
    """진행률 분모 — 단계 수 × 조각 수. 조각 수는 공식 코드처럼 어림한 길이가 30초를 넘으면 15초씩으로 셈한다(audio_chunk_*)."""
    steps = kwargs["num_step"]
    try:
        prompt = kwargs.get("voice_clone_prompt")
        rate = model.audio_tokenizer.config.frame_rate
        if "duration" in kwargs:
            frames = kwargs["duration"] * rate
        else:
            frames = model._estimate_target_tokens(
                text,
                prompt.ref_text if prompt else None,
                prompt.ref_audio_tokens.shape[-1] if prompt else None,
                kwargs.get("speed", 1.0),
            )
        chunks = 1 if frames <= 30 * rate else math.ceil(frames / (15 * rate))
    except Exception:
        chunks = 1
    return steps * chunks


def _out_of_memory(exc):
    import torch

    if isinstance(exc, torch.cuda.OutOfMemoryError) or "out of memory" in str(exc).lower():
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        return RuntimeError(
            "그래픽카드 메모리가 모자랍니다. 다른 AI 작업 · 프로그램을 닫고 다시 하거나, 글을 나눠 짧게 읽혀 주세요."
        )
    return None


def omnivoice(ctx, inputs, settings):
    import numpy as np
    import soundfile as sf
    import torch

    text = (inputs.get("text") or "").strip()
    if not text:
        raise ValueError("읽을 글을 이어 주세요.")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"글이 너무 깁니다({len(text)}자). 한 번에 {MAX_TEXT_CHARS}자까지 읽습니다 — 나눠서 이어 주세요.")
    ref_path = inputs.get("ref-audio")
    ref_text = (inputs.get("ref-text") or "").strip() or None

    kwargs = {"num_step": QUALITY_STEPS[settings["quality"]]}
    if settings["language"] != "auto":
        kwargs["language"] = settings["language"]
    if settings["duration"] > 0:
        kwargs["duration"] = float(settings["duration"])
    elif abs(settings["speed"] - 1.0) > 1e-6:
        kwargs["speed"] = float(settings["speed"])

    model = _model(ctx)
    ctx.check_cancel()
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()

    mode = "auto"
    if ref_path:
        mode = "clone"
        wav, sr = _read_audio(ref_path)
        seconds = wav.shape[-1] / sr
        if seconds > MAX_REF_SEC:
            raise ValueError(f"참조 목소리가 너무 깁니다({seconds:.0f}초). 3~10초가 가장 좋고, {MAX_REF_SEC:.0f}초까지 받습니다.")
        if seconds < 1.0:
            raise ValueError("참조 목소리가 너무 짧습니다. 3~10초 길이의 말소리를 이어 주세요.")
        if ref_text is None:
            if settings["transcribe"] != "whisper":
                raise ValueError(
                    "참조 목소리에서 말한 글을 '참조 글'에 이어 주세요. 직접 적기 어려우면 설정 '참조 글 받아쓰기'를 Whisper 로 바꿉니다."
                )
            _load_asr(ctx, model)
        ctx.progress(0.15, "prepare")
        try:
            prompt = model.create_voice_clone_prompt(ref_audio=(torch.from_numpy(wav), sr), ref_text=ref_text)
        except Exception as exc:
            raise (_out_of_memory(exc) or exc) from None
        if ref_text is None:
            ctx.log(f"reference transcribed: {len(prompt.ref_text)} chars")
        kwargs["voice_clone_prompt"] = prompt
    else:
        instruct = _instruct(settings)
        if instruct:
            mode = "design"
            kwargs["instruct"] = instruct
    ctx.check_cancel()

    # 디퓨전 한 단계 = 모델 forward 한 번. 그 앞에서 취소를 보고 진행률을 알린다(긴 글은 15초 조각마다 단계가 다시 돈다).
    progress = Progress(ctx, 0.2, 0.95)
    expected = _expected_steps(model, text, kwargs)
    calls = [0]

    def before_step(_module, _args):
        ctx.check_cancel()
        calls[0] += 1
        progress(calls[0] / expected, "generate")

    hook = model.register_forward_pre_hook(before_step)
    start = time.monotonic()
    try:
        audio = model.generate(text=text, **kwargs)[0]
    except Exception as exc:
        raise (_out_of_memory(exc) or exc) from None
    finally:
        hook.remove()
    took = time.monotonic() - start

    ctx.progress(0.97, "save")
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        raise RuntimeError("목소리가 만들어지지 않았습니다. 글을 바꿔 다시 해 주세요.")
    out = ctx.out_path("omnivoice.wav")
    sf.write(out, audio, model.sampling_rate, subtype="PCM_16")
    peak = torch.cuda.max_memory_reserved() / 2**20 if torch.cuda.is_available() else 0
    ctx.log(
        f"mode {mode}, {len(text)} chars → {audio.size / model.sampling_rate:.1f}s audio in {took:.1f}s "
        f"({calls[0]} steps), peak VRAM reserved {peak:.0f} MiB"
    )
    ctx.progress(1.0, "done")
    return {"audio": out}


NODES = {"omnivoice": omnivoice}
