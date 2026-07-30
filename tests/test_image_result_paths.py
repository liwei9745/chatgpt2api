import base64

from services.image_storage_service import StoredImage
from services.protocol import conversation


PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4"
    "z8DwHwAFgAI/ScL4yAAAAABJRU5ErkJggg=="
)


def test_image_result_exposes_the_stored_relative_path_for_studio_push(monkeypatch):
    stored = StoredImage(
        rel="2026/07/30/generated.png",
        url="http://sender.example/images/2026/07/30/generated.png",
        storage="local",
        size=70,
    )
    monkeypatch.setattr(conversation, "save_image_bytes", lambda *_args, **_kwargs: stored)

    result = conversation.format_image_result(
        [{"b64_json": PNG_1X1, "revised_prompt": "test image"}],
        "fallback prompt",
        "url",
        "http://sender.example",
    )

    asset = result["data"][0]
    assert asset["url"] == stored.url
    assert asset["path"] == stored.rel
    assert asset["revised_prompt"] == "test image"
    assert base64.b64decode(PNG_1X1)
