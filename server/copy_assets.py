# 从参考项目复制风格素材。运行：.venv\Scripts\python.exe server\copy_assets.py
import os
import shutil

SRC = r"F:\mysite\rednote_format\rednote_format"
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

PAIRS = [
    ("imgs/bg/1.jpg", "backgrounds/autumn.jpg"),
    ("imgs/bg/2.jpg", "backgrounds/sketch.jpg"),
    ("imgs/bg/7.jpg", "backgrounds/cartoon.jpg"),
    ("imgs/bg/6.jpg", "backgrounds/literary.jpg"),
    ("imgs/bg/5.jpg", "backgrounds/beige.jpg"),
    ("fonts/优设标题黑.ttf", "fonts/title-black.ttf"),
    ("fonts/文津宋体.ttf", "fonts/song.ttf"),
    ("fonts/寒蝉黑宋.otf", "fonts/hanchan.otf"),
    ("fonts/屏显臻宋.ttf", "fonts/screen-song.ttf"),
    ("fonts/975圆体.ttf", "fonts/round.ttf"),
    ("fonts/手书体.ttf", "fonts/handwrite.ttf"),
    ("fonts/朱雀仿宋.ttf", "fonts/fangsong.ttf"),
    ("fonts/极影毁片和圆.ttf", "fonts/jiying.ttf"),
]


def main():
    for src, dst in PAIRS:
        target = os.path.join(STATIC, dst.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(os.path.join(SRC, src.replace("/", os.sep)), target)
        print("copied", dst)
    print("done")


if __name__ == "__main__":
    main()
