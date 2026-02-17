"""
Buffered CSV Writer

Handles:
- Buffered writing to CSV files
- Configurable flush intervals
- Daily file rollover
- SHUNYA naming conventions
"""

import csv
import logging
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from engines.zerodha.snapshot_builder import COLUMNS, format_csv_value

logger = logging.getLogger(__name__)


class CSVWriter:
    """Buffered CSV writer with rollover support."""

    def __init__(
        self,
        output_dir: Path,
        underlying: str,
        venue: str = "NSEFO",
        flush_rows: int = 500,
        flush_interval_seconds: float = 1.0,
    ):
        """
        Initialize CSVWriter.

        Args:
            output_dir: Directory to write CSV files.
            underlying: Underlying symbol (e.g., "NIFTY").
            venue: Venue token for filename (e.g., "NSEFO").
            flush_rows: Flush buffer after this many rows.
            flush_interval_seconds: Maximum time between flushes.
        """
        self.output_dir = Path(output_dir)
        self.underlying = underlying.upper()
        self.venue = venue
        self.flush_rows = flush_rows
        self.flush_interval_seconds = flush_interval_seconds

        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._buffer: List[List[Any]] = []
        self._buffer_lock = threading.Lock()
        self._current_file: Optional[Path] = None
        self._file_handle = None
        self._csv_writer = None
        self._start_date: Optional[date] = None
        self._last_flush_time: Optional[datetime] = None
        self._rows_written: int = 0

    def _get_filename(self, start_date: date, end_date: Optional[date] = None) -> str:
        """
        Generate filename following SHUNYA convention.

        Format: {UNDERLYING}_NSEFO_OPTION_CHAIN_1S_{START_YYYYMMDD}_{END_YYYYMMDD}.csv
        """
        start_str = start_date.strftime("%Y%m%d")
        end_str = (end_date or start_date).strftime("%Y%m%d")
        return f"{self.underlying}_{self.venue}_OPTION_CHAIN_1S_{start_str}_{end_str}.csv"

    def _open_file(self, for_date: date) -> None:
        """Open a new file for writing."""
        self._close_file()

        self._start_date = for_date
        filename = self._get_filename(for_date)
        self._current_file = self.output_dir / filename

        # Check if file exists to determine if we need header
        file_exists = self._current_file.exists()

        self._file_handle = open(self._current_file, "a", newline="", buffering=1)
        self._csv_writer = csv.writer(self._file_handle)

        if not file_exists:
            self._csv_writer.writerow(COLUMNS)
            logger.info(f"Created new file with header: {self._current_file}")
        else:
            logger.info(f"Appending to existing file: {self._current_file}")

        self._last_flush_time = datetime.now()

    def _close_file(self) -> None:
        """Close the current file."""
        if self._file_handle:
            try:
                self._file_handle.flush()
                self._file_handle.close()
            except Exception as e:
                logger.error(f"Error closing file: {e}")
            finally:
                self._file_handle = None
                self._csv_writer = None

    def _rename_with_end_date(self) -> None:
        """Rename file to include actual end date."""
        if not self._current_file or not self._start_date:
            return

        today = date.today()
        if today != self._start_date:
            new_filename = self._get_filename(self._start_date, today)
            new_path = self.output_dir / new_filename

            try:
                self._close_file()
                if self._current_file.exists() and not new_path.exists():
                    self._current_file.rename(new_path)
                    logger.info(f"Renamed file to: {new_path}")
                    self._current_file = new_path
            except Exception as e:
                logger.error(f"Error renaming file: {e}")

    def _check_rollover(self) -> None:
        """Check if we need to roll over to a new file."""
        today = date.today()
        if self._start_date and self._start_date != today:
            logger.info(f"Day rollover detected: {self._start_date} -> {today}")
            self._rename_with_end_date()
            self._open_file(today)

    def write_row(self, row: Dict[str, Any]) -> None:
        """
        Add a row to the buffer.

        Args:
            row: Row dictionary with column values.
        """
        # Format row values
        formatted = [format_csv_value(row.get(col)) for col in COLUMNS]

        with self._buffer_lock:
            self._buffer.append(formatted)

            # Check if we should flush
            if len(self._buffer) >= self.flush_rows:
                self._do_flush()

    def write_rows(self, rows: List[Dict[str, Any]]) -> None:
        """
        Add multiple rows to the buffer.

        Args:
            rows: List of row dictionaries.
        """
        formatted_rows = []
        for row in rows:
            formatted = [format_csv_value(row.get(col)) for col in COLUMNS]
            formatted_rows.append(formatted)

        with self._buffer_lock:
            self._buffer.extend(formatted_rows)

            if len(self._buffer) >= self.flush_rows:
                self._do_flush()

    def _do_flush(self) -> None:
        """Flush buffer to disk. Must be called with lock held."""
        if not self._buffer:
            return

        # Check rollover
        self._check_rollover()

        # Ensure file is open
        if self._csv_writer is None:
            self._open_file(date.today())

        # Write buffer
        try:
            self._csv_writer.writerows(self._buffer)
            self._file_handle.flush()
            self._rows_written += len(self._buffer)
            logger.debug(f"Flushed {len(self._buffer)} rows, total: {self._rows_written}")
        except Exception as e:
            logger.error(f"Error writing to CSV: {e}")
        finally:
            self._buffer.clear()
            self._last_flush_time = datetime.now()

    def flush(self) -> None:
        """Force flush the buffer."""
        with self._buffer_lock:
            self._do_flush()

    def check_time_flush(self) -> None:
        """Flush if enough time has passed since last flush."""
        if self._last_flush_time is None:
            return

        elapsed = (datetime.now() - self._last_flush_time).total_seconds()
        if elapsed >= self.flush_interval_seconds:
            with self._buffer_lock:
                if self._buffer:
                    self._do_flush()

    def close(self) -> None:
        """Close the writer and flush remaining data."""
        with self._buffer_lock:
            if self._buffer:
                self._do_flush()
        self._close_file()
        logger.info(f"CSVWriter closed. Total rows written: {self._rows_written}")

    def get_stats(self) -> Dict[str, Any]:
        """Get writer statistics."""
        return {
            "current_file": str(self._current_file) if self._current_file else None,
            "rows_written": self._rows_written,
            "buffer_size": len(self._buffer),
            "start_date": str(self._start_date) if self._start_date else None,
        }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


class MultiCSVWriter:
    """Manages multiple CSV writers, one per underlying."""

    def __init__(
        self,
        output_dir: Path,
        underlyings: List[str],
        venue: str = "NSEFO",
        flush_rows: int = 500,
        flush_interval_seconds: float = 1.0,
    ):
        """
        Initialize MultiCSVWriter.

        Args:
            output_dir: Directory to write CSV files.
            underlyings: List of underlying symbols.
            venue: Venue token for filename.
            flush_rows: Flush buffer after this many rows.
            flush_interval_seconds: Maximum time between flushes.
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._writers: Dict[str, CSVWriter] = {}
        for underlying in underlyings:
            self._writers[underlying.upper()] = CSVWriter(
                output_dir=self.output_dir,
                underlying=underlying,
                venue=venue,
                flush_rows=flush_rows,
                flush_interval_seconds=flush_interval_seconds,
            )

    def write_row(self, row: Dict[str, Any]) -> None:
        """Write a row to the appropriate underlying's file."""
        underlying = row.get("underlying_symbol", "").upper()
        writer = self._writers.get(underlying)
        if writer:
            writer.write_row(row)
        else:
            logger.warning(f"No writer for underlying: {underlying}")

    def write_rows(self, rows: List[Dict[str, Any]]) -> None:
        """Write multiple rows, routing to appropriate files."""
        # Group by underlying
        by_underlying: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            underlying = row.get("underlying_symbol", "").upper()
            if underlying not in by_underlying:
                by_underlying[underlying] = []
            by_underlying[underlying].append(row)

        # Write to each
        for underlying, underlying_rows in by_underlying.items():
            writer = self._writers.get(underlying)
            if writer:
                writer.write_rows(underlying_rows)

    def flush(self) -> None:
        """Flush all writers."""
        for writer in self._writers.values():
            writer.flush()

    def check_time_flush(self) -> None:
        """Check time-based flush for all writers."""
        for writer in self._writers.values():
            writer.check_time_flush()

    def close(self) -> None:
        """Close all writers."""
        for writer in self._writers.values():
            writer.close()

    def get_stats(self) -> Dict[str, Any]:
        """Get combined stats."""
        stats = {}
        for underlying, writer in self._writers.items():
            stats[underlying] = writer.get_stats()
        return stats

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
