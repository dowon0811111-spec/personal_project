"""리뷰 총평과 대표 아티스트를 이미지 카드(PNG)로 만든다.

한글을 그리려면 한글 글꼴이 필요하다. 환경 변수 TASTE_CRITIC_FONT(굵은 글꼴은 TASTE_CRITIC_FONT_BOLD)로
지정하거나, 아래 후보 경로에서 찾는다. 리눅스 서버에서는 packages.txt로 fonts-nanum을 설치한다.
Last.fm 데이터를 쓰므로 카드에 출처를 표기한다.
"""

import io
import os
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1080, 1350
MARGIN = 90
TOP_COLOR = (24, 20, 56)
BOTTOM_COLOR = (92, 48, 120)
TEXT_COLOR = (255, 255, 255)
MUTED_COLOR = (200, 190, 225)
ACCENT_COLOR = (255, 196, 92)
MAX_HEADLINE_LINES = 4
MAX_ARTISTS = 3

_REGULAR_FONTS = (
    r"C:\Windows\Fonts\malgun.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
)
_BOLD_FONTS = (
    r"C:\Windows\Fonts\malgunbd.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
)


class CardFontError(RuntimeError):
    """사용할 수 있는 한글 글꼴을 찾지 못했을 때 발생한다."""


class _Measurable(Protocol):
    def getlength(self, text: str) -> float: ...


def find_font_path(bold: bool = False) -> str:
    env_name = "TASTE_CRITIC_FONT_BOLD" if bold else "TASTE_CRITIC_FONT"
    candidates = [os.getenv(env_name, ""), os.getenv("TASTE_CRITIC_FONT", "")] if bold else [os.getenv(env_name, "")]
    for path in [*candidates, *(_BOLD_FONTS if bold else _REGULAR_FONTS)]:
        if path and Path(path).is_file():
            return path
    raise CardFontError(
        "공유 카드에 쓸 한글 글꼴을 찾지 못했습니다. TASTE_CRITIC_FONT에 글꼴 파일 경로를 지정하거나, "
        "리눅스에서는 fonts-nanum을 설치하세요."
    )


def wrap_text(text: str, font: _Measurable, max_width: int, max_lines: int) -> list[str]:
    """단어 단위로 줄을 나누고, 한 줄보다 긴 단어만 글자 단위로 자른다. 줄 수를 넘으면 마지막 줄을 '…'으로 줄인다."""
    lines: list[str] = []
    current = ""
    for word in text.replace("\n", " ").split():
        candidate = f"{current} {word}" if current else word
        if font.getlength(candidate) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        for char in word:
            if current and font.getlength(current + char) > max_width:
                lines.append(current)
                current = char
            else:
                current += char
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and font.getlength(last + "…") > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def _truncate(text: str, font: _Measurable, max_width: int) -> str:
    return text if font.getlength(text) <= max_width else wrap_text(text, font, max_width, 1)[0]


def _gradient() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        ratio = y / (HEIGHT - 1)
        color = tuple(round(top + (bottom - top) * ratio) for top, bottom in zip(TOP_COLOR, BOTTOM_COLOR))
        draw.line([(0, y), (WIDTH, y)], fill=color)
    return image


def render_share_card(
    headline: str, score: int, score_max: int, artists: list[str], period_label: str
) -> bytes:
    regular, bold = find_font_path(), find_font_path(bold=True)
    font = lambda path, size: ImageFont.truetype(path, size)  # noqa: E731
    image = _gradient()
    draw = ImageDraw.Draw(image)
    inner_width = WIDTH - 2 * MARGIN

    draw.text((MARGIN, 90), "TASTE CRITIC", font=font(bold, 34), fill=ACCENT_COLOR)
    draw.text((MARGIN, 140), f"취향 평론가 · {period_label}", font=font(regular, 34), fill=MUTED_COLOR)

    headline_font = font(bold, 68)
    y = 270
    for line in wrap_text(headline, headline_font, inner_width, MAX_HEADLINE_LINES):
        draw.text((MARGIN, y), line, font=headline_font, fill=TEXT_COLOR)
        y += 92

    score_font, max_font = font(bold, 230), font(bold, 70)
    score_text = str(score)
    draw.text((MARGIN, 640), score_text, font=score_font, fill=ACCENT_COLOR)
    draw.text(
        (MARGIN + score_font.getlength(score_text) + 16, 800), f"/ {score_max}", font=max_font, fill=MUTED_COLOR
    )

    if artists:
        draw.text((MARGIN, 950), "대표 아티스트", font=font(regular, 34), fill=MUTED_COLOR)
        artist_font = font(bold, 50)
        for rank, name in enumerate(artists[:MAX_ARTISTS], start=1):
            draw.text((MARGIN, 1005 + (rank - 1) * 70), f"{rank}. {_truncate(name, artist_font, inner_width - 60)}", font=artist_font, fill=TEXT_COLOR)

    draw.text((MARGIN, HEIGHT - 80), "Data: Last.fm  ·  Reviewed by Claude", font=font(regular, 28), fill=MUTED_COLOR)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
