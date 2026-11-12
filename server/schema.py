from typing import Annotated, Literal
from urllib.parse import urlparse

import chess
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


Text = Annotated[str, Field(max_length=12000)]
ShortText = Annotated[str, Field(max_length=500)]


class Metadata(Model):
    source: ShortText | None = None
    author: ShortText | None = None
    date: ShortText | None = None
    score: int | None = None
    commentCount: int | None = Field(default=None, ge=0)


class Comment(Model):
    author: ShortText | None = None
    text: Text
    replies: list["Comment"] = Field(default_factory=list, max_length=30)


class ListItem(Model):
    text: Text
    checkbox: bool = False
    checked: bool = False
    ordered: bool = False
    items: list["ListItem | Text"] = Field(default_factory=list, max_length=100)


class ImageFields(Model):
    assetId: str = Field(pattern=r"^[a-f0-9]{32}$")
    caption: ShortText | None = None
    maxHeight: int = Field(default=800, ge=32, le=2000)


class Masthead(Model):
    type: Literal["masthead"]
    title: ShortText
    date: ShortText | None = None
    subtitle: ShortText | None = None


class Section(Model):
    type: Literal["section"]
    title: ShortText
    size: Literal["small", "medium", "large"] = "medium"


class Headline(Model):
    type: Literal["headline"]
    text: ShortText
    size: Literal["small", "medium", "large"] = "medium"


class Paragraph(Model):
    type: Literal["text"]
    text: Text


class MetadataComponent(Metadata):
    type: Literal["metadata"]


class Comments(Model):
    type: Literal["comments"]
    items: list[Comment] = Field(max_length=30)


class ListComponent(Model):
    type: Literal["list"]
    ordered: bool = False
    items: list[ListItem | Text] = Field(max_length=100)


class KeyValueItem(Model):
    label: ShortText
    value: ShortText


class KeyValue(Model):
    type: Literal["keyValue"]
    items: list[KeyValueItem] = Field(max_length=100)


class TableColumn(Model):
    label: ShortText
    align: Literal["left", "center", "right"] = "left"
    width: float = Field(default=1, gt=0, le=20, allow_inf_nan=False)


Cell = Annotated[str, Field(max_length=1000)] | int | Annotated[float, Field(allow_inf_nan=False)] | None


class BoxScore(Model):
    title: ShortText | None = None
    columns: list[TableColumn | ShortText] = Field(min_length=1, max_length=6)
    rows: list[list[Cell]] = Field(min_length=1, max_length=100)
    caption: ShortText | None = None

    @model_validator(mode="after")
    def matching_rows(self):
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("Each table row must match the number of columns")
        weights = [column.width if isinstance(column, TableColumn) else 1 for column in self.columns]
        if any(528 * weight / sum(weights) < 52 for weight in weights):
            raise ValueError("Table columns are too narrow; use more balanced widths")
        return self


class Table(BoxScore):
    type: Literal["table"]


class ScoreTeam(Model):
    name: str = Field(min_length=1, max_length=100)
    score: int | None = Field(default=None, ge=0, le=999)


class ScoreGame(Model):
    league: ShortText | None = None
    status: Literal["final", "live", "scheduled", "postponed"] = "final"
    detail: ShortText | None = None
    away: ScoreTeam
    home: ScoreTeam
    note: ShortText | None = None
    boxScores: list[BoxScore] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def final_scores(self):
        if self.status == "final" and (self.away.score is None or self.home.score is None):
            raise ValueError("Final games need both scores")
        return self


class SportsScores(Model):
    type: Literal["sportsScores"]
    title: ShortText | None = None
    games: list[ScoreGame] = Field(min_length=1, max_length=30)


class ImageComponent(ImageFields):
    type: Literal["image"]


class QR(Model):
    type: Literal["qr"]
    url: str = Field(max_length=300)
    label: ShortText | None = None
    size: int = Field(default=200, ge=100, le=528)

    @field_validator("url")
    @classmethod
    def http_url(cls, value):
        parsed = urlparse(value)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise ValueError("Use an absolute HTTP or HTTPS URL")
        return value


class Divider(Model):
    type: Literal["divider"]
    style: Literal["solid", "double", "dashed"] = "solid"


class Arrow(Model):
    from_: str = Field(alias="from", pattern=r"^[a-h][1-8]$")
    to: str = Field(pattern=r"^[a-h][1-8]$")

    @model_validator(mode="after")
    def distinct_squares(self):
        if self.from_ == self.to:
            raise ValueError("Arrow endpoints must differ")
        return self


class Chessboard(Model):
    type: Literal["chessboard"]
    fen: str = Field(max_length=150)
    orientation: Literal["white", "black"] = "white"
    title: ShortText | None = None
    prompt: ShortText | None = None
    caption: ShortText | None = None
    arrows: list[Arrow] = Field(default_factory=list, max_length=20)

    @field_validator("fen")
    @classmethod
    def valid_position(cls, value):
        if not chess.Board(value).is_valid():
            raise ValueError("Invalid chess position")
        return value


class Article(Metadata):
    type: Literal["article"]
    title: ShortText
    description: Text | None = None
    body: Text | None = None
    image: ImageFields | None = None
    comments: list[Comment] = Field(default_factory=list, max_length=30)
    url: str | None = Field(default=None, max_length=300)
    components: list["Component"] = Field(default_factory=list, max_length=50)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        return QR.http_url(value) if value is not None else None


Component = Annotated[
    Masthead | Section | Headline | Paragraph | MetadataComponent | Comments | ListComponent
    | KeyValue | Table | SportsScores | ImageComponent | QR | Divider | Chessboard | Article,
    Field(discriminator="type"),
]
Article.model_rebuild()


class Theme(Model):
    spacing: int = Field(default=16, ge=0, le=60)
    lineGap: int = Field(default=8, ge=0, le=30)
    bodySize: int = Field(default=26, ge=18, le=48)
    headingSize: int = Field(default=36, ge=20, le=64)
    titleSize: int = Field(default=44, ge=24, le=72)
    smallSize: int = Field(default=20, ge=16, le=32)
    dither: bool = True


class Document(Model):
    schemaVersion: Literal[1] = 1
    title: ShortText | None = None
    date: ShortText | None = None
    subtitle: ShortText | None = None
    theme: Theme = Field(default_factory=Theme)
    components: list[Component] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def table_layout(self):
        def visit(components):
            for component in components:
                if isinstance(component, BoxScore):
                    weights = [column.width if isinstance(column, TableColumn) else 1 for column in component.columns]
                    edges = [round(528 * sum(weights[:index]) / sum(weights)) for index in range(len(weights) + 1)]
                    if any(edges[index + 1] - edges[index] - 12 < 2 * self.theme.smallSize for index in range(len(weights))):
                        raise ValueError("Table columns are too narrow for theme.smallSize")
                elif isinstance(component, Article):
                    visit(component.components)
                elif isinstance(component, SportsScores):
                    for game in component.games:
                        visit(game.boxScores)

        visit(self.components)
        return self

    @model_validator(mode="before")
    @classmethod
    def bound_complexity(cls, value):
        count = 0

        def visit(node, depth=0):
            nonlocal count
            count += 1
            if depth > 18 or count > 6000:
                raise ValueError("Document is too deeply nested or too large")
            if isinstance(node, dict):
                for child in node.values():
                    visit(child, depth + 1)
            elif isinstance(node, list):
                for child in node:
                    visit(child, depth + 1)

        visit(value)
        return value


class ImmediateEdition(Model):
    document: Document
    expiresAt: AwareDatetime | None = None
    idempotencyKey: str = Field(min_length=1, max_length=128)


class CreateEdition(ImmediateEdition):
    printAt: AwareDatetime

    @model_validator(mode="after")
    def expiry_after_print(self):
        if self.expiresAt is not None and self.expiresAt <= self.printAt:
            raise ValueError("expiresAt must be after printAt")
        return self


class ClaimUpdate(Model):
    claimToken: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    error: str | None = Field(default=None, max_length=2000)


class Heartbeat(Model):
    softwareVersion: ShortText | None = None
    error: str | None = Field(default=None, max_length=2000)


def asset_ids(node):
    if isinstance(node, dict):
        if "assetId" in node:
            yield node["assetId"]
        for value in node.values():
            yield from asset_ids(value)
    elif isinstance(node, list):
        for value in node:
            yield from asset_ids(value)
