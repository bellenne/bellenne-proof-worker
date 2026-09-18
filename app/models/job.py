from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.preset import Preset


ProofVariant = Literal[
    "fragment_90x30",
    "fragment_60x30",
    "two_fragments_30x30",
    "fragment_30x30_color",
    "fragment_30x30",
    "thumbnail",
]


class ProofFragment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=255)
    position: int = Field(ge=0, le=2)
    proof_variant: Literal["fragment_30x30", "fragment_30x30_color"]
    brightness_direction: Literal["add", "subtract"] | None = None
    brightness_percent: float | None = Field(default=None, gt=0, le=100)

    @model_validator(mode="after")
    def validate_brightness(self):
        if self.proof_variant == "fragment_30x30_color":
            if self.brightness_direction not in {"add", "subtract"}:
                raise ValueError("Brightness direction is required for a corrected fragment")
            if self.brightness_percent is None:
                raise ValueError("Brightness percent is required for a corrected fragment")
        elif self.brightness_direction is not None or self.brightness_percent is not None:
            raise ValueError("Ordinary fragments cannot contain brightness settings")
        return self


class ProofItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=255)
    position: int = Field(ge=0, le=99)
    layout_number: int = Field(ge=1, le=999_999)
    proof_variant: ProofVariant
    brightness_direction: Literal["add", "subtract"] | None = None
    brightness_percent: float | None = Field(default=None, gt=0, le=100)
    fragments: list[ProofFragment] | None = None

    @model_validator(mode="after")
    def validate_variant(self):
        if self.proof_variant == "fragment_30x30_color":
            if self.brightness_direction not in {"add", "subtract"}:
                raise ValueError("Brightness direction is required for the color variant")
            if self.brightness_percent is None:
                raise ValueError("Brightness percent is required for the color variant")
        elif self.brightness_direction is not None or self.brightness_percent is not None:
            raise ValueError("Only the color variant can contain brightness settings")
        if self.proof_variant == "fragment_90x30":
            if self.fragments is None or len(self.fragments) != 3:
                raise ValueError("The 90x30 variant requires exactly three fragments")
            if [fragment.position for fragment in self.fragments] != [0, 1, 2]:
                raise ValueError("The 90x30 fragment positions must be sequential")
        elif self.fragments is not None:
            raise ValueError("Only the 90x30 variant can contain fragments")
        return self


class Job(BaseModel):
    job_id: str
    public_id: str = ""
    source_path: str = Field(min_length=1, max_length=2048)
    layout_number: int = Field(ge=1, le=999_999)
    layout_numbers: list[int] = Field(default_factory=list, max_length=100)
    proof_variant: ProofVariant = "fragment_60x30"
    brightness_direction: Literal["add", "subtract"] | None = None
    brightness_percent: float | None = Field(default=None, gt=0, le=100)
    items: list[ProofItem] = Field(default_factory=list, max_length=100)
    order_number: str = ""
    preset: Preset
    metadata: dict[str, Any] = Field(default_factory=dict)
    attempt: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def normalize_layouts_and_variant(self):
        if self.items:
            if [item.position for item in self.items] != list(range(len(self.items))):
                raise ValueError("Proof item positions must be sequential")
            identifiers = [item.id for item in self.items]
            identifiers.extend(
                fragment.id
                for item in self.items
                for fragment in (item.fragments or [])
            )
            if len(identifiers) != len(set(identifiers)):
                raise ValueError("Proof item and fragment IDs must be unique")
            self.layout_numbers = [item.layout_number for item in self.items]
            self.layout_number = self.items[0].layout_number
            self.proof_variant = self.items[0].proof_variant
            self.brightness_direction = self.items[0].brightness_direction
            self.brightness_percent = self.items[0].brightness_percent
            return self
        numbers = self.layout_numbers or [self.layout_number]
        if any(isinstance(number, bool) or not 1 <= number <= 999_999 for number in numbers):
            raise ValueError("Layout numbers must be positive integers")
        numbers = list(dict.fromkeys(numbers))
        self.layout_numbers = numbers
        self.layout_number = numbers[0]
        if self.proof_variant == "fragment_30x30_color":
            if self.brightness_direction not in {"add", "subtract"}:
                raise ValueError("Brightness direction is required for the color variant")
            if self.brightness_percent is None:
                raise ValueError("Brightness percent is required for the color variant")
        return self

    def execution_items(self) -> list[ProofItem]:
        if self.items:
            return self.items
        return [
            ProofItem(
                id=f"legacy-{index + 1}",
                position=index,
                layout_number=layout_number,
                proof_variant=self.proof_variant,
                brightness_direction=self.brightness_direction,
                brightness_percent=self.brightness_percent,
            )
            for index, layout_number in enumerate(self.layout_numbers)
        ]
