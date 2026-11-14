"""Small, deterministic UX vocabulary. No Tkinter, IO, or background timers.

All UI presentations use this single state description to avoid contradictory
labels (e.g. 'ready' while inference is still running).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Presentation:
    title: str
    hint: str
    tone: str = "neutral"
    can_record: bool = True
    can_stop: bool = False


PRESENTATIONS = {
    "idle": Presentation("準備完了", "Ctrl + Alt + Space で話す"),
    "listening": Presentation("聞いています", "話し終えたら自動で認識します", "recording", False, True),
    "transcribing": Presentation("音声を認識中", "文字起こしをしています", "working", False, True),
    "acting": Presentation("操作中", "Ctrl + Alt + Esc で中止", "working", False, True),
    "confirming": Presentation("操作の確認待ち", "確認ウィンドウで選択してください", "working", False, True),
    "cancelling": Presentation("停止しています", "現在の処理が終わると停止します", "working", False, False),
    "completed": Presentation("確認できました", "操作結果を確認しました", "success"),
    "handoff": Presentation("ChatGPTを開きました", "質問を確認して送信してください", "success"),
    "handoff_clipboard": Presentation("ChatGPTへ渡す準備ができました", "コピーした文章を貼り付けて送信してください", "neutral"),
    "unverified": Presentation("操作を実行しました", "結果は未確認です。必要なら画面を確認してください", "warning"),
    "interrupted": Presentation("中止しました", "必要なら指示を直して再実行できます", "neutral"),
    "incomplete": Presentation("完了していません", "理由を確認して、言い直せます", "warning"),
    "error": Presentation("実行できませんでした", "認識結果を直して再実行できます", "error"),
    "no_speech": Presentation("声を聞き取れませんでした", "マイクを確認して、もう一度話してください", "warning"),
}


def presentation(stage: str, *, heard_voice: bool = False, elapsed: float = 0,
                 auto_stop: bool = True) -> Presentation:
    """Returns visible copy, with listening hints but no arbitrary polling."""
    if stage not in PRESENTATIONS:
        raise ValueError(f"unknown UX stage: {stage}")
    if stage == "listening":
        if not auto_stop:
            return Presentation("録音中", "Ctrl + Alt + Space で録音終了", "recording", False, True)
        if not heard_voice and elapsed > 1.8:
            return Presentation("聞いています", "声が検出されません。マイクを確認してください", "recording", False, True)
        if heard_voice:
            return Presentation("声を検出しました", "話し終えたら自動で認識します", "recording", False, True)
    return PRESENTATIONS[stage]


def meter_level(rms: float, threshold: float) -> int:
    """0..5 indicator segments; only based on the already sampled mic energy."""
    if threshold <= 0 or rms <= 0:
        return 0
    ratio = rms / threshold
    if ratio < 0.35: return 0
    if ratio < 0.7: return 1
    if ratio < 1.0: return 2
    if ratio < 1.6: return 3
    if ratio < 2.8: return 4
    return 5


def friendly_error(err: Exception) -> str:
    """Show a useful short message without echoing sensitive request payloads."""
    message = str(err).lower()
    if "connection refused" in message or "winerror 10061" in message or "localhost:11434" in message:
        return "Ollamaに接続できません。Ollamaが起動しているか確認してください。"
    if "401" in message or "invalid api key" in message:
        return "APIキーが無効です。設定を確認してください。"
    if "429" in message or "quota" in message or "rate limit" in message:
        return "AIの無料枠の上限に達しました。Ollamaへ切り替えるか、時間をおいてください。"
    if "no default input device" in message or "error querying device" in message:
        return "マイクが見つかりません。Windowsの入力デバイスを確認してください。"
    # Exception class names are stable and avoid leaking cloud payloads.
    return f"処理に失敗しました（{type(err).__name__}）。操作ログを確認してください。"