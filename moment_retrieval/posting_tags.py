"""AI tags for YouTube uploads (snippet.tags) and hashtags for the description.

Off by default.  Turned on with ``{"enabled": true}`` in ``<DATA_DIR>/posting_tags.json``.
When on, the auto-publish upload step calls :func:`apply_to_sidecar` once per clip just
before uploading; it writes the tags and a hashtag line into ``<clip>.metadata.json``
(which :func:`youtube_upload.metadata_for` already reads), and records ``tag_suggest`` there
so a retried upload does not ask the AI again.  Any failure leaves the sidecar as it was.

Spellings are fixed by ``<DATA_DIR>/tag_aliases.json`` (falls back to the bundled
``tag_aliases_default.json``): each group lists the tag spellings to keep, the variants
to rewrite into them, and the one hashtag used for the whole group.  The same file holds
the words never to use (``exclude``) and the services / brands allowed despite being
company names (``allow``).

CLI for trying it on one clip: ``tools/tag_suggest.py``.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path

from . import config, short_style, source_titles

MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 2500
PRICE_INPUT_PER_MTOK = 2.0  # same figures as oneshot_selector.py
PRICE_OUTPUT_PER_MTOK = 10.0
TRANSCRIPT_SUMMARY_CHARS = 2500  # longer transcripts are summarised first
MAX_TAG_CHARS = 30
MAX_TAGS_TOTAL = 500  # YouTube's limit for snippet.tags (counted as below)
MIN_TAGS, MAX_TAGS = 10, 15
MAX_HASHTAGS = 5
SETTINGS_FILE = "posting_tags.json"
ALIASES_FILE = "tag_aliases.json"
BUNDLED_ALIASES = Path(__file__).with_name("tag_aliases_default.json")
DEFAULT_SETTINGS = {"enabled": False, "streamer_name": ""}

KINDS = ["配信者", "ゲーム", "内容", "ジャンル", "サービス", "企業・ブランド", "人名"]
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "kind": {"type": "string", "enum": KINDS},
                    "public_figure": {"type": "boolean"},
                    "central": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["tag", "kind", "public_figure", "central", "reason"],
                "additionalProperties": False,
            },
        },
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "game": {"type": "string"},
        "game_related": {"type": "boolean"},
    },
    "required": ["tags", "hashtags", "game", "game_related"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """あなたはYouTubeショート（配信の切り抜き）の投稿担当です。
1本のクリップについて、YouTubeの snippet.tags と、説明文に入れるハッシュタグを決めます。

game / game_related:
- game: 元配信で遊んでいるゲーム名（不明・ゲームなしなら空文字）。「表記の一覧」にあるゲームはその表記を使う。
- game_related: クリップの話題そのものがゲームのプレイや内容なら true。ゲーム配信中でも、
  話題がゲームと関係ない雑談（お金、仕事、生活の話など）なら false。

tags のルール:
- 10〜15個。重複・表記違いの重複（全角半角、空白の有無、大文字小文字）は入れない。1個30文字以内。
- 次を混ぜる: 配信者名（分かる場合のみ）、クリップの内容を表すキーワード（話題・出来事・登場する固有名詞）、
  ジャンル語（切り抜き、ゲーム実況、雑談 など）、ゲーム名。
- game_related が true のときだけ、ゲーム名を正式名と略称で最大2つ入れてよい。false のときはゲーム名を1つだけにする。
- 「表記の一覧」にある語は、その一覧の表記を使う。
- 情報にない配信者名やゲーム名を推測で作らない。分からなければ入れない。「#」は付けない。
- kind は次から選ぶ: 配信者／ゲーム／内容／ジャンル／サービス（Twitch・YouTube・ChatGPT など配信やネットのサービス）／
  企業・ブランド（会社名・商品ブランド・金融会社など）／人名（実在の人物の名前。ゲームやアニメのキャラクターは「内容」）。
- 人名のときだけ public_figure（有名人・公人か）と central（クリップの話題の中心か）を正しく付ける。それ以外は両方 false。
- reason に選んだ根拠を一言（20字程度）。

hashtags のルール:
- 3〜5個。必ず "#shorts" を含める。各要素は "#" で始め、空白を含めない。
- 先頭3つがタイトルの上に表示されるので、視聴者が検索・興味を持ちやすい順に並べる
  （話題またはゲーム名 → 配信者名 → #shorts の順を基本とし、#shorts は3番目以内）。
- 人名、企業・ブランド名はハッシュタグにしない。game_related が false ならゲーム名もハッシュタグにしない。
- 「表記の一覧」にある語は、一覧のハッシュタグ表記をそのまま使う。"""


class TagSuggestError(RuntimeError):
    pass


# ---------------------------------------------------------------- spelling
def norm_key(text: str) -> str:
    """Comparison key: NFKC (full/half width), no spaces or middle dots, case-folded."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    return re.sub(r"[\s・･]", "", text).casefold()


def clean_tag(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = re.sub(r"[<>#,]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:MAX_TAG_CHARS].strip()


class Aliases:
    def __init__(self, data: dict | None = None):
        data = data if isinstance(data, dict) else {}
        self.groups: list[dict] = []
        self.tag_for: dict[str, str] = {}  # key of any spelling -> tag spelling to use
        self.group_for: dict[str, dict] = {}  # key of any spelling -> its group
        for raw in data.get("groups") or []:
            if not isinstance(raw, dict) or not raw.get("tags"):
                continue
            tags = [clean_tag(tag) for tag in raw["tags"] if clean_tag(tag)]
            hashtag = "#" + re.sub(r"[\s#]", "", unicodedata.normalize("NFKC", str(raw.get("hashtag") or tags[0])))
            group = {"name": str(raw.get("name") or tags[0]), "kind": str(raw.get("kind") or ""),
                     "tags": tags, "hashtag": hashtag}
            self.groups.append(group)
            for tag in tags:
                self.tag_for[norm_key(tag)] = tag
                self.group_for[norm_key(tag)] = group
            for variant in raw.get("variants") or []:
                self.tag_for.setdefault(norm_key(variant), tags[0])
                self.group_for.setdefault(norm_key(variant), group)
            self.group_for.setdefault(norm_key(hashtag), group)
        self.exclude = [norm_key(word) for word in data.get("exclude") or [] if norm_key(word)]
        self.allow = {norm_key(word) for word in data.get("allow") or [] if norm_key(word)}

    def tag(self, text: str) -> str:
        tag = clean_tag(text)
        return self.tag_for.get(norm_key(tag), tag)

    def group(self, text: str) -> dict | None:
        return self.group_for.get(norm_key(text))

    def hashtag(self, text: str) -> str:
        group = self.group(str(text).lstrip("#＃"))
        if group:
            return group["hashtag"]
        return "#" + re.sub(r"[\s#＃<>,]", "", unicodedata.normalize("NFKC", str(text)))

    def excluded(self, text: str) -> bool:
        key = norm_key(str(text).lstrip("#"))
        return key not in self.allow and any(word in key for word in self.exclude)

    def prompt_table(self) -> str:
        return "\n".join(
            f"- {group['name']}: タグは {' / '.join(group['tags'])}、ハッシュタグは {group['hashtag']}"
            for group in self.groups)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def aliases_path() -> Path:
    return Path(config.DATA_DIR) / ALIASES_FILE


def load_aliases() -> Aliases:
    path = aliases_path()
    return Aliases(_read_json(path if path.is_file() else BUNDLED_ALIASES))


# ---------------------------------------------------------------- settings
def settings_path() -> Path:
    return Path(config.DATA_DIR) / SETTINGS_FILE


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    raw = _read_json(settings_path())
    if isinstance(raw.get("enabled"), bool):
        settings["enabled"] = raw["enabled"]
    if isinstance(raw.get("streamer_name"), str):
        settings["streamer_name"] = raw["streamer_name"].strip()
    return settings


def enabled() -> bool:
    return bool(load_settings()["enabled"])


# ---------------------------------------------------------------- input gathering
def _library_conn() -> sqlite3.Connection | None:
    path = Path(config.DB_PATH).resolve()
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _library_info(source_range: dict) -> tuple[dict | None, list[str]]:
    """(video row, transcript lines of the range) from the library, read-only."""
    from . import db

    if not source_range.get("video_id"):
        return None, []
    conn = _library_conn()
    if conn is None:
        return None, []
    try:
        video = db.get_video(conn, str(source_range["video_id"]))
        rows = db.get_segments_in_range(
            conn, str(source_range["video_id"]),
            float(source_range.get("start_sec") or 0), float(source_range.get("end_sec") or 0),
        )
        return video, [str(row["text"]).strip() for row in rows if str(row["text"]).strip()]
    except Exception:
        return None, []
    finally:
        conn.close()


def _source_entry(*names: str) -> dict:
    titles = source_titles.load()
    for name in names:
        key = source_titles.source_key(name or "")
        if key and titles.get(key, {}).get("title"):
            return titles[key]
    return {}


def gather(clip: Path, streamer: str = "") -> dict:
    """Everything known about one clip, without local paths."""
    clip = Path(clip)
    meta = _read_json(clip.with_suffix(".metadata.json"))
    finish = _read_json(clip.with_suffix(".finish.json"))
    raw = finish.get("raw") if isinstance(finish.get("raw"), dict) else {}
    captions = [str(line).strip() for line in (raw.get("captions") or []) if str(line).strip()]
    transcript_source = "finish.json" if captions else ""
    video, library_lines = _library_info(meta.get("source_range") or {})
    if not captions and library_lines:
        captions, transcript_source = library_lines, "library"
    video_names = [str((video or {}).get(key) or "") for key in ("path", "display_name")]
    source = _source_entry(*video_names, clip.parent.name, clip.name)
    title = str(meta.get("title") or clip.stem)
    streamer = (streamer or load_settings()["streamer_name"]
                or short_style.load_settings().get("streamer_name") or ""
                or short_style.streamer_from_title(str(raw.get("hook_text") or title)))
    receipt = _read_json(clip.with_suffix(".youtube.json"))
    return {
        "clip_title": title,
        "description": str(meta.get("description") or ""),
        "current_tags": list(meta.get("tags") or []),
        "hook_text": str(raw.get("hook_text") or ""),
        "transcript": captions,
        "transcript_source": transcript_source,
        "source_title": str(source.get("title") or ""),
        "source_date": str(source.get("upload_date") or ""),
        "streamer": streamer,
        "youtube_video_id": str(receipt.get("video_id") or ""),
    }


# ---------------------------------------------------------------- API
def _client():
    import anthropic

    from .api_keys import resolve

    key, _source = resolve("anthropic")
    if not key:
        raise TagSuggestError("Claude（Anthropic）のAPIキーが設定されていません。")
    return anthropic.Anthropic(api_key=key, max_retries=2, timeout=120)


def _usage(response) -> dict:
    usage = getattr(response, "usage", None)
    return {key: int(getattr(usage, key, 0) or 0) for key in ("input_tokens", "output_tokens")}


def _add_usage(total: dict, usage: dict) -> None:
    for key, value in usage.items():
        total[key] = total.get(key, 0) + value


def cost_usd(usage: dict) -> float:
    return round((usage.get("input_tokens", 0) * PRICE_INPUT_PER_MTOK
                  + usage.get("output_tokens", 0) * PRICE_OUTPUT_PER_MTOK) / 1_000_000, 5)


def _create(client, **kwargs):
    """temperature=0 for repeatable answers; retried without it if the model rejects it."""
    try:
        return client.messages.create(temperature=0, **kwargs), True
    except Exception as exc:  # anthropic.BadRequestError when temperature is not accepted
        if "temperature" not in str(exc):
            raise
        return client.messages.create(**kwargs), False


def _text(response) -> str:
    if getattr(response, "stop_reason", None) == "refusal":
        raise TagSuggestError("モデルが回答を断りました。")
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise TagSuggestError("出力が上限で切れました（max_tokens）。")
    return "".join(block.text for block in response.content if getattr(block, "type", "") == "text")


def _transcript_for_prompt(client, info: dict, usage: dict) -> tuple[str, bool]:
    text = "\n".join(info["transcript"])
    if len(text) <= TRANSCRIPT_SUMMARY_CHARS:
        return text, False
    response, _ = _create(
        client, model=MODEL, max_tokens=800,
        system="配信の切り抜きの文字起こしを、話題・出来事・登場する固有名詞（ゲーム名、キャラ名、人名、サービス名）を落とさずに300字程度で要約してください。",
        messages=[{"role": "user", "content": text[:40000]}],
    )
    _add_usage(usage, _usage(response))
    return _text(response).strip(), True


def _prompt(info: dict, transcript: str, summarized: bool, aliases: Aliases) -> str:
    lines = [
        f"クリップのタイトル: {info['clip_title']}",
        f"冒頭テロップ: {info['hook_text'] or '（なし）'}",
        f"説明文（AIの要約）: {info['description'] or '（なし）'}",
        f"元配信のタイトル: {info['source_title'] or '（不明）'}",
        f"配信日: {info['source_date'] or '（不明）'}",
        f"配信者名: {info['streamer'] or '（不明。推測で書かないこと）'}",
        f"今付いているタグ: {', '.join(info['current_tags']) or '（なし）'}",
        "",
        "表記の一覧:",
        aliases.prompt_table() or "（なし）",
        "",
        "クリップの発話" + ("（要約）" if summarized else "") + ":",
        transcript or "（なし）",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- post-check
def _tag_cost(tag: str) -> int:
    # YouTube counts a tag with a space as quoted, plus a separating comma.
    return len(tag) + (2 if " " in tag else 0) + 1


def _is_game(item: dict, game: str, aliases: Aliases) -> bool:
    group = aliases.group(item["tag"])
    if group is not None:
        return group.get("kind") == "game"
    return item.get("kind") == "ゲーム" or (bool(game) and norm_key(item["tag"]) == norm_key(game))


def clean(answer: dict, aliases: Aliases | None = None) -> dict:
    """Fix spellings, drop excluded / brand / private-person words, apply the game rule."""
    aliases = aliases or Aliases()
    game_related = bool(answer.get("game_related", True))
    game_group = aliases.group(answer.get("game") or "")
    game = game_group["tags"][0] if game_group else clean_tag(answer.get("game") or "")
    dropped: dict[str, str] = {}
    blocked_keys: set[str] = set()  # words that must not become hashtags
    items = []
    for raw in answer.get("tags") or []:
        if not isinstance(raw, dict):
            continue
        item = {**raw, "tag": aliases.tag(raw.get("tag") or "")}
        if not item["tag"]:
            continue
        kind = str(item.get("kind") or "")
        key = norm_key(item["tag"])
        if aliases.excluded(item["tag"]):
            dropped[item["tag"]] = "除外語リスト"
            blocked_keys.add(key)
            continue
        if kind == "企業・ブランド" and key not in aliases.allow:
            dropped[item["tag"]] = "企業・ブランド名"
            blocked_keys.add(key)
            continue
        if kind == "人名":
            blocked_keys.add(key)
            if not (item.get("public_figure") and item.get("central")):
                dropped[item["tag"]] = "人名（公人で話題の中心ではない）"
                continue
        items.append(item)
    # Game names: at most two when the clip is about the game, else one at the end.
    game_items = [item for item in items if _is_game(item, game, aliases)]
    if not game_related and game_items:
        keep = game_items[0]
        for item in game_items[1:]:
            dropped[item["tag"]] = "ゲームと関係ない話題（ゲーム名は1つだけ）"
        items = [item for item in items if item not in game_items] + [keep]
    tags, reasons, seen, used = [], {}, set(), 0
    for item in items:
        tag, key = item["tag"], norm_key(item["tag"])
        if key in seen or used + _tag_cost(tag) > MAX_TAGS_TOTAL or len(tags) >= MAX_TAGS:
            continue
        seen.add(key)
        used += _tag_cost(tag)
        tags.append(tag)
        reasons[tag] = f"[{item.get('kind', '')}] {str(item.get('reason') or '').strip()}"
    game_hashtag_keys = {norm_key(group["hashtag"]) for group in aliases.groups if group.get("kind") == "game"}
    if game_group:
        game_hashtag_keys.add(norm_key(game_group["hashtag"]))
    elif game:
        game_hashtag_keys.add(norm_key("#" + game))
    hashtags, seen_h = [], set()
    for value in answer.get("hashtags") or []:
        tag = aliases.hashtag(value)
        key = norm_key(tag)
        bare = key.lstrip("#")
        if len(tag) <= 1 or key in seen_h:
            continue
        if bare in blocked_keys or aliases.excluded(tag) or any(word and word in bare for word in blocked_keys):
            dropped.setdefault(tag, "ハッシュタグに使わない語（人名・企業・除外語）")
            continue
        if not game_related and key in game_hashtag_keys:
            dropped.setdefault(tag, "ゲームと関係ない話題のためハッシュタグにしない")
            continue
        seen_h.add(key)
        hashtags.append(tag)
    lowered = [tag.casefold() for tag in hashtags]
    if "#shorts" in lowered:
        hashtags.pop(lowered.index("#shorts"))
    hashtags.insert(min(2, len(hashtags)), "#shorts")
    hashtags = hashtags[:MAX_HASHTAGS]
    warnings = []
    if len(tags) < MIN_TAGS:
        warnings.append(f"タグが{len(tags)}個しかありません（目安{MIN_TAGS}〜{MAX_TAGS}個）")
    return {"tags": tags, "tags_chars": used, "hashtags": hashtags, "reason": reasons,
            "dropped": dropped, "game": game, "game_related": game_related, "warnings": warnings}


def suggest(clip: Path, *, streamer: str = "", client=None, aliases: Aliases | None = None) -> dict:
    aliases = aliases or load_aliases()
    info = gather(clip, streamer)
    client = client or _client()
    usage: dict = {}
    transcript, summarized = _transcript_for_prompt(client, info, usage)
    response, deterministic = _create(
        client, model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _prompt(info, transcript, summarized, aliases)}],
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    _add_usage(usage, _usage(response))
    text = _text(response)
    try:
        answer = json.loads(text)
    except ValueError as exc:
        raise TagSuggestError(f"応答がJSONではありません: {text[:200]}") from exc
    result = clean(answer, aliases)
    result.update({
        "input": {key: info[key] for key in (
            "clip_title", "hook_text", "source_title", "source_date", "streamer", "current_tags",
            "transcript_source", "youtube_video_id")},
        "transcript_summarized": summarized,
        "model": MODEL,
        "temperature": 0 if deterministic else "default (temperature rejected)",
        "usage": usage,
        "cost_usd": cost_usd(usage),
    })
    return result


# ---------------------------------------------------------------- pipeline hook
def _merge_tags(first: list[str], second: list[str], limit: int, aliases: Aliases) -> list[str]:
    tags, seen, used = [], set(), 0
    for tag in [*first, *second]:
        tag = aliases.tag(tag)
        key = norm_key(tag)
        if not tag or key in seen or aliases.excluded(tag) or used + _tag_cost(tag) > limit:
            continue
        seen.add(key)
        used += _tag_cost(tag)
        tags.append(tag)
    return tags


def with_hashtags(description: str, hashtags: list[str]) -> str:
    """The description with one hashtag line at the end (kept as is when it has #shorts)."""
    description = str(description or "").rstrip()
    if not hashtags or "#shorts" in description.casefold():
        return description
    return (description + "\n\n" if description else "") + " ".join(hashtags)


def apply_to_sidecar(clip: Path, *, client=None) -> dict | None:
    """Add AI tags / hashtags to ``<clip>.metadata.json``; None when off or already done."""
    from .youtube_upload import MAX_TAGS_CHARS

    settings = load_settings()
    sidecar = Path(clip).with_suffix(".metadata.json")
    data = _read_json(sidecar)
    if not settings["enabled"] or not data or data.get("tag_suggest"):
        return None
    aliases = load_aliases()
    result = suggest(Path(clip), streamer=settings["streamer_name"], client=client, aliases=aliases)
    data["tags"] = _merge_tags(result["tags"], list(data.get("tags") or []), MAX_TAGS_CHARS, aliases)
    data["description"] = with_hashtags(str(data.get("description") or ""), result["hashtags"])
    data["tag_suggest"] = {key: result[key] for key in ("hashtags", "game", "game_related", "model", "cost_usd")}
    temporary = sidecar.with_name(f".{sidecar.name}.tags.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, sidecar)
    return result
