import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel
from pypdf import PdfReader

logger = logging.getLogger(__name__)

SUPPORTED_TYPES = {".pdf"}  # TODO: ".xlsx", ".csv"


class UnsupportedFileTypeError(ValueError):
    """The file extension has no extractor yet."""


class NoTextLayerError(ValueError):
    """The PDF has no extractable text (probably a scan: needs OCR)."""


class FileMetadata(BaseModel):
    """For logging and auditing only. Do NOT send this to the LLM."""

    # File-system metadata
    file_name: str
    file_path: str
    created_at_utc: datetime
    modified_at_utc: datetime

    # PDF document metadata
    page_count: int
    is_encrypted: bool
    title: str | None = None
    author: str | None = None
    subject: str | None = None
    creator: str | None = None
    producer: str | None = None
    pdf_creation_date: datetime | None = None
    pdf_modification_date: datetime | None = None


class ExtractedDocument(BaseModel):
    """What we READ from a file. This is the INPUT side of the LLM step.
    (The OUTPUT side, the fields the LLM must fill, is a separate model,
    e.g. InvoiceExtraction, which you write next.)
    """
    raw_text: str          # the only part that goes into the LLM prompt
    metadata: FileMetadata  # kept for logs, error reports, audit trail


class ExtractInvoice:
    def extract_invoice(self, file_path: str) -> ExtractedDocument:
        """File type orchestrator: one extractor per supported type."""
        # resolve and check path
        path, file_type = self._path_resolver(file_path)
        
        if file_type == ".pdf":
            return self._extract_pdf_invoice(path)
        raise UnsupportedFileTypeError(f"Cannot extract file type: {file_type!r}")
    
    def _path_resolver(self, file_path: str) -> tuple[Path, str]:
            path = Path(file_path)
            if not path.is_file():
                logger.error("File not found: %s", path)
                raise FileNotFoundError(path)
    
            ext = path.suffix.lower()
            if ext not in SUPPORTED_TYPES:
                raise UnsupportedFileTypeError(f"Cannot extract file type: {ext!r}")
    
            return path, ext

    def _extract_pdf_invoice(self, path: Path) -> ExtractedDocument:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported.")

        pages_text: list[str] = []
        for page_num, page in enumerate(reader.pages, start=1):
            # layout mode preserves horizontal column spacing for tables
            text = page.extract_text(
                extraction_mode="layout",
                layout_mode_space_vertically=False,
            )
            pages_text.append(f"--- Page {page_num} ---\n{text.strip()}")

        raw_text = "\n\n".join(pages_text)

        # page markers are always present, so check the text itself
        if not any(p.split("\n", 1)[-1].strip() for p in pages_text):
            raise NoTextLayerError(f"{path.name}: no extractable text (scanned PDF?)")

        return ExtractedDocument(raw_text=raw_text, metadata=self._file_metadata(reader, path))

    def _file_metadata(self, reader: PdfReader, path: Path) -> FileMetadata:
        """Both OS file-system metadata and internal PDF document properties."""
        meta = reader.metadata  # may be None if the PDF has no Info dictionary
        stat = path.stat()

        return FileMetadata(
            file_name=path.name,
            file_path=str(path),
            created_at_utc=datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc),
            modified_at_utc=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            page_count=len(reader.pages),
            is_encrypted=reader.is_encrypted,
            title=meta.title if meta else None,
            author=meta.author if meta else None,
            subject=meta.subject if meta else None,
            creator=meta.creator if meta else None,
            producer=meta.producer if meta else None,
            pdf_creation_date=meta.creation_date if meta else None,
            pdf_modification_date=meta.modification_date if meta else None,
        )
