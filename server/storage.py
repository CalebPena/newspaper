import io
import os
import warnings
from uuid import uuid4

import boto3
from botocore.config import Config
from PIL import Image, ImageOps, UnidentifiedImageError

from server.database import Asset


class ImageStorage:
    def __init__(self):
        self.bucket = os.environ["S3_BUCKET"]
        self.client = boto3.client(
            "s3",
            endpoint_url=os.environ["S3_ENDPOINT_URL"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
            region_name=os.getenv("S3_REGION", "auto"),
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": os.getenv("S3_ADDRESSING_STYLE", "virtual")},
                connect_timeout=10,
                read_timeout=30,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def upload(self, data):
        if not data or len(data) > 8 * 1024 * 1024:
            raise ValueError("Image must be between 1 byte and 8 MiB")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as source:
                    if source.width * source.height > 16_000_000:
                        raise ValueError("Image exceeds 16 million pixels")
                    source = ImageOps.exif_transpose(source).convert("RGBA")
                    background = Image.new("RGBA", source.size, "white")
                    background.alpha_composite(source)
                    image = background.convert("L")
                    image.thumbnail((528, 2000))
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise ValueError("Unsupported or invalid image") from error
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        data = output.getvalue()
        asset_id = uuid4().hex
        key = f"images/{asset_id}.png"
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType="image/png")
        return Asset(id=asset_id, object_key=key, width=image.width, height=image.height, size=len(data))

    def download_url(self, asset):
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": asset.object_key}, ExpiresIn=900
        )
