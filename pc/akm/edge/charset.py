"""文字認識で見分ける文字の一覧 (Pillow などの重い依存なしで読み込めるよう分けてある)。"""
CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz()/:,.-+%[]!?'"
JUNK = len(CHARS)  # 文字ではない (背景の模様など)

# 欄ごとに出てくる文字 (OcrReader.read の allowed に渡す。ほかの文字と読んだ切り出しは背景の点として捨てる)
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
NAME_CHARS = _LETTERS + "-'"                  # モンスター名 (Sapi-Unus, Bloody wolf …)
LOC_CHARS = _LETTERS + "0123456789()," + "'-"  # 現在地 "Lost Tower (12, 34)"
DIGITS = "0123456789"                          # レベル
