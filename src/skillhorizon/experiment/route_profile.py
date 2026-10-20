"""The explicitly audited OpenLux declaration, not permission to generate images."""

IMAGE_DECLARATION = {
    "background": "auto", "model": "gpt-image-2-codex", "moderation": "auto",
    "n": 1, "output_compression": 100, "output_format": "png",
    "quality": "auto", "size": "auto", "type": "image_generation",
}

ROUTE_POLICY = "openlux_declared_image_serial"
BATCH_ROUTE_POLICY = "openlux_declared_image_ordered_batch"
