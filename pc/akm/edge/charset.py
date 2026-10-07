"""文字認識で見分ける文字の一覧 (Pillow などの重い依存なしで読み込めるよう分けてある)。"""
CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz()/:,.-+%[]!?'"
JUNK = len(CHARS)  # 文字ではない (背景の模様など)
