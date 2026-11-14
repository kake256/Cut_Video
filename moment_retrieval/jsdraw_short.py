"""JS描画ショート：配信の一区間から、Canvas で絵を描く縦型ショートを Claude API で自動生成する。

流れ（1本あたりの実測：Fable 約0.5ドル、Sonnet 約0.1ドル。Batch API の半額込み）
1. 素材の用意（無料）：区間の音声、顔カメラの切り出しと背景抜き（RobustVideoMatting）、文字起こし、顔の見本画像
2. 生成（Claude API・Batch）：選んだモデルが scene.html（SEGS＝使う音声区間、draw(t, s, tEnd)）を書く。effort は medium
3. プレビュー（無料）：ブラウザで9コマ書き出す
4. 直し（Claude API・Batch）：Sonnet にコードとプレビューだけを送り、直す箇所だけを差分で返させる
5. 書き出し（無料）：全コマを描いて mp4 にし、音声区間をつなぐ。切れ目は近くの無音（±0.6秒）へ寄せる

APIキーは「設定」タブで保存した Anthropic のキー（api_keys）を使う。キーはログにも作業フォルダにも書かない。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable

import numpy as np

from . import api_keys, config, db

# 選べるモデル：表示名 → (モデルID, 入力$/1M, 出力$/1M)
MODELS = {
    "fable": ("claude-fable-5-2", 8.0, 40.0),
    "opus": ("claude-opus-5-5", 4.0, 20.0),
    "sonnet": ("claude-sonnet-5-5", 2.0, 10.0),
}
MODEL_LABELS = {
    "fable": "品質重視：Fable 5.2（1本 約0.5ドル）",
    "opus": "バランス：Opus 5.5（1本 約0.25ドル）",
    "sonnet": "量産向け：Sonnet 5.5（1本 約0.1ドル）",
}
DEFAULT_MODEL = "fable"
FIXER_MODEL = "claude-sonnet-5-5"
EFFORT = "medium"
FPS = 30
TAIL = 1.5
# 書き出す動画は 50〜58 秒。最後に TAIL の余韻が付くので、音声区間（SEGS）の合計は 48.5〜56.5 秒に収める。
VIDEO_MIN_SEC = 50.0
VIDEO_MAX_SEC = 58.0
SEGS_MAX_SEC = VIDEO_MAX_SEC - TAIL
SEGS_MIN_SEC = VIDEO_MIN_SEC - TAIL
SEGS_TARGET_SEC = 55.0
FACE_CROP = (440, 320, 1480, 760)  # w, h, x, y（1920x1080 の配信画面の右下の顔カメラ）
RVM_URL = "https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3_fp32.onnx"
REFERENCE_HTML = Path(__file__).resolve().parent / "jsdraw_reference.html"

SYSTEM = ("あなたは配信の切り抜きショート動画を作る映像作家兼フロントエンドエンジニアです。"
          "絵はすべて JavaScript の Canvas 2D で描き、1つの完結した scene.html を書きます。"
          "考えるのは短めにし、早めに書き始めてください。")

Progress = Callable[[str], None]


class JsDrawError(RuntimeError):
    pass


# ---------------------------------------------------------------- 素材の用意

def _ffmpeg(*args: str) -> None:
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode:
        raise JsDrawError("ffmpeg に失敗しました: " + r.stderr.strip()[-400:])


def _rvm_model() -> Path:
    path = Path(config.DATA_DIR) / "models" / "rvm_mobilenetv3_fp32.onnx"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        # Windows Python's urllib has no CA bundle of its own; use certifi's (installed with requests).
        import ssl

        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(RVM_URL, context=context, timeout=120) as response, open(tmp, "wb") as fh:
            shutil.copyfileobj(response, fh)
        tmp.replace(path)
    return path


def _matte_faces(face_mp4: Path, out_dir: Path, size: tuple[int, int], progress: Progress) -> int:
    """顔カメラの背景を抜き、1コマずつ RGBA の webp にする（00001.webp から）。"""
    import onnxruntime as ort
    from PIL import Image

    w, h = size
    sess = ort.InferenceSession(str(_rvm_model()), providers=["CPUExecutionProvider"])
    rec = [np.zeros([1, 1, 1, 1], np.float32)] * 4
    ratio = np.array([0.6], np.float32)
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(face_mp4), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                            stdout=subprocess.PIPE)
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    try:
        while True:
            buf = proc.stdout.read(w * h * 3)
            if len(buf) < w * h * 3:
                break
            frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            src = (frame.astype(np.float32) / 255).transpose(2, 0, 1)[None]
            _fgr, pha, *rec = sess.run(None, {"src": src, "r1i": rec[0], "r2i": rec[1], "r3i": rec[2],
                                              "r4i": rec[3], "downsample_ratio": ratio})
            alpha = (pha[0, 0] * 255).clip(0, 255).astype(np.uint8)
            n += 1
            Image.fromarray(np.dstack([frame, alpha]), "RGBA").save(out_dir / f"{n:05d}.webp", quality=88)
            if n % 300 == 0:
                progress(f"顔カメラの背景を抜いています（{n}コマ）")
    finally:
        proc.stdout.close()
        proc.wait()
    return n


def _transcript(video_path: str, start: float, end: float, video_id: str = "") -> list[list]:
    conn = db.get_conn()
    try:
        video = db.get_video(conn, video_id) if video_id else db.find_video_by_path(conn, str(video_path))
        if not video:
            raise JsDrawError("この動画は文字起こしがまだありません（先にインデックスを作ってください）。")
        rows = db.get_segments_in_range(conn, video["video_id"], start, end)
    finally:
        conn.close()
    if not rows:
        raise JsDrawError("この区間に文字起こしがありません。")
    return [[round(r["start_sec"], 2), round(r["end_sec"], 2), r["text"]] for r in rows]


def prepare(video_path: str, start: float, end: float, workdir: Path, progress: Progress,
            crop: tuple[int, int, int, int] = FACE_CROP, *, video_id: str = "", media_offset: float = 0.0) -> dict:
    """区間 [start, end]（元配信の秒）の素材を workdir に用意する。

    ``video_path`` が元配信の一部分だけのファイルのときは、その 0 秒が元配信の ``media_offset`` 秒。
    文字起こしは ``video_id``（ライブラリの動画）から読む。
    """
    if end - start < 50:
        raise JsDrawError("区間は50秒以上にしてください（その中から約55秒に詰めます）。")
    if end - start > 300:
        raise JsDrawError("区間は5分以内にしてください。")
    workdir.mkdir(parents=True, exist_ok=True)
    off = max(0.0, start - 8.0)
    dur = end - off + 8.0
    w, h, x, y = crop
    transcript = _transcript(video_path, start, end, video_id)
    seek = max(0.0, off - media_offset)
    (workdir / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
    progress("音声と顔カメラを切り出しています")
    _ffmpeg("-ss", f"{seek:.3f}", "-t", f"{dur:.3f}", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "48000",
            str(workdir / "src.wav"))
    _ffmpeg("-ss", f"{seek:.3f}", "-t", f"{dur:.3f}", "-i", str(video_path), "-an",
            "-vf", f"crop={w}:{h}:{x}:{y},fps={FPS}", "-c:v", "libx264", "-crf", "12", "-preset", "fast",
            str(workdir / "face.mp4"))
    thumbs = workdir / "thumbs"
    thumbs.mkdir(exist_ok=True)
    for s in range(int(start // 10 * 10 + 10), int(end), 10):
        _ffmpeg("-ss", f"{s - off:.3f}", "-i", str(workdir / "face.mp4"), "-frames:v", "1", "-q:v", "5",
                str(thumbs / f"s{s}.jpg"))
    # 10ms ごとの音量（dB）：音声の切れ目を無音へ寄せるのに使う
    import wave
    with wave.open(str(workdir / "src.wav")) as wf:
        sr = wf.getframerate()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(np.float64)
    hop = sr // 100
    n = len(pcm) // hop
    env = 20 * np.log10(np.sqrt((pcm[: n * hop].reshape(n, hop) ** 2).mean(1)) + 1)
    (workdir / "env.json").write_text(json.dumps([round(float(v), 1) for v in env]), encoding="utf-8")
    nface = _matte_faces(workdir / "face.mp4", workdir / "face", (w, h), progress)
    meta = {"video": str(video_path), "start": start, "end": end, "off": off, "nface": nface, "crop": crop}
    (workdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


# ---------------------------------------------------------------- プロンプト

def _rules(lo: float, hi: float) -> str:
    return f"""## ハーネスとの約束（必ず守る）
- `<canvas width="1080" height="1920">` を1つ置き、`window.SEGS` と `function draw(t, s, tEnd)` をグローバルに定義する。
- `window.SEGS`：使う音声の区間（元配信の秒）のリスト。ハーネスが順につないで音声にする。合計50〜56秒（最後に1.5秒の余韻が付き、動画は58秒以内）、区間は3〜5個まで。
  区間は{lo:.0f}〜{hi:.0f}秒の中から。切れ目は文字起こしの区間の境目（発話の切れ目）に合わせる。文の途中で切らない。
  ハーネスが切れ目を近くの無音へ最大0.6秒ずらすので、字幕は SEGS ではなく s（元配信の秒）で出し入れする。
- `draw(t, s, tEnd)`：t=出力動画の秒、s=いま流れている音声の元配信の秒、tEnd=動画の長さ。毎フレーム全面を描き直す（t と s だけで決まる）。
- 顔カメラ：毎フレーム `window.FACE`（いまの瞬間の顔カメラ、背景透過済みの Image）が入る。人物は画像の中央〜右下寄り。口の動きと声が合う。
- フォントは "IPAGothic","IPAPGothic","Yu Gothic","Meiryo",sans-serif の順で指定する。外部ファイル・ネットワークは使わない。
- 画面上部220pxと下部320pxはSNSのUIがかぶるので文字を置かない。"""


def _spec(title: str, story: str, lo: float, hi: float) -> str:
    about = f"題材：「{title}」。{story}" if title else "題材：文字起こしを読み、一番おもしろい流れを自分で選ぶ。"
    return f"""## 作るもの
配信の一区間から、縦型ショート（1080x1920、50〜58秒）を1本。
{about}
下に元配信の文字起こし（音声認識、元配信の秒）と、顔カメラの数枚を付けます。

{_rules(lo, hi)}
- 字幕：SUBS を自分で作る。文字起こしの区間の時刻に合わせ、長い区間は文字数の位置で比例配分。1枚14文字前後。音声認識の誤りは文脈で直してよい。

## 期待する絵
参考に別の話で作った scene.html を付けます（絵柄と完成度の目安。場面をそのまま流用しない）。
話の流れに合わせて場面を5〜7つに分け、「動く図解」で見せる。冒頭2秒でつかむ見出しを入れる。コードは簡潔に（全体で600行以内）。

## 出力
説明は1行だけ。そのあと ```html で始まるコードブロックに scene.html 全体を書く。"""


def _img(path: Path) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(path.read_bytes()).decode()}}


def _text(message) -> str:
    return "".join(b.text for b in message.content if b.type == "text")


# ---------------------------------------------------------------- Claude API（Batch）

def _client():
    import anthropic

    key, _source = api_keys.resolve("anthropic")
    if not key:
        raise JsDrawError("AnthropicのAPIキーがありません。「設定」タブの「APIキーで使う」で保存してください。")
    return anthropic.Anthropic(api_key=key)


def _batch(client, model: str, messages: list, progress: Progress, label: str) -> tuple[object, dict]:
    """1件だけの Batch を投げて、終わるまで待つ（半額。ふつう数分）。"""
    params = {"model": model, "max_tokens": 128000, "system": SYSTEM, "messages": messages,
              "output_config": {"effort": EFFORT}}
    # custom_id must match ^[a-zA-Z0-9_-]{1,64}$, so the Japanese label cannot be used here.
    batch = client.messages.batches.create(requests=[{"custom_id": "jsdraw", "params": params}])
    t0 = time.time()
    while True:
        status = client.messages.batches.retrieve(batch.id).processing_status
        if status == "ended":
            break
        progress(f"{label}：Claude API の結果を待っています（{int(time.time() - t0)}秒）")
        time.sleep(15)
    result = next(iter(client.messages.batches.results(batch.id))).result
    if result.type != "succeeded":
        raise JsDrawError(f"{label} に失敗しました（{result.type}）。")
    msg = result.message
    if msg.stop_reason == "refusal":
        raise JsDrawError(f"{label} がモデルに断られました。区間を変えて試してください。")
    price_in, price_out = next((p[1], p[2]) for p in MODELS.values() if p[0] == model)
    usage = {"step": label, "model": model, "stop_reason": msg.stop_reason,
             "input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens,
             "usd": round((msg.usage.input_tokens * price_in + msg.usage.output_tokens * price_out) / 1e6 / 2, 4),
             "minutes": round((time.time() - t0) / 60, 1)}
    return msg, usage


def _apply_edits(html: str, reply: str) -> tuple[str, int, int]:
    ok = ng = 0
    for find, rep in re.findall(r"<<<<<<< FIND\n(.*?)\n=======\n(.*?)\n?>>>>>>> END", reply, re.S):
        if html.count(find) == 1:
            html = html.replace(find, rep)
            ok += 1
        else:
            ng += 1
    return html, ok, ng


# ---------------------------------------------------------------- 描画と書き出し（Playwright）

_HARNESS_JS = """([OFF, FPS, N]) => {
  const cache = new Map();
  const idx = s => Math.max(1, Math.min(N, Math.round((s - OFF) * FPS) + 1));
  window.faceAt = s => cache.get(idx(s)) || null;
  window.__load = async list => {
    for (const s of list) { const i = idx(s); if (cache.has(i)) continue;
      const im = new Image(); im.src = 'face/' + String(i).padStart(5, '0') + '.webp'; await im.decode(); cache.set(i, im);
      if (cache.size > 400) cache.delete(cache.keys().next().value); }
  };
  window.__frame = async (t, s, e) => {
    const extra = window.needFaces ? (window.needFaces(t, s, e) || []) : [];
    await window.__load([s, ...extra]); window.FACE = window.faceAt(s);
    draw(t, s, e); return document.querySelector('canvas').toDataURL('image/jpeg', 0.92);
  };
}"""


def segs_total(segs) -> float:
    return sum(b - a for a, b in segs)


def _quietest(env: list, off: float, lo: float, hi: float) -> float:
    """[lo, hi]（元配信の秒）の中で一番静かな時刻。"""
    best, bv = hi, 1e9
    for i in range(round((lo - off) * 100), round((hi - off) * 100) + 1):
        if 1 <= i < len(env) - 1:
            v = env[i - 1] + env[i] + env[i + 1]
            if v < bv:
                best, bv = off + i / 100, v
    return min(max(best, lo), hi)


def fit_length(segs, env: list, off: float, max_total: float = SEGS_MAX_SEC, search: float = 2.0) -> list:
    """SEGS の合計が max_total を超えたら、最後の区間の終わりを静かな所で切って収める。

    最後の区間が1秒未満になるほど削るときは、その区間ごと外して繰り返す。
    """
    segs = [list(map(float, seg)) for seg in segs]
    while segs and segs_total(segs) > max_total + 1e-6:
        excess = segs_total(segs) - max_total
        a, b = segs[-1]
        limit = b - excess  # これより前で切れば収まる
        if limit - a < 1.0:
            segs.pop()
            continue
        segs[-1][1] = _quietest(env, off, max(a + 1.0, limit - search), limit)
    return segs


def length_note(total: float) -> str:
    """生成された SEGS の合計が範囲外なら、直しの依頼に添える一文。"""
    if SEGS_MIN_SEC <= total <= SEGS_MAX_SEC:
        return ""
    return (f"\n\n【長さ】いまの SEGS の合計が{total:.1f}秒です。合計が{SEGS_TARGET_SEC:.0f}秒前後"
            f"（{SEGS_MIN_SEC:g}〜{SEGS_MAX_SEC:g}秒）になるように SEGS を直してください。"
            "区間を減らすか、区間の終わりを発話の切れ目で早めます。")


class _Scene:
    """scene.html をブラウザで開き、t ごとのコマを取る。"""

    def __init__(self, workdir: Path, scene: Path):
        from playwright.sync_api import sync_playwright

        self.meta = json.loads((workdir / "meta.json").read_text(encoding="utf-8"))
        if scene.parent.resolve() != workdir.resolve():
            raise JsDrawError("scene.html は素材と同じフォルダ（face/ がある所）に置いてください。")
        self._pw = sync_playwright().start()
        args = ["--allow-file-access-from-files", "--disable-web-security"]
        exe = os.environ.get("CUT_JSDRAW_BROWSER", "")
        if exe:  # ブラウザの場所を明示するとき（Edge も Playwright のブラウザも無い環境向け）
            self._browser = self._pw.chromium.launch(executable_path=exe, args=args)
        else:
            try:  # Windows には Edge が必ずあるので、まずそれを使う
                self._browser = self._pw.chromium.launch(channel="msedge", args=args)
            except Exception:
                self._browser = self._pw.chromium.launch(args=args)
        page = self._browser.new_page(viewport={"width": 1080, "height": 1920})
        self.errors: list[str] = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.goto(scene.resolve().as_uri())
        page.evaluate(_HARNESS_JS, [self.meta["off"], FPS, self.meta["nface"]])
        self.page = page
        raw = page.evaluate("() => window.SEGS")
        if not raw:
            raise JsDrawError("scene.html に window.SEGS がありません。")
        env = json.loads((workdir / "env.json").read_text(encoding="utf-8"))
        self.segs = [[self._quiet(env, a), self._quiet(env, b)] for a, b in raw]
        self.raw_total = segs_total(self.segs)  # AI が書いたままの長さ（直しの依頼に使う）
        self.segs = fit_length(self.segs, env, self.meta["off"])
        self.starts, acc = [], 0.0
        for a, b in self.segs:
            self.starts.append(acc)
            acc += b - a
        self.tend = acc + TAIL

    def _quiet(self, env: list, s: float) -> float:
        """s の前後0.6秒で一番静かな所（発話の切れ目）へ寄せる。"""
        c = round((s - self.meta["off"]) * 100)
        best, bv = c, 1e9
        for i in range(c - 60, c + 61):
            if 1 <= i < len(env) - 1:
                v = env[i - 1] + env[i] + env[i + 1] + abs(i - c) * 0.05
                if v < bv:
                    best, bv = i, v
        return self.meta["off"] + best / 100

    def src(self, t: float) -> float:
        for i in range(len(self.segs) - 1, -1, -1):
            if t >= self.starts[i]:
                return min(self.segs[i][0] + t - self.starts[i], self.segs[i][1])
        return self.segs[0][0]

    def frame(self, t: float) -> bytes:
        data = self.page.evaluate("([t, s, e]) => window.__frame(t, s, e)", [t, self.src(t), self.tend])
        return base64.b64decode(data.split(",", 1)[1])

    def close(self):
        self._browser.close()
        self._pw.stop()


def _preview(workdir: Path, scene: Path, outdir: Path) -> tuple[list, float, str, float]:
    sc = _Scene(workdir, scene)
    try:
        times = [0.8] + [round(sc.tend * k / 8, 1) for k in range(1, 8)] + [round(sc.tend - 0.8, 1)]
        blocks = []
        for t in times:
            full = outdir / f"prev_{t:g}.jpg"
            full.write_bytes(sc.frame(t))
            small = outdir / f"small_{t:g}.jpg"
            _ffmpeg("-i", str(full), "-vf", "scale=540:960", "-q:v", "4", str(small))
            blocks += [("text", f"t={t:g}"), ("image", small)]
        return blocks, sc.tend, "\n".join(sc.errors), sc.raw_total
    finally:
        sc.close()


def render(workdir: Path, scene: Path, out_mp4: Path, progress: Progress) -> float:
    sc = _Scene(workdir, scene)
    if sc.raw_total > SEGS_MAX_SEC + 1e-6:
        progress(f"音声区間の合計が{sc.raw_total:.1f}秒だったため、最後の区間を静かな所で切って"
                 f"{segs_total(sc.segs):.1f}秒にしました")
    elif sc.raw_total < SEGS_MIN_SEC:
        progress(f"音声区間の合計が{sc.raw_total:.1f}秒で、動画が{VIDEO_MIN_SEC:g}秒より短くなります（そのまま作ります）")
    vtmp = out_mp4.with_suffix(".video.mp4")
    try:
        ff = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg",
                               "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
                               str(vtmp)], stdin=subprocess.PIPE)
        total = round(sc.tend * FPS)
        for f in range(total):
            ff.stdin.write(sc.frame(f / FPS))
            if f % 150 == 0:
                progress(f"書き出し中 {f}/{total} コマ")
        ff.stdin.close()
        ff.wait()
        segs, tend = sc.segs, sc.tend
    finally:
        sc.close()
    off = json.loads((workdir / "meta.json").read_text(encoding="utf-8"))["off"]
    parts = [f"[1:a]atrim={a - off:.3f}:{b - off:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=0.03,"
             f"afade=t=out:st={b - a - 0.04:.3f}:d=0.04[a{i}]" for i, (a, b) in enumerate(segs)]
    fc = ";".join(parts) + ";" + "".join(f"[a{i}]" for i in range(len(segs))) + \
        f"concat=n={len(segs)}:v=0:a=1,loudnorm=I=-16:TP=-1.5,apad=whole_dur={tend:.3f}[a]"
    _ffmpeg("-i", str(vtmp), "-i", str(workdir / "src.wav"), "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest", str(out_mp4))
    vtmp.unlink(missing_ok=True)
    return tend


# ---------------------------------------------------------------- 問い合わせ先（API / Claude Code / Codex）
#
# 依頼は「部品」のリストで組み立てる：("text", 文字列) と ("image", 画像ファイル)。
# API には画像を base64 で載せ、CLI には作業フォルダの画像を見せる（Claude Code は Read ツール、
# Codex は -i で添付）。CLI はログインの枠で動き、料金は「APIならいくら」という換算額だけを記録する。

BACKENDS = ("api", "claude", "codex")
CLI_TIMEOUT_SEC = 1800
CLI_NOTE = ("ファイルを書いたりコマンドを実行したりせず、答えはすべて回答本文に出力してください。")


def _api_content(parts: list) -> list[dict]:
    return [{"type": "text", "text": part[1]} if part[0] == "text" else _img(part[1]) for part in parts]


def _claude_prompt(parts: list, workdir: Path) -> str:
    lines = [CLI_NOTE, "画像は Read ツールで開いて見てください（パスは作業フォルダからの相対パス）。", ""]
    for kind, value in parts:
        if kind == "text":
            lines.append(value)
        else:
            lines.append(f"［画像: {Path(value).resolve().relative_to(workdir.resolve()).as_posix()}］")
    return "\n".join(lines)


def _codex_prompt(parts: list) -> tuple[str, list[Path]]:
    images = [Path(value) for kind, value in parts if kind == "image"]
    lines, n = [SYSTEM, CLI_NOTE, f"画像を{len(images)}枚添付しています（下の［添付画像 k］が k 枚目）。", ""], 0
    for kind, value in parts:
        if kind == "text":
            lines.append(value)
        else:
            n += 1
            lines.append(f"［添付画像 {n}］")
    return "\n".join(lines), images


def _ask_claude(model: str, parts: list, workdir: Path, progress: Progress, label: str,
                run=subprocess.run) -> tuple[str, dict]:
    from . import agent_runner

    workdir = Path(workdir).resolve()  # the CLI runs inside it, so relative paths would double up
    exe = agent_runner.find_executable("claude")
    if not exe:
        raise JsDrawError("Claude Code が見つかりません。")
    empty = workdir / "empty_mcp.json"
    empty.write_text('{"mcpServers": {}}', encoding="utf-8")
    command = [exe, "-p", "--model", model, "--output-format", "json", "--effort", EFFORT,
               "--strict-mcp-config", "--mcp-config", str(empty), "--allowedTools", "Read",
               "--add-dir", str(workdir), "--append-system-prompt", SYSTEM]
    progress(f"{label}：Claude Code（ログイン）に描いてもらっています")
    t0 = time.time()
    # The prompt goes through stdin (it is far longer than Windows' command-line limit).
    done = run(command, input=_claude_prompt(parts, workdir), text=True, encoding="utf-8", errors="replace",
               capture_output=True, cwd=str(workdir), env=api_keys.agent_env("claude"), timeout=CLI_TIMEOUT_SEC)
    try:
        data = json.loads(done.stdout)
    except ValueError as exc:
        raise JsDrawError(f"{label}：Claude Code の出力を読めません（exit {done.returncode}）"
                          f"{(done.stderr or done.stdout)[-200:]}") from exc
    if data.get("is_error") or done.returncode != 0:
        raise JsDrawError(f"{label}：Claude Code がエラーを返しました: {str(data.get('result'))[:200]}")
    usage = data.get("usage") or {}
    return str(data.get("result") or ""), {
        "step": label, "backend": "claude", "model": model, "stop_reason": data.get("stop_reason") or "",
        "num_turns": data.get("num_turns"), "duration_ms": data.get("duration_ms"),
        "input_tokens": int(usage.get("input_tokens") or 0), "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "usd": 0.0, "usd_equivalent": round(float(data.get("total_cost_usd") or 0.0), 4),
        "minutes": round((time.time() - t0) / 60, 1)}


_TOKENS_USED = re.compile(r"tokens used\s*[:\n]?\s*([\d,]+)", re.IGNORECASE)


def _ask_codex(model: str, parts: list, workdir: Path, progress: Progress, label: str,
               run=subprocess.run) -> tuple[str, dict]:
    from . import agent_runner

    workdir = Path(workdir).resolve()  # the CLI runs inside it, so relative paths would double up
    exe = agent_runner.find_executable("codex")
    if not exe:
        raise JsDrawError("Codex が見つかりません。")
    prompt, images = _codex_prompt(parts)
    answer = workdir / f"codex_{ {'生成': 'gen', '直し': 'fix'}.get(label, 'ask')}.txt"
    answer.unlink(missing_ok=True)
    command = [exe, "exec", "-s", "read-only", "--skip-git-repo-check",
               "-c", f'model_reasoning_effort="{EFFORT}"', "-o", str(answer)]
    if model:
        command += ["-m", model]
    command += [f"--image={path}" for path in images] + ["-"]
    progress(f"{label}：Codex（ログイン）に描いてもらっています")
    t0 = time.time()
    done = run(command, input=prompt, text=True, encoding="utf-8", errors="replace", capture_output=True,
               cwd=str(workdir), env=api_keys.agent_env("codex"), timeout=CLI_TIMEOUT_SEC)
    if done.returncode != 0 or not answer.is_file():
        raise JsDrawError(f"{label}：Codex が異常終了しました（exit {done.returncode}）"
                          f"{(done.stderr or done.stdout)[-200:]}")
    found = _TOKENS_USED.findall((done.stdout or "") + "\n" + (done.stderr or ""))
    return answer.read_text(encoding="utf-8"), {
        "step": label, "backend": "codex", "model": model or "既定", "stop_reason": "",
        "tokens_used": int(found[-1].replace(",", "")) if found else None,
        "usd": 0.0, "usd_equivalent": None, "minutes": round((time.time() - t0) / 60, 1)}


def _ask(backend: str, model: str, parts: list, workdir: Path, progress: Progress, label: str,
         client=None) -> tuple[str, dict]:
    if backend == "claude":
        return _ask_claude(model, parts, workdir, progress, label)
    if backend == "codex":
        return _ask_codex(model, parts, workdir, progress, label)
    msg, usage = _batch(client, model, [{"role": "user", "content": _api_content(parts)}], progress, label)
    return _text(msg), usage


# ---------------------------------------------------------------- 全体

def make_short(video_path: str, start: float, end: float, *, model: str = DEFAULT_MODEL, title: str = "",
               story: str = "", out_dir: Path | None = None, progress: Progress = print,
               video_id: str = "", media_offset: float = 0.0, backend: str = "api", codex_model: str = "") -> dict:
    """1本作る。戻り値：{"mp4", "scene", "usage", "usd", "usd_equivalent", "seconds", ...}。

    ``backend``：描く AI。"api"（Claude API の Batch・従量課金）、"claude"（Claude Code のログイン）、
    "codex"（Codex のログイン。モデルは ``codex_model``）。``model`` は api / claude のときの描画モデル。
    ``video_id`` と ``media_offset`` は、元配信の一部分だけのファイルから作るとき（``prepare`` を参照）。
    """
    if model not in MODELS:
        raise JsDrawError(f"未対応のモデルです: {model}")
    if backend not in BACKENDS:
        raise JsDrawError(f"未対応の描き方です: {backend}")
    client = _client() if backend == "api" else None
    stem = f"{Path(video_path).stem}_{int(start)}_{int(end)}"
    work = Path(config.DATA_DIR) / "jsdraw" / stem
    out_dir = Path(out_dir or Path("exports") / "jsdraw")
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path = work / "meta.json"
    if meta_path.is_file() and json.loads(meta_path.read_text(encoding="utf-8")).get("nface"):
        progress("素材は用意済みのものを使います")
    else:
        prepare(video_path, start, end, work, progress, video_id=video_id, media_offset=media_offset)
    tag = model if backend != "codex" else "codex"
    run = work / f"run_{backend}_{tag}_{time.strftime('%Y%m%d_%H%M%S')}"
    run.mkdir()
    lo, hi = start, end
    parts = [("text", _spec(title, story, lo, hi))]
    if REFERENCE_HTML.is_file():
        parts.append(("text", "## 参考 scene.html（別の話）\n```html\n" + REFERENCE_HTML.read_text(encoding="utf-8") + "\n```"))
    transcript = json.loads((work / "transcript.json").read_text(encoding="utf-8"))
    parts += [("text", "## 文字起こし（[開始秒, 終了秒, 本文]）\n" + json.dumps(transcript, ensure_ascii=False)),
              ("text", "## 顔カメラ（元配信の秒）")]
    for p in sorted((work / "thumbs").glob("s*.jpg"), key=lambda p: int(p.stem[1:])):
        parts += [("text", f"{p.stem[1:]}秒"), ("image", p)]

    usage = []
    draw_model = codex_model if backend == "codex" else MODELS[model][0]
    fix_model = codex_model if backend == "codex" else FIXER_MODEL
    who = {"api": "Claude API", "claude": "Claude Code（ログイン）", "codex": "Codex（ログイン）"}[backend]
    progress(f"生成中（{who}・{MODEL_LABELS[model] if backend != 'codex' else (codex_model or '既定のモデル')}）")
    reply, u = _ask(backend, draw_model, parts, work, progress, "生成", client)
    usage.append(u)
    found = re.search(r"```html\s*\n(.*?)```", reply, re.S)
    if not found:
        raise JsDrawError(f"コードが返ってきませんでした（{u.get('stop_reason') or '形式違い'}）。")
    html = found.group(1)
    scene = work / f"scene_{run.name}.html"  # face/ と同じフォルダに置く（ブラウザから相対パスで読むため）
    scene.write_text(html, encoding="utf-8")
    (run / "scene_v1.html").write_text(html, encoding="utf-8")

    progress("プレビューを作っています")
    blocks, tend, errors, raw_total = _preview(work, scene, run)
    note = length_note(raw_total)
    if note:
        progress(f"音声区間の合計が{raw_total:.1f}秒なので、直しで長さも直してもらいます")
    ask = [("text", (
        f"縦型ショート動画（1080x1920）を描く scene.html と、その書き出しプレビュー（出力動画の秒）です。長さ {tend:.1f} 秒。"
        f"ブラウザのエラー：{errors or 'なし'}\n\n{_rules(lo, hi)}\n\n"
        "見た目の崩れ（文字のはみ出し・重なり・読みにくさ・空白の多い画面・意味が伝わらない絵）とエラーを点検し、"
        "直す箇所だけを次の形式で出力してください。全文は書き直さない。\n"
        "<<<<<<< FIND\n（scene.html にそのまま1回だけ現れる元の文字列）\n=======\n（置き換え後）\n>>>>>>> END\n"
        "直す必要がなければ「直しなし」とだけ書く。" + note + "\n\n```html\n" + html + "\n```"))] + blocks
    progress(f"直しています（{who}）")
    fix_reply, u2 = _ask(backend, fix_model, ask, work, progress, "直し", client)
    usage.append(u2)
    html, ok, ng = _apply_edits(html, fix_reply)
    scene.write_text(html, encoding="utf-8")
    (run / "scene.html").write_text(html, encoding="utf-8")

    out_mp4 = out_dir / f"{stem}_{backend}_{tag}.mp4"
    seconds = render(work, scene, out_mp4, progress)
    total = round(sum(x["usd"] for x in usage), 3)
    equivalents = [x.get("usd_equivalent") for x in usage if x.get("usd_equivalent") is not None]
    equivalent = round(sum(equivalents), 3) if equivalents else None
    (run / "usage.json").write_text(json.dumps({"backend": backend, "usage": usage, "usd": total,
                                                "usd_equivalent": equivalent, "edits": [ok, ng]},
                                               ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.copy(scene, out_mp4.with_suffix(".scene.html"))
    cost = f"API費用 約{total}ドル" if backend == "api" else (
        "ログインの枠" + (f"（API換算 約{equivalent}ドル）" if equivalent is not None else ""))
    progress(f"完成：{out_mp4}（{cost}）")
    return {"mp4": str(out_mp4), "scene": str(scene), "backend": backend, "usage": usage, "usd": total,
            "usd_equivalent": equivalent, "seconds": round(seconds, 2), "segs_total_generated": round(raw_total, 2)}
