"""Shared fakes: a text model that answers in the director's JSON shape, and an image model."""
import io
import json
from PIL import Image
from engine import providers
from engine.core import FilmError
from engine.imagegen import ImageProvider

WORLD = {"story": "한 여자가 오래된 부엌에서 옛 기억을 떠올리고 마지막에 문을 열고 나간다.",
         "style": {"visual_style": {"medium": "2D ink animation", "background": "warm gray paper"},
                   "palette": {"paper": "#ECEAE4"}, "rules": ["no text", "restrained emotion"]},
         "characters": [{"id": "CHAR_A", "name": "Mina", "look": "adult woman, short black hair, red coat, round glasses",
                         "behavior": "calm", "invariants": {"coat": "red"}}],
         "locations": [{"id": "LOC_KITCHEN", "description": "narrow kitchen with a green table and a window"}]}


class FakeText:
    def __init__(self, world=None, mode="LIMITED_MOTION"):
        self.provider = providers.get("groq")
        self.model = "fake-model"
        self.world, self.mode = world or WORLD, mode
        self.calls, self.fail_at = [], None

    def complete(self, system, user, max_tokens=8000):
        self.calls.append((system, user))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise FilmError("network down")
        if "exactly the shots" in system:
            ids = [s["id"] for s in json.loads(user)["shots"]]
            rows = [{"id": i, "description": f"Mina stands at the table in shot {i}, slowly turning her head",
                     "characters": ["CHAR_A"], "locations": ["LOC_KITCHEN"], "composition": "medium shot, table in the foreground",
                     "camera": {"type": "locked", "movement": "none"},
                     "motion": {"complexity": "low", "instruction": "She turns her head toward the window over two seconds"},
                     "render_mode": self.mode} for i in ids]
            return "Here you go:\n```json\n" + json.dumps({"shots": rows}) + "\n```"
        return json.dumps(self.world)


class FakeImages(ImageProvider):
    def __init__(self, unit=0.0, refs=False):
        self.id, self.model, self.unit_usd = "fake_image", "fake-1", unit
        self.supports_references, self.max_references = refs, 3
        self.calls = 0

    def generate(self, prompt, references, aspect):
        self.calls += 1
        buffer = io.BytesIO()
        Image.new("RGB", (160, 90), "teal").save(buffer, format="PNG")
        return buffer.getvalue()
