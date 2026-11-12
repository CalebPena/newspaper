import argparse
import json
import math
import struct
from pathlib import Path

import chess
import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageOps

DEFAULT_THEME = {
    "width": 576,
    "margin": 24,
    "spacing": 16,
    "lineGap": 8,
    "bodySize": 26,
    "headingSize": 36,
    "titleSize": 44,
    "smallSize": 20,
    "fontDirectory": None,
    "dither": True,
}


class NewspaperRenderer:
    def __init__(self, theme=None, asset_directory="."):
        self.theme = DEFAULT_THEME | (theme or {})
        self.assets = Path(asset_directory)
        self.width = self.theme["width"]
        self.margin = self.theme["margin"]
        if self.width <= 2 * self.margin or self.width % 8:
            raise ValueError("Width must be divisible by 8 and greater than twice the margin")
        self.fonts = {}
        self.parts = []
        directories = [Path(self.theme["fontDirectory"])] if self.theme["fontDirectory"] else [
            Path("/usr/share/fonts/truetype/liberation"),
            Path("/usr/share/fonts/truetype/liberation2"),
            Path("/usr/share/fonts/liberation"),
        ]
        self.font_directory = next((directory for directory in directories if (directory / "LiberationSerif-Regular.ttf").exists()), None)
        if self.font_directory is None:
            raise ValueError("Install Liberation fonts or set theme.fontDirectory")

    def font(self, size, bold=False):
        key = (size, bold)
        if key not in self.fonts:
            name = f"LiberationSerif-{'Bold' if bold else 'Regular'}.ttf"
            self.fonts[key] = ImageFont.truetype(str(self.font_directory / name), size)
        return self.fonts[key]

    def text_image(self, text, width, size=None, bold=False, align="left"):
        if width < (size or self.theme["bodySize"]) * 2:
            raise ValueError("Nesting leaves too little room for text")
        font = self.font(size or self.theme["bodySize"], bold)
        measure = ImageDraw.Draw(Image.new("L", (1, 1)))
        lines = []
        for paragraph in str(text).split("\n"):
            line = ""
            for word in paragraph.split():
                candidate = f"{line}{word}"
                if line and measure.textlength(candidate, font=font) > width:
                    lines.append(line)
                    line = ""
                for character in word:
                    if line and measure.textlength(line + character, font=font) > width:
                        lines.append(line)
                        line = ""
                    line += character
                line += " "
            lines.append(line.rstrip())
        ascent, descent = font.getmetrics()
        step = ascent + descent + self.theme["lineGap"]
        result = Image.new("L", (width, max(1, len(lines)) * step), 255)
        draw = ImageDraw.Draw(result)
        for index, line in enumerate(lines):
            length = draw.textlength(line, font=font)
            x = {"left": 0, "center": (width - length) / 2, "right": width - length}[align]
            draw.text((x, index * step), line, font=font, fill=0, anchor="lt")
        return result

    def add(self, image, indent=0):
        row = Image.new("L", (self.width, image.height + self.theme["spacing"]), 255)
        row.paste(image, (self.margin + indent, 0))
        self.parts.append(row)

    def text(self, text, indent=0, **style):
        width = self.width - 2 * self.margin - indent
        if width < self.theme["bodySize"] * 2:
            raise ValueError("Nesting leaves too little room for text")
        self.add(self.text_image(text, width, **style), indent)

    def metadata(self, config, indent=0):
        values = [str(config[key]) for key in ("source", "author", "date") if config.get(key)]
        if "score" in config:
            values.append(f"Score: {config['score']}")
        if "commentCount" in config:
            values.append(f"{config['commentCount']} comments")
        if values:
            self.text(" | ".join(values), indent, size=self.theme["smallSize"])

    def comments(self, items, indent=0):
        for item in items:
            if item.get("author"):
                self.text(item["author"], indent, size=self.theme["smallSize"], bold=True)
            self.text(item["text"], indent)
            self.comments(item.get("replies", []), indent + 24)

    def listing(self, config, indent=0):
        for index, item in enumerate(config["items"], 1):
            item = {"text": item} if isinstance(item, str) else item
            checkbox = item.get("checkbox", False)
            prefix = f"{index}." if config.get("ordered", False) else "•"
            if checkbox:
                prefix = ""
            body = self.text_image(item["text"], self.width - 2 * self.margin - indent - 40)
            row = Image.new("L", (body.width + 40, max(30, body.height)), 255)
            row.paste(body, (40, 0))
            draw = ImageDraw.Draw(row)
            if checkbox:
                draw.rectangle((1, 3, 23, 25), outline=0, width=2)
                if item.get("checked", False):
                    draw.line((5, 14, 11, 21, 21, 7), fill=0, width=3)
            else:
                draw.text((0, 0), prefix, font=self.font(self.theme["bodySize"]), fill=0, anchor="lt")
            self.add(row, indent)
            if item.get("items"):
                if self.width - 2 * self.margin - indent - 72 < 80:
                    raise ValueError("List nesting exceeds printable width")
                self.listing({"items": item["items"], "ordered": item.get("ordered", False)}, indent + 32)

    def picture(self, config):
        with Image.open(self.assets / config["src"]) as source:
            source = ImageOps.exif_transpose(source).convert("RGBA")
            background = Image.new("RGBA", source.size, "white")
            background.alpha_composite(source)
            image = background.convert("L")
        image.thumbnail((self.width - 2 * self.margin, config.get("maxHeight", 800)))
        self.add(image)
        if config.get("caption"):
            self.text(config["caption"], size=self.theme["smallSize"])

    def table_image(self, columns, rows, header=True, bold_rows=(), compact=False, vertical_lines=False):
        columns = [{"label": column} if isinstance(column, str) else column for column in columns]
        if not 1 <= len(columns) <= 16:
            raise ValueError("Tables need between one and sixteen columns")
        available = self.width - 2 * self.margin
        weights = [column.get("width", 1) for column in columns]
        if any(weight <= 0 for weight in weights):
            raise ValueError("Column widths must be positive relative weights")
        edges = [round(available * sum(weights[:index]) / sum(weights)) for index in range(len(columns) + 1)]
        padding = 2 if compact else 6
        size = min(16, self.theme["smallSize"]) if compact else self.theme["smallSize"]
        parts = []

        def row_image(cells, bold=False):
            if len(cells) != len(columns):
                raise ValueError("Each table row must match the number of columns")
            rendered = [
                self.text_image(
                    "—" if value is None else str(value),
                    edges[index + 1] - edges[index] - 2 * padding,
                    size=size, bold=bold,
                    align=columns[index].get("align", "left"),
                )
                for index, value in enumerate(cells)
            ]
            image = Image.new("L", (available, max(cell.height for cell in rendered) + 2 * padding), 255)
            for index, cell in enumerate(rendered):
                image.paste(cell, (edges[index] + padding, padding))
            ImageDraw.Draw(image).line((0, image.height - 1, available - 1, image.height - 1), fill=0, width=2 if bold else 1)
            return image

        if header:
            parts.append(row_image([column["label"] for column in columns], bold=True))
        for index, row in enumerate(rows):
            parts.append(row_image(row, bold=index in bold_rows))
        image = Image.new("L", (available, max(1, sum(part.height for part in parts))), 255)
        top = 0
        for part in parts:
            image.paste(part, (0, top))
            top += part.height
        if vertical_lines:
            draw = ImageDraw.Draw(image)
            draw.line((0, 0, available - 1, 0), fill=0)
            for edge in edges:
                x = min(edge, available - 1)
                draw.line((x, 0, x, image.height - 1), fill=0)
        return image

    def table(self, config):
        if config.get("title"):
            self.text(config["title"], size=self.theme["headingSize"], bold=True)
        self.add(self.table_image(
            config["columns"], config["rows"],
            compact=config.get("compact", False), vertical_lines=config.get("verticalLines", False),
        ))
        if config.get("caption"):
            self.text(config["caption"], size=self.theme["smallSize"])

    def sports_scores(self, config):
        if config.get("title"):
            self.text(config["title"], size=self.theme["headingSize"], bold=True)
        labels = {"final": "FINAL", "live": "LIVE", "scheduled": "UPCOMING", "postponed": "POSTPONED"}
        for game in config["games"]:
            status = game.get("status", "final")
            label = " | ".join(str(value) for value in (game.get("league"), labels[status], game.get("detail")) if value)
            self.text(label, size=self.theme["smallSize"], bold=True)
            teams = [game["away"], game["home"]]
            scores = [team.get("score") for team in teams]
            if status == "final" and any(score is None for score in scores):
                raise ValueError("Final games need both scores")
            winners = []
            if status == "final" and scores[0] != scores[1]:
                winners = [0 if scores[0] > scores[1] else 1]
            rows = [[team["name"], team.get("score")] for team in teams]
            self.add(self.table_image(
                [{"label": "Team", "width": 4}, {"label": "Score", "width": 1, "align": "right"}],
                rows, header=False, bold_rows=winners,
            ))
            if game.get("note"):
                self.text(game["note"], size=self.theme["smallSize"])
            for box_score in game.get("boxScores", []):
                self.table(box_score)

    def qr(self, config):
        code = qrcode.QRCode(border=4, box_size=1)
        code.add_data(config["url"])
        code.make(fit=True)
        image = code.make_image().convert("L")
        maximum = min(config.get("size", 200), self.width - 2 * self.margin)
        scale = maximum // image.width
        if scale < 2:
            raise ValueError("QR code is too dense; use a shorter URL or larger size")
        image = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
        self.add(image)
        if config.get("label"):
            self.text(config["label"], size=self.theme["smallSize"])

    def chess_arrow(self, draw, arrow, square, gutter, orientation):
        def center(name):
            position = chess.parse_square(name)
            file, rank = chess.square_file(position), chess.square_rank(position)
            column, row = (file, 7 - rank) if orientation == "white" else (7 - file, rank)
            return gutter + (column + 0.5) * square, (row + 0.5) * square

        start = center(arrow["from"])
        end = center(arrow["to"])
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length == 0:
            raise ValueError("Chess arrow endpoints must be different squares")
        ux, uy = dx / length, dy / length
        head_length, head_width = square * 0.3, square * 0.16
        base = (end[0] - ux * head_length, end[1] - uy * head_length)
        head = [end, (base[0] - uy * head_width, base[1] + ux * head_width), (base[0] + uy * head_width, base[1] - ux * head_width)]
        width = max(3, round(square * 0.08))
        draw.line((start, base), fill=255, width=width + 4)
        draw.polygon(head, fill=255)
        draw.line(head + [head[0]], fill=255, width=5)
        draw.line((start, base), fill=0, width=width)
        draw.polygon(head, fill=0)

    def chessboard(self, config):
        board = chess.Board(config["fen"])
        if not board.is_valid():
            raise ValueError("Chess position is not valid")
        orientation = config.get("orientation", "white")
        if orientation not in ("white", "black"):
            raise ValueError("Chess orientation must be white or black")
        if config.get("title"):
            self.text(config["title"], size=self.theme["headingSize"], bold=True)
        self.text(config.get("prompt", f"{'White' if board.turn else 'Black'} to move"))
        available = self.width - 2 * self.margin
        gutter = 28
        square = (available - gutter) // 8
        font_paths = [
            Path(config["pieceFont"])
        ] if config.get("pieceFont") else [
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
            Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/noto/NotoSansSymbols2-Regular.ttf"),
        ]
        piece_font_path = next((path for path in font_paths if path.exists()), None)
        if piece_font_path is None:
            raise ValueError("Install DejaVu Sans or Noto Sans Symbols 2, or set chessboard.pieceFont")
        piece_font = ImageFont.truetype(str(piece_font_path), int(square * 0.85))
        label_font = self.font(self.theme["smallSize"])
        image = Image.new("L", (available, 8 * square + gutter), 255)
        draw = ImageDraw.Draw(image)
        files = list(range(8)) if orientation == "white" else list(reversed(range(8)))
        ranks = list(reversed(range(8))) if orientation == "white" else list(range(8))
        for row, rank in enumerate(ranks):
            draw.text((gutter / 2, row * square + square / 2), str(rank + 1), font=label_font, fill=0, anchor="mm")
            for column, file in enumerate(files):
                x, y = gutter + column * square, row * square
                shade = 215 if (file + rank) % 2 == 0 else 255
                draw.rectangle((x, y, x + square - 1, y + square - 1), fill=shade)
                piece = board.piece_at(chess.square(file, rank))
                if piece:
                    draw.text((x + square / 2, y + square / 2), piece.unicode_symbol(), font=piece_font, fill=0, anchor="mm", stroke_width=1, stroke_fill=255)
        for arrow in config.get("arrows", []):
            self.chess_arrow(draw, arrow, square, gutter, orientation)
        for column, file in enumerate(files):
            draw.text((gutter + column * square + square / 2, 8 * square + gutter / 2), chess.FILE_NAMES[file], font=label_font, fill=0, anchor="mm")
        draw.rectangle((gutter, 0, gutter + square * 8 - 1, square * 8 - 1), outline=0, width=2)
        self.add(image)
        if config.get("caption"):
            self.text(config["caption"], size=self.theme["smallSize"])

    def component(self, config):
        kind = config["type"]
        if kind == "masthead":
            self.text(config["title"], size=self.theme["titleSize"], bold=True, align="center")
            for key in ("date", "subtitle"):
                if config.get(key):
                    self.text(config[key], size=self.theme["smallSize"], align="center")
            self.component({"type": "divider", "style": "double"})
        elif kind in ("section", "headline"):
            sizes = {"small": self.theme["bodySize"], "medium": self.theme["headingSize"], "large": self.theme["titleSize"]}
            self.text(config.get("title", config.get("text", "")), size=sizes[config.get("size", "medium")], bold=True)
        elif kind == "text":
            self.text(config["text"])
        elif kind == "metadata":
            self.metadata(config)
        elif kind == "comments":
            self.comments(config["items"])
        elif kind == "list":
            self.listing(config)
        elif kind == "keyValue":
            for item in config["items"]:
                self.text(f"{item['label']}: {item['value']}")
        elif kind == "image":
            self.picture(config)
        elif kind == "table":
            self.table(config)
        elif kind == "sportsScores":
            self.sports_scores(config)
        elif kind == "qr":
            self.qr(config)
        elif kind == "chessboard":
            self.chessboard(config)
        elif kind == "divider":
            image = Image.new("L", (self.width - 2 * self.margin, 10), 255)
            draw = ImageDraw.Draw(image)
            style = config.get("style", "solid")
            if style not in ("solid", "double", "dashed"):
                raise ValueError(f"Unknown divider style: {style}")
            for y in ([1, 7] if style == "double" else [4]):
                if style == "dashed":
                    for x in range(0, image.width, 20):
                        draw.line((x, y, min(x + 10, image.width - 1), y), fill=0, width=2)
                else:
                    draw.line((0, y, image.width - 1, y), fill=0, width=2)
            self.add(image)
        elif kind == "article":
            self.component({"type": "headline", "text": config["title"]})
            self.metadata(config)
            for key in ("description", "body"):
                if config.get(key):
                    self.text(config[key])
            if config.get("image"):
                self.picture(config["image"])
            self.comments(config.get("comments", []))
            if config.get("url"):
                self.qr({"url": config["url"], "label": "Read the original"})
            for child in config.get("components", []):
                self.component(child)
            self.component({"type": "divider"})
        else:
            raise ValueError(f"Unknown component type: {kind}")

    def render(self, config):
        self.parts = []
        if config.get("title"):
            self.component({"type": "masthead", **{key: config[key] for key in ("title", "date", "subtitle") if key in config}})
        for component in config["components"]:
            self.component(component)
        result = Image.new("L", (self.width, 2 * self.margin + sum(part.height for part in self.parts)), 255)
        y = self.margin
        for part in self.parts:
            result.paste(part, (0, y))
            y += part.height
        if self.theme["dither"]:
            return result.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
        return result.point(lambda value: 255 if value >= 160 else 0, mode="1")


def save_receipt(image, path):
    with Path(path).open("wb") as output:
        output.write(b"\x1b@")
        for top in range(0, image.height, 256):
            strip = image.crop((0, top, image.width, min(top + 256, image.height)))
            output.write(b"\x1dv0\x00" + struct.pack("<HH", image.width // 8, strip.height))
            output.write(bytes(value ^ 255 for value in strip.tobytes()))
        output.write(b"\x1bd\x03\x1dVB\x00")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("--output", type=Path, default=Path("edition.png"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    renderer = NewspaperRenderer(config.get("theme"), args.config.resolve().parent)
    image = renderer.render(config)
    image.save(args.output)
    save_receipt(image, args.output.with_suffix(".escpos"))
    print(f"Rendered {image.width} x {image.height}: {args.output}")


if __name__ == "__main__":
    main()
