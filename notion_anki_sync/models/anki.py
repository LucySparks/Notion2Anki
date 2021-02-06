"""Anki data models."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field

from notion_anki_sync.exceptions import AnkiError


class ResponseSchema(BaseModel):
    """Anki response schema."""

    #: Result
    result: Optional[Union[int, list, Dict[str, Any]]]
    #: Error message
    error_message: str = Field(..., alias='error')
    #: Error
    error: Optional[AnkiError]


@dataclass
class Image:
    """An image from HTML document."""

    #: `src` attribute as is in HTML document
    src: str
    #: Filename to be stored as
    filename: str
    #: Absolute path to the image
    abs_path: Path


@dataclass
class Note:
    """Anki note model."""

    #: Front side
    front: str
    #: Back side (can be empty for cloze note)
    back: Optional[str] = None
    #: Tags
    tags: Optional[List[str]] = None
    #: Link to Notion page
    source: Optional[str] = None
    #: Note images
    images: Optional[List[Image]] = None
